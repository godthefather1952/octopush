"""Phase 6 — H6, H7, H20: is UNKNOWN treated as unresolved, or as failed?

The order model is unambiguous. ``UNKNOWN`` is documented as "a real state, not
an error … Never assumed to mean 'failed'", it is excluded from
``TERMINAL_STATUSES``, and ``OrderManager.mark_unknown`` says reconciliation
owns resolving it.

``PaperOrder.is_live``, however, is ``not is_terminal and status is not
UNKNOWN``. That is the right predicate for its own name — an UNKNOWN order is
not known to be working — but it makes ``is_live`` false for two completely
different situations: *this order is finished* and *nobody knows what this
order did*. Every call site that reads ``is_live`` as "resolved" inherits the
second meaning silently.

This file separates the call sites that are safe from the ones that are not,
and pins the two resource consequences: an UNKNOWN order must stay retained
(H20) while never granting free order capacity (H7).
"""

from __future__ import annotations

import inspect

from core.models.execution import OrderStatus, PaperOrder
from execution.paper.executor import PaperExecutor
from tests.audit.veska_fixtures import (
    VENUE_A_LATENCY_MS,
    audit_settings,
    deterministic_execution,
    one_venue_market,
    plan,
    planned,
    rig,
)
from tests.conftest import START_MS

CERTAIN = audit_settings(**deterministic_execution())
ACK_AT = START_MS + VENUE_A_LATENCY_MS


async def unknown_order(built, *, quantity: float = 0.10) -> PaperOrder:
    """Submit an order and drive it to UNKNOWN with zero known fills."""
    await built.executor.submit(
        plan(planned(quantity=quantity, limit_price=1.0)), START_MS
    )
    order = built.only_order()
    built.executor.inject_timeout(order.client_order_id)
    await built.executor.poll(ACK_AT)
    assert order.status is OrderStatus.UNKNOWN
    return order


class TestTheModelItself:
    """The premise, recorded before any judgement about the call sites."""

    def test_unknown_is_not_terminal(self):
        from core.models.execution import TERMINAL_STATUSES

        assert OrderStatus.UNKNOWN not in TERMINAL_STATUSES

    def test_but_is_live_is_false_for_unknown(self):
        source = inspect.getsource(PaperOrder.is_live.fget)
        assert "OrderStatus.UNKNOWN" in source

    def test_unknown_can_still_resolve_into_a_fill(self):
        """Which is why optimistic resolution is unsafe: the venue may have
        filled the order the whole time."""
        from core.models.execution import ORDER_TRANSITIONS

        assert OrderStatus.FILLED in ORDER_TRANSITIONS[OrderStatus.UNKNOWN]
        assert OrderStatus.PARTIALLY_FILLED in ORDER_TRANSITIONS[OrderStatus.UNKNOWN]


class TestTheExecutorLeavesUnknownAlone:
    """Controls. The executor's own handling is correct and must stay so."""

    async def test_poll_does_not_advance_an_unknown_order(self):
        built = rig(settings=CERTAIN, market_state=one_venue_market())
        order = await unknown_order(built)

        built.executor.update_market(one_venue_market(mid=30_000.0))
        fills = await built.executor.poll(ACK_AT + 1_000)

        assert not fills
        assert order.status is OrderStatus.UNKNOWN

    async def test_an_unknown_order_never_expires_on_its_own(self):
        """Expiry is a resolution, and only reconciliation may resolve one."""
        built = rig(settings=CERTAIN, market_state=one_venue_market())
        order = await unknown_order(built)
        await built.executor.poll(START_MS + 60_000)
        assert order.status is OrderStatus.UNKNOWN


class TestOpenOrderCapacity:
    """H7. An UNKNOWN order may still be working at the venue."""

    async def test_open_orders_excludes_unknown(self):
        """The premise: ``open_orders`` is ``oms.live_orders()``."""
        built = rig(settings=CERTAIN, market_state=one_venue_market())
        await unknown_order(built)
        assert built.executor.open_orders() == []
        assert len(built.oms.unknown_orders()) == 1

    async def test_uncertain_orders_must_not_create_free_order_capacity(self):
        """The safety property.

        ``Orchestrator`` feeds ``len(veska.open_orders())`` into RUNE's
        ``MAX_OPEN_ORDERS`` gate. An order whose venue-side state is unknown
        may well be resting on the book; counting it as zero lets the platform
        authorise more orders than the limit permits, and the more orders that
        time out the more headroom the gate reports.
        """
        built = rig(settings=CERTAIN, market_state=one_venue_market())
        for index in range(5):
            await built.executor.submit(
                plan(
                    planned(
                        client_order_id=f"ord-unknown-{index}", limit_price=1.0
                    ),
                    correlation_id=f"opp-{index}",
                ),
                START_MS,
            )
        for index in range(5):
            built.executor.inject_timeout(f"ord-unknown-{index}")
        await built.executor.poll(ACK_AT)

        outstanding = len(built.oms.unknown_orders())
        assert outstanding == 5

        assert len(built.executor.open_orders()) == outstanding, (
            f"{outstanding} orders are outstanding with unknown venue state, "
            f"but the capacity input reports "
            f"{len(built.executor.open_orders())}; uncertainty is being "
            "reported as free headroom"
        )

    async def test_committed_exposure_does_count_unknown_orders(self):
        """The control, and the reason this finding is scoped to capacity.

        P5-1's committed-exposure snapshot keys off ``not order.is_terminal``,
        not ``is_live``, so an UNKNOWN order still reserves notional. The
        notional side is already correct; the count is not.
        """
        import apps.orchestrator.orchestrator as orchestrator

        source = inspect.getsource(orchestrator.Orchestrator._current_committed_exposure)
        assert "order.is_terminal" in source
        assert "is_live" not in source


class TestOrchestratorCallSites:
    """H6. Where ``is_live`` is read as though it meant 'resolved'.

    Each test builds a real UNKNOWN order and evaluates the *same expression*
    the call site uses, so the evidence is the object's behaviour rather than
    the presence of a word in a source file. The structural assertion beside it
    only pins that the call site still uses that expression.
    """

    async def test_advance_execution_sees_an_unknown_entry_as_finished(self):
        """``live`` empty and ``filled`` zero — the CLOSED branch.

        ``_advance_execution`` is: ``live = [o for o in orders if o is not
        None and o.is_live]``; if none, and nothing filled, transition to
        CLOSED and release the reservation. An UNKNOWN entry order satisfies
        both conditions while the venue may still be working it.
        """
        import apps.orchestrator.orchestrator as orchestrator

        built = rig(settings=CERTAIN, market_state=one_venue_market())
        order = await unknown_order(built)
        orders = [order]

        live = [o for o in orders if o is not None and o.is_live]
        filled = sum(o.filled_quantity for o in orders if o is not None)

        source = inspect.getsource(orchestrator.Orchestrator._advance_execution)
        assert "o.is_live" in source and "StrategyState.CLOSED" in source

        assert live or filled > 0, (
            "an entry order in UNKNOWN with no reported fills presents to "
            "_advance_execution as live=[] and filled=0, which is its "
            "'nothing traded' branch: the opportunity closes and its strategy "
            "reservation is released while the order may still be resting at "
            "the venue"
        )

    async def test_advance_exit_sees_an_unknown_exit_as_finished(self):
        """The retry is sized from the position that actually exists, so if
        the UNKNOWN exit later fills the position is closed twice."""
        import apps.orchestrator.orchestrator as orchestrator

        built = rig(settings=CERTAIN, market_state=one_venue_market())
        order = await unknown_order(built)

        still_working = any(o is not None and o.is_live for o in [order])

        source = inspect.getsource(orchestrator.Orchestrator._advance_exit)
        assert "o.is_live" in source and "_submit_exit" in source

        assert still_working, (
            "an exit order in UNKNOWN reports as not working, so _advance_exit "
            "reaches its residual-retry branch and submits a second full exit "
            "for the same position"
        )

    async def test_hedge_in_flight_sees_an_unknown_hedge_as_finished(self):
        """An UNKNOWN hedge is forgotten and a duplicate full hedge is sent."""
        import apps.orchestrator.orchestrator as orchestrator

        built = rig(settings=CERTAIN, market_state=one_venue_market())
        order = await unknown_order(built)
        by_id = {order.client_order_id: order}

        live = [
            oid
            for oid in [order.client_order_id]
            if (found := by_id.get(oid)) is not None and found.is_live
        ]

        source = inspect.getsource(orchestrator.Orchestrator._hedge_in_flight)
        assert "order.is_live" in source
        assert "del self.working_hedges[symbol]" in source

        assert live, (
            "a hedge order in UNKNOWN leaves _hedge_in_flight's live list "
            "empty, so the symbol's working-hedge record is deleted and the "
            "next tick sends a duplicate full hedge"
        )

    def test_cancel_all_cannot_reach_an_unknown_order(self):
        """Recorded, not judged.

        ``cancel_all`` iterates ``live_orders()``, so an UNKNOWN order is not
        cancelled by the kill switch. That is arguably right — there is nothing
        known to cancel — but it means an engaged kill switch does not reduce
        outstanding uncertain exposure, and nothing else does either.
        """
        source = inspect.getsource(PaperExecutor.cancel_all)
        assert "live_orders()" in source

    async def test_cancel_is_a_no_op_on_an_unknown_order(self):
        built = rig(settings=CERTAIN, market_state=one_venue_market())
        order = await unknown_order(built)
        await built.executor.cancel(order.client_order_id, ACK_AT + 1)
        assert order.status is OrderStatus.UNKNOWN


class TestUnknownRetention:
    """H20. Unresolved truth must stay resident, however long it lasts."""

    async def test_compaction_never_archives_an_unknown_order(self):
        built = rig(settings=CERTAIN, market_state=one_venue_market())
        order = await unknown_order(built)

        archived = built.oms.compact(unsealed_fills=set())

        assert archived == 0
        assert built.oms.get(order.client_order_id) is not None
        assert built.oms.archived.count == 0

    async def test_an_unknown_order_is_retained_indefinitely(self):
        """The distinction H19 must respect: this growth is legitimate."""
        built = rig(settings=CERTAIN, market_state=one_venue_market())
        await unknown_order(built)
        for step in range(10):
            await built.executor.poll(ACK_AT + step * 10_000)
            built.oms.compact(unsealed_fills=set())
        assert len(built.oms.unknown_orders()) == 1

    async def test_an_unknown_order_keeps_its_pending_venue_record(self):
        """Its arrival and cancel schedule are still needed if it resolves."""
        built = rig(settings=CERTAIN, market_state=one_venue_market())
        order = await unknown_order(built)
        assert order.client_order_id in built.executor._pending
