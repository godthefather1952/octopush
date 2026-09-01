"""NORO fair value, the transaction-cost model, and ZEPHR's sizing curve."""

from __future__ import annotations

import pytest

from agents.noro.fair_value import compute_fair_value, usable_liquidity, venue_price
from agents.zephr.liquidity import build_sizing_curve, quote_leg
from core.config import FeeSchedule, NoroConfig, ZephrConfig
from core.models.common import DataQuality, Side
from core.models.market import PriceLevel
from execution.costs import latency_cost_bps, market_impact_bps, walk_book
from tests.conftest import make_book, venue_state_from_book


@pytest.fixture
def noro_config() -> NoroConfig:
    return NoroConfig()


@pytest.fixture
def zephr_config() -> ZephrConfig:
    return ZephrConfig(size_ladder=[1_000, 5_000, 10_000, 25_000, 50_000])


def state(
    venue: str,
    mid: float,
    *,
    size: float = 1.0,
    quality=DataQuality.FRESH,
    levels: int = 8,
    tick: float = 0.01,
):
    return venue_state_from_book(
        make_book(venue, "BTC-USD", mid, size=size, levels=levels, tick=tick), quality=quality
    )


class TestFairValue:
    def test_equal_liquidity_gives_the_average(self, noro_config):
        fair = compute_fair_value(
            "BTC-USD", [state("A", 100.0), state("B", 102.0)], noro_config
        )
        assert fair is not None
        assert fair.fair_value == pytest.approx(101.0, rel=1e-3)

    def test_deep_venue_pulls_fair_value_towards_it(self, noro_config):
        fair = compute_fair_value(
            "BTC-USD",
            [state("A", 100.0, size=10.0), state("B", 102.0, size=0.5)],
            noro_config,
        )
        assert fair is not None
        # The deep venue dominates: fair value sits much nearer 100 than 101.
        assert fair.fair_value < 100.5

    def test_deviations_are_signed_and_sum_towards_zero(self, noro_config):
        fair = compute_fair_value(
            "BTC-USD", [state("A", 100.0), state("B", 102.0)], noro_config
        )
        cheap, rich = fair.cheapest, fair.richest
        assert cheap.venue == "A" and cheap.deviation_bps < 0
        assert rich.venue == "B" and rich.deviation_bps > 0

    def test_unusable_venues_are_excluded(self, noro_config):
        fair = compute_fair_value(
            "BTC-USD",
            [state("A", 100.0), state("B", 200.0, quality=DataQuality.STALE)],
            noro_config,
        )
        assert [v.venue for v in fair.venues] == ["A"]
        assert fair.fair_value == pytest.approx(100.0, rel=1e-3)

    def test_no_usable_venue_returns_none(self, noro_config):
        assert (
            compute_fair_value(
                "BTC-USD", [state("A", 100.0, quality=DataQuality.UNAVAILABLE)], noro_config
            )
            is None
        )

    def test_venue_price_blends_mid_and_microprice(self):
        s = state("A", 100.0)
        blended = venue_price(s, 1.0)
        assert blended == pytest.approx(s.metrics.microprice)
        assert venue_price(s, 0.0) == pytest.approx(s.metrics.mid)

    def test_usable_liquidity_takes_the_thinner_side(self):
        s = state("A", 100.0)
        s.metrics.bid_depth_by_bps["10"] = 1_000.0
        s.metrics.ask_depth_by_bps["10"] = 400.0
        assert usable_liquidity(s, 10.0) == pytest.approx(400.0)


class TestWalkBook:
    def test_small_order_fills_at_the_touch(self):
        levels = [PriceLevel(price=100.0, size=10.0)]
        result = walk_book(levels, 100.0, Side.BUY)
        assert result.average_price == pytest.approx(100.0)
        assert result.slippage_bps == pytest.approx(0.0)
        assert not result.exhausted

    def test_large_order_walks_and_pays_slippage(self):
        levels = [
            PriceLevel(price=100.0, size=1.0),
            PriceLevel(price=101.0, size=1.0),
            PriceLevel(price=102.0, size=1.0),
        ]
        result = walk_book(levels, 250.0, Side.BUY)
        assert result.average_price > 100.0
        assert result.slippage_bps > 0
        assert result.levels_consumed >= 2

    def test_selling_slippage_is_also_a_positive_cost(self):
        levels = [
            PriceLevel(price=100.0, size=1.0),
            PriceLevel(price=99.0, size=1.0),
        ]
        result = walk_book(levels, 190.0, Side.SELL)
        assert result.average_price < 100.0
        assert result.slippage_bps > 0

    def test_exhausted_when_the_book_is_too_thin(self):
        result = walk_book([PriceLevel(price=100.0, size=0.1)], 10_000.0, Side.BUY)
        assert result.exhausted

    def test_empty_book_fills_nothing(self):
        result = walk_book([], 100.0, Side.BUY)
        assert result.filled_quantity == 0 and result.exhausted


class TestCostComponents:
    def test_impact_is_superlinear_in_size(self, zephr_config):
        small = market_impact_bps(1_000, 100_000, zephr_config)
        double = market_impact_bps(2_000, 100_000, zephr_config)
        assert double > 2 * small

    def test_impact_is_infinite_without_depth(self, zephr_config):
        assert market_impact_bps(1_000, 0.0, zephr_config) == float("inf")

    def test_latency_cost_grows_with_latency_and_volatility(self, zephr_config):
        quiet = latency_cost_bps(100, 1.0, zephr_config)
        slow = latency_cost_bps(400, 1.0, zephr_config)
        volatile = latency_cost_bps(100, 10.0, zephr_config)
        assert slow > quiet
        assert volatile > quiet

    def test_latency_cost_has_a_floor(self, zephr_config):
        assert latency_cost_bps(0, 0.0, zephr_config) == zephr_config.latency_penalty_bps


class TestSizingCurve:
    def _legs(self, buy_size: float = 30.0, sell_size: float = 30.0):
        buy_state = state("A", 100.0, size=buy_size, levels=20)
        sell_state = state("B", 100.5, size=sell_size, levels=20)
        return [
            (buy_state, Side.BUY, buy_state.book.asks, FeeSchedule(taker_bps=1.0)),
            (sell_state, Side.SELL, sell_state.book.bids, FeeSchedule(taker_bps=1.0)),
        ]

    def test_net_edge_decays_with_size(self, zephr_config):
        curve = build_sizing_curve("BTC-USD", 40.0, self._legs(), zephr_config)
        edges = [p.net_edge_bps for p in curve.points]
        assert edges == sorted(edges, reverse=True)

    def test_max_economical_size_is_the_largest_feasible_point(self, zephr_config):
        curve = build_sizing_curve("BTC-USD", 40.0, self._legs(), zephr_config)
        feasible = [p.notional for p in curve.points if p.feasible]
        assert curve.max_economical_notional == max(feasible)

    def test_thin_edge_yields_no_economical_size(self, zephr_config):
        # A 1 bps gross edge cannot survive two crossings plus fees.
        curve = build_sizing_curve("BTC-USD", 1.0, self._legs(), zephr_config)
        assert curve.best is None
        assert curve.max_economical_notional == 0.0
        assert not curve.feasible

    def test_thin_book_makes_large_sizes_infeasible(self, zephr_config):
        curve = build_sizing_curve("BTC-USD", 60.0, self._legs(buy_size=0.01), zephr_config)
        largest = curve.points[-1]
        assert not largest.feasible

    def test_costs_are_charged_on_every_leg(self, zephr_config):
        one_leg = build_sizing_curve("BTC-USD", 40.0, self._legs()[:1], zephr_config)
        two_legs = build_sizing_curve("BTC-USD", 40.0, self._legs(), zephr_config)
        assert two_legs.points[0].total_cost_bps > one_leg.points[0].total_cost_bps

    def test_quote_leg_reports_a_maker_fee_when_passive(self, zephr_config):
        s = state("A", 100.0)
        taker = quote_leg(s, Side.BUY, 1_000, s.book.asks, FeeSchedule(maker_bps=1, taker_bps=7), zephr_config)
        maker = quote_leg(
            s, Side.BUY, 1_000, s.book.asks, FeeSchedule(maker_bps=1, taker_bps=7), zephr_config, is_maker=True
        )
        assert taker.fee_bps == 7 and maker.fee_bps == 1
        # A passive leg does not pay the spread.
        assert maker.spread_bps == 0.0 and taker.spread_bps > 0
