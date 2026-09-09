"""H10 — an expired plan must not start new risk.

Batch B makes the absolute deadline part of VESKA's canonical preflight gate.
The boundary is inclusive: `now_ms == deadline_ms` remains valid, while
`now_ms > deadline_ms` is refused before OMS or venue-timing mutation.

This finding is intentionally narrower than the lifecycle of an order accepted
in time. Existing tests below continue to pin acknowledgement/fill behavior
after the deadline for already-working orders without redefining that policy.
"""

from __future__ import annotations

import inspect
from pathlib import Path

from core.models.common import TimeInForce
from core.models.execution import OrderStatus
from tests.audit.veska_fixtures import (
    T0,
    VENUE_A,
    build_harness,
    execution_plan,
    planned_order,
    two_venue_market,
)

DEADLINE = T0 + 10_000


def _plan_with_deadline(**kwargs):
    return execution_plan(
        planned_order(
            quantity=1.0, time_in_force=TimeInForce.IOC, limit_price=101.0
        ),
        created_at=T0,
        deadline_ms=DEADLINE,
        **kwargs,
    )


class TestWhereTheDeadlineIsRead:
    """Static inventory for the submission boundary."""

    def test_preflight_owns_the_absolute_submission_comparison(self):
        from execution.veska.preflight import preflight_plan

        source = inspect.getsource(preflight_plan)
        assert "now_ms > plan.deadline_ms" in source
        assert "EXPIRED_DEADLINE" in source

    def test_the_plan_record_remains_observability_not_enforcement(self):
        text = Path("core/models/execution.py").read_text()
        assert "nothing enforces it here" in text

    def test_the_orchestrator_cleanup_still_handles_already_working_orders(self):
        from apps.orchestrator import orchestrator as orch_module

        source = inspect.getsource(orch_module.Orchestrator._advance_execution)
        assert "self.tick_time > deadline" in source
        assert "await self.veska.cancel(" in source


class TestSubmissionAgainstTheDeadline:
    """The boundary, one millisecond at a time."""

    async def test_submission_one_millisecond_before_the_deadline_is_accepted(
        self,
    ):
        """Pins the permitted side, so the refusal below is a real boundary."""
        harness = build_harness()
        harness.update_market(two_venue_market())
        plan = _plan_with_deadline(plan_id="plan-before")

        report = await harness.veska.execute(plan, DEADLINE - 1)

        assert report.orders
        assert report.orders[0].status is OrderStatus.SUBMITTING

    async def test_submission_exactly_at_the_deadline(self):
        """Recorded rather than judged: an absolute deadline is inclusive or not.

        What matters is that the answer is the same every time and the same
        under replay. This pins it.
        """
        harness = build_harness()
        harness.update_market(two_venue_market())
        plan = _plan_with_deadline(plan_id="plan-at")

        report = await harness.veska.execute(plan, DEADLINE)

        accepted = bool(report.orders) and not report.notes
        assert accepted, (
            "submission exactly at the deadline is currently accepted; the "
            "audit's record of this boundary is stale"
        )

    async def test_submission_after_the_deadline_creates_no_new_risk(self):
        """The invariant. An expired plan must not open a position.

        This is the weakest form: not "the plan is rejected loudly", merely
        that nothing becomes workable at a venue after its own deadline has
        passed.
        """
        harness = build_harness()
        harness.update_market(two_venue_market())
        plan = _plan_with_deadline(plan_id="plan-after")

        report = await harness.veska.execute(plan, DEADLINE + 1)

        assert report.orders == []
        assert harness.orders_of(plan.plan_id) == []
        assert harness.oms.orders_created == 0
        assert harness.executor._pending == {}
        assert any("EXPIRED_DEADLINE" in note for note in report.notes)

    async def test_an_order_submitted_after_the_deadline_does_not_fill(self):
        """The economic consequence of the same gap."""
        harness = build_harness()
        harness.update_market(two_venue_market())
        latency = harness.settings.venue(VENUE_A).latency_ms
        plan = _plan_with_deadline(plan_id="plan-after-fill")

        await harness.veska.execute(plan, DEADLINE + 1)
        harness.update_market(two_venue_market(created_at=DEADLINE + latency + 1))
        fills = await harness.veska.poll(DEADLINE + latency + 2)

        assert not fills, (
            f"an order submitted {1}ms after its plan's deadline filled "
            f"{sum(f.quantity for f in fills)} units"
        )


class TestAcknowledgementAndFillPastTheDeadline:
    """An order accepted in time whose venue-side life outlives the deadline."""

    async def test_an_order_acknowledged_after_the_deadline_is_recorded(self):
        """Pins current behaviour: acknowledgement is not deadline-aware.

        Whether it should be is a design question. That it is deterministic
        is not.
        """
        harness = build_harness()
        harness.update_market(two_venue_market())
        latency = harness.settings.venue(VENUE_A).latency_ms
        plan = execution_plan(
            planned_order(time_in_force=TimeInForce.GTC, limit_price=95.0, ttl_ms=60_000),
            created_at=T0,
            deadline_ms=T0 + latency // 2,
            plan_id="plan-ack-late",
        )
        await harness.veska.execute(plan, T0)
        order = harness.orders_of(plan.plan_id)[0]

        await harness.veska.poll(T0 + latency)

        assert order.status is OrderStatus.OPEN
        assert order.acknowledged_at == T0 + latency
        assert order.acknowledged_at > plan.deadline_ms

    async def test_a_fill_past_the_deadline_is_recorded_and_deterministic(self):
        harness = build_harness()
        harness.update_market(two_venue_market())
        latency = harness.settings.venue(VENUE_A).latency_ms
        plan = execution_plan(
            planned_order(
                quantity=0.5, time_in_force=TimeInForce.IOC, limit_price=101.0
            ),
            created_at=T0,
            deadline_ms=T0 + latency // 2,
            plan_id="plan-fill-late",
        )
        await harness.veska.execute(plan, T0)

        fills = await harness.veska.poll(T0 + latency)

        assert fills, "no fill; the deadline comparison below is untested"
        assert all(f.created_at > plan.deadline_ms for f in fills), (
            "the audit expected fills past the deadline and got none"
        )


class TestCancelRaceAfterTheDeadline:
    """The orchestrator's cleanup path, exercised at the execution boundary."""

    async def test_a_cancel_issued_past_the_deadline_still_takes_effect(self):
        harness = build_harness()
        harness.update_market(
            two_venue_market(a_bid=90.0, a_ask=110.0, b_bid=90.0, b_ask=110.0)
        )
        latency = harness.settings.venue(VENUE_A).latency_ms
        cancel_latency = harness.settings.venue(VENUE_A).cancel_latency_ms
        plan = execution_plan(
            planned_order(
                time_in_force=TimeInForce.GTC, limit_price=95.0, ttl_ms=600_000
            ),
            created_at=T0,
            deadline_ms=T0 + latency,
            plan_id="plan-cancel-late",
        )
        await harness.veska.execute(plan, T0)
        order = harness.orders_of(plan.plan_id)[0]
        await harness.veska.poll(T0 + latency)

        past = plan.deadline_ms + 1_000
        await harness.veska.cancel(order.client_order_id, past)
        await harness.veska.poll(past + cancel_latency)

        assert order.status is OrderStatus.CANCELLED, (
            "the deadline-driven cleanup could not cancel an order past its "
            f"deadline: it is {order.status.value}"
        )
