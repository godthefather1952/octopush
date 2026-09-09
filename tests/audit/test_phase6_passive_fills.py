"""H15, H16, H18 — passive-fill realism, partial caps and provenance.

Batch C changes three execution-fidelity boundaries:

* rolling-window trade volume is treated as a stock and only positive deltas
  advance a resting order's queue;
* ``max_partial_fraction`` constrains both marketable and passive one-step
  fills;
* fill provenance comes from the exact order venue/symbol via
  ``MarketState.source_data_timestamp_for``, never from unrelated freshness.

Batch B's POST_ONLY rule remains in force, so passive-fill tests use genuinely
resting orders plus explicit new trade-flow deltas rather than an already
crossing order.
"""

from __future__ import annotations

import inspect

import pytest

from core.models.common import Liquidity, Side, TimeInForce
from tests.audit.veska_fixtures import (
    T0,
    VENUE_A,
    VENUE_B,
    build_harness,
    deterministic_settings,
    execution_plan,
    market_state,
    planned_order,
    price_levels,
    venue_state,
)


def _latency(harness, venue: str = VENUE_A) -> int:
    return harness.settings.venue(venue).latency_ms


class TestTradeFlowAccrual:
    """H15 — queue progress must come from prints, not from polling."""

    def test_accrual_runs_on_every_market_update(self):
        from execution.paper.executor import PaperExecutor

        source = inspect.getsource(PaperExecutor.update_market)
        assert "self._accrue_trade_flow()" in source

    def test_accrual_uses_positive_deltas_of_the_rolling_stock(self):
        from execution.paper.executor import PaperExecutor

        source = inspect.getsource(PaperExecutor._accrue_trade_flow)
        assert "_last_rolling_volume" in source
        assert "max(0.0, current - previous)" in source
        assert "pending.traded_through += delta * 0.01" in source

    async def test_no_new_prints_produces_no_new_queue_progress(self):
        """The invariant, stated as plainly as it can be.

        One fixed population of trades enters the rolling window. The market
        is then updated repeatedly with no new prints at all. An order's queue
        position must not advance.
        """
        harness = build_harness()
        resting = market_state(
            venue_state(
                venue=VENUE_A,
                bids=price_levels((99.0, 100.0)),
                asks=price_levels((110.0, 100.0)),
                buy_volume=500.0,
                sell_volume=500.0,
            )
        )
        harness.update_market(resting)
        plan = execution_plan(
            planned_order(
                time_in_force=TimeInForce.GTC,
                limit_price=99.0,
                expected_price=99.0,
                ttl_ms=600_000,
            ),
            created_at=T0,
        )
        await harness.veska.execute(plan, T0)
        order_id = harness.orders_of(plan.plan_id)[0].client_order_id
        await harness.veska.poll(T0 + _latency(harness))

        first = harness.executor._pending[order_id].traded_through
        for step in range(1, 11):
            # The SAME rolling volumes. No new trade has printed.
            harness.update_market(
                market_state(
                    venue_state(
                        venue=VENUE_A,
                        bids=price_levels((99.0, 100.0)),
                        asks=price_levels((110.0, 100.0)),
                        as_of=T0 + step * 100,
                        buy_volume=500.0,
                        sell_volume=500.0,
                    ),
                    created_at=T0 + step * 100,
                )
            )
        last = harness.executor._pending[order_id].traded_through

        assert last == first, (
            "with no new prints, queue progress grew from "
            f"{first} to {last} across ten market updates: the same rolling "
            "window volume was credited eleven times"
        )

    async def test_progress_scales_with_update_count_not_volume(self):
        """Twice the updates, twice the credited flow, identical market."""
        def book(ts: int):
            return market_state(
                venue_state(
                    venue=VENUE_A,
                    bids=price_levels((99.0, 100.0)),
                    asks=price_levels((110.0, 100.0)),
                    as_of=ts,
                    buy_volume=250.0,
                    sell_volume=250.0,
                ),
                created_at=ts,
            )

        async def progress_after(updates: int) -> float:
            harness = build_harness()
            harness.update_market(book(T0))
            plan = execution_plan(
                planned_order(
                    time_in_force=TimeInForce.GTC,
                    limit_price=99.0,
                    expected_price=99.0,
                    ttl_ms=600_000,
                ),
                created_at=T0,
                plan_id=f"plan-{updates}",
            )
            await harness.veska.execute(plan, T0)
            oid = harness.orders_of(plan.plan_id)[0].client_order_id
            await harness.veska.poll(T0 + _latency(harness))
            for step in range(1, updates + 1):
                harness.update_market(book(T0 + step * 10))
            return harness.executor._pending[oid].traded_through

        few = await progress_after(2)
        many = await progress_after(20)
        assert few == many, (
            f"two updates credited {few} of queue progress and twenty "
            f"credited {many}, against an identical, unchanging market"
        )


    async def test_a_real_increase_is_credited_once_and_a_decrease_does_not_reverse_it(self):
        harness = build_harness()
        def book(ts: int, volume: float):
            return market_state(
                venue_state(
                    venue=VENUE_A,
                    bids=price_levels((98.5, 100.0)),
                    asks=price_levels((100.0, 100.0)),
                    as_of=ts,
                    buy_volume=volume,
                ),
                created_at=ts,
            )

        harness.update_market(book(T0, 100.0))
        plan = execution_plan(
            planned_order(
                time_in_force=TimeInForce.GTC,
                limit_price=99.0,
                expected_price=99.0,
                ttl_ms=600_000,
            ),
            created_at=T0,
        )
        await harness.veska.execute(plan, T0)
        oid = harness.orders_of(plan.plan_id)[0].client_order_id
        await harness.veska.poll(T0 + _latency(harness))

        harness.update_market(book(T0 + 100, 300.0))
        credited = harness.executor._pending[oid].traded_through
        assert credited == pytest.approx(2.0)

        harness.update_market(book(T0 + 200, 300.0))
        assert harness.executor._pending[oid].traded_through == pytest.approx(credited)

        harness.update_market(book(T0 + 300, 50.0))
        assert harness.executor._pending[oid].traded_through == pytest.approx(credited)


class TestPartialFillCap:
    """H16 — does ``max_partial_fraction`` govern both fill paths?"""

    def test_the_cap_is_applied_on_both_fill_paths(self):
        from execution.paper.simulator import FillSimulator

        marketable = inspect.getsource(FillSimulator.fill_marketable)
        passive = inspect.getsource(FillSimulator.fill_passive)
        assert "max_partial_fraction" in marketable
        assert "max_partial_fraction" in passive

    async def test_a_marketable_fill_respects_the_cap(self):
        """Baseline, so the passive comparison below means something."""
        cap = 0.10
        settings = deterministic_settings(execution={"max_partial_fraction": cap})
        harness = build_harness(settings=settings)
        harness.update_market(
            market_state(
                venue_state(
                    venue=VENUE_A,
                    bids=price_levels((99.0, 1_000.0)),
                    asks=price_levels((100.0, 1_000.0)),
                )
            )
        )
        plan = execution_plan(
            planned_order(
                quantity=10.0,
                time_in_force=TimeInForce.IOC,
                limit_price=105.0,
                expected_price=100.0,
            ),
            created_at=T0,
        )
        await harness.veska.execute(plan, T0)
        await harness.veska.poll(T0 + _latency(harness))

        order = harness.orders_of(plan.plan_id)[0]
        assert order.filled_quantity <= 10.0 * cap + 1e-9

    async def test_a_passive_fill_respects_the_same_cap(self):
        """The invariant: one evaluation should not fill the whole order.

        With ``max_partial_fraction`` at 0.10 an order filling 80-100% in a
        single evaluation is a materially different execution model on the
        passive path than on the marketable one.
        """
        cap = 0.10
        settings = deterministic_settings(
            execution={"max_partial_fraction": cap, "queue_ahead_fraction": 0.0}
        )
        harness = build_harness(settings=settings)
        resting = market_state(
            venue_state(
                venue=VENUE_A,
                bids=price_levels((98.5, 100.0)),
                asks=price_levels((100.0, 100.0)),
                buy_volume=0.0,
            )
        )
        harness.update_market(resting)
        plan = execution_plan(
            planned_order(
                quantity=10.0,
                time_in_force=TimeInForce.POST_ONLY,
                limit_price=99.0,
                expected_price=99.0,
                ttl_ms=600_000,
            ),
            created_at=T0,
        )
        await harness.veska.execute(plan, T0)
        latency = _latency(harness)
        await harness.veska.poll(T0 + latency)
        harness.update_market(
            market_state(
                venue_state(
                    venue=VENUE_A,
                    bids=price_levels((98.5, 100.0)),
                    asks=price_levels((100.0, 100.0)),
                    as_of=T0 + latency + 1,
                    buy_volume=100_000.0,
                ),
                created_at=T0 + latency + 1,
            )
        )
        await harness.veska.poll(T0 + latency + 2)

        order = harness.orders_of(plan.plan_id)[0]
        assert order.filled_quantity <= 10.0 * cap + 1e-9, (
            f"a passive order filled {order.filled_quantity} of 10.0 "
            f"({order.filled_quantity / 10.0:.0%}) in one evaluation, against "
            f"a configured max_partial_fraction of {cap}"
        )

    async def test_zero_queue_ahead_still_cannot_override_the_partial_cap(self):
        settings = deterministic_settings(
            execution={"queue_ahead_fraction": 0.0, "max_partial_fraction": 0.10}
        )
        harness = build_harness(settings=settings)
        harness.update_market(
            market_state(
                venue_state(
                    venue=VENUE_A,
                    bids=price_levels((98.5, 100.0)),
                    asks=price_levels((100.0, 100.0)),
                    buy_volume=0.0,
                )
            )
        )
        plan = execution_plan(
            planned_order(
                quantity=4.0,
                time_in_force=TimeInForce.POST_ONLY,
                limit_price=99.0,
                expected_price=99.0,
                ttl_ms=600_000,
            ),
            created_at=T0,
        )
        await harness.veska.execute(plan, T0)
        latency = _latency(harness)
        await harness.veska.poll(T0 + latency)
        harness.update_market(
            market_state(
                venue_state(
                    venue=VENUE_A,
                    bids=price_levels((98.5, 100.0)),
                    asks=price_levels((100.0, 100.0)),
                    as_of=T0 + latency + 1,
                    buy_volume=100_000.0,
                ),
                created_at=T0 + latency + 1,
            )
        )
        await harness.veska.poll(T0 + latency + 2)
        order = harness.orders_of(plan.plan_id)[0]

        assert order.filled_quantity == pytest.approx(0.4)


class TestFillProvenance:
    """H18 — a fill's source timestamp must belong to its own venue."""

    def test_the_executor_stamps_the_order_leg_timestamp(self):
        from execution.paper.executor import PaperExecutor

        helper = inspect.getsource(PaperExecutor._source_timestamp)
        attempt = inspect.getsource(PaperExecutor._attempt_fill)
        assert "source_data_timestamp_for" in helper
        assert "source_ts = self._source_timestamp(order)" in attempt
        assert "self.market.source_data_timestamp" not in attempt

    def test_the_per_leg_helper_exists_and_is_used_elsewhere(self):
        """The platform already knows how to do this correctly.

        ``MarketState.source_data_timestamp_for`` was added for TIDAL-H4 and
        is used by the orchestrator when building intents. The execution path
        does not use it.
        """
        from core.models.market import MarketState

        assert hasattr(MarketState, "source_data_timestamp_for")
        from apps.orchestrator import orchestrator as orch_module

        source = inspect.getsource(orch_module.Orchestrator)
        assert "source_data_timestamp_for" in source

    async def test_a_fill_does_not_inherit_an_unrelated_venues_freshness(self):
        """VENUE_A is stale; VENUE_B is fresh. A fill on A must say so."""
        harness = build_harness()
        stale_ts = T0 - 120_000
        fresh_ts = T0
        harness.update_market(
            market_state(
                venue_state(
                    venue=VENUE_A,
                    bids=price_levels((99.0, 100.0)),
                    asks=price_levels((100.0, 100.0)),
                    as_of=stale_ts,
                    exchange_ts=stale_ts,
                ),
                venue_state(
                    venue=VENUE_B,
                    bids=price_levels((99.0, 100.0)),
                    asks=price_levels((100.0, 100.0)),
                    as_of=fresh_ts,
                    exchange_ts=fresh_ts,
                ),
                created_at=T0,
                source_data_timestamp=fresh_ts,
            )
        )
        plan = execution_plan(
            planned_order(
                venue=VENUE_A,
                quantity=1.0,
                time_in_force=TimeInForce.IOC,
                limit_price=105.0,
                expected_price=100.0,
            ),
            created_at=T0,
        )
        await harness.veska.execute(plan, T0)
        fills = await harness.veska.poll(T0 + _latency(harness))

        assert fills, "no fill; the provenance comparison below is untested"
        assert fills[0].source_data_timestamp == stale_ts, (
            "a fill on a venue whose data is 120s old carries "
            f"source_data_timestamp {fills[0].source_data_timestamp}, which "
            f"is the market-wide value ({fresh_ts}) taken from a different "
            "venue entirely"
        )

    async def test_a_fill_on_a_different_symbol_does_not_borrow_freshness(self):
        """The same defect, across symbols rather than venues."""
        harness = build_harness()
        stale_ts = T0 - 90_000
        harness.update_market(
            market_state(
                venue_state(
                    venue=VENUE_A,
                    symbol="ETH-USD",
                    bids=price_levels((99.0, 100.0)),
                    asks=price_levels((100.0, 100.0)),
                    as_of=stale_ts,
                    exchange_ts=stale_ts,
                ),
                venue_state(
                    venue=VENUE_A,
                    symbol="BTC-USD",
                    bids=price_levels((99.0, 100.0)),
                    asks=price_levels((100.0, 100.0)),
                    as_of=T0,
                    exchange_ts=T0,
                ),
                created_at=T0,
                source_data_timestamp=T0,
            )
        )
        plan = execution_plan(
            planned_order(
                venue=VENUE_A,
                symbol="ETH-USD",
                quantity=1.0,
                time_in_force=TimeInForce.IOC,
                limit_price=105.0,
                expected_price=100.0,
            ),
            created_at=T0,
            symbol="ETH-USD",
        )
        await harness.veska.execute(plan, T0)
        fills = await harness.veska.poll(T0 + _latency(harness))

        assert fills, "no fill; the provenance comparison below is untested"
        assert fills[0].source_data_timestamp == stale_ts


class TestLiquidityLabelling:
    """Which tier a fill is billed at, and whether it matches what happened."""

    async def test_a_marketable_fill_is_labelled_taker(self):
        harness = build_harness()
        harness.update_market(
            market_state(
                venue_state(
                    venue=VENUE_A,
                    bids=price_levels((99.0, 100.0)),
                    asks=price_levels((100.0, 100.0)),
                )
            )
        )
        plan = execution_plan(
            planned_order(
                quantity=1.0,
                time_in_force=TimeInForce.IOC,
                limit_price=105.0,
                expected_price=100.0,
            ),
            created_at=T0,
        )
        await harness.veska.execute(plan, T0)
        fills = await harness.veska.poll(T0 + _latency(harness))
        assert fills
        assert all(f.liquidity is Liquidity.TAKER for f in fills)

    async def test_passive_fills_are_maker_only_after_crossing_is_excluded(self):
        from execution.paper.simulator import FillSimulator

        source = inspect.getsource(FillSimulator.fill_passive)
        assert "if would_cross(order, view):" in source
        assert "return None" in source
        assert "liquidity=Liquidity.MAKER" in source

    async def test_the_maker_and_taker_fee_tiers_actually_differ(self):
        """So a mislabel has a real cost, not a nominal one."""
        harness = build_harness()
        fees = harness.settings.venue(VENUE_A).fees
        assert fees.fee_bps(True) != fees.fee_bps(False), (
            "maker and taker fees are identical in this configuration, so "
            "the fee-tier findings carry no economic consequence here"
        )
        assert fees.fee_bps(True) < fees.fee_bps(False)


class TestSideCoverage:
    """Both directions, since the sign conventions differ."""

    @pytest.mark.parametrize("side", [Side.BUY, Side.SELL])
    async def test_a_taker_fill_records_positive_slippage_on_both_sides(
        self, side: Side
    ):
        harness = build_harness()
        venue = VENUE_A if side is Side.BUY else VENUE_B
        harness.update_market(
            market_state(
                venue_state(
                    venue=venue,
                    bids=price_levels((99.0, 100.0)),
                    asks=price_levels((100.0, 100.0)),
                )
            )
        )
        limit = 110.0 if side is Side.BUY else 90.0
        plan = execution_plan(
            planned_order(
                venue=venue,
                side=side,
                quantity=1.0,
                time_in_force=TimeInForce.IOC,
                limit_price=limit,
                expected_price=100.0 if side is Side.BUY else 99.0,
            ),
            created_at=T0,
            max_slippage_bps=1_000.0,
        )
        await harness.veska.execute(plan, T0)
        fills = await harness.veska.poll(T0 + _latency(harness, venue))
        assert fills
        assert fills[0].slippage_bps >= 0.0, (
            f"a {side.value} taker fill recorded negative slippage "
            f"({fills[0].slippage_bps}), which would read as a cost saving"
        )
