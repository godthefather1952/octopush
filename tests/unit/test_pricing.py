"""NORO fair value, the transaction-cost model, and ZEPHR's sizing curve."""

from __future__ import annotations

import pytest

from agents.noro.fair_value import compute_fair_value, near_touch_notional, venue_price
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


#: Reliability saturates far below any of these books, so every contributor
#: weighs exactly 1.0 and the weighted median reduces to the plain median.
#: That is the shape most of these tests want: the estimator's *ordering*
#: behaviour, with weighting held constant.
EQUAL_WEIGHT = NoroConfig(reliability_saturation_notional=1.0)


class TestFairValue:
    """NORO v0.2: a reliability-weighted MEDIAN, not a liquidity-weighted mean.

    The old suite asserted mean behaviour -- "two venues average", "the deep
    venue pulls fair value towards it". Both were true of the pre-remediation
    estimator and are deliberately false now: a median cannot be dragged by
    one venue's size, which is the P3-5 fix.
    """

    def test_two_equally_reliable_venues_give_the_midpoint(self):
        fair = compute_fair_value(
            "BTC-USD", [state("A", 100.0), state("B", 102.0)], EQUAL_WEIGHT
        )
        assert fair is not None
        # Exactly half the weight sits at or below each price, so the
        # straddling pair is averaged.
        assert fair.fair_value == pytest.approx(101.0, rel=1e-3)

    def test_identical_contributors_give_the_common_price(self, noro_config):
        fair = compute_fair_value(
            "BTC-USD",
            [state("A", 100.0), state("B", 100.0), state("C", 100.0)],
            noro_config,
        )
        assert fair is not None
        assert fair.fair_value == pytest.approx(100.0, rel=1e-6)
        assert fair.dispersion_bps == pytest.approx(0.0, abs=1e-6)

    def test_the_benchmark_lies_within_the_contributor_price_range(self, noro_config):
        states = [state("A", 100.0), state("B", 101.0), state("C", 130.0)]
        fair = compute_fair_value("BTC-USD", states, noro_config)
        prices = [v.price for v in fair.venues]
        assert min(prices) <= fair.fair_value <= max(prices)

    def test_p3_5_a_deep_venue_does_not_drag_the_benchmark(self):
        """P3-5 regression. The old weighted mean moved with raw depth; the
        median does not. C is the outlier AND by far the deepest venue."""
        modest = [state("A", 100.0, size=0.5), state("B", 100.1, size=0.5)]
        outlier = state("C", 130.0, size=500.0)
        fair = compute_fair_value("BTC-USD", [*modest, outlier], EQUAL_WEIGHT)
        assert fair.fair_value == pytest.approx(100.1, rel=1e-3), (
            "the middle price wins; the deep outlier is one vote, not a weight"
        )

    def test_reliability_is_bounded_at_one(self, noro_config):
        fair = compute_fair_value(
            "BTC-USD", [state("A", 100.0, size=10_000.0)], noro_config
        )
        assert fair.venues[0].reliability <= 1.0

    def test_contributor_order_does_not_change_the_benchmark(self, noro_config):
        states = [state("A", 100.0), state("B", 101.0), state("C", 103.0)]
        forward = compute_fair_value("BTC-USD", states, noro_config)
        backward = compute_fair_value("BTC-USD", list(reversed(states)), noro_config)
        assert forward.fair_value == pytest.approx(backward.fair_value)
        assert [v.venue for v in forward.venues] == [v.venue for v in backward.venues]

    def test_deviations_are_signed_around_the_benchmark(self):
        fair = compute_fair_value(
            "BTC-USD", [state("A", 100.0), state("B", 102.0)], EQUAL_WEIGHT
        )
        by_venue = {v.venue: fair.deviation_of(v.price) for v in fair.venues}
        assert by_venue["A"] < 0 and by_venue["B"] > 0

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

    def test_no_contributor_at_all_returns_none(self, noro_config):
        assert compute_fair_value("BTC-USD", [], noro_config) is None

    def test_only_the_requested_symbol_contributes(self, noro_config):
        eth = venue_state_from_book(make_book("B", "ETH-USD", 3_000.0))
        fair = compute_fair_value("BTC-USD", [state("A", 100.0), eth], noro_config)
        assert [v.venue for v in fair.venues] == ["A"]
        assert fair.fair_value == pytest.approx(100.0, rel=1e-3)

    def test_venue_price_blends_mid_and_microprice(self):
        s = state("A", 100.0)
        blended = venue_price(s, NoroConfig(microprice_weight=1.0))
        assert blended == pytest.approx(s.metrics.microprice)
        assert venue_price(s, NoroConfig(microprice_weight=0.0)) == pytest.approx(
            s.metrics.mid
        )

    def test_near_touch_notional_takes_the_thinner_side(self):
        s = state("A", 100.0)
        s.metrics.bid_depth_by_bps["10"] = 1_000.0
        s.metrics.ask_depth_by_bps["10"] = 400.0
        assert near_touch_notional(s, NoroConfig(liquidity_window_bps=10.0)) == (
            pytest.approx(400.0)
        )

    def test_p3_1_a_missing_bucket_excludes_rather_than_falling_back(self):
        """P3-1 regression. ``near_touch_notional`` must never answer an
        unmeasured window with whole-book depth."""
        s = state("A", 100.0)
        s.metrics.bid_depth_by_bps.pop("10")
        s.metrics.ask_depth_by_bps.pop("10")
        assert s.metrics.bid_depth_notional > 0, "the full book is still there"
        assert near_touch_notional(s, NoroConfig(liquidity_window_bps=10.0)) is None


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
