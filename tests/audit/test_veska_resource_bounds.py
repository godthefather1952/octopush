"""Phase 6 — H19, H20, H23: what a long session accumulates.

``OrderManager`` was built for this: ``compact`` archives terminal orders whose
fills reconciliation has finished with, so the resident set tracks concurrent
activity rather than session length, and ``ArchivedOrders`` keeps the totals
reconciliation needs.

Two structures beside it have no such policy:

* ``PaperExecutor._pending`` — one ``_Pending`` per order ever submitted. Never
  deleted, including for orders the OMS has archived out of memory entirely.
* ``Veska.plans`` — every plan ever built, keyed by ``plan_id``. Never pruned.

Both are small per entry, and both are unbounded in session length, which is
the shape that matters: a structure whose size is a function of uptime rather
than of activity does not have a size, it has a schedule.

H20 draws the line this must not cross. An UNKNOWN order's state is legitimately
unbounded — it stays until something authoritative resolves it — and "fixing"
retention by dropping it would replace a memory question with a safety one.

H23 asks a different question: a plan naming a venue that is not configured.
``PaperExecutor._latency`` invents 40ms for it rather than refusing.
"""

from __future__ import annotations

import inspect

import pytest

from core.models.execution import OrderStatus
from execution.paper.executor import PaperExecutor
from execution.veska.engine import Veska
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

#: Enough orders to make a linear structure obvious without a slow test.
CYCLES = 1_000


async def churn(built, cycles: int = CYCLES) -> None:
    """Submit, fill and terminate ``cycles`` orders, compacting as we go.

    The bus is drained periodically so the queue does not become the thing
    under measurement: this test is about the executor's own structures.
    """
    for index in range(cycles):
        await built.executor.submit(
            plan(
                planned(client_order_id=f"ord-{index}", limit_price=40_100.0),
                correlation_id=f"opp-{index}",
            ),
            START_MS + index,
        )
        await built.executor.poll(START_MS + index + VENUE_A_LATENCY_MS)
        built.oms.compact(unsealed_fills=set())
        if index % 50 == 0:
            built.published.clear()
            await built.bus.drain()
            built.published.clear()


class TestTerminalOrdersLeaveMemory:
    """The control: the structure that does have a retention policy."""

    async def test_the_resident_order_set_stays_flat(self):
        built = rig(settings=CERTAIN, market_state=one_venue_market(mid=30_000.0))
        await churn(built)
        assert len(built.oms.orders) < 50, (
            f"{len(built.oms.orders)} orders resident after {CYCLES} "
            "completed round trips"
        )

    async def test_the_archive_keeps_the_totals(self):
        built = rig(settings=CERTAIN, market_state=one_venue_market(mid=30_000.0))
        await churn(built)
        assert built.oms.archived.count >= CYCLES - 50
        assert built.oms.archived.filled_quantity > 0


class TestPendingVenueRecordsAccumulate:
    """H19. ``_pending`` has no delete."""

    def test_nothing_ever_removes_a_pending_entry(self):
        source = inspect.getsource(PaperExecutor)
        for removal in ("del self._pending", "self._pending.pop", "_pending.clear"):
            assert removal not in source, (
                f"{removal!r} exists now; H19's premise needs rechecking"
            )

    async def test_pending_does_not_grow_with_session_length(self):
        built = rig(settings=CERTAIN, market_state=one_venue_market(mid=30_000.0))
        await churn(built)

        resident_orders = len(built.oms.orders)
        pending = len(built.executor._pending)

        assert pending <= resident_orders + 50, (
            f"the executor holds {pending} pending venue records against "
            f"{resident_orders} resident orders after {CYCLES} completed round "
            "trips; the structure is sized by the session, not by activity"
        )

    async def test_pending_entries_outlive_the_orders_they_describe(self):
        """The specific leak: the OMS has archived the order, the executor has
        not noticed."""
        built = rig(settings=CERTAIN, market_state=one_venue_market(mid=30_000.0))
        await built.executor.submit(
            plan(planned(client_order_id="ord-gone", limit_price=40_100.0)),
            START_MS,
        )
        await built.executor.poll(ACK_AT)
        built.oms.compact(unsealed_fills=set())

        assert built.oms.get("ord-gone") is None
        assert "ord-gone" not in built.executor._pending, (
            "the order has been archived out of the OMS but its venue-side "
            "record is still resident in the executor and will be for the "
            "rest of the process"
        )

    async def test_the_poll_loop_is_not_slowed_by_archived_orders(self):
        """A control: ``poll`` walks ``oms.orders``, which compaction bounds,
        rather than ``_pending``, which it does not."""
        source = inspect.getsource(PaperExecutor.poll)
        assert "self.oms.orders.values()" in source
        assert "self._pending.values()" not in source
        assert "self._pending.items()" not in source


class TestPlansAccumulate:
    """H19. ``Veska.plans`` has no eviction either."""

    def test_every_plan_is_retained_by_id(self):
        source = inspect.getsource(Veska.build_plan)
        assert "self.plans[plan.plan_id] = plan" in source

    def test_nothing_ever_removes_a_plan(self):
        source = inspect.getsource(Veska)
        for removal in ("del self.plans", "self.plans.pop", "self.plans.clear"):
            assert removal not in source, (
                f"{removal!r} exists now; the plan-retention premise needs "
                "rechecking"
            )

    def test_the_plan_store_is_bounded_by_something(self):
        """Either a cap, a compaction hook, or a documented lifetime ledger.

        ``ArchivedOrders`` shows the shape a deliberate lifetime ledger takes
        here: aggregates, not whole objects. ``Veska.plans`` keeps whole plans,
        each carrying every ``PlannedOrder``, with no stated bound.
        """
        source = inspect.getsource(Veska)
        bounded = any(
            marker in source
            for marker in ("maxlen", "deque", "compact", "max_plans", "_trim")
        )
        assert bounded, (
            "Veska.plans grows by one whole ExecutionPlan per authorised "
            "trade for the life of the process, with no cap, no eviction and "
            "no documented retention policy"
        )


class TestUnknownRetentionIsNotALeak:
    """H20. The line the resource findings must not cross."""

    async def test_an_unknown_order_is_not_a_leak_to_be_collected(self):
        built = rig(settings=CERTAIN, market_state=one_venue_market())
        await built.executor.submit(
            plan(planned(client_order_id="ord-unknown", limit_price=1.0)),
            START_MS,
        )
        built.executor.inject_timeout("ord-unknown")
        await built.executor.poll(ACK_AT)
        assert built.oms.get("ord-unknown").status is OrderStatus.UNKNOWN

        for _ in range(10):
            built.oms.compact(unsealed_fills=set())

        assert built.oms.get("ord-unknown") is not None
        assert "ord-unknown" in built.executor._pending

    def test_compaction_only_takes_terminal_orders(self):
        from execution.oms import OrderManager

        source = inspect.getsource(OrderManager.compact)
        assert "order.is_terminal" in source
        assert "UNKNOWN" not in source


class TestUnknownVenueHandling:
    """H23. A plan naming a venue that is not configured."""

    def test_the_latency_lookup_invents_a_number(self):
        source = inspect.getsource(PaperExecutor._latency)
        assert "except KeyError" in source and "return 40" in source

    def test_planning_refuses_an_unknown_venue_first(self):
        """The mitigation on the shipped path: ``build_plan`` reads the venue's
        fee schedule, which raises for a venue that is not configured."""
        source = inspect.getsource(Veska.build_plan)
        assert "self.settings.venue(leg.venue).fees" in source

    async def test_the_executor_refuses_an_unknown_venue_explicitly(self):
        """The boundary's own behaviour, for a persisted or replayed plan that
        never passes through ``build_plan``."""
        built = rig(settings=CERTAIN, market_state=one_venue_market())
        report = await built.executor.submit(
            plan(planned(venue="VENUE_NOWHERE", limit_price=40_100.0)),
            START_MS,
        )
        order = built.only_order()

        assert order.status is OrderStatus.REJECTED, (
            "an order was accepted for venue 'VENUE_NOWHERE', which is not in "
            f"settings, using an invented 40ms latency: status="
            f"{order.status.value} notes={report.notes}"
        )

    async def test_an_unknown_venue_order_can_never_fill(self):
        """The mitigating detail, recorded so the severity is honest.

        ``_book_view`` finds no venue state, so ``_attempt_fill`` returns
        before it reaches ``settings.venue(...).fees``. The order rests
        harmlessly until it expires — but it consumed order capacity and
        reserved committed exposure the whole time.
        """
        built = rig(settings=CERTAIN, market_state=one_venue_market())
        await built.executor.submit(
            plan(planned(venue="VENUE_NOWHERE", limit_price=40_100.0)),
            START_MS,
        )
        fills = await built.executor.poll(ACK_AT)
        assert not fills

    async def test_cancelling_an_unknown_venue_order_raises(self):
        """The place the invented latency stops being harmless.

        ``cancel`` reads ``settings.venue(order.venue).cancel_latency_ms``
        without the ``_latency`` fallback, so a kill-switch ``cancel_all``
        touching such an order raises out of ``_protect``.
        """
        built = rig(settings=CERTAIN, market_state=one_venue_market())
        await built.executor.submit(
            plan(planned(venue="VENUE_NOWHERE", limit_price=40_100.0)),
            START_MS,
        )
        order = built.only_order()
        await built.executor.poll(ACK_AT)
        assert order.status is OrderStatus.OPEN

        try:
            await built.executor.cancel_all(ACK_AT + 1)
        except KeyError as exc:
            pytest.fail(
                "cancel_all raised KeyError for an order on an unconfigured "
                f"venue ({exc}); the kill switch's cancel path is not "
                "fail-safe against a plan the executor accepted"
            )


class TestPollCostIsBoundedByActivity:
    """Algorithmic behaviour in the fast loop. Measured, never optimised."""

    def test_poll_walks_the_resident_orders_once(self):
        source = inspect.getsource(PaperExecutor.poll)
        assert source.count("for order in list(self.oms.orders.values())") == 1

    def test_open_orders_is_a_single_pass(self):
        from execution.oms import OrderManager

        source = inspect.getsource(OrderManager.live_orders)
        assert "for o in self.orders.values()" in source

    def test_the_trade_flow_accrual_walks_live_orders_only(self):
        source = inspect.getsource(PaperExecutor._accrue_trade_flow)
        assert "self.oms.live_orders()" in source

    async def test_a_thousand_live_orders_stay_resident_and_pollable(self):
        """Not a benchmark: a statement that the fast loop's cost tracks live
        orders rather than session history."""
        built = rig(settings=CERTAIN, market_state=one_venue_market())
        for index in range(1_000):
            await built.executor.submit(
                plan(
                    planned(client_order_id=f"ord-live-{index}", limit_price=1.0),
                    correlation_id=f"opp-{index}",
                ),
                START_MS,
            )
        await built.executor.poll(ACK_AT)
        assert len(built.executor.open_orders()) == 1_000
        assert len(built.oms.orders) == 1_000
        await built.bus.drain()
