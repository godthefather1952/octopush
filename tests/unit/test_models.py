"""Schema invariants: freshness, order state, and position arithmetic."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from core.models.agent import AgentOpinion
from core.models.common import AgentId, DataQuality, Liquidity, OrderType, Side, TimeInForce
from core.models.execution import (
    FillEvent,
    IllegalTransition,
    OrderStatus,
    PaperOrder,
)
from core.models.market import OrderBookSnapshot, PriceLevel
from core.models.portfolio import PositionState
from tests.conftest import START_MS


def opinion(**overrides) -> AgentOpinion:
    defaults = dict(
        agent_id=AgentId.NORO,
        symbol="BTC-USD",
        created_at=START_MS,
        signal=0.5,
        confidence=0.8,
        expires_at=START_MS + 1_000,
        model_version="test-0",
    )
    defaults.update(overrides)
    return AgentOpinion(**defaults)


class TestOpinionFreshness:
    def test_fresh_before_expiry(self):
        assert opinion().quality_at(START_MS + 999) is DataQuality.FRESH

    def test_degrades_inside_grace_window(self):
        # An expired opinion is DEGRADED, not neutral: it still carries its
        # signal, it is simply worth less.
        assert (
            opinion().quality_at(START_MS + 1_200, degraded_grace_ms=500)
            is DataQuality.DEGRADED
        )

    def test_stale_beyond_grace_window(self):
        assert (
            opinion().quality_at(START_MS + 2_000, degraded_grace_ms=500)
            is DataQuality.STALE
        )

    def test_stale_immediately_without_grace(self):
        assert opinion().quality_at(START_MS + 1_001) is DataQuality.STALE

    def test_stale_never_becomes_a_neutral_signal(self):
        # The signal itself is untouched by expiry; consumers must branch on
        # quality rather than reading a conveniently zeroed value.
        expired = opinion(signal=0.9)
        assert expired.quality_at(START_MS + 5_000) is DataQuality.STALE
        assert expired.signal == 0.9

    def test_subject_prefers_correlation_id(self):
        assert opinion(correlation_id="opp-1").subject == "opp-1"
        assert opinion().subject == "BTC-USD"

    @pytest.mark.parametrize("signal", [-1.5, 1.5])
    def test_signal_is_bounded(self, signal):
        with pytest.raises(ValidationError):
            opinion(signal=signal)

    def test_confidence_is_bounded(self):
        with pytest.raises(ValidationError):
            opinion(confidence=1.2)


class TestBookSnapshotValidation:
    def test_bids_must_descend(self):
        with pytest.raises(ValidationError):
            OrderBookSnapshot(
                venue="V",
                symbol="BTC-USD",
                exchange_ts=START_MS,
                received_ts=START_MS,
                bids=[PriceLevel(price=100, size=1), PriceLevel(price=101, size=1)],
            )

    def test_asks_must_ascend(self):
        with pytest.raises(ValidationError):
            OrderBookSnapshot(
                venue="V",
                symbol="BTC-USD",
                exchange_ts=START_MS,
                received_ts=START_MS,
                asks=[PriceLevel(price=101, size=1), PriceLevel(price=100, size=1)],
            )

    def test_crossed_book_is_detectable(self):
        book = OrderBookSnapshot(
            venue="V",
            symbol="BTC-USD",
            exchange_ts=START_MS,
            received_ts=START_MS,
            bids=[PriceLevel(price=101, size=1)],
            asks=[PriceLevel(price=100, size=1)],
        )
        assert book.crossed


def make_order(**overrides) -> PaperOrder:
    defaults = dict(
        created_at=START_MS,
        venue="VENUE_A",
        symbol="BTC-USD",
        side=Side.BUY,
        order_type=OrderType.LIMIT,
        time_in_force=TimeInForce.IOC,
        quantity=1.0,
        expected_price=100.0,
    )
    defaults.update(overrides)
    return PaperOrder(**defaults)


class TestOrderStateMachine:
    def test_happy_path(self):
        order = make_order()
        for status in (
            OrderStatus.SUBMITTING,
            OrderStatus.ACKNOWLEDGED,
            OrderStatus.OPEN,
            OrderStatus.FILLED,
        ):
            order.transition(status, START_MS)
        assert order.is_terminal
        assert order.terminal_at == START_MS
        assert [s for _, s in order.history] == [
            OrderStatus.SUBMITTING,
            OrderStatus.ACKNOWLEDGED,
            OrderStatus.OPEN,
            OrderStatus.FILLED,
        ]

    def test_illegal_transition_raises(self):
        order = make_order()
        with pytest.raises(IllegalTransition):
            order.transition(OrderStatus.FILLED, START_MS)

    def test_terminal_orders_are_final(self):
        order = make_order()
        order.transition(OrderStatus.REJECTED, START_MS)
        with pytest.raises(IllegalTransition):
            order.transition(OrderStatus.OPEN, START_MS)

    def test_unknown_is_reachable_from_live_states_and_resolvable(self):
        order = make_order()
        order.transition(OrderStatus.SUBMITTING, START_MS)
        order.transition(OrderStatus.UNKNOWN, START_MS)
        # UNKNOWN is a real state: not terminal, and not assumed to be failed.
        assert not order.is_terminal
        assert not order.is_live
        order.transition(OrderStatus.FILLED, START_MS)
        assert order.status is OrderStatus.FILLED

    def test_fill_updates_average_price(self):
        order = make_order(quantity=2.0)
        order.apply_fill(
            FillEvent(
                created_at=START_MS,
                client_order_id=order.client_order_id,
                venue="VENUE_A",
                symbol="BTC-USD",
                side=Side.BUY,
                quantity=1.0,
                price=100.0,
                fee=0.5,
            )
        )
        order.apply_fill(
            FillEvent(
                created_at=START_MS,
                client_order_id=order.client_order_id,
                venue="VENUE_A",
                symbol="BTC-USD",
                side=Side.BUY,
                quantity=1.0,
                price=102.0,
                fee=0.5,
            )
        )
        assert order.filled_quantity == pytest.approx(2.0)
        assert order.average_price == pytest.approx(101.0)
        assert order.fees_paid == pytest.approx(1.0)
        assert order.remaining_quantity == pytest.approx(0.0)

    def test_duplicate_fill_is_ignored(self):
        order = make_order()
        fill = FillEvent(
            created_at=START_MS,
            client_order_id=order.client_order_id,
            venue="VENUE_A",
            symbol="BTC-USD",
            side=Side.BUY,
            quantity=0.5,
            price=100.0,
        )
        order.apply_fill(fill)
        order.apply_fill(fill)
        assert order.filled_quantity == pytest.approx(0.5)
        assert len(order.fills) == 1


class TestFillArithmetic:
    def test_buy_reduces_cash_by_notional_plus_fee(self):
        fill = FillEvent(
            created_at=START_MS,
            client_order_id="x",
            venue="V",
            symbol="BTC-USD",
            side=Side.BUY,
            quantity=2.0,
            price=100.0,
            fee=1.0,
            liquidity=Liquidity.TAKER,
        )
        assert fill.cash_delta == pytest.approx(-201.0)

    def test_sell_increases_cash_by_notional_minus_fee(self):
        fill = FillEvent(
            created_at=START_MS,
            client_order_id="x",
            venue="V",
            symbol="BTC-USD",
            side=Side.SELL,
            quantity=2.0,
            price=100.0,
            fee=1.0,
        )
        assert fill.cash_delta == pytest.approx(199.0)


class TestPositionArithmetic:
    def test_opening_sets_average_entry(self):
        position = PositionState(venue="V", symbol="BTC-USD")
        assert position.apply(Side.BUY, 1.0, 100.0, 0.0) == pytest.approx(0.0)
        assert position.average_entry_price == pytest.approx(100.0)

    def test_adding_rolls_the_average(self):
        position = PositionState(venue="V", symbol="BTC-USD")
        position.apply(Side.BUY, 1.0, 100.0, 0.0)
        position.apply(Side.BUY, 1.0, 110.0, 0.0)
        assert position.average_entry_price == pytest.approx(105.0)
        assert position.quantity == pytest.approx(2.0)

    def test_closing_realises_pnl(self):
        position = PositionState(venue="V", symbol="BTC-USD")
        position.apply(Side.BUY, 2.0, 100.0, 0.0)
        realized = position.apply(Side.SELL, 1.0, 110.0, 0.0)
        assert realized == pytest.approx(10.0)
        assert position.quantity == pytest.approx(1.0)
        assert position.average_entry_price == pytest.approx(100.0)

    def test_full_close_flattens(self):
        position = PositionState(venue="V", symbol="BTC-USD")
        position.apply(Side.BUY, 1.0, 100.0, 0.0)
        position.apply(Side.SELL, 1.0, 90.0, 0.0)
        assert position.is_flat
        assert position.average_entry_price == 0.0
        assert position.realized_pnl == pytest.approx(-10.0)

    def test_flipping_through_zero_reopens_at_fill_price(self):
        position = PositionState(venue="V", symbol="BTC-USD")
        position.apply(Side.BUY, 1.0, 100.0, 0.0)
        realized = position.apply(Side.SELL, 3.0, 110.0, 0.0)
        assert realized == pytest.approx(10.0)
        assert position.quantity == pytest.approx(-2.0)
        assert position.average_entry_price == pytest.approx(110.0)

    def test_short_position_realises_correctly(self):
        position = PositionState(venue="V", symbol="BTC-USD")
        position.apply(Side.SELL, 1.0, 100.0, 0.0)
        realized = position.apply(Side.BUY, 1.0, 90.0, 0.0)
        assert realized == pytest.approx(10.0)

    def test_unrealised_pnl_follows_the_mark(self):
        position = PositionState(venue="V", symbol="BTC-USD")
        position.apply(Side.BUY, 2.0, 100.0, 0.0)
        position.mark_price = 105.0
        assert position.unrealized_pnl == pytest.approx(10.0)
