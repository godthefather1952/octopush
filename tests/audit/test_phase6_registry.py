"""H21, H26, H27 — the execution registry must not claim more than it knows.

H26: REGISTRY TRUTH
===================
``derive_plan_status`` computes a plan's status from the orders it owns, with
one rule that carries all the weight: **anything UNKNOWN dominates**. A plan
cannot read COMPLETE or CANCELLED while part of it might still be working.

The registry's own docstring flags the open question honestly — "validation
will decide whether COMPLETE is the right word for 'filled 0.02 of 1.0 and
cancelled the rest'" — so the mixed-terminal cases below are pinned rather than
judged, except where they would let unresolved truth read as resolved.

H27: IDENTITY
=============
One ``plan_id`` names one execution attempt. Re-registering must return the
held record rather than replacing it, or the record loses the orders the first
registration attached.

H21: EXECUTION REPORT SEMANTICS
===============================
The report returned at submission remains a detached snapshot of that instant:
working orders can legitimately have no fills and ``complete=False``. Batch D
adds lifecycle reports derived from registry + OMS truth. VESKA fingerprints
report-visible state and publishes a new ``EXECUTION_REPORT`` only when that
truth changes, so later snapshots carry actual fills and terminal completion
without mutating previously returned report objects.
"""

from __future__ import annotations

import inspect

import pytest

from core.models.common import Side, TimeInForce
from core.models.execution import (
    ExecutionPlanRecord,
    ExecutionPlanStatus,
    ExecutionRole,
    OrderStatus,
)
from execution.veska.registry import ExecutionRegistry, derive_plan_status
from tests.audit.veska_fixtures import (
    T0,
    VENUE_A,
    VENUE_B,
    build_harness,
    execution_plan,
    market_state,
    planned_order,
    price_levels,
    two_venue_market,
    venue_state,
)


def _record(**kwargs) -> ExecutionPlanRecord:
    base = {
        "plan_id": "p",
        "intent_id": "i",
        "strategy": "s",
        "symbol": "BTC-USD",
        "execution_role": ExecutionRole.ENTRY,
        "status": ExecutionPlanStatus.SUBMITTING,
        "created_at": T0,
        "updated_at": T0,
    }
    base.update(kwargs)
    return ExecutionPlanRecord(**base)


def _order(status: OrderStatus, *, filled: float = 0.0, oid: str = "o"):
    from core.models.common import OrderType
    from core.models.execution import PaperOrder

    order = PaperOrder(
        created_at=T0,
        client_order_id=oid,
        venue=VENUE_A,
        symbol="BTC-USD",
        side=Side.BUY,
        quantity=1.0,
        order_type=OrderType.LIMIT,
        time_in_force=TimeInForce.GTC,
        expected_price=100.0,
        status=status,
    )
    order.filled_quantity = filled
    return order


def _latency(harness, venue: str = VENUE_A) -> int:
    return harness.settings.venue(venue).latency_ms


class TestDerivePlanStatus:
    """H26 — every case the function claims to handle."""

    def test_no_orders_keeps_the_record_status(self):
        record = _record(status=ExecutionPlanStatus.SUBMITTING)
        assert derive_plan_status(record, []) is ExecutionPlanStatus.SUBMITTING

    def test_all_working_reads_working(self):
        record = _record()
        orders = [_order(OrderStatus.OPEN, oid="a"), _order(OrderStatus.OPEN, oid="b")]
        assert derive_plan_status(record, orders) is ExecutionPlanStatus.WORKING

    def test_a_partial_fill_reads_partially_filled(self):
        record = _record()
        orders = [
            _order(OrderStatus.PARTIALLY_FILLED, filled=0.4, oid="a"),
            _order(OrderStatus.OPEN, oid="b"),
        ]
        assert (
            derive_plan_status(record, orders)
            is ExecutionPlanStatus.PARTIALLY_FILLED
        )

    def test_a_cancel_in_flight_reads_cancel_pending(self):
        record = _record()
        orders = [
            _order(OrderStatus.CANCEL_PENDING, oid="a"),
            _order(OrderStatus.OPEN, oid="b"),
        ]
        assert (
            derive_plan_status(record, orders) is ExecutionPlanStatus.CANCEL_PENDING
        )

    def test_all_filled_reads_complete(self):
        record = _record()
        orders = [_order(OrderStatus.FILLED, filled=1.0, oid="a")]
        assert derive_plan_status(record, orders) is ExecutionPlanStatus.COMPLETE

    def test_all_cancelled_with_nothing_filled_reads_cancelled(self):
        record = _record()
        orders = [_order(OrderStatus.CANCELLED, oid="a")]
        assert derive_plan_status(record, orders) is ExecutionPlanStatus.CANCELLED

    def test_all_expired_reads_expired(self):
        record = _record()
        orders = [_order(OrderStatus.EXPIRED, oid="a")]
        assert derive_plan_status(record, orders) is ExecutionPlanStatus.EXPIRED

    def test_all_rejected_reads_failed(self):
        record = _record()
        orders = [_order(OrderStatus.REJECTED, oid="a")]
        assert derive_plan_status(record, orders) is ExecutionPlanStatus.FAILED

    def test_any_unknown_dominates_every_other_reading(self):
        """The rule the whole design rests on, swept across companions."""
        record = _record()
        for companion in (
            OrderStatus.OPEN,
            OrderStatus.FILLED,
            OrderStatus.CANCELLED,
            OrderStatus.EXPIRED,
            OrderStatus.REJECTED,
            OrderStatus.PARTIALLY_FILLED,
        ):
            orders = [
                _order(OrderStatus.UNKNOWN, oid="a"),
                _order(companion, filled=1.0 if companion is OrderStatus.FILLED else 0.0, oid="b"),
            ]
            assert derive_plan_status(record, orders) is ExecutionPlanStatus.UNKNOWN, (
                f"a plan holding an UNKNOWN order alongside a {companion.value} "
                "one did not read UNKNOWN"
            )

    def test_a_plan_never_reads_complete_while_unresolved(self):
        """The invariant restated: no resolution claim over unresolved truth."""
        record = _record()
        orders = [
            _order(OrderStatus.FILLED, filled=1.0, oid="a"),
            _order(OrderStatus.UNKNOWN, oid="b"),
        ]
        status = derive_plan_status(record, orders)
        assert status not in (
            ExecutionPlanStatus.COMPLETE,
            ExecutionPlanStatus.CANCELLED,
            ExecutionPlanStatus.EXPIRED,
            ExecutionPlanStatus.FAILED,
        )

    def test_a_partial_fill_then_cancellation_is_recorded_as_complete(self):
        """Pinned, not judged. The registry itself flags this as an open question.

        "filled 0.02 of 1.0 and cancelled the rest" currently reads COMPLETE.
        Whether that is the right word is for validation to decide; that it is
        deterministic is what this asserts.
        """
        record = _record()
        orders = [
            _order(OrderStatus.CANCELLED, filled=0.02, oid="a"),
        ]
        assert derive_plan_status(record, orders) is ExecutionPlanStatus.COMPLETE

    def test_mixed_terminal_states_fall_back_to_cancelled(self):
        record = _record()
        orders = [
            _order(OrderStatus.CANCELLED, oid="a"),
            _order(OrderStatus.EXPIRED, oid="b"),
        ]
        assert derive_plan_status(record, orders) is ExecutionPlanStatus.CANCELLED

    def test_the_function_is_pure(self):
        """Same record, same orders, same answer — twice, and no mutation."""
        record = _record()
        orders = [_order(OrderStatus.OPEN, oid="a")]
        before = record.model_dump()
        first = derive_plan_status(record, orders)
        second = derive_plan_status(record, orders)
        assert first is second
        assert record.model_dump() == before


class TestRegistryIdentity:
    """H27 — one plan id, one record."""

    def test_re_registering_returns_the_held_record(self):
        registry = ExecutionRegistry()
        plan = execution_plan(planned_order(), created_at=T0, plan_id="p1")
        first = registry.register_plan(plan, T0)
        second = registry.register_plan(plan, T0 + 1_000)
        assert first is second
        assert registry.plans_registered == 1

    def test_re_registering_does_not_erase_attached_order_ids(self):
        registry = ExecutionRegistry()
        plan = execution_plan(planned_order(), created_at=T0, plan_id="p1")
        registry.register_plan(plan, T0)
        registry.attach_orders("p1", ["o1", "o2"], T0)

        registry.register_plan(plan, T0 + 5_000)

        assert registry.get("p1").order_ids == ["o1", "o2"]

    def test_re_registering_does_not_reset_status_or_timestamps(self):
        registry = ExecutionRegistry()
        plan = execution_plan(planned_order(), created_at=T0, plan_id="p1")
        registry.register_plan(plan, T0)
        registry.set_status("p1", ExecutionPlanStatus.WORKING, T0 + 100)

        registry.register_plan(plan, T0 + 5_000)

        record = registry.get("p1")
        assert record.status is ExecutionPlanStatus.WORKING
        assert record.created_at == T0

    def test_a_different_plan_id_never_replaces_an_unrelated_record(self):
        registry = ExecutionRegistry()
        a = execution_plan(planned_order(), created_at=T0, plan_id="pa")
        b = execution_plan(planned_order(), created_at=T0, plan_id="pb")
        registry.register_plan(a, T0)
        registry.attach_orders("pa", ["oa"], T0)
        registry.register_plan(b, T0)

        assert registry.get("pa").order_ids == ["oa"]
        assert registry.get("pb").order_ids == []
        assert registry.resident_plans == 2

    def test_attaching_a_duplicate_order_id_is_ignored(self):
        registry = ExecutionRegistry()
        plan = execution_plan(planned_order(), created_at=T0, plan_id="p1")
        registry.register_plan(plan, T0)
        registry.attach_orders("p1", ["o1"], T0)
        registry.attach_orders("p1", ["o1", "o2"], T0 + 1)
        assert registry.get("p1").order_ids == ["o1", "o2"]

    def test_attaching_to_an_unregistered_plan_does_not_raise(self):
        """Observability must never break the path it observes."""
        registry = ExecutionRegistry()
        registry.attach_orders("nope", ["o1"], T0)
        assert registry.get("nope") is None


class TestRegistryLookups:
    """H27 — the intent and correlation queries return exactly the right set."""

    def _seed(self) -> ExecutionRegistry:
        registry = ExecutionRegistry()
        for plan_id, intent_id, correlation in (
            ("p1", "i1", "c1"),
            ("p2", "i1", "c1"),
            ("p3", "i2", "c2"),
        ):
            registry.register_plan(
                execution_plan(
                    planned_order(),
                    created_at=T0,
                    plan_id=plan_id,
                    intent_id=intent_id,
                    correlation_id=correlation,
                ),
                T0,
            )
        return registry

    def test_plans_for_intent_returns_exactly_the_matching_plans(self):
        registry = self._seed()
        assert {r.plan_id for r in registry.plans_for_intent("i1")} == {"p1", "p2"}
        assert {r.plan_id for r in registry.plans_for_intent("i2")} == {"p3"}
        assert registry.plans_for_intent("nope") == []

    def test_plans_for_correlation_returns_exactly_the_matching_plans(self):
        registry = self._seed()
        assert {r.plan_id for r in registry.plans_for_correlation("c1")} == {
            "p1",
            "p2",
        }
        assert registry.plans_for_correlation("nope") == []

    def test_plan_for_order_finds_the_owning_plan(self):
        registry = self._seed()
        registry.attach_orders("p2", ["o-x"], T0)
        found = registry.plan_for_order("o-x")
        assert found is not None and found.plan_id == "p2"
        assert registry.plan_for_order("o-none") is None

    def test_active_and_unresolved_are_disjoint(self):
        registry = self._seed()
        registry.set_status("p1", ExecutionPlanStatus.WORKING, T0)
        registry.set_status("p2", ExecutionPlanStatus.UNKNOWN, T0)
        registry.set_status("p3", ExecutionPlanStatus.COMPLETE, T0)

        active = {r.plan_id for r in registry.active_plans()}
        unresolved = {r.plan_id for r in registry.unresolved_plans()}
        terminal = {r.plan_id for r in registry.terminal_plans()}

        assert active == {"p1"}
        assert unresolved == {"p2"}
        assert terminal == {"p3"}
        assert active & unresolved == set()


class TestRegistryAgainstRealOrders:
    """The registry's status against the OMS's, end to end."""

    async def test_a_working_plan_reads_working(self):
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
        plan = execution_plan(
            planned_order(
                time_in_force=TimeInForce.GTC, limit_price=95.0, ttl_ms=600_000
            ),
            created_at=T0,
        )
        await harness.veska.execute(plan, T0)
        await harness.veska.poll(T0 + _latency(harness))
        harness.veska.refresh_plan(plan.plan_id, T0 + _latency(harness))

        record = harness.veska.get_plan(plan.plan_id)
        assert record.status is ExecutionPlanStatus.WORKING
        assert record.is_active

    async def test_an_unknown_order_makes_its_plan_unresolved(self):
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
        plan = execution_plan(
            planned_order(
                time_in_force=TimeInForce.GTC, limit_price=95.0, ttl_ms=600_000
            ),
            created_at=T0,
        )
        await harness.veska.execute(plan, T0)
        oid = harness.orders_of(plan.plan_id)[0].client_order_id
        harness.executor.inject_timeout(oid)
        await harness.veska.poll(T0 + _latency(harness))
        harness.veska.refresh_plan(plan.plan_id, T0 + _latency(harness))

        record = harness.veska.get_plan(plan.plan_id)
        assert record.status is ExecutionPlanStatus.UNKNOWN
        assert record.is_unresolved
        assert not record.is_active
        assert not record.is_terminal
        assert plan.plan_id in {r.plan_id for r in harness.veska.unresolved_plans()}

    async def test_a_multi_venue_plan_reflects_both_legs(self):
        harness = build_harness()
        harness.update_market(two_venue_market())
        latency = max(_latency(harness, VENUE_A), _latency(harness, VENUE_B))
        plan = execution_plan(
            planned_order(
                venue=VENUE_A, side=Side.BUY, quantity=0.5, limit_price=101.0
            ),
            planned_order(
                venue=VENUE_B, side=Side.SELL, quantity=0.5, limit_price=99.0
            ),
            created_at=T0,
        )
        await harness.veska.execute(plan, T0)
        await harness.veska.poll(T0 + latency)
        harness.veska.refresh_plan(plan.plan_id, T0 + latency)

        record = harness.veska.get_plan(plan.plan_id)
        orders = harness.orders_of(plan.plan_id)
        assert len(orders) == 2
        assert set(record.order_ids) == {o.client_order_id for o in orders}
        assert record.status is derive_plan_status(record, orders)

    async def test_a_terminal_plan_records_when_it_became_terminal(self):
        registry = ExecutionRegistry()
        plan = execution_plan(planned_order(), created_at=T0, plan_id="pt")
        registry.register_plan(plan, T0)
        registry.set_status("pt", ExecutionPlanStatus.COMPLETE, T0 + 4_242)
        record = registry.get("pt")
        assert record.terminal_at == T0 + 4_242
        assert record.is_terminal


class TestRegistryRetention:
    """Unresolved plan records must survive compaction."""

    def test_compaction_releases_nothing_by_default(self):
        registry = ExecutionRegistry()
        registry.register_plan(
            execution_plan(planned_order(), created_at=T0, plan_id="p1"), T0
        )
        registry.set_status("p1", ExecutionPlanStatus.COMPLETE, T0)
        assert registry.compact() == 0
        assert registry.resident_plans == 1

    def test_compaction_never_releases_an_unresolved_plan(self):
        registry = ExecutionRegistry()
        for plan_id, status in (
            ("done", ExecutionPlanStatus.COMPLETE),
            ("open", ExecutionPlanStatus.WORKING),
            ("lost", ExecutionPlanStatus.UNKNOWN),
        ):
            registry.register_plan(
                execution_plan(planned_order(), created_at=T0, plan_id=plan_id), T0
            )
            registry.set_status(plan_id, status, T0)

        registry.compact(keep_terminal=False)

        assert registry.get("lost") is not None, (
            "compaction released a plan whose venue truth is unresolved"
        )
        assert registry.get("open") is not None
        assert registry.get("done") is None


class TestExecutionReportSemantics:
    """H21 — reports are detached snapshots across the execution lifecycle."""

    def test_veska_has_one_canonical_current_report_builder(self):
        from execution.veska.engine import Veska

        source = inspect.getsource(Veska.current_report)
        assert "orders_for_plan" in source
        assert "for fill in order.fills" in source
        assert "complete=record.is_terminal" in source

    async def test_a_later_report_is_published_when_execution_truth_changes(self):
        from core.events import EventType

        harness = build_harness()
        harness.update_market(two_venue_market())
        plan = execution_plan(
            planned_order(
                quantity=0.5,
                time_in_force=TimeInForce.IOC,
                limit_price=101.0,
            ),
            created_at=T0,
        )
        await harness.veska.execute(plan, T0)
        await harness.veska.poll(T0 + _latency(harness))
        await harness.drain_events()

        reports = harness.events_of(EventType.EXECUTION_REPORT)
        assert len(reports) >= 2
        latest = reports[-1].payload
        assert latest["plan_id"] == plan.plan_id
        assert latest["fills"]
        assert latest["complete"] is True

    async def test_submission_report_stays_a_snapshot_while_current_report_advances(self):
        harness = build_harness()
        harness.update_market(two_venue_market())
        plan = execution_plan(
            planned_order(
                quantity=0.5,
                time_in_force=TimeInForce.IOC,
                limit_price=101.0,
            ),
            created_at=T0,
        )
        submission = await harness.veska.execute(plan, T0)
        submission_status = submission.orders[0].status
        fills = await harness.veska.poll(T0 + _latency(harness))
        latest = harness.veska.current_report(
            plan.plan_id, T0 + _latency(harness)
        )

        assert fills, "no fill; lifecycle report semantics are untested"
        assert submission.fills == []
        assert submission.complete is False
        assert submission.filled_notional == 0.0
        assert submission.orders[0].status is submission_status

        assert latest.fills
        assert latest.complete is True
        assert latest.filled_notional == pytest.approx(
            sum(fill.notional for fill in fills)
        )
