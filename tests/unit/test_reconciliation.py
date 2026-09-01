"""MARIN: reconciliation catches every way the two views can disagree."""

from __future__ import annotations

import pytest

from agents.marin import Marin
from core.models.common import OrderType, Side, TimeInForce
from core.models.execution import FillEvent, OrderStatus
from core.models.ops import MismatchKind, Severity
from execution.oms import OrderManager
from execution.paper import PaperAccount
from tests.conftest import START_MS


@pytest.fixture
def oms(clock) -> OrderManager:
    return OrderManager(clock=clock)


@pytest.fixture
def account(clock) -> PaperAccount:
    return PaperAccount(clock=clock, initial_balance=100_000.0)


@pytest.fixture
def marin(bus, clock, health, oms, account) -> Marin:
    return Marin(bus=bus, clock=clock, health=health, oms=oms, account=account)


def traded(oms: OrderManager, account: PaperAccount, *, quantity=1.0, price=100.0, fee=0.5):
    """Put one matched order+fill through both the OMS and the account."""
    order = oms.create(
        venue="VENUE_A",
        symbol="BTC-USD",
        side=Side.BUY,
        quantity=quantity,
        order_type=OrderType.LIMIT,
        time_in_force=TimeInForce.IOC,
        expected_price=price,
    )
    for status in (OrderStatus.SUBMITTING, OrderStatus.ACKNOWLEDGED, OrderStatus.OPEN):
        oms.transition(order.client_order_id, status)
    fill = FillEvent(
        created_at=START_MS,
        client_order_id=order.client_order_id,
        venue="VENUE_A",
        symbol="BTC-USD",
        side=Side.BUY,
        quantity=quantity,
        price=price,
        fee=fee,
    )
    oms.apply_fill(fill)
    account.apply_fill(fill)
    return order, fill


def kinds(result):
    return {m.kind for m in result.mismatches}


class TestCleanState:
    def test_empty_account_reconciles(self, marin):
        result = marin.reconcile()
        assert result.ok and not result.mismatches

    def test_matched_trades_reconcile(self, marin, oms, account):
        for i in range(5):
            traded(oms, account, quantity=0.5, price=100.0 + i)
        result = marin.reconcile()
        assert result.ok
        assert result.fills_checked == 5
        assert result.positions_checked >= 1

    def test_many_trades_still_reconcile_exactly(self, marin, oms, account):
        for i in range(50):
            traded(oms, account, quantity=0.1, price=100.0 + (i % 7))
        assert marin.reconcile().ok


class TestMismatchDetection:
    def test_fill_missing_from_the_account_is_critical(self, marin, oms, account):
        order, _ = traded(oms, account, quantity=1.0)
        order.quantity = 2.0  # room for a second fill on the same order
        # The second fill reaches the OMS but never lands in the account —
        # exactly what a dropped callback looks like.
        oms.apply_fill(
            FillEvent(
                created_at=START_MS,
                client_order_id=order.client_order_id,
                venue="VENUE_A",
                symbol="BTC-USD",
                side=Side.BUY,
                quantity=1.0,
                price=100.0,
            )
        )
        result = marin.reconcile()
        assert not result.ok
        assert MismatchKind.MISSING_FILL in kinds(result)
        assert result.has_critical

    def test_fill_the_oms_never_saw_is_critical(self, marin, oms, account):
        account.apply_fill(
            FillEvent(
                created_at=START_MS,
                client_order_id="ghost-order",
                venue="VENUE_A",
                symbol="BTC-USD",
                side=Side.BUY,
                quantity=1.0,
                price=100.0,
            )
        )
        result = marin.reconcile()
        assert MismatchKind.UNKNOWN_FILL in kinds(result)
        assert not result.ok

    def test_corrupted_cash_is_detected(self, marin, oms, account):
        traded(oms, account)
        account.cash += 1_000.0
        result = marin.reconcile()
        assert MismatchKind.CASH_MISMATCH in kinds(result)
        mismatch = next(m for m in result.mismatches if m.kind is MismatchKind.CASH_MISMATCH)
        assert mismatch.difference == pytest.approx(1_000.0)

    def test_corrupted_position_is_detected(self, marin, oms, account):
        traded(oms, account)
        account.positions["VENUE_A:BTC-USD"].quantity += 0.25
        result = marin.reconcile()
        assert MismatchKind.POSITION_MISMATCH in kinds(result)

    def test_corrupted_realised_pnl_is_detected(self, marin, oms, account):
        traded(oms, account)
        account.realized_pnl += 5.0
        assert MismatchKind.PNL_MISMATCH in kinds(marin.reconcile())

    def test_fee_drift_is_a_warning_not_a_stop(self, marin, oms, account):
        traded(oms, account)
        account.fees_paid += 0.25
        result = marin.reconcile()
        assert MismatchKind.FEE_MISMATCH in kinds(result)
        fee_mismatch = next(m for m in result.mismatches if m.kind is MismatchKind.FEE_MISMATCH)
        assert fee_mismatch.severity is Severity.WARNING
        assert result.ok  # warnings do not suspend trading

    def test_order_filled_quantity_drift_is_detected(self, marin, oms, account):
        order, _ = traded(oms, account)
        order.filled_quantity += 0.5
        result = marin.reconcile()
        assert MismatchKind.ORDER_STATE_MISMATCH in kinds(result)
        assert not result.ok

    def test_unknown_orders_are_surfaced_as_warnings(self, marin, oms, account):
        order = oms.create(
            venue="VENUE_A",
            symbol="BTC-USD",
            side=Side.BUY,
            quantity=1.0,
            order_type=OrderType.LIMIT,
            time_in_force=TimeInForce.IOC,
            expected_price=100.0,
        )
        oms.transition(order.client_order_id, OrderStatus.SUBMITTING)
        oms.mark_unknown(order.client_order_id)
        result = marin.reconcile()
        # Unresolved truth is reported every run until it is settled, but it
        # is not itself a corruption.
        assert MismatchKind.ORDER_STATE_MISMATCH in kinds(result)
        assert result.ok

    def test_float_noise_stays_below_tolerance(self, marin, oms, account):
        traded(oms, account)
        account.cash += 1e-12
        assert marin.reconcile().ok


class TestPublication:
    async def test_clean_run_publishes_complete(self, marin, bus):
        from core.events import EventType

        seen: list = []
        bus.subscribe(lambda e: seen.append(e) or _noop(), name="probe")
        await marin.run()
        await bus.drain()
        assert any(e.type is EventType.RECONCILIATION_COMPLETE for e in seen)

    async def test_mismatch_run_publishes_mismatch_and_degrades_health(
        self, marin, bus, oms, account, health
    ):
        from core.events import EventType
        from core.models.ops import HealthStatus

        traded(oms, account)
        account.cash += 500.0
        seen: list = []
        bus.subscribe(lambda e: seen.append(e) or _noop(), name="probe")
        result = await marin.run()
        await bus.drain()
        assert not result.ok
        assert any(e.type is EventType.RECONCILIATION_MISMATCH for e in seen)
        assert health.status_of("MARIN") is HealthStatus.OFFLINE


async def _noop() -> None:
    return None
