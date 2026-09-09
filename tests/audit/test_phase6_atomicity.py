"""H8 and H17 — a partially completed submission, and state that outruns its record.

H8: MULTI-LEG SUBMISSION FAILURE
================================
Batch A made interrupted submission recoverable: if a per-leg publication
raises, ``Veska.execute`` inspects the executor's actual resident orders and
attaches every created identity to the plan before re-raising. ``cancel_plan``
therefore retains a route to every outstanding leg.

Batch D preserves that reachability while changing the per-order event
boundary: a projected creation/update is published before the corresponding
working-state transition commits. An order whose creation publication is
rejected can remain resident as inert CREATED state, but it is still named by
the plan and can be cancelled or reconciled rather than becoming unmanaged.

H17: EVENT AND STATE ATOMICITY
==============================
Batch D makes publication the commit boundary.

Generic order-state changes are first projected onto a detached order, the
projected ``PAPER_ORDER_UPDATED`` is published, and only then is the identical
transition committed to the resident OMS order. A rejected publication
therefore leaves the real order unchanged.

For fills, OMS validation and account realized-PnL preview are non-mutating.
The canonical ``PAPER_FILL`` is published before OMS/account mutation. A
derived order update that fails after that durable fill is retained for retry
rather than rolling the already-recorded economic fact back.
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
        publish_at = source.index("event_type=EventType.PAPER_ORDER_CREATED")
        assert publish_at > loop_at, (
            "the projected creation publication is no longer inside the "
            "per-order loop; the H8 reproduction below needs rebuilding"
        )

    def test_veska_recovers_actual_orders_when_submit_raises(self):
        """The exception path attaches resident identities before re-raising."""
        from execution.veska.engine import Veska

        source = inspect.getsource(Veska.execute)
        submit_at = source.index("await self.executor.submit(plan, now_ms)")
        except_at = source.index("except Exception:")
        actual_at = source.index("actual_orders = self.executor.orders_for_plan")
        attach_at = source.index("self.registry.attach_orders(", except_at)
        assert submit_at < except_at < actual_at < attach_at

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
            f"venue but its plan record names only {record.order_ids}"
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
    """H17 — accepted event truth and live state move in the same direction."""

    def test_record_fill_publishes_before_it_mutates_economic_state(self):
        from execution.paper.executor import PaperExecutor

        source = inspect.getsource(PaperExecutor._record_fill)
        publish_at = source.index("await self.bus.publish(")
        oms_at = source.index("self.oms.apply_fill(")
        account_at = source.index("self.account.apply_fill(fill)")
        assert publish_at < oms_at
        assert publish_at < account_at

    async def test_a_failed_fill_publication_leaves_no_unrecorded_ledger_move(
        self,
    ):
        """A rejected canonical fill publication must commit no economics."""
        # #1 EXECUTION_PLAN, #2 PAPER_ORDER_CREATED, #3 EXECUTION_REPORT,
        # #4 acknowledgement/open PAPER_ORDER_UPDATED, #5 PAPER_FILL.
        # Fail on #5 so the canonical fill is rejected before economic commit.
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

        assert harness.account.fills_applied == fills_before
        assert harness.account.cash == cash_before
        assert harness.oms.fills_applied == 0
        assert not any(
            e.type is EventType.PAPER_FILL for e in bus.published
        )

    async def test_an_order_transition_is_not_lost_when_its_event_fails(self):
        """A rejected projected update must leave the resident order untouched."""
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
        assert order.status is OrderStatus.SUBMITTING
        assert order.history[-1][1] is OrderStatus.SUBMITTING
        assert published_updates == []

    async def test_a_failed_fill_derived_update_is_retried_without_double_apply(self):
        """PAPER_FILL is canonical; its derived order update may retry safely."""
        # #1 EXECUTION_PLAN, #2 PAPER_ORDER_CREATED, #3 EXECUTION_REPORT,
        # #4 OPEN update, #5 PAPER_FILL succeeds, #6 derived order update fails.
        bus = ExplodingBus(fail_on_publish=6)
        harness = build_harness(bus=bus)
        harness.update_market(two_venue_market())
        latency = harness.settings.venue(VENUE_A).latency_ms
        plan = execution_plan(
            planned_order(
                quantity=0.5,
                time_in_force=TimeInForce.IOC,
                limit_price=101.0,
            ),
            created_at=T0,
        )
        await harness.veska.execute(plan, T0)

        with pytest.raises(RuntimeError, match="audit-injected"):
            await harness.veska.poll(T0 + latency)

        order = harness.orders_of(plan.plan_id)[0]
        assert harness.account.fills_applied == 1
        assert harness.oms.fills_applied == 1
        assert order.filled_quantity > 0
        assert order.client_order_id in harness.executor._pending_order_updates
        assert len([e for e in bus.published if e.type is EventType.PAPER_FILL]) == 1

        await harness.veska.poll(T0 + latency + 1)

        assert order.client_order_id not in harness.executor._pending_order_updates
        assert harness.account.fills_applied == 1
        assert harness.oms.fills_applied == 1
        assert len([e for e in bus.published if e.type is EventType.PAPER_FILL]) == 1
        updates = [
            e for e in bus.published if e.type is EventType.PAPER_ORDER_UPDATED
        ]
        assert updates


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
