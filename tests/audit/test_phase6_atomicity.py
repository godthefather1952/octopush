"""H8 and H17 — a partially completed submission, and state that outruns its record.

H8: MULTI-LEG SUBMISSION FAILURE
================================
``PaperExecutor.submit`` loops over the plan's orders, and for each one it
creates the order in the OMS, marks it SUBMITTING, records its pending
timing and **awaits a bus publication**. A failure in that publication — a
transport error, a cascade-capacity refusal — propagates out of ``submit``.

What has already happened when it does: leg one exists in the OMS, is
SUBMITTING, has an ``ack_at``, and will fill on the next poll.

What has not happened: ``Veska.execute``'s ``registry.attach_orders`` call,
which is downstream of the ``await self.executor.submit(...)`` that raised. The
plan record's ``order_ids`` stays empty. And in the orchestrator,
``record.order_ids = [...]`` is likewise downstream, so the opportunity record
does not name it either.

The result is a live order that no plan record, no opportunity record and
therefore no cancellation path can reach.

H17: EVENT AND STATE ATOMICITY
==============================
``_record_fill`` applies the fill to the OMS and to the paper account, and
*then* publishes ``PAPER_FILL``. The recorder is bus middleware, so an event
that never reaches ``publish`` is never recorded. The account has moved; the
durable history has not.

The exception does propagate, which makes this loud rather than silent — and
that distinction is the difference between HIGH and CRITICAL. It is recorded
either way, because a replayed session reconstructs the ledger from recorded
fills, and a fill that changed the account without being recorded is a
divergence replay cannot close.
"""

from __future__ import annotations

import inspect

import pytest

from core.events import EventType
from core.models.common import Side, TimeInForce
from core.models.execution import OrderStatus
from tests.audit.veska_fixtures import (
    T0,
    VENUE_A,
    VENUE_B,
    ExplodingBus,
    build_harness,
    execution_plan,
    planned_order,
    two_venue_market,
)


def _two_leg_plan(**kwargs):
    return execution_plan(
        planned_order(
            venue=VENUE_A,
            side=Side.BUY,
            quantity=1.0,
            limit_price=101.0,
            client_order_id="leg-a",
        ),
        planned_order(
            venue=VENUE_B,
            side=Side.SELL,
            quantity=1.0,
            limit_price=99.0,
            client_order_id="leg-b",
        ),
        created_at=T0,
        plan_id="plan-two-leg",
        **kwargs,
    )


class TestSubmissionOrdering:
    """Where the publication sits relative to the state mutation."""

    def test_submit_publishes_inside_the_per_order_loop(self):
        """Static evidence: the failure point is per-leg, not per-plan."""
        from execution.paper.executor import PaperExecutor

        source = inspect.getsource(PaperExecutor.submit)
        loop_at = source.index("for planned in plan.orders:")
        publish_at = source.index("_publish_order(order, EventType.PAPER_ORDER_CREATED")
        assert publish_at > loop_at, (
            "the creation publication is no longer inside the per-order loop; "
            "the H8 reproduction below needs rebuilding"
        )

    def test_veska_attaches_orders_after_submit_returns(self):
        """So a raising submit leaves the plan record with no order ids."""
        from execution.veska.engine import Veska

        source = inspect.getsource(Veska.execute)
        submit_at = source.index("await self.executor.submit(plan, now_ms)")
        attach_at = source.index("self.registry.attach_orders(")
        assert attach_at > submit_at

    def test_the_orchestrator_records_order_ids_after_execute_returns(self):
        from apps.orchestrator import orchestrator as orch_module

        source = inspect.getsource(orch_module.Orchestrator._decide)
        execute_at = source.index("await self.veska.execute(plan, self.tick_time)")
        record_at = source.index("record.order_ids = [")
        assert record_at > execute_at


class TestPartialMultiLegSubmission:
    """H8 — the first leg must not become unmanaged."""

    async def _submit_failing_on_second_leg(self):
        # Publish #1 is EXECUTION_PLAN, #2 is leg A's PAPER_ORDER_CREATED,
        # #3 is leg B's. ExplodingBus fails on exactly that publication,
        # then permits later polling so the stranded-leg consequence can
        # be observed rather than hidden by a permanently failing audit bus.
        bus = ExplodingBus(fail_on_publish=3)
        harness = build_harness(bus=bus)
        harness.update_market(two_venue_market())
        plan = _two_leg_plan()
        with pytest.raises(RuntimeError, match="audit-injected"):
            await harness.veska.execute(plan, T0)
        return harness, plan

    async def test_the_accepted_leg_is_reachable_from_its_plan_record(self):
        """The invariant: no live order without a plan that names it."""
        harness, plan = await self._submit_failing_on_second_leg()

        leg_a = harness.oms.get("leg-a")
        assert leg_a is not None, "the audit's injection point is wrong"
        assert leg_a.is_outstanding, (
            "leg A is not outstanding, so there is nothing to strand; the "
            "reproduction needs rebuilding"
        )

        record = harness.veska.get_plan(plan.plan_id)
        assert record is not None
        assert "leg-a" in record.order_ids, (
            "the first leg of a partially submitted plan is working at the "
            f"venue and its plan record names {record.order_ids}: nothing can "
            "cancel it through cancel_plan"
        )

    async def test_cancel_plan_can_reach_the_accepted_leg(self):
        """The operational form: the control surface must be able to stop it."""
        harness, plan = await self._submit_failing_on_second_leg()

        result = await harness.veska.cancel_plan(plan.plan_id, T0 + 1)

        leg_a = harness.oms.get("leg-a")
        assert leg_a is not None
        cancelled_or_pending = leg_a.status in (
            OrderStatus.CANCEL_PENDING,
            OrderStatus.CANCELLED,
        )
        assert cancelled_or_pending, (
            "cancel_plan reported "
            f"{result.reason!r} and left the stranded leg {leg_a.status.value}"
        )

    async def test_the_accepted_leg_does_not_fill_unnoticed(self):
        """The economic consequence: it trades, and nobody is tracking it."""
        harness, plan = await self._submit_failing_on_second_leg()
        latency = harness.settings.venue(VENUE_A).latency_ms

        fills = await harness.veska.poll(T0 + latency)

        record = harness.veska.get_plan(plan.plan_id)
        tracked = set(record.order_ids) if record else set()
        untracked = [f for f in fills if f.client_order_id not in tracked]
        assert not untracked, (
            f"{len(untracked)} fill(s) landed on an order no plan record "
            f"names: {[f.client_order_id for f in untracked]}"
        )

    async def test_the_plan_record_does_not_read_as_finished(self):
        """A plan whose submission failed must not look complete or cancelled."""
        harness, plan = await self._submit_failing_on_second_leg()
        record = harness.veska.get_plan(plan.plan_id)
        assert record is not None
        assert not record.is_terminal, (
            f"a plan whose submission raised mid-flight reads as "
            f"{record.status.value}"
        )


class TestFillStateVersusEventTruth:
    """H17 — the account moves before the event is durable."""

    def test_record_fill_mutates_before_it_publishes(self):
        from execution.paper.executor import PaperExecutor

        source = inspect.getsource(PaperExecutor._record_fill)
        oms_at = source.index("self.oms.apply_fill(fill)")
        account_at = source.index("self.account.apply_fill(fill)")
        publish_at = source.index("await self.bus.publish(")
        assert oms_at < publish_at
        assert account_at < publish_at

    async def test_a_failed_fill_publication_leaves_no_unrecorded_ledger_move(
        self,
    ):
        """The invariant: economic truth and recorded truth must not diverge.

        If the publication fails, either the account must not have moved or
        some recovery must exist. Neither holds today: the account has the
        fill, the bus never saw it, and the exception simply propagates.
        """
        # #1 EXECUTION_PLAN, #2 PAPER_ORDER_CREATED, #3 EXECUTION_REPORT,
        # #4 acknowledgement/open PAPER_ORDER_UPDATED, #5 PAPER_FILL.
        # Fail on #5 so OMS/account mutation precedes the injected failure.
        bus = ExplodingBus(fail_on_publish=5)
        harness = build_harness(bus=bus)
        harness.update_market(two_venue_market())
        latency = harness.settings.venue(VENUE_A).latency_ms
        plan = execution_plan(
            planned_order(
                quantity=0.5, time_in_force=TimeInForce.IOC, limit_price=101.0
            ),
            created_at=T0,
        )
        await harness.veska.execute(plan, T0)
        cash_before = harness.account.cash
        fills_before = harness.account.fills_applied

        with pytest.raises(RuntimeError, match="audit-injected"):
            await harness.veska.poll(T0 + latency)

        moved = (
            harness.account.fills_applied != fills_before
            or harness.account.cash != cash_before
        )
        recorded = any(
            e.type is EventType.PAPER_FILL for e in bus.published
        )
        assert not (moved and not recorded), (
            "the paper account applied a fill "
            f"({fills_before} -> {harness.account.fills_applied} fills, cash "
            f"{cash_before} -> {harness.account.cash}) that was never "
            "published, so no recorder could persist it and no replay can "
            "reconstruct it"
        )

    async def test_an_order_transition_is_not_lost_when_its_event_fails(self):
        """The same question for a status change rather than a fill."""
        # #1 EXECUTION_PLAN, #2 PAPER_ORDER_CREATED, #3 EXECUTION_REPORT,
        # #4 is the acknowledgement/open PAPER_ORDER_UPDATED under test.
        bus = ExplodingBus(fail_on_publish=4)
        harness = build_harness(bus=bus)
        harness.update_market(two_venue_market())
        latency = harness.settings.venue(VENUE_A).latency_ms
        plan = execution_plan(
            planned_order(time_in_force=TimeInForce.GTC, limit_price=95.0),
            created_at=T0,
        )
        await harness.veska.execute(plan, T0)
        order = harness.orders_of(plan.plan_id)[0]
        assert order.status is OrderStatus.SUBMITTING

        with pytest.raises(RuntimeError, match="audit-injected"):
            await harness.veska.poll(T0 + latency)

        published_updates = [
            e for e in bus.published if e.type is EventType.PAPER_ORDER_UPDATED
        ]
        assert not (order.status is not OrderStatus.SUBMITTING and not published_updates), (
            f"the order moved to {order.status.value} and no "
            "PAPER_ORDER_UPDATED reached the bus, so the durable history "
            "still says SUBMITTING"
        )


class TestNoSilentAbsorption:
    """A failure must not be swallowed into a quietly incomplete result."""

    async def test_a_raising_submission_does_not_return_a_report(self):
        bus = ExplodingBus(fail_on_publish=3)
        harness = build_harness(bus=bus)
        harness.update_market(two_venue_market())
        with pytest.raises(RuntimeError):
            await harness.veska.execute(_two_leg_plan(), T0)

    async def test_execution_disabled_is_reported_rather_than_raised(self):
        """The one path that legitimately returns a partial report.

        Contrast with the above: a kill-switched executor rejects each order
        and says so in ``notes``, which is a reported outcome rather than a
        lost one.
        """
        harness = build_harness()
        harness.update_market(two_venue_market())
        harness.executor.execution_disabled = True

        report = await harness.veska.execute(_two_leg_plan(), T0)

        assert len(report.orders) == 2
        assert all(o.status is OrderStatus.REJECTED for o in report.orders)
        assert len(report.notes) == 2
        assert harness.executor.rejected_submissions == 2
