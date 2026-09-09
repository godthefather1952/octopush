"""Paper execution: OMS bookkeeping, fill realism, account arithmetic."""

from __future__ import annotations

import pytest

from core.config import ExecutionConfig, FeeSchedule, Settings
from core.models.common import Liquidity, OrderType, Side, TimeInForce
from core.models.execution import FillEvent, OrderStatus
from core.models.market import MarketState, PriceLevel
from execution.oms import OrderManager
from execution.paper import BookView, FillSimulator, PaperAccount, PaperExecutor
from execution.paper.simulator import is_marketable
from tests.conftest import START_MS, make_book, venue_state_from_book


@pytest.fixture
def oms(clock) -> OrderManager:
    return OrderManager(clock=clock)


@pytest.fixture
def account(clock) -> PaperAccount:
    return PaperAccount(clock=clock, initial_balance=100_000.0)


@pytest.fixture
def simulator() -> FillSimulator:
    return FillSimulator(ExecutionConfig(), seed=7)


def new_order(oms: OrderManager, **overrides):
    defaults = dict(
        venue="VENUE_A",
        symbol="BTC-USD",
        side=Side.BUY,
        quantity=1.0,
        order_type=OrderType.LIMIT,
        time_in_force=TimeInForce.IOC,
        expected_price=100.0,
        limit_price=101.0,
    )
    defaults.update(overrides)
    return oms.create(**defaults)


def fill_for(order, quantity=1.0, price=100.0, fee=0.0, fill_id=None):
    event = FillEvent(
        created_at=START_MS,
        client_order_id=order.client_order_id,
        venue=order.venue,
        symbol=order.symbol,
        side=order.side,
        quantity=quantity,
        price=price,
        fee=fee,
    )
    if fill_id:
        event.fill_id = fill_id
    return event


class TestOrderManager:
    def test_created_orders_start_in_created(self, oms):
        order = new_order(oms)
        assert order.status is OrderStatus.CREATED
        assert oms.get(order.client_order_id) is order

    def test_fill_moves_to_partially_then_filled(self, oms):
        order = new_order(oms, quantity=2.0)
        for status in (OrderStatus.SUBMITTING, OrderStatus.ACKNOWLEDGED, OrderStatus.OPEN):
            oms.transition(order.client_order_id, status)
        oms.apply_fill(fill_for(order, quantity=1.0))
        assert order.status is OrderStatus.PARTIALLY_FILLED
        oms.apply_fill(fill_for(order, quantity=1.0))
        assert order.status is OrderStatus.FILLED
        assert order.remaining_quantity == pytest.approx(0.0)

    def test_duplicate_fill_is_idempotent(self, oms):
        order = new_order(oms, quantity=2.0)
        for status in (OrderStatus.SUBMITTING, OrderStatus.ACKNOWLEDGED, OrderStatus.OPEN):
            oms.transition(order.client_order_id, status)
        fill = fill_for(order, quantity=1.0)
        assert oms.apply_fill(fill) is True
        assert oms.apply_fill(fill) is False
        assert order.filled_quantity == pytest.approx(1.0)
        assert oms.duplicate_fills == 1

    def test_overfill_is_refused(self, oms):
        order = new_order(oms, quantity=1.0)
        for status in (OrderStatus.SUBMITTING, OrderStatus.ACKNOWLEDGED, OrderStatus.OPEN):
            oms.transition(order.client_order_id, status)
        with pytest.raises(ValueError, match="overfill"):
            oms.apply_fill(fill_for(order, quantity=2.0))

    def test_fill_for_unknown_order_raises(self, oms):
        order = new_order(oms)
        stray = fill_for(order)
        stray.client_order_id = "does-not-exist"
        with pytest.raises(KeyError):
            oms.apply_fill(stray)

    def test_timeout_marks_unknown_not_failed(self, oms):
        order = new_order(oms)
        oms.transition(order.client_order_id, OrderStatus.SUBMITTING)
        oms.mark_unknown(order.client_order_id, "no response")
        assert order.status is OrderStatus.UNKNOWN
        assert not order.is_terminal
        assert oms.unknown_orders() == [order]

    def test_unknown_resolves_to_the_truth(self, oms):
        order = new_order(oms)
        oms.transition(order.client_order_id, OrderStatus.SUBMITTING)
        oms.mark_unknown(order.client_order_id)
        oms.resolve_unknown(order.client_order_id, OrderStatus.CANCELLED)
        assert order.status is OrderStatus.CANCELLED

    def test_expired_orders_are_found_by_ttl(self, oms, clock):
        order = new_order(oms, ttl_ms=1_000)
        oms.transition(order.client_order_id, OrderStatus.SUBMITTING)
        assert list(oms.expired(clock.now_ms())) == []
        assert list(oms.expired(clock.now_ms() + 2_000)) == [order]


class TestFillSimulator:
    def _view(self, prices_sizes):
        return BookView(opposing=[PriceLevel(price=p, size=s) for p, s in prices_sizes])

    def test_marketable_order_walks_the_book(self, oms, simulator):
        order = new_order(oms, quantity=2.0, limit_price=200.0)
        view = self._view([(100.0, 1.0), (101.0, 1.0), (102.0, 5.0)])
        fill = simulator.fill_marketable(order, view, FeeSchedule(taker_bps=5.0), 0.0)
        assert fill is not None
        assert fill.price > 100.0
        assert fill.liquidity is Liquidity.TAKER
        assert fill.fee == pytest.approx(fill.quantity * fill.price * 5.0 / 10_000)

    def test_limit_price_caps_the_walk(self, oms, simulator):
        order = new_order(oms, quantity=5.0, limit_price=100.5)
        view = self._view([(100.0, 1.0), (101.0, 100.0)])
        fill = simulator.fill_marketable(order, view, FeeSchedule(), 0.0)
        # Only the level inside the limit is reachable.
        assert fill is not None
        assert fill.price <= 100.5
        assert fill.quantity < 5.0

    def test_unreachable_limit_does_not_fill(self, oms, simulator):
        order = new_order(oms, quantity=1.0, limit_price=99.0)
        fill = simulator.fill_marketable(order, self._view([(100.0, 10.0)]), FeeSchedule(), 0.0)
        assert fill is None

    def test_latency_moves_the_book_against_the_buyer(self, simulator):
        levels = [PriceLevel(price=100.0, size=1.0)]
        drifted = simulator.latency_adjusted_levels(levels, Side.BUY, 500.0)
        assert drifted[0].price > 100.0

    def test_latency_moves_the_book_against_the_seller(self, simulator):
        levels = [PriceLevel(price=100.0, size=1.0)]
        drifted = simulator.latency_adjusted_levels(levels, Side.SELL, 500.0)
        assert drifted[0].price < 100.0

    def test_empty_book_does_not_fill(self, oms, simulator):
        order = new_order(oms)
        assert simulator.fill_marketable(order, BookView(opposing=[]), FeeSchedule(), 0.0) is None

    def test_passive_fills_are_partial_and_earn_the_maker_fee(self, oms):
        # Force the fill probability so the outcome is deterministic.
        simulator = FillSimulator(
            ExecutionConfig(maker_fill_probability=1.0, queue_ahead_fraction=0.5), seed=3
        )
        order = new_order(
            oms, quantity=4.0, time_in_force=TimeInForce.POST_ONLY, limit_price=99.0
        )
        view = BookView(
            opposing=[PriceLevel(price=99.5, size=10.0)],
            traded_through=1_000.0,
        )
        fill = simulator.fill_passive(order, view, FeeSchedule(maker_bps=1.0))
        assert fill is not None
        assert fill.liquidity is Liquidity.MAKER
        assert fill.price == pytest.approx(99.0)
        assert fill.fee == pytest.approx(fill.quantity * fill.price * 0.0001)
        # A queue and the global partial cap both prevent a whole-size fill.
        assert 0.0 < fill.quantity < 4.0

    def test_passive_order_away_from_the_market_needs_flow(self, oms):
        simulator = FillSimulator(ExecutionConfig(maker_fill_probability=1.0), seed=3)
        order = new_order(
            oms, quantity=1.0, time_in_force=TimeInForce.POST_ONLY, limit_price=90.0
        )
        # Market is at 99.5 and nothing has traded through our price.
        view = BookView(opposing=[PriceLevel(price=99.5, size=10.0)], traded_through=0.0)
        assert simulator.fill_passive(order, view, FeeSchedule()) is None

    def test_never_filling_maker_never_fills(self, oms):
        simulator = FillSimulator(ExecutionConfig(maker_fill_probability=0.0), seed=1)
        order = new_order(
            oms, quantity=1.0, time_in_force=TimeInForce.POST_ONLY, limit_price=100.0
        )
        view = BookView(opposing=[PriceLevel(price=99.0, size=10.0)])
        assert simulator.fill_passive(order, view, FeeSchedule()) is None

    def test_vanishing_liquidity_removes_levels(self):
        simulator = FillSimulator(ExecutionConfig(liquidity_vanish_probability=1.0), seed=1)
        levels = [PriceLevel(price=100.0, size=1.0), PriceLevel(price=101.0, size=1.0)]
        assert simulator.apply_vanishing_liquidity(levels) == []

    def test_cancel_can_lose_the_race_to_a_fill(self, oms):
        simulator = FillSimulator(ExecutionConfig(maker_fill_probability=1.0), seed=1)
        order = new_order(oms, quantity=1.0, limit_price=100.0, order_type=OrderType.LIMIT)
        # The market has reached our price, so the cancel is not guaranteed.
        view = BookView(opposing=[PriceLevel(price=99.0, size=1.0)])
        assert simulator.cancel_wins_race(order, view) is False

    def test_cancel_wins_when_the_market_is_away(self, oms, simulator):
        order = new_order(oms, quantity=1.0, limit_price=90.0, order_type=OrderType.LIMIT)
        view = BookView(opposing=[PriceLevel(price=99.0, size=1.0)])
        assert simulator.cancel_wins_race(order, view) is True

    def test_determinism_under_the_same_seed(self, oms):
        def run():
            simulator = FillSimulator(ExecutionConfig(), seed=99)
            local = OrderManager(clock=oms.clock)
            order = new_order(local, quantity=3.0, limit_price=105.0)
            view = BookView(opposing=[PriceLevel(price=100.0 + i * 0.1, size=0.4) for i in range(20)])
            fill = simulator.fill_marketable(order, view, FeeSchedule(), 40.0)
            return (fill.quantity, fill.price, fill.fee)

        assert run() == run()

    def test_marketability_classification(self, oms):
        assert is_marketable(new_order(oms, order_type=OrderType.MARKET))
        assert is_marketable(new_order(oms, time_in_force=TimeInForce.IOC))
        assert not is_marketable(new_order(oms, time_in_force=TimeInForce.POST_ONLY))
        assert not is_marketable(new_order(oms, time_in_force=TimeInForce.GTC))


class TestPaperAccount:
    def test_buy_then_sell_realises_pnl_and_cash(self, account, oms):
        order = new_order(oms)
        account.apply_fill(fill_for(order, quantity=1.0, price=100.0, fee=0.5))
        assert account.cash == pytest.approx(100_000.0 - 100.0 - 0.5)
        sell = fill_for(order, quantity=1.0, price=110.0, fee=0.5)
        sell.side = Side.SELL
        account.apply_fill(sell)
        assert account.realized_pnl == pytest.approx(10.0)
        assert account.cash == pytest.approx(100_000.0 - 100.5 + 110.0 - 0.5)
        assert account.fees_paid == pytest.approx(1.0)

    def test_duplicate_fill_does_not_double_count(self, account, oms):
        order = new_order(oms)
        fill = fill_for(order, quantity=1.0, price=100.0, fee=0.5)
        assert account.apply_fill(fill) is True
        assert account.apply_fill(fill) is False
        assert account.cash == pytest.approx(100_000.0 - 100.5)

    def test_marks_drive_unrealised_and_equity(self, account, oms):
        order = new_order(oms)
        account.apply_fill(fill_for(order, quantity=2.0, price=100.0))
        account.mark({"VENUE_A:BTC-USD": 105.0})
        assert account.unrealized_pnl == pytest.approx(10.0)
        assert account.equity == pytest.approx(100_000.0 + 10.0)

    def test_peak_equity_and_drawdown(self, account, oms):
        order = new_order(oms)
        account.apply_fill(fill_for(order, quantity=2.0, price=100.0))
        account.mark({"VENUE_A:BTC-USD": 110.0})
        peak = account.equity
        account.mark({"VENUE_A:BTC-USD": 95.0})
        snapshot = account.snapshot()
        assert snapshot.peak_equity == pytest.approx(peak)
        assert snapshot.drawdown > 0

    def test_recompute_from_fills_matches_running_state(self, account, oms):
        order = new_order(oms)
        for i in range(6):
            fill = fill_for(order, quantity=0.5, price=100.0 + i, fee=0.1)
            fill.side = Side.BUY if i % 2 == 0 else Side.SELL
            account.apply_fill(fill)
        cash, positions, realized = account.recompute_from_fills()
        assert cash == pytest.approx(account.cash)
        assert realized == pytest.approx(account.realized_pnl)
        for key, position in positions.items():
            assert position.quantity == pytest.approx(account.positions[key].quantity)


class TestPaperExecutorLifecycle:
    def _market(self) -> MarketState:
        states = {}
        for venue, mid in (("VENUE_A", 100.0), ("VENUE_B", 100.2)):
            state = venue_state_from_book(
                make_book(venue, "BTC-USD", mid, size=20.0, levels=15, tick=0.01)
            )
            states[f"{venue}:BTC-USD"] = state
        return MarketState(created_at=START_MS, source_data_timestamp=START_MS, venues=states)

    def _executor(self, bus, clock, settings: Settings, oms, account) -> PaperExecutor:
        executor = PaperExecutor(
            bus=bus,
            clock=clock,
            settings=settings,
            oms=oms,
            account=account,
            simulator=FillSimulator(settings.execution, seed=5),
        )
        executor.update_market(self._market())
        return executor

    async def test_order_progresses_through_the_state_machine(
        self, bus, clock, settings, oms, account
    ):
        from core.models.opportunity import ExecutionPlan, PlannedOrder

        executor = self._executor(bus, clock, settings, oms, account)
        plan = ExecutionPlan(
            created_at=clock.now_ms(),
            intent_id="int-1",
            strategy="cross_venue",
            symbol="BTC-USD",
            orders=[
                PlannedOrder(
                    venue="VENUE_A",
                    symbol="BTC-USD",
                    side=Side.BUY,
                    quantity=1.0,
                    order_type=OrderType.LIMIT,
                    time_in_force=TimeInForce.IOC,
                    limit_price=200.0,
                    expected_price=100.0,
                    expected_fee_bps=5.0,
                    ttl_ms=5_000,
                )
            ],
            deadline_ms=clock.now_ms() + 5_000,
            max_slippage_bps=10.0,
            notional=100.0,
        )
        report = await executor.submit(plan, clock.now_ms())
        order = report.orders[0]
        assert order.status is OrderStatus.SUBMITTING

        # Nothing happens until the modelled venue latency has elapsed.
        assert await executor.poll(clock.now_ms()) == []
        assert order.status is OrderStatus.SUBMITTING

        clock.advance(settings.venue("VENUE_A").latency_ms + 1)
        fills = await executor.poll(clock.now_ms())
        assert fills, "order should fill once it reaches the venue"
        assert order.status is OrderStatus.FILLED
        assert account.positions["VENUE_A:BTC-USD"].quantity == pytest.approx(1.0)

    async def test_execution_disabled_rejects_new_orders(
        self, bus, clock, settings, oms, account
    ):
        from core.models.opportunity import ExecutionPlan, PlannedOrder

        executor = self._executor(bus, clock, settings, oms, account)
        executor.execution_disabled = True
        plan = ExecutionPlan(
            created_at=clock.now_ms(),
            intent_id="int-1",
            strategy="cross_venue",
            symbol="BTC-USD",
            orders=[
                PlannedOrder(
                    venue="VENUE_A",
                    symbol="BTC-USD",
                    side=Side.BUY,
                    quantity=1.0,
                    order_type=OrderType.LIMIT,
                    time_in_force=TimeInForce.IOC,
                    expected_price=100.0,
                    expected_fee_bps=5.0,
                )
            ],
            deadline_ms=clock.now_ms() + 1_000,
            max_slippage_bps=10.0,
            notional=100.0,
        )
        report = await executor.submit(plan, clock.now_ms())
        assert report.orders[0].status is OrderStatus.REJECTED
        assert executor.rejected_submissions == 1
        assert account.fill_log == []

    async def test_injected_timeout_moves_the_order_to_unknown(
        self, bus, clock, settings, oms, account
    ):
        from core.models.opportunity import ExecutionPlan, PlannedOrder

        executor = self._executor(bus, clock, settings, oms, account)
        plan = ExecutionPlan(
            created_at=clock.now_ms(),
            intent_id="int-1",
            strategy="cross_venue",
            symbol="BTC-USD",
            orders=[
                PlannedOrder(
                    venue="VENUE_A",
                    symbol="BTC-USD",
                    side=Side.BUY,
                    quantity=1.0,
                    order_type=OrderType.LIMIT,
                    time_in_force=TimeInForce.IOC,
                    limit_price=200.0,
                    expected_price=100.0,
                    expected_fee_bps=5.0,
                )
            ],
            deadline_ms=clock.now_ms() + 1_000,
            max_slippage_bps=10.0,
            notional=100.0,
        )
        report = await executor.submit(plan, clock.now_ms())
        order = report.orders[0]
        executor.inject_timeout(order.client_order_id)
        clock.advance(1_000)
        await executor.poll(clock.now_ms())
        assert order.status is OrderStatus.UNKNOWN
        # Crucially, the platform has not decided the order failed.
        assert not order.is_terminal
        assert oms.unknown_orders() == [order]
