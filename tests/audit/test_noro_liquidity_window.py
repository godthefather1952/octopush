"""P3-1 / P3-9 regression: the depth window, and what it is allowed to weigh.

**What the audit found (P3-1).** TIDAL publishes depth in exactly four measured
buckets -- ``DEPTH_BUCKETS_BPS = (1.0, 5.0, 10.0, 25.0)`` -- keyed by
``f"{bucket:g}"``. ``NoroConfig.liquidity_window_bps`` was a plain ``float``
with only ``gt=0``, so its declared domain was the positive reals, while the
data behind it was discrete. The old ``usable_liquidity`` formatted the
requested window the same way, looked it up, and **on a miss fell back to
``bid_depth_notional``/``ask_depth_notional`` -- the WHOLE book**. A value
0.001 bps off a bucket did not interpolate; it silently switched to a
different quantity, measured at 201x on the audit's cliff book, and that
reversed which venue anchored fair value.

**What the remediation did.** The bucket set is now the config's domain: an
off-bucket window is a startup error. At runtime a missing measurement
excludes the contributor. ``near_touch_notional`` has no fallback path at all,
and the whole-book totals are never read by NORO.

**What the audit also found (P3-9).** The same number both weighted a venue's
contribution to fair value and drove confidence, and it was two-sided
executable capacity -- ZEPHR's question. It is now bounded into a reliability
in [0, 1], so depth past saturation buys no further influence.

Every test below asserts the closed behaviour. The old numbers appear only in
docstrings, as the thing that must not come back.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from agents.noro.fair_value import compute_fair_value, near_touch_notional
from core.config import NoroConfig
from core.models.market import DEPTH_BUCKETS_BPS, depth_bucket_key
from tests.audit.helpers import (
    PUBLISHED_BUCKETS,
    SYMBOL,
    cliff_venue,
    fair_of,
    reliabilities,
    venue,
)

#: The cliff used throughout: thin at the touch, enormous further out. If the
#: fallback ever returns, FAR is what leaks into the valuation.
NEAR = 5_000.0
FAR = 1_000_000.0


def config(window: float) -> NoroConfig:
    return NoroConfig(liquidity_window_bps=window)


# ======================================================================
# P3-1 — the config domain now matches the data
# ======================================================================


class TestTheConfigDomainMatchesTheData:
    def test_tidal_publishes_exactly_four_buckets(self):
        assert DEPTH_BUCKETS_BPS == (1.0, 5.0, 10.0, 25.0)

    @pytest.mark.parametrize("bucket", PUBLISHED_BUCKETS)
    def test_every_published_bucket_is_accepted(self, bucket):
        assert NoroConfig(liquidity_window_bps=bucket).liquidity_window_bps == bucket

    @pytest.mark.parametrize(
        "window", [0.001, 3.7, 7.5, 9.999, 10.001, 12.5, 24.999, 25.001, 1e6]
    )
    def test_p3_1_an_off_bucket_window_is_rejected_at_construction(self, window):
        """The finding, closed at its root.

        Before: ``NoroConfig(liquidity_window_bps=9.999)`` constructed happily
        and every venue silently reported whole-book depth. Now the value that
        can never hit a measurement is refused before the platform starts.
        """
        with pytest.raises(ValidationError):
            NoroConfig(liquidity_window_bps=window)

    def test_the_rejection_names_the_supported_set(self):
        with pytest.raises(ValidationError) as caught:
            NoroConfig(liquidity_window_bps=10.001)
        message = str(caught.value)
        assert "10.001" in message
        for bucket in PUBLISHED_BUCKETS:
            assert f"{bucket:g}" in message

    def test_the_default_window_is_a_measured_bucket(self):
        assert NoroConfig().liquidity_window_bps in DEPTH_BUCKETS_BPS


# ======================================================================
# P3-1 — the runtime fallback is gone
# ======================================================================


class TestTheFullBookFallbackIsClosed:
    @pytest.mark.parametrize("bucket", PUBLISHED_BUCKETS)
    def test_a_window_on_a_bucket_uses_that_exact_measurement(self, bucket):
        state = cliff_venue("A", 100.0, near_notional=NEAR, far_notional=FAR)
        measured = state.metrics.bid_depth_by_bps[depth_bucket_key(bucket)]
        assert near_touch_notional(state, config(bucket)) == measured

    def test_depth_within_returns_the_requested_bucket(self):
        state = venue(
            "A",
            100.0,
            buckets={1.0: (10.0, 11.0), 5.0: (50.0, 55.0), 10.0: (100.0, 110.0),
                     25.0: (250.0, 275.0)},
            full_book=(FAR, FAR),
        )
        assert state.metrics.depth_within(5.0) == (50.0, 55.0)
        assert state.metrics.depth_within(10.0) == (100.0, 110.0)

    def test_depth_within_returns_none_for_an_unmeasured_distance(self):
        state = venue("A", 100.0, buckets={10.0: (100.0, 100.0)}, full_book=(FAR, FAR))
        assert state.metrics.depth_within(10.0) == (100.0, 100.0)
        assert state.metrics.depth_within(5.0) is None

    def test_p3_1_a_missing_bucket_never_returns_full_book_depth(self):
        """The headline regression.

        The old code could not distinguish "this venue measured no depth
        inside 10 bps" from "nobody measured a 9.999 bps bucket" -- both were
        a dict miss, and the miss answered with the whole book. Now a missing
        measurement is ``None``, and ``None`` is not a number the valuation can
        use.
        """
        state = venue("A", 100.0, buckets={10.0: (0.0, 0.0)}, full_book=(FAR, FAR))
        assert state.metrics.bid_depth_notional == FAR, "the full book is present"
        assert near_touch_notional(state, config(10.0)) == 0.0, (
            "measured, and genuinely empty"
        )
        assert state.metrics.depth_within(5.0) is None, "unmeasured stays unmeasured"

    def test_p3_1_arbitrary_window_cannot_trigger_full_book_fallback(self):
        """P3-1, stated as the property that must never be false again.

        Neither route to the fallback survives: the off-bucket window cannot
        be configured, and a genuinely absent measurement yields ``None``
        rather than ``bid_depth_notional``/``ask_depth_notional``.
        """
        with pytest.raises(ValidationError):
            NoroConfig(liquidity_window_bps=10.001)

        no_measurement = venue("A", 100.0, buckets={}, full_book=(FAR, FAR))
        assert no_measurement.metrics.bid_depth_notional == FAR
        assert near_touch_notional(no_measurement, config(10.0)) is None, (
            "a venue that measured nothing near the touch must not come back "
            "as the deepest contributor in the market"
        )

    def test_an_unmeasurable_venue_is_excluded_from_the_valuation(self):
        states = [
            venue("A", 100.0, liquidity=50_000.0),
            venue("B", 130.0, buckets={}, full_book=(FAR, FAR)),
        ]
        fair = compute_fair_value(SYMBOL, states, config(10.0))
        assert [v.venue for v in fair.venues] == ["A"]
        assert fair.fair_value == pytest.approx(100.0), (
            "a venue with a million dollars of unbucketed book contributed nothing"
        )

    def test_measured_depth_is_monotone_in_the_window(self):
        """The old sequence was non-monotone -- 10.001 bps 'saw' 201x what 10
        bps saw, then 25 bps saw less again, because two different quantities
        were reported under one name. Across the real buckets it is monotone."""
        state = cliff_venue(
            "A", 100.0, near_notional=NEAR, far_notional=FAR, near_bps=8.0
        )
        measured = [near_touch_notional(state, config(w)) for w in PUBLISHED_BUCKETS]
        assert measured == sorted(measured), f"non-monotone: {measured}"


# ======================================================================
# P3-1 — the economic consequence is gone with it
# ======================================================================


class TestTheAnchorCannotBeFlippedByAConfigDigit:
    """The audit's economic payload: 0.001 bps of configuration decided which
    venue looked cheap and which looked rich. Two things now prevent it."""

    @staticmethod
    def _pair():
        # A: slightly cheap, thin at the touch, enormous deep book.
        # B: slightly rich, deep at the touch, moderate book overall.
        cheap = cliff_venue("A", 100.00, near_notional=5_000.0, far_notional=2_000_000.0)
        rich = venue(
            "B",
            100.10,
            buckets={b: (500_000.0, 500_000.0) for b in PUBLISHED_BUCKETS},
            full_book=(600_000.0, 600_000.0),
        )
        return [cheap, rich]

    def test_the_off_bucket_window_that_flipped_it_cannot_be_configured(self):
        with pytest.raises(ValidationError):
            config(10.001)

    def test_p3_9_the_deep_venue_no_longer_dominates_the_benchmark(self):
        """Reliability is capped at 1.0, so B's hundred-fold depth advantage
        buys it one vote, not 98% of the weight it used to hold."""
        fair = fair_of(self._pair(), config(10.0))
        weights = reliabilities(fair)
        assert all(w <= 1.0 for w in weights.values()), weights
        assert weights["B"] == pytest.approx(1.0), "B is past saturation"
        assert weights["B"] / weights["A"] < 25, (
            f"a bounded weight cannot express a 100x depth ratio: {weights}"
        )

    def test_the_benchmark_still_lies_between_the_two_venues(self):
        fair = fair_of(self._pair(), config(10.0))
        prices = sorted(v.price for v in fair.venues)
        assert prices[0] <= fair.fair_value <= prices[-1]


# ======================================================================
# P3-9 — what the near-touch number means, and what it may not do
# ======================================================================


class TestNearTouchDepthIsTwoSidedEvidence:
    @pytest.mark.parametrize(
        ("name", "bid", "ask", "expected"),
        [
            ("A balanced", 100_000.0, 100_000.0, 100_000.0),
            ("B bid-heavy", 1_000_000.0, 5_000.0, 5_000.0),
            ("C ask-heavy", 5_000.0, 1_000_000.0, 5_000.0),
            ("D extreme", 1.0, 1_000_000.0, 1.0),
            ("E both tiny", 10.0, 10.0, 10.0),
            ("F both huge", 5_000_000.0, 5_000_000.0, 5_000_000.0),
        ],
    )
    def test_the_thinner_side_governs(self, name, bid, ask, expected):
        state = venue("V", 100.0, bid_liquidity=bid, ask_liquidity=ask)
        assert near_touch_notional(state, config(10.0)) == pytest.approx(expected)

    def test_a_one_sided_book_is_excluded_from_price_discovery_entirely(self):
        """An empty side takes the venue's price out of the benchmark
        altogether, however well observed its other side is."""
        states = [
            venue("A", 100.0, liquidity=50_000.0),
            venue("B", 105.0, bid_liquidity=10_000_000.0, ask_liquidity=0.0),
        ]
        fair = compute_fair_value(SYMBOL, states, config(10.0))
        assert [v.venue for v in fair.venues] == ["A"]
        assert fair.fair_value == pytest.approx(100.0), (
            "a venue quoting ten million dollars of bid contributed nothing"
        )

    @pytest.mark.parametrize("notional", [1.0, 1_000.0, 100_000.0, 1e9, 1e15])
    def test_reliability_is_bounded_regardless_of_depth(self, notional):
        state = venue("V", 100.0, liquidity=notional)
        fair = compute_fair_value(SYMBOL, [state], config(10.0))
        assert 0.0 < fair.venues[0].reliability <= 1.0

    def test_p3_9_depth_past_saturation_buys_no_further_influence(self):
        """The bound is the whole point: two venues both past the saturation
        notional weigh the same however far past it they are, so no venue can
        become the benchmark by being large."""
        settings = NoroConfig(
            liquidity_window_bps=10.0, reliability_saturation_notional=50_000.0
        )
        big = venue("A", 100.0, liquidity=100_000.0)
        colossal = venue("B", 100.0, liquidity=5_000_000_000.0)
        fair = compute_fair_value(SYMBOL, [big, colossal], settings)
        weights = reliabilities(fair)
        assert weights["A"] == pytest.approx(1.0)
        assert weights["B"] == pytest.approx(1.0)
        assert weights["A"] == weights["B"], (
            f"a 50,000x depth ratio produced identical influence: {weights}"
        )

    def test_far_book_liquidity_does_not_influence_valuation_reliability(self):
        """P3-1 and P3-9 together: what is out at 40 bps is not evidence about
        the price at the touch, and NORO never reads it."""
        thin_near = cliff_venue(
            "A", 100.0, near_notional=1_000.0, far_notional=10_000_000.0
        )
        same_near_no_far = venue(
            "B",
            100.0,
            buckets={b: (1_000.0, 1_000.0) for b in PUBLISHED_BUCKETS},
            full_book=(1_000.0, 1_000.0),
        )
        window = config(10.0)
        assert near_touch_notional(thin_near, window) == near_touch_notional(
            same_near_no_far, window
        ), "a ten-million-dollar far book changed nothing"


class TestReliabilityAndConfidenceNoLongerShareOneNumber:
    """P3-9's second half.

    The old ``usable_liquidity`` both decided a venue's WEIGHT in the benchmark
    and, summed, drove NORO's CONFIDENCE -- so one config change moved both at
    once, and neither number was really about valuation. Confidence is now
    built from breadth, contributor agreement and mean reliability, and the
    weight is bounded, so raw capacity drives neither.
    """

    def test_total_near_touch_notional_is_reported_but_bounded_weights_are_used(self):
        states = [
            venue("A", 100.0, liquidity=25_000.0),
            venue("B", 100.1, liquidity=50_000.0),
        ]
        fair = fair_of(states, config(10.0))
        assert fair.total_near_touch_notional == pytest.approx(75_000.0)
        assert all(0.0 < v.reliability <= 1.0 for v in fair.venues)

    def test_mean_reliability_is_the_quality_signal_not_the_raw_sum(self):
        modest = fair_of([venue("A", 100.0, liquidity=10_000.0)], config(10.0))
        saturated = fair_of([venue("A", 100.0, liquidity=10_000_000.0)], config(10.0))
        assert modest.mean_reliability < saturated.mean_reliability
        assert saturated.mean_reliability == pytest.approx(1.0)
        assert saturated.mean_reliability <= 1.0, "and it stops there"
