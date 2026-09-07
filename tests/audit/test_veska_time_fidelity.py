"""Phase 6 — H1: one order, two timelines.

``PaperExecutor``'s module docstring states the contract plainly: it "never
calls ``self.clock.now_ms()``", because a clock read taken during execution is
not the instant the tick recorded, and P2-14 was the divergence that followed
from taking one. ``tests/unit/test_execution_time_fidelity.py`` guards that
absence, and it holds.

But the executor does not stamp orders. ``OrderManager`` does, and every one of
its mutating operations — ``create``, ``transition``, ``mark_unknown``,
``resolve_unknown``, ``reject``, and the transition inside ``apply_fill`` —
calls ``self.clock.now_ms()``. So a single execution action at logical time T
produces:

* ``submitted_at``, ``acknowledged_at``, event ``ts_ms`` and ``fill.created_at``
  from T, the caller's logical instant; and
* ``created_at``, ``expires_at``, ``terminal_at`` and every ``history`` entry
  from whatever the live clock reads when the OMS is called.

Under the same conditions that motivated P2-14 — a live feed advancing the
clock between the tick's snapshot and the moment execution runs — those are
different numbers for the same event.

``expires_at`` is the case with economic consequences rather than merely
forensic ones: it is computed from the clock and then compared against logical
time in ``poll``, so an order's time-to-live is measured across two bases.

The tests use ``oms_now`` to place the OMS clock away from the logical instant.
That is not an artificial condition; it is the condition the executor's own
docstring says it exists to survive.
"""

from __future__ import annotations

import inspect

import pytest

from core.events import EventType
from core.models.execution import OrderStatus
from execution.oms import OrderManager
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

#: How far the live clock has run ahead of the tick that produced the plan.
DRIFT_MS = 5_000


class TestThePremise:
    """Where each timestamp comes from, established before it is judged."""

    def test_the_executor_still_reads_no_clock(self):
        """The Phase 2 invariant, restated so this file cannot be misread as
        contradicting it."""
        source = inspect.getsource(PaperExecutor)
        body = "\n".join(
            line for line in source.splitlines() if not line.strip().startswith("#")
        )
        assert "self.clock.now_ms()" not in body

    @pytest.mark.parametrize(
        "operation",
        ["create", "transition", "mark_unknown", "resolve_unknown", "reject"],
    )
    def test_every_oms_operation_reads_the_clock(self, operation):
        source = inspect.getsource(getattr(OrderManager, operation))
        assert "self.clock.now_ms()" in source, (
            f"OrderManager.{operation} no longer reads the clock; the "
            "divergence this file measures may be gone"
        )

    def test_the_fill_transition_reads_the_clock_too(self):
        source = inspect.getsource(OrderManager.apply_fill)
        assert "self.clock.now_ms()" in source

    def test_no_oms_operation_accepts_an_explicit_instant(self):
        """The shape of the gap: there is nowhere to pass T in."""
        for name in ("create", "transition", "mark_unknown", "reject"):
            params = set(inspect.signature(getattr(OrderManager, name)).parameters)
            assert "now_ms" not in params, (
                f"OrderManager.{name} now takes an explicit instant; the "
                "contract may already be closed"
            )


class TestOneActionTwoTimestamps:
    """H1. Execution at logical T, OMS clock at T + drift."""

    async def test_submission_stamps_the_order_from_two_clocks(self):
        built = rig(
            settings=CERTAIN,
            oms_now=START_MS + DRIFT_MS,
            market_state=one_venue_market(),
        )
        await built.executor.submit(plan(planned(limit_price=1.0)), START_MS)
        order = built.only_order()

        assert order.submitted_at == START_MS
        assert order.created_at == START_MS + DRIFT_MS
        assert order.created_at == order.submitted_at, (
            "one submission produced two creation instants: submitted_at="
            f"{order.submitted_at} from the caller's logical time, created_at="
            f"{order.created_at} from the live clock — a difference of "
            f"{order.created_at - order.submitted_at}ms for the same event"
        )

    async def test_the_history_disagrees_with_the_published_event(self):
        """The forensic consequence: the event log and the order's own record
        of the same transition carry different instants."""
        built = rig(
            settings=CERTAIN,
            oms_now=START_MS + DRIFT_MS,
            market_state=one_venue_market(),
        )
        await built.executor.submit(plan(planned(limit_price=1.0)), START_MS)
        await built.bus.drain()
        order = built.only_order()

        created = built.events(EventType.PAPER_ORDER_CREATED)
        assert len(created) == 1
        published_at = created[0].ts_ms
        history_at = [ts for ts, _status in order.history]

        assert all(ts == published_at for ts in history_at), (
            f"PAPER_ORDER_CREATED was published at {published_at} while the "
            f"order's own history records {history_at}; a reader cannot "
            "reconstruct one timeline from the two"
        )

    async def test_the_acknowledgement_transition_is_stamped_from_the_clock(self):
        built = rig(
            settings=CERTAIN,
            oms_now=START_MS + DRIFT_MS,
            market_state=one_venue_market(),
        )
        await built.executor.submit(plan(planned(limit_price=1.0)), START_MS)
        order = built.only_order()

        await built.executor.poll(ACK_AT)

        assert order.acknowledged_at == ACK_AT
        open_stamps = [ts for ts, status in order.history if status is OrderStatus.OPEN]
        assert open_stamps == [ACK_AT], (
            f"the order acknowledged at logical {ACK_AT} but recorded its OPEN "
            f"transition at {open_stamps}"
        )

    async def test_a_fill_is_stamped_from_both_bases_at_once(self):
        """The fill event carries T; the transition it causes carries the
        clock. One economic action, two instants."""
        built = rig(
            settings=CERTAIN,
            oms_now=START_MS + DRIFT_MS,
            market_state=one_venue_market(),
        )
        await built.executor.submit(plan(planned(limit_price=40_100.0)), START_MS)
        order = built.only_order()

        fills = await built.executor.poll(ACK_AT)
        assert len(fills) == 1

        filled_stamps = [
            ts for ts, status in order.history if status is OrderStatus.FILLED
        ]
        assert filled_stamps == [fills[0].created_at], (
            f"the fill is stamped {fills[0].created_at} and the FILLED "
            f"transition it caused is stamped {filled_stamps}"
        )

    async def test_terminal_at_matches_the_terminating_poll(self):
        built = rig(
            settings=CERTAIN,
            oms_now=START_MS + DRIFT_MS,
            market_state=one_venue_market(),
        )
        await built.executor.submit(plan(planned(limit_price=40_100.0)), START_MS)
        order = built.only_order()
        await built.executor.poll(ACK_AT)

        assert order.terminal_at == ACK_AT, (
            f"the order became terminal during the poll at {ACK_AT} but "
            f"records terminal_at={order.terminal_at}"
        )


class TestTimeToLiveCrossesTheTwoBases:
    """The case with economic rather than forensic consequences."""

    async def test_expires_at_is_computed_from_the_clock(self):
        built = rig(
            settings=CERTAIN,
            oms_now=START_MS + DRIFT_MS,
            market_state=one_venue_market(),
        )
        ttl = 5_000
        await built.executor.submit(
            plan(planned(limit_price=1.0, ttl_ms=ttl)), START_MS
        )
        order = built.only_order()

        assert order.expires_at == START_MS + ttl, (
            "expires_at was computed as clock + ttl "
            f"({order.expires_at}) rather than as the submission instant + ttl "
            f"({START_MS + ttl}); poll then compares it against logical time"
        )

    async def test_a_lagging_clock_expires_an_order_inside_its_own_ttl(self):
        """The consequence, in the direction that destroys working orders.

        ``expires_at`` is ``clock + ttl``; ``poll`` expires the order once
        logical ``now_ms`` reaches that number. With the clock reading
        ``DRIFT_MS`` behind the logical instant, ``expires_at`` lands at or
        before the acknowledgement — so an order with a five-second lifetime
        can expire on the very poll that acknowledges it, having had no
        opportunity to trade at all.
        """
        ttl = 5_000
        built = rig(
            settings=CERTAIN,
            oms_now=START_MS - DRIFT_MS,
            market_state=one_venue_market(),
        )
        await built.executor.submit(
            plan(planned(limit_price=1.0, ttl_ms=ttl)), START_MS
        )
        order = built.only_order()

        await built.executor.poll(ACK_AT)

        assert order.status is not OrderStatus.EXPIRED, (
            f"an order with a {ttl}ms time-to-live expired on its "
            f"acknowledgement poll at {ACK_AT}: expires_at="
            f"{order.expires_at} was computed from a clock reading "
            f"{DRIFT_MS}ms behind the logical execution time, so its lifetime "
            "was consumed before the order existed"
        )

    async def test_a_leading_clock_extends_an_order_past_its_ttl(self):
        """And the opposite direction, which lets an order work too long."""
        ttl = 1_000
        built = rig(
            settings=CERTAIN,
            oms_now=START_MS + DRIFT_MS,
            market_state=one_venue_market(),
        )
        await built.executor.submit(
            plan(planned(limit_price=1.0, ttl_ms=ttl)), START_MS
        )
        order = built.only_order()

        await built.executor.poll(ACK_AT)
        await built.executor.poll(START_MS + ttl)

        assert order.status is OrderStatus.EXPIRED, (
            f"an order with a {ttl}ms time-to-live was still "
            f"{order.status.value} at its own deadline, because "
            f"expires_at={order.expires_at} was computed from a clock reading "
            f"{DRIFT_MS}ms ahead of the logical execution time"
        )


class TestReplayEquivalenceUnderDrift:
    """Two runs of the same logical sequence, different live-clock positions.

    A replay re-executes a recorded tick at the recorded instant. The live
    clock during replay has no reason to sit where it sat during the original
    run — that is the whole point of P2-14. Everything reconstructible from the
    tick must therefore be identical.
    """

    @staticmethod
    async def _run(oms_now: int) -> dict:
        built = rig(
            settings=CERTAIN,
            oms_now=oms_now,
            market_state=one_venue_market(),
        )
        await built.executor.submit(
            plan(planned(client_order_id="ord-fixed", limit_price=40_100.0)),
            START_MS,
        )
        fills = await built.executor.poll(ACK_AT)
        await built.bus.drain()
        order = built.order("ord-fixed")
        return {
            "status": order.status.value,
            "filled_quantity": order.filled_quantity,
            "average_price": order.average_price,
            "fees_paid": order.fees_paid,
            "submitted_at": order.submitted_at,
            "acknowledged_at": order.acknowledged_at,
            "fill_times": [f.created_at for f in fills],
            "fill_prices": [f.price for f in fills],
            "event_times": [e.ts_ms for e in built.published],
            "history": [(ts, status.value) for ts, status in order.history],
            "created_at": order.created_at,
            "expires_at": order.expires_at,
            "terminal_at": order.terminal_at,
        }

    async def test_the_economic_outcome_is_identical(self):
        """The half that already holds, and must keep holding."""
        original = await self._run(START_MS)
        replayed = await self._run(START_MS + DRIFT_MS)
        for field in (
            "status",
            "filled_quantity",
            "average_price",
            "fees_paid",
            "submitted_at",
            "acknowledged_at",
            "fill_times",
            "fill_prices",
            "event_times",
        ):
            assert original[field] == replayed[field], (
                f"{field} diverged between two runs of the same logical "
                f"sequence: {original[field]} vs {replayed[field]}"
            )

    async def test_the_recorded_order_is_identical_too(self):
        """The half that does not.

        These fields are part of the order the recorder persists and MARIN
        reconciles against, so a divergence here is a divergence in the audit
        trail, not merely in a debugging aid.
        """
        original = await self._run(START_MS)
        replayed = await self._run(START_MS + DRIFT_MS)
        divergent = {
            field: (original[field], replayed[field])
            for field in ("created_at", "expires_at", "terminal_at", "history")
            if original[field] != replayed[field]
        }
        assert not divergent, (
            "the same logical execution produced different recorded orders "
            f"when the live clock sat elsewhere: {divergent}"
        )
