"""H9 — one client order id names one order, and a retry is not a second order.

Batch A closes the two identity-loss paths this module originally exposed:

* an exact retry of a successfully submitted plan returns existing order truth
  rather than recreating the order or resetting venue timing;
* duplicate client-order ids fail closed before mutation, and OrderManager
  independently refuses to overwrite a resident identity.

The tests retain the economic invariants: fills, history and pending timing
survive retries, lifetime creation counters do not increment, and a caller
cannot replace an existing order by bypassing VESKA.
"""

from __future__ import annotations

import inspect

import pytest

from core.models.common import Side, TimeInForce
from core.models.execution import ExecutionPlanStatus, OrderStatus
from tests.audit.veska_fixtures import (
    T0,
    VENUE_A,
    build_harness,
    execution_plan,
    fill_for,
    planned_order,
    two_venue_market,
)


def _fixed_plan(**kwargs):
    """One plan with pinned ids, so a resubmission is genuinely the same plan."""
    return execution_plan(
        planned_order(
            quantity=1.0,
            time_in_force=TimeInForce.GTC,
            limit_price=95.0,
            client_order_id="ord-fixed",
            ttl_ms=60_000,
        ),
        created_at=T0,
        plan_id="plan-fixed",
        **kwargs,
    )


class TestCreateRejectsIdentityOverwrite:
    """The OMS itself is the final identity boundary."""

    def test_create_checks_identity_before_assigning_into_the_order_map(self):
        from execution.oms import OrderManager

        source = inspect.getsource(OrderManager.create)
        guard = source.index("already exists")
        assignment = source.index("self.orders[order.client_order_id] = order")
        assert guard < assignment
        assert "if order.client_order_id in self.orders" in source

    def test_execute_short_circuits_an_exact_retry_before_submission(self):
        from execution.veska.engine import Veska

        source = inspect.getsource(Veska.execute)
        resident_guard = source.index("if resident:")
        submit_call = source.index("await self.executor.submit(plan, now_ms)")
        assert resident_guard < submit_call
        assert "idempotent retry: existing order truth returned" in source
        assert "submission interrupted" in source


class TestPlanResubmission:
    """H9 — submitting the same plan twice."""

    async def test_resubmitting_a_plan_does_not_duplicate_its_orders(self):
        harness = build_harness()
        harness.update_market(two_venue_market())
        plan = _fixed_plan()

        await harness.veska.execute(plan, T0)
        await harness.veska.execute(plan, T0 + 1)

        orders = harness.orders_of(plan.plan_id)
        assert len(orders) == 1, (
            f"the same plan submitted twice produced {len(orders)} resident "
            "orders"
        )
        assert harness.oms.orders_created == 1, (
            "the OMS counted "
            f"{harness.oms.orders_created} creations for one plan submitted "
            "twice, so a retry is indistinguishable from a second trade in "
            "every lifetime counter and metric"
        )

    async def test_resubmission_does_not_discard_an_existing_fill(self):
        """The load-bearing case: a retry after something already traded."""
        harness = build_harness()
        harness.update_market(two_venue_market())
        plan = _fixed_plan()
        await harness.veska.execute(plan, T0)

        order = harness.oms.get("ord-fixed")
        assert order is not None
        harness.oms.transition("ord-fixed", OrderStatus.ACKNOWLEDGED)
        harness.oms.transition("ord-fixed", OrderStatus.OPEN)
        harness.oms.apply_fill(
            fill_for(
                order,
                quantity=0.4,
                price=100.0,
                now_ms=T0 + 100,
                fill_id="fill-audit-1",
            )
        )
        assert harness.oms.get("ord-fixed").filled_quantity == 0.4

        await harness.veska.execute(plan, T0 + 200)

        after = harness.oms.get("ord-fixed")
        assert after is not None
        assert after.filled_quantity == 0.4, (
            "resubmitting the plan reset filled_quantity from 0.4 to "
            f"{after.filled_quantity}: the platform now believes it holds "
            "nothing from a fill the paper account has already applied"
        )
        assert len(after.fills) == 1

    async def test_resubmission_does_not_discard_order_history(self):
        harness = build_harness()
        harness.update_market(two_venue_market())
        plan = _fixed_plan()
        await harness.veska.execute(plan, T0)
        latency = harness.settings.venue(VENUE_A).latency_ms
        await harness.veska.poll(T0 + latency)

        before = list(harness.oms.get("ord-fixed").history)
        assert len(before) >= 3

        await harness.veska.execute(plan, T0 + latency + 1)

        after = list(harness.oms.get("ord-fixed").history)
        assert after[: len(before)] == before, (
            f"history was replaced rather than extended: {before} became "
            f"{after}"
        )

    async def test_resubmission_does_not_reset_the_venue_timing_record(self):
        """``_pending`` is overwritten too, so the arrival schedule restarts."""
        harness = build_harness()
        harness.update_market(two_venue_market())
        plan = _fixed_plan()
        await harness.veska.execute(plan, T0)
        first_ack = harness.executor._pending["ord-fixed"].ack_at

        await harness.veska.execute(plan, T0 + 5_000)
        second_ack = harness.executor._pending["ord-fixed"].ack_at

        assert second_ack == first_ack, (
            f"the order's acknowledgement time moved from {first_ack} to "
            f"{second_ack} because a resubmission replaced its pending record"
        )


class TestDuplicateClientOrderIdWithinOnePlan:
    """The same id twice inside a single plan."""

    async def test_two_orders_sharing_an_id_are_refused_or_kept_distinct(self):
        harness = build_harness()
        harness.update_market(two_venue_market())
        plan = execution_plan(
            planned_order(
                side=Side.BUY, quantity=1.0, client_order_id="dupe",
                time_in_force=TimeInForce.GTC, limit_price=95.0,
            ),
            planned_order(
                side=Side.SELL, quantity=1.0, client_order_id="dupe",
                time_in_force=TimeInForce.GTC, limit_price=105.0,
            ),
            created_at=T0,
            plan_id="plan-dupe",
        )

        report = await harness.veska.execute(plan, T0)

        assert report.orders == []
        assert not report.complete
        assert any(
            "DUPLICATE_CLIENT_ORDER_ID" in note for note in report.notes
        ), report.notes

        record = harness.veska.get_plan(plan.plan_id)
        assert record is not None
        assert record.status is ExecutionPlanStatus.FAILED

        resident = harness.orders_of("plan-dupe")
        assert resident == [], (
            "duplicate order identity was detected only after mutating the OMS"
        )
        assert harness.oms.orders_created == 0

    def test_the_oms_refuses_a_direct_identity_overwrite(self):
        """Defense in depth: bypassing VESKA still cannot replace an order."""
        harness = build_harness()
        planned = planned_order(client_order_id="resident-id")
        first = harness.oms.from_plan(
            planned,
            plan_id="plan-a",
            intent_id="intent-a",
            strategy="cross_venue",
        )
        assert first.client_order_id == "resident-id"

        with pytest.raises(ValueError, match="already exists"):
            harness.oms.from_plan(
                planned,
                plan_id="plan-b",
                intent_id="intent-b",
                strategy="cross_venue",
            )

        assert harness.oms.orders_created == 1
        assert harness.oms.get("resident-id") is first

    def test_preflight_detects_the_duplicate(self):
        """The canonical preflight gate catches duplicate identity before mutation."""
        from execution.paper.executor import PAPER_CAPABILITIES
        from execution.veska.preflight import preflight_plan

        plan = execution_plan(
            planned_order(client_order_id="dupe"),
            planned_order(client_order_id="dupe"),
            created_at=T0,
        )
        result = preflight_plan(plan, capabilities=PAPER_CAPABILITIES)
        assert result.blocked
        assert "DUPLICATE_CLIENT_ORDER_ID" in result.reason_codes


class TestFillIdempotency:
    """The one idempotency the layer does implement, asserted so it stays."""

    async def test_a_duplicate_fill_is_rejected_by_the_oms(self):
        harness = build_harness()
        harness.update_market(two_venue_market())
        plan = _fixed_plan()
        await harness.veska.execute(plan, T0)
        harness.oms.transition("ord-fixed", OrderStatus.ACKNOWLEDGED)
        harness.oms.transition("ord-fixed", OrderStatus.OPEN)
        order = harness.oms.get("ord-fixed")

        fill = fill_for(
            order, quantity=0.3, price=100.0, now_ms=T0 + 10, fill_id="dupe-fill"
        )
        assert harness.oms.apply_fill(fill) is True
        assert harness.oms.apply_fill(fill) is False
        assert harness.oms.duplicate_fills == 1
        assert harness.oms.get("ord-fixed").filled_quantity == 0.3

    async def test_a_duplicate_fill_is_rejected_by_the_account(self):
        harness = build_harness()
        harness.update_market(two_venue_market())
        plan = _fixed_plan()
        await harness.veska.execute(plan, T0)
        harness.oms.transition("ord-fixed", OrderStatus.ACKNOWLEDGED)
        harness.oms.transition("ord-fixed", OrderStatus.OPEN)
        order = harness.oms.get("ord-fixed")

        fill = fill_for(
            order, quantity=0.3, price=100.0, now_ms=T0 + 10, fill_id="dupe-fill-2"
        )
        assert harness.account.apply_fill(fill) is True
        cash_after_first = harness.account.cash
        assert harness.account.apply_fill(fill) is False
        assert harness.account.cash == cash_after_first

    async def test_an_overfill_is_refused_rather_than_absorbed(self):
        harness = build_harness()
        harness.update_market(two_venue_market())
        plan = _fixed_plan()
        await harness.veska.execute(plan, T0)
        harness.oms.transition("ord-fixed", OrderStatus.ACKNOWLEDGED)
        harness.oms.transition("ord-fixed", OrderStatus.OPEN)
        order = harness.oms.get("ord-fixed")

        import pytest

        with pytest.raises(ValueError, match="overfill"):
            harness.oms.apply_fill(
                fill_for(
                    order,
                    quantity=order.quantity + 1.0,
                    price=100.0,
                    now_ms=T0 + 10,
                    fill_id="overfill",
                )
            )
