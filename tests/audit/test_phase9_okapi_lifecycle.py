"""Phase 9 audit: request lifecycle, derived status and in-flight suppression."""

from __future__ import annotations

import pytest

from agents.okapi.policy import derive_hedge_status, is_active, is_outstanding
from agents.okapi.registry import HedgeRegistry
from core.models.common import DataQuality, OrderType, Side, TimeInForce
from core.models.execution import (
    ExecutionPlanRecord,
    ExecutionPlanStatus,
    OrderStatus,
    OrderSummary,
)
from core.models.hedging import HedgeRequestStatus
from tests.audit.phase9_fixtures import make_intent, make_market, make_portfolio
from tests.conftest import START_MS


def _plan(status: ExecutionPlanStatus, *, plan_id: str = "plan-a") -> ExecutionPlanRecord:
    return ExecutionPlanRecord(
        plan_id=plan_id,
        intent_id="intent-a",
        strategy="CROSS_VENUE",
        symbol="BTC-USD",
        status=status,
        created_at=START_MS,
        updated_at=START_MS + 1,
        requested_notional=1_000.0,
        approved_notional=1_000.0,
    )


def _order(
    status: OrderStatus,
    *,
    filled: float,
    quantity: float = 10.0,
    plan_id: str = "plan-a",
) -> OrderSummary:
    return OrderSummary(
        client_order_id="order-a",
        plan_id=plan_id,
        intent_id="intent-a",
        venue="VENUE_A",
        symbol="BTC-USD",
        side=Side.SELL,
        order_type=OrderType.LIMIT,
        time_in_force=TimeInForce.GTC,
        status=status,
        quantity=quantity,
        filled_quantity=filled,
        average_price=100.0 if filled > 0 else None,
    )


def test_unknown_is_outstanding_but_not_active():
    assert is_outstanding(HedgeRequestStatus.UNKNOWN) is True
    assert is_active(HedgeRequestStatus.UNKNOWN) is False


def test_no_execution_plan_means_proposed():
    assert derive_hedge_status([]) is HedgeRequestStatus.PROPOSED


def test_any_unknown_plan_dominates_terminal_plans():
    status = derive_hedge_status(
        [_plan(ExecutionPlanStatus.COMPLETE), _plan(ExecutionPlanStatus.UNKNOWN, plan_id="p2")]
    )
    assert status is HedgeRequestStatus.UNKNOWN


def test_active_plan_with_known_fill_is_partially_filled():
    status = derive_hedge_status(
        [_plan(ExecutionPlanStatus.WORKING)],
        [_order(OrderStatus.PARTIALLY_FILLED, filled=2.0)],
    )
    assert status is HedgeRequestStatus.PARTIALLY_FILLED


def test_cancelled_partial_fill_is_not_misreported_complete():
    """Something traded is insufficient proof that the requested hedge completed."""
    status = derive_hedge_status(
        [_plan(ExecutionPlanStatus.CANCELLED)],
        [_order(OrderStatus.CANCELLED, filled=2.0, quantity=10.0)],
    )
    assert status is not HedgeRequestStatus.COMPLETE


def test_failed_partial_fill_is_not_misreported_complete():
    status = derive_hedge_status(
        [_plan(ExecutionPlanStatus.FAILED)],
        [_order(OrderStatus.CANCELLED, filled=2.0, quantity=10.0)],
    )
    assert status is not HedgeRequestStatus.COMPLETE


def test_status_reapplication_is_counter_idempotent():
    registry = HedgeRegistry()
    record = registry.register_request(make_intent(), START_MS)
    registry.set_status(record.hedge_id, HedgeRequestStatus.UNKNOWN, START_MS + 1)
    registry.set_status(record.hedge_id, HedgeRequestStatus.UNKNOWN, START_MS + 2)

    assert registry.hedges_unknown == 1
    assert registry.outstanding() == [record]
    assert registry.active() == []


@pytest.mark.asyncio
async def test_inflight_suppression_does_not_create_ghost_proposed_requests(
    platform, monkeypatch
):
    """A symbol already hedging must not gain one never-worked request per tick."""
    market = make_market(
        ("VENUE_A", "BTC-USD", 100.0, START_MS, DataQuality.FRESH),
        ("VENUE_B", "BTC-USD", 101.0, START_MS, DataQuality.FRESH),
    )
    portfolio = make_portfolio(("VENUE_A", "BTC-USD", 500.0, 100.0))
    platform.okapi.set_desired_delta("BTC-USD", 0.0)
    platform.orchestrator._tick_time = START_MS
    monkeypatch.setattr(platform.orchestrator, "_hedge_in_flight", lambda symbol: True)
    before_records = platform.okapi.hedge_registry.resident_hedges
    before_requested = platform.okapi.hedges_requested

    await platform.orchestrator._hedge(market, portfolio)
    await platform.orchestrator._hedge(market, portfolio)

    assert platform.okapi.hedge_registry.resident_hedges == before_records
    assert platform.okapi.hedges_requested == before_requested
