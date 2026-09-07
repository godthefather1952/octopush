"""Phase 6 — H2, H24: what a cancel request actually does.

``PaperExecutor.cancel`` has two paths. For an acknowledged order it
transitions to ``CANCEL_PENDING`` and schedules an arrival; for an order still
in ``CREATED``/``SUBMITTING`` it sets ``pending.cancel_at`` and returns,
leaving the status untouched. The comment says the order "resolves once it
arrives".

This file asks whether it does. ``poll`` reads the cancel schedule only inside
``if order.status is OrderStatus.CANCEL_PENDING``, and the pre-ack path never
sets that status — so the question is whether the earlier request survives the
acknowledgement that follows it.

This matters beyond tidiness. ``Orchestrator._protect`` answers a kill-switch
``cancel_all_requested`` with ``veska.cancel_all(tick_time)``, and an order
submitted on the tick the switch engages is exactly an order still in
``SUBMITTING``. P5-6 established that a safety action must not be undone by
state that predates it.

H24 (same-instant ordering) is pinned in the second class: at one logical
instant a cancel arrival, an expiry and a fillable book can all be due, and the
precedence between them must be a decision rather than an accident of
statement order.
"""

from __future__ import annotations

import pytest

from core.models.execution import OrderStatus
from tests.audit.veska_fixtures import (
    VENUE_A_CANCEL_LATENCY_MS,
    VENUE_A_LATENCY_MS,
    audit_settings,
    deterministic_execution,
    one_venue_market,
    plan,
    planned,
    rig,
)
from tests.conftest import START_MS

#: Every random source pinned: this file is about order state, not the RNG.
CERTAIN = audit_settings(**deterministic_execution())

ACK_AT = START_MS + VENUE_A_LATENCY_MS


def crossing_buy(**overrides):
    """A BUY whose limit sits above the ask, so it fills the instant it can."""
    fields = {"expected_price": 40_001.0, "limit_price": 40_100.0, "quantity": 0.10}
    fields.update(overrides)
    return planned(**fields)


class TestCancelBeforeAcknowledgement:
    """H2. A cancel requested before the venue acknowledged the order."""

    async def test_the_pre_ack_path_leaves_the_status_untouched(self):
        """The premise, recorded before any judgement about it."""
        built = rig(settings=CERTAIN, market_state=one_venue_market())
        await built.executor.submit(plan(crossing_buy()), START_MS)
        order = built.only_order()
        assert order.status is OrderStatus.SUBMITTING

        await built.executor.cancel(order.client_order_id, START_MS + 1)
        assert built.executor._pending[order.client_order_id].cancel_at == START_MS + 1
        assert order.status is OrderStatus.SUBMITTING, (
            "premise: the pre-ack path records an arrival time without moving "
            "the order into CANCEL_PENDING"
        )

    async def test_a_pre_ack_cancel_must_not_silently_disappear(self):
        """The safety property.

        Submit, request a cancel before the acknowledgement lands, then let the
        order arrive into a book it can cross immediately. The order may cancel
        on arrival, or follow some other documented venue-realistic race — but
        it must not behave as though no cancellation was ever requested.
        """
        built = rig(settings=CERTAIN, market_state=one_venue_market())
        await built.executor.submit(plan(crossing_buy()), START_MS)
        order = built.only_order()
        await built.executor.cancel(order.client_order_id, START_MS + 1)

        fills = await built.executor.poll(ACK_AT)

        assert not fills, (
            "an order cancelled before it was acknowledged filled on arrival: "
            f"status={order.status.value} filled={order.filled_quantity} "
            f"cancel_at={built.executor._pending[order.client_order_id].cancel_at} "
            "— the cancellation request was never consumed"
        )

    async def test_the_cancellation_reaches_a_recognisable_state(self):
        """Whatever the race model, the request must leave a trace in state.

        Either the order is cancelled, or it is at least marked as having a
        cancel in flight. Silently returning to the ordinary OPEN lifecycle is
        the one outcome that loses the operator's instruction.
        """
        built = rig(settings=CERTAIN, market_state=one_venue_market())
        await built.executor.submit(plan(crossing_buy()), START_MS)
        order = built.only_order()
        await built.executor.cancel(order.client_order_id, START_MS + 1)

        await built.executor.poll(ACK_AT)

        assert order.status in (
            OrderStatus.CANCELLED,
            OrderStatus.CANCEL_PENDING,
            OrderStatus.EXPIRED,
        ), (
            "after a pre-ack cancel the order reached "
            f"{order.status.value}, which is indistinguishable from an order "
            "nobody ever asked to cancel"
        )

    async def test_a_pre_ack_cancel_survives_several_polls(self):
        """Not merely deferred by one tick.

        If the request is genuinely queued for arrival, a later poll must still
        honour it. If it was dropped, every later poll treats the order as an
        ordinary working order.
        """
        built = rig(settings=CERTAIN, market_state=one_venue_market())
        await built.executor.submit(plan(crossing_buy()), START_MS)
        order = built.only_order()
        await built.executor.cancel(order.client_order_id, START_MS + 1)

        for step in (ACK_AT, ACK_AT + 100, ACK_AT + 500):
            await built.executor.poll(step)

        assert order.filled_quantity == 0.0, (
            f"cancelled-before-ack order filled {order.filled_quantity} across "
            "later polls"
        )


class TestCancelAllBeforeAcknowledgement:
    """H2, through the path the kill switch actually uses."""

    async def test_cancel_all_counts_the_not_yet_acknowledged_order(self):
        built = rig(settings=CERTAIN, market_state=one_venue_market())
        await built.executor.submit(plan(crossing_buy()), START_MS)
        assert await built.executor.cancel_all(START_MS + 1) == 1

    async def test_a_kill_switch_cancel_all_stops_an_unacknowledged_order(self):
        """P5-6's property, at the execution boundary.

        ``_protect`` calls ``cancel_all`` on the tick the switch engages. An
        order submitted on that same tick has not been acknowledged yet. If the
        cancel is dropped, the platform opens exposure *after* the kill switch
        told it to stop — a safety action undone by state that predates it.
        """
        built = rig(settings=CERTAIN, market_state=one_venue_market())
        await built.executor.submit(plan(crossing_buy()), START_MS)
        order = built.only_order()

        await built.executor.cancel_all(START_MS + 1)
        fills = await built.executor.poll(ACK_AT)

        assert not fills and order.filled_quantity == 0.0, (
            "cancel_all did not stop an order that had not yet been "
            f"acknowledged: it filled {order.filled_quantity} at "
            f"{[f.price for f in fills]} after the switch requested a "
            "cancel-all"
        )

    async def test_cancel_all_is_not_defeated_by_execution_being_disabled(self):
        """A control: disabling execution blocks NEW submissions only.

        Outstanding orders still have to be cancelled or resolved, which is why
        the flag deliberately does not touch them.
        """
        built = rig(settings=CERTAIN, market_state=one_venue_market())
        await built.executor.submit(plan(crossing_buy()), START_MS)
        built.executor.execution_disabled = True
        assert await built.executor.cancel_all(START_MS + 1) == 1


class TestCancelAfterAcknowledgement:
    """The path that does transition, pinned so the two are distinguishable."""

    async def test_an_acknowledged_order_enters_cancel_pending(self):
        built = rig(settings=CERTAIN, market_state=one_venue_market())
        await built.executor.submit(plan(crossing_buy(limit_price=1.0)), START_MS)
        order = built.only_order()
        await built.executor.poll(ACK_AT)
        assert order.status is OrderStatus.OPEN

        await built.executor.cancel(order.client_order_id, ACK_AT)
        assert order.status is OrderStatus.CANCEL_PENDING
        assert built.executor._pending[order.client_order_id].cancel_at == (
            ACK_AT + VENUE_A_CANCEL_LATENCY_MS
        )

    async def test_the_cancel_completes_once_its_latency_has_elapsed(self):
        built = rig(settings=CERTAIN, market_state=one_venue_market())
        # Limit far below the ask: never marketable, so the cancel wins.
        await built.executor.submit(plan(crossing_buy(limit_price=1.0)), START_MS)
        order = built.only_order()
        await built.executor.poll(ACK_AT)
        await built.executor.cancel(order.client_order_id, ACK_AT)

        await built.executor.poll(ACK_AT + VENUE_A_CANCEL_LATENCY_MS - 1)
        assert order.status is OrderStatus.CANCEL_PENDING

        await built.executor.poll(ACK_AT + VENUE_A_CANCEL_LATENCY_MS)
        assert order.status is OrderStatus.CANCELLED

    async def test_an_in_flight_cancel_suspends_fill_evaluation(self):
        """Recorded, not judged.

        While ``now_ms < cancel_at`` the poll ``continue``s, so an order cannot
        trade during the cancel's flight time even if the market reaches it.
        For an entry that is conservative; for an exit it delays getting out.
        Pinned so a change of model is visible rather than silent.
        """
        built = rig(settings=CERTAIN, market_state=one_venue_market())
        # Limit below the ask: not fillable at acknowledgement.
        await built.executor.submit(
            plan(crossing_buy(limit_price=39_000.0)), START_MS
        )
        order = built.only_order()
        await built.executor.poll(ACK_AT)
        assert order.status is OrderStatus.OPEN and order.filled_quantity == 0.0

        await built.executor.cancel(order.client_order_id, ACK_AT)
        assert order.status is OrderStatus.CANCEL_PENDING

        # The market now comes to the order, mid-cancel.
        built.executor.update_market(one_venue_market(mid=38_000.0))
        fills = await built.executor.poll(ACK_AT + 1)

        assert not fills, "an order filled while its cancel was still in flight"
        assert order.status is OrderStatus.CANCEL_PENDING


class TestSameInstantOrdering:
    """H24. Cancel arrival, expiry and a fillable book all due at one instant."""

    async def test_a_cancel_that_has_arrived_is_evaluated_before_a_fill(self):
        """Structural: the precedence must be a decision, not an accident."""
        import inspect

        from execution.paper.executor import PaperExecutor

        source = inspect.getsource(PaperExecutor.poll)
        cancel_at = source.index("CANCEL_PENDING")
        fill_at = source.index("_attempt_fill")
        expiry_at = source.index("expires_at")
        assert cancel_at < fill_at < expiry_at, (
            "poll's ordering changed: cancel arrival, then fill, then expiry "
            "is the sequence every scenario below assumes"
        )

    async def test_a_non_marketable_cancel_wins_against_expiry_at_the_same_instant(self):
        """Cancel arrival and TTL expiry both due at T."""
        ttl = VENUE_A_LATENCY_MS + VENUE_A_CANCEL_LATENCY_MS
        built = rig(settings=CERTAIN, market_state=one_venue_market())
        await built.executor.submit(
            plan(crossing_buy(limit_price=1.0, ttl_ms=ttl)), START_MS
        )
        order = built.only_order()
        await built.executor.poll(ACK_AT)
        await built.executor.cancel(order.client_order_id, ACK_AT)

        await built.executor.poll(ACK_AT + VENUE_A_CANCEL_LATENCY_MS)
        assert order.status is OrderStatus.CANCELLED, (
            "with a cancel arrival and an expiry due at the same instant the "
            f"order reached {order.status.value}; the precedence must be "
            "deterministic"
        )

    async def test_a_crossing_order_fills_deterministically_on_arrival(self):
        """The control every scenario above is measured against.

        With randomness pinned off, an order whose limit is through the ask
        fills in full on the poll that acknowledges it. Anything a cancel test
        observes instead of this is the cancel's doing, not the RNG's.
        """
        built = rig(settings=CERTAIN, market_state=one_venue_market())
        await built.executor.submit(plan(crossing_buy()), START_MS)
        order = built.only_order()

        fills = await built.executor.poll(ACK_AT)

        assert len(fills) == 1
        assert order.status is OrderStatus.FILLED
        assert order.filled_quantity == pytest.approx(0.10)

    @pytest.mark.parametrize("offset", [-1, 0, 1])
    async def test_expiry_is_inclusive_at_its_instant(self, offset):
        """``now_ms >= expires_at``: at the deadline the order is done."""
        ttl = 500
        built = rig(settings=CERTAIN, market_state=one_venue_market())
        await built.executor.submit(
            plan(crossing_buy(limit_price=1.0, ttl_ms=ttl)), START_MS
        )
        order = built.only_order()
        await built.executor.poll(ACK_AT)
        expires_at = order.expires_at
        assert expires_at is not None

        await built.executor.poll(expires_at + offset)
        if offset < 0:
            assert order.status is OrderStatus.OPEN
        else:
            assert order.status is OrderStatus.EXPIRED
