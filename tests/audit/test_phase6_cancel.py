"""H2 and H24 — a cancel must never silently disappear.

H2: CANCEL BEFORE ACKNOWLEDGEMENT
=================================
``PaperExecutor.cancel`` branches on status:

    if order.status in (CREATED, SUBMITTING):
        self._pending.setdefault(...).cancel_at = now_ms
        return

It records ``cancel_at`` and returns **without transitioning the order to
CANCEL_PENDING**. The comment says the order "resolves once it arrives".

It does not. ``poll`` reads ``cancel_at`` only inside

    if order.status is OrderStatus.CANCEL_PENDING:

and by the time the order arrives, ``poll`` has already moved it
SUBMITTING → ACKNOWLEDGED → OPEN. The status is OPEN, so the CANCEL_PENDING
branch is skipped, ``cancel_at`` is never read again, and the order proceeds
to ``_attempt_fill``.

The cancellation is not a race that was lost. It is a request that left no
trace on the order at all.

H24: SAME-TIMESTAMP PRECEDENCE
==============================
At one instant a cancel can arrive, an expiry can fall due and the book can be
fillable. The policy need not match anyone's preference, but it must be
explicit, deterministic and replayable. These tests pin what it currently is.
"""

from __future__ import annotations

from core.models.common import Side, TimeInForce
from core.models.execution import ORDER_TRANSITIONS, OrderStatus
from tests.audit.veska_fixtures import (
    SYMBOL,
    T0,
    VENUE_A,
    build_harness,
    execution_plan,
    market_state,
    planned_order,
    price_levels,
    two_venue_market,
    venue_state,
)

#: VENUE_A's configured latency in the shipped simulated venue set. Read from
#: settings in the tests rather than hard-coded, so a configuration change
#: surfaces as a changed expectation instead of a silent pass.


def _ack_at(harness, venue: str = VENUE_A) -> int:
    return harness.settings.venue(venue).latency_ms


class TestCancelBeforeAcknowledgement:
    """The order must not behave as though cancel was never requested."""

    async def test_a_cancel_before_ack_leaves_a_trace_on_the_order(self):
        """Whatever the outcome, the request must be visible somewhere.

        The weakest possible form of the invariant: not "the cancel wins", not
        "the order is cancelled", merely that asking for a cancellation
        changed *something* observable about the order.
        """
        harness = build_harness()
        harness.update_market(two_venue_market())
        plan = execution_plan(
            planned_order(time_in_force=TimeInForce.GTC, limit_price=100.05),
            created_at=T0,
        )
        await harness.veska.execute(plan, T0)
        order = harness.orders_of(plan.plan_id)[0]
        assert order.status is OrderStatus.SUBMITTING

        await harness.veska.cancel(order.client_order_id, T0 + 1)

        statuses_seen = {status for _, status in order.history}
        assert OrderStatus.CANCEL_PENDING in statuses_seen or order.is_terminal, (
            "a cancel requested before acknowledgement left no mark on the "
            f"order: history is {order.history}"
        )

    async def test_an_order_cancelled_before_ack_does_not_fill_afterwards(self):
        """The economically load-bearing form of the invariant.

        Submit, cancel before the venue has acknowledged, then advance past
        acknowledgement with liquidity that would fill the order. A cancelled
        order must not trade.
        """
        harness = build_harness()
        harness.update_market(two_venue_market())
        latency = _ack_at(harness)
        plan = execution_plan(
            planned_order(
                side=Side.BUY,
                quantity=1.0,
                time_in_force=TimeInForce.IOC,
                limit_price=101.0,
            ),
            created_at=T0,
        )
        await harness.veska.execute(plan, T0)
        order_id = harness.orders_of(plan.plan_id)[0].client_order_id

        # Cancel while still SUBMITTING.
        await harness.veska.cancel(order_id, T0 + 1)

        # Now let it arrive into a book that would fill it.
        harness.update_market(two_venue_market(created_at=T0 + latency))
        fills = await harness.veska.poll(T0 + latency + 1)

        order = harness.oms.get(order_id)
        assert order is not None
        assert not fills, (
            f"an order cancelled at {T0 + 1}, before its acknowledgement at "
            f"{T0 + latency}, filled anyway: {fills}"
        )
        assert order.filled_quantity == 0.0
        assert order.status is not OrderStatus.FILLED

    async def test_cancel_all_before_ack_has_the_same_guarantee(self):
        """``cancel_all`` is the kill switch's instrument. It must not lose one.

        This is the path a halted platform relies on: if a cancel issued here
        can evaporate because the order had not been acknowledged yet, the
        kill switch does not stop what it believes it stopped.
        """
        harness = build_harness()
        harness.update_market(two_venue_market())
        latency = _ack_at(harness)
        plan = execution_plan(
            planned_order(quantity=1.0, limit_price=101.0),
            planned_order(quantity=1.0, limit_price=101.0),
            created_at=T0,
        )
        await harness.veska.execute(plan, T0)

        cancelled = await harness.veska.cancel_all(T0 + 1)
        assert cancelled == 2

        harness.update_market(two_venue_market(created_at=T0 + latency))
        fills = await harness.veska.poll(T0 + latency + 1)

        assert not fills, (
            f"cancel_all at {T0 + 1} cancelled {cancelled} orders, and "
            f"{len(fills)} of them filled afterwards anyway"
        )

    async def test_cancel_at_is_recorded_but_never_consulted(self):
        """Pins the mechanism, so the finding's evidence is not an inference.

        ``cancel_at`` is written on the pending record and the order's status
        is left untouched, which is precisely why ``poll``'s CANCEL_PENDING
        branch never reads it.
        """
        harness = build_harness()
        harness.update_market(two_venue_market())
        plan = execution_plan(planned_order(), created_at=T0)
        await harness.veska.execute(plan, T0)
        order_id = harness.orders_of(plan.plan_id)[0].client_order_id

        await harness.veska.cancel(order_id, T0 + 1)

        pending = harness.executor._pending[order_id]
        assert pending.cancel_at == T0 + 1, "the cancel time was not recorded"
        assert harness.oms.get(order_id).status is OrderStatus.SUBMITTING, (
            "status changed, which would mean poll's cancel branch can see it"
        )


class TestCancelAfterAcknowledgement:
    """The acknowledged path, which does model the race explicitly."""

    async def test_cancel_after_ack_reaches_cancel_pending(self):
        harness = build_harness()
        harness.update_market(two_venue_market())
        latency = _ack_at(harness)
        plan = execution_plan(
            planned_order(time_in_force=TimeInForce.GTC, limit_price=99.0),
            created_at=T0,
        )
        await harness.veska.execute(plan, T0)
        order_id = harness.orders_of(plan.plan_id)[0].client_order_id

        await harness.veska.poll(T0 + latency)
        assert harness.oms.get(order_id).status is OrderStatus.OPEN

        await harness.veska.cancel(order_id, T0 + latency + 1)
        assert harness.oms.get(order_id).status is OrderStatus.CANCEL_PENDING

    async def test_a_cancel_in_flight_does_not_resolve_before_its_latency(self):
        """Deterministic: the cancel takes the venue's configured time."""
        harness = build_harness()
        harness.update_market(two_venue_market())
        latency = _ack_at(harness)
        cancel_latency = harness.settings.venue(VENUE_A).cancel_latency_ms
        plan = execution_plan(
            planned_order(time_in_force=TimeInForce.GTC, limit_price=99.0),
            created_at=T0,
        )
        await harness.veska.execute(plan, T0)
        order_id = harness.orders_of(plan.plan_id)[0].client_order_id

        await harness.veska.poll(T0 + latency)
        cancel_requested = T0 + latency + 1
        await harness.veska.cancel(order_id, cancel_requested)

        await harness.veska.poll(cancel_requested + cancel_latency - 1)
        assert harness.oms.get(order_id).status is OrderStatus.CANCEL_PENDING

        await harness.veska.poll(cancel_requested + cancel_latency)
        assert harness.oms.get(order_id).status is OrderStatus.CANCELLED


class TestSameTimestampPrecedence:
    """H24 — pin the ordering when several things fall due at one instant."""

    async def test_expiry_and_fillable_book_at_the_same_instant(self):
        """Which wins at exactly ``expires_at``, with a fillable book?

        ``poll`` attempts the fill first and only then checks expiry, so a fill
        at the expiry instant is taken. Recorded rather than judged: the
        requirement is that it be deterministic, not that it be one way.
        """
        harness = build_harness()
        harness.update_market(two_venue_market())
        latency = _ack_at(harness)
        ttl = latency + 1_000
        plan = execution_plan(
            planned_order(
                quantity=1.0,
                time_in_force=TimeInForce.IOC,
                limit_price=101.0,
                ttl_ms=ttl,
            ),
            created_at=T0,
        )
        await harness.veska.execute(plan, T0)
        order = harness.orders_of(plan.plan_id)[0]
        assert order.expires_at is not None

        fills = await harness.veska.poll(order.expires_at)
        assert fills, (
            "at exactly expires_at with a fillable book the current "
            "implementation fills before it expires; it did not, so this "
            "precedence has changed and the audit's record of it is stale"
        )
        assert order.status is OrderStatus.FILLED

    async def test_expiry_with_no_fill_yields_expired_not_cancelled(self):
        """A never-filled order expires; a partially-filled one cancels."""
        harness = build_harness()
        harness.update_market(
            market_state(
                venue_state(
                    venue=VENUE_A,
                    bids=price_levels((90.0, 10.0)),
                    asks=price_levels((110.0, 10.0)),
                )
            )
        )
        latency = _ack_at(harness)
        ttl = latency + 100
        plan = execution_plan(
            planned_order(
                time_in_force=TimeInForce.GTC, limit_price=95.0, ttl_ms=ttl
            ),
            created_at=T0,
        )
        await harness.veska.execute(plan, T0)
        order = harness.orders_of(plan.plan_id)[0]

        await harness.veska.poll(order.expires_at)
        assert order.filled_quantity == 0.0
        assert order.status is OrderStatus.EXPIRED

    async def test_cancel_and_expiry_falling_due_together(self):
        """A cancel already in flight when the TTL lands.

        Whatever the outcome, it must be terminal and it must be one of the
        two the model allows — never left working past its own deadline.
        """
        harness = build_harness()
        harness.update_market(
            market_state(
                venue_state(
                    venue=VENUE_A,
                    bids=price_levels((90.0, 10.0)),
                    asks=price_levels((110.0, 10.0)),
                )
            )
        )
        latency = _ack_at(harness)
        cancel_latency = harness.settings.venue(VENUE_A).cancel_latency_ms
        ttl = latency + cancel_latency + 10
        plan = execution_plan(
            planned_order(
                time_in_force=TimeInForce.GTC, limit_price=95.0, ttl_ms=ttl
            ),
            created_at=T0,
        )
        await harness.veska.execute(plan, T0)
        order = harness.orders_of(plan.plan_id)[0]

        await harness.veska.poll(T0 + latency)
        await harness.veska.cancel(order.client_order_id, order.expires_at - cancel_latency)
        await harness.veska.poll(order.expires_at)

        assert order.is_terminal, (
            f"an order whose cancel and TTL both fell due at "
            f"{order.expires_at} is still {order.status.value}"
        )
        assert order.status in (OrderStatus.CANCELLED, OrderStatus.EXPIRED)


class TestTransitionLegality:
    """The state machine is the one place a transition can be invented."""

    def test_cancel_pending_is_reachable_from_every_working_state(self):
        """If it were not, the pre-ack path would have nowhere legal to go."""
        for working in (
            OrderStatus.ACKNOWLEDGED,
            OrderStatus.OPEN,
            OrderStatus.PARTIALLY_FILLED,
        ):
            assert OrderStatus.CANCEL_PENDING in ORDER_TRANSITIONS[working]

    def test_submitting_can_reach_cancelled_without_a_fill(self):
        """The transition a correct pre-ack cancel would need.

        Recorded because it bears on remediation: if SUBMITTING → CANCELLED
        were illegal, fixing H2 would require a state-machine change as well
        as an executor change.
        """
        reachable = ORDER_TRANSITIONS[OrderStatus.SUBMITTING]
        assert OrderStatus.CANCEL_PENDING in reachable or (
            OrderStatus.CANCELLED in reachable
        ), (
            "an order that has not been acknowledged has no legal route to "
            f"cancellation: SUBMITTING may only become {reachable}"
        )

    def test_symbol_and_venue_constants_match_the_shipped_venue_set(self):
        """Guards the fixtures themselves against a configuration drift."""
        harness = build_harness()
        assert harness.settings.venue(VENUE_A).name == VENUE_A
        assert SYMBOL in harness.settings.venue(VENUE_A).symbols
