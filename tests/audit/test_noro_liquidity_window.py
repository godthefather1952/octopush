"""Phase 3 audit, Sections 7 / 8 / 9: the liquidity window and what it weighs.

Two separate questions, both about ``usable_liquidity``.

**The window (H1).** TIDAL publishes depth in exactly four measured buckets --
``DEPTH_BUCKETS_BPS = (1.0, 5.0, 10.0, 25.0)`` -- keyed by ``f"{bucket:g}"``.
``NoroConfig.liquidity_window_bps`` is a plain ``float`` with only ``gt=0``, so
its declared domain is the positive reals. ``usable_liquidity`` formats the
requested window the same way and looks it up; on a miss it falls back to
``bid_depth_notional``/``ask_depth_notional`` -- the WHOLE book. So the config
domain is continuous while the data behind it is discrete, and a value between
buckets does not interpolate: it silently switches to a different, much larger
quantity.

**The quantity (Section 9).** ``min(bid_depth, ask_depth)`` is two-sided
usable capacity. Whether that is the right weight for a FAIR VALUE -- as
opposed to for executability, which is ZEPHR's job -- is an architectural
question this suite measures rather than answers.
"""

from __future__ import annotations

import pytest

from agents.noro.fair_value import compute_fair_value, usable_liquidity
from agents.tidal.metrics import DEPTH_BUCKETS_BPS
from core.config import NoroConfig
from tests.audit.helpers import (
    PUBLISHED_BUCKETS,
    SYMBOL,
    cliff_venue,
    fair_of,
    venue,
    weights,
)

#: The cliff used throughout: thin at the touch, enormous further out.
NEAR = 5_000.0
FAR = 1_000_000.0


def config(window: float) -> NoroConfig:
    return NoroConfig(liquidity_window_bps=window)


# ======================================================================
# Section 8 — is the configuration domain honest?
# ======================================================================


class TestTheConfigDomainIsWiderThanTheData:
    def test_tidal_publishes_exactly_four_buckets(self):
        assert DEPTH_BUCKETS_BPS == (1.0, 5.0, 10.0, 25.0)

    def test_the_config_accepts_any_positive_float(self):
        for window in (0.001, 3.7, 9.999, 10.001, 12.5, 1e6):
            assert NoroConfig(liquidity_window_bps=window).liquidity_window_bps == window

    def test_nothing_validates_the_window_against_the_published_buckets(self):
        """The finding in one line: a value that can never hit a bucket is
        accepted as readily as one that always will."""
        off_bucket = NoroConfig(liquidity_window_bps=9.999)
        assert off_bucket.liquidity_window_bps not in DEPTH_BUCKETS_BPS

    def test_a_missing_bucket_is_indistinguishable_from_absent_data(self):
        """``usable_liquidity`` cannot tell "this venue measured no depth
        inside 10 bps" from "nobody measured a 9.999 bps bucket": both are a
        dict miss. One should be zero; the other falls back to the full book."""
        thin = venue("A", 100.0, buckets={10.0: (0.0, 0.0)}, full_book=(FAR, FAR))
        assert usable_liquidity(thin, 10.0) == 0.0, "measured, and genuinely empty"
        assert usable_liquidity(thin, 9.999) == FAR, "unmeasured, so full book"


# ======================================================================
# Section 7 — H1: the fallback discontinuity, quantified
# ======================================================================


class TestH1_TheFallbackDiscontinuity:
    @pytest.mark.parametrize("bucket", PUBLISHED_BUCKETS)
    def test_a_window_on_a_bucket_uses_the_measured_depth(self, bucket):
        state = cliff_venue("A", 100.0, near_notional=NEAR, far_notional=FAR)
        measured = state.metrics.bid_depth_by_bps[f"{bucket:g}"]
        assert usable_liquidity(state, bucket) == measured

    @pytest.mark.parametrize(
        "window", [1.001, 4.9, 9.9, 9.999, 10.001, 10.1, 15.0, 20.0, 24.999, 25.001]
    )
    def test_a_window_off_a_bucket_silently_uses_the_whole_book(self, window):
        state = cliff_venue("A", 100.0, near_notional=NEAR, far_notional=FAR)
        assert usable_liquidity(state, window) == pytest.approx(NEAR + FAR)

    def test_the_jump_across_one_thousandth_of_a_basis_point(self):
        """The headline number for P3-1."""
        state = cliff_venue("A", 100.0, near_notional=NEAR, far_notional=FAR)
        at_bucket = usable_liquidity(state, 10.0)
        just_past = usable_liquidity(state, 10.001)
        assert at_bucket == pytest.approx(NEAR)
        assert just_past == pytest.approx(NEAR + FAR)
        assert just_past / at_bucket > 200, (
            f"a 0.001 bps config change multiplied usable liquidity by "
            f"{just_past / at_bucket:.0f}x"
        )

    def test_the_fallback_is_not_monotone_in_the_window(self):
        """A wider window should never see LESS liquidity. Here 10.001 bps
        sees 201x what 10 bps sees, and 25 bps sees less than 10.001 bps --
        the sequence is not monotone, because two different quantities are
        being reported under one name."""
        state = cliff_venue("A", 100.0, near_notional=NEAR, far_notional=FAR)
        measured = [
            (w, usable_liquidity(state, w))
            for w in (1.0, 5.0, 10.0, 10.001, 25.0, 25.001)
        ]
        values = [v for _, v in measured]
        assert values != sorted(values), f"non-monotone sequence: {measured}"


class TestH1_EconomicConsequence:
    """Can 0.001 bps of configuration reverse which venue anchors fair value?"""

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

    def test_at_the_bucket_the_deep_touch_venue_dominates(self):
        fair = fair_of(self._pair(), config(10.0))
        w = weights(fair)
        assert w["B"] > 0.98, f"B anchors fair value: {w}"
        assert fair.fair_value > 100.09

    def test_one_thousandth_past_it_the_other_venue_dominates(self):
        fair = fair_of(self._pair(), config(10.001))
        w = weights(fair)
        assert w["A"] > 0.75, f"A now anchors fair value: {w}"
        assert fair.fair_value < 100.05

    def test_the_deviations_change_sign_for_both_venues(self):
        """The economic payload of the finding: which venue looks cheap and
        which looks rich is decided by a config digit, not by the market."""
        at_bucket = fair_of(self._pair(), config(10.0))
        past_bucket = fair_of(self._pair(), config(10.001))

        a_before = at_bucket.deviation("A")
        a_after = past_bucket.deviation("A")
        b_before = at_bucket.deviation("B")
        b_after = past_bucket.deviation("B")

        assert a_before < -9, "A looks clearly cheap at the bucket"
        assert abs(a_after) < 3, "...and nearly at fair value one thousandth later"
        assert abs(b_before) < 0.5, "B looks fair at the bucket"
        assert b_after > 7, "...and clearly rich one thousandth later"

    def test_the_swing_reaches_noro_signal_but_is_damped(self):
        """Carried to the agent's own output -- and measurably damped there.

        Measured: signal 0.340 -> 0.487 across a 0.001 bps config change.
        Real, but far smaller than the 78x swing in venue weight, because
        ``min + mean`` cancels most of it: with two venues the ``mean`` term
        depends only on the raw price gap, never on the weights (proved in
        the information-value suite). Only the ``min`` term carries the
        weighting, so a total inversion of which venue anchors fair value
        moves the signal by roughly half the gap.
        """
        from tests.audit.noro_fixtures import signal_for

        legs = ("A", "B")  # BUY A, SELL B -- the detector's shape
        at_bucket = signal_for(self._pair(), config(10.0), *legs)
        past_bucket = signal_for(self._pair(), config(10.001), *legs)
        assert at_bucket == pytest.approx(0.3396, abs=1e-3)
        assert past_bucket == pytest.approx(0.4868, abs=1e-3)
        assert past_bucket > at_bucket, (
            "the direction follows the weight inversion"
        )
        assert past_bucket - at_bucket > 0.14, (
            f"one thousandth of a bps moved NORO's signal from "
            f"{at_bucket:.3f} to {past_bucket:.3f}"
        )

    def test_the_damping_is_why_the_severity_is_not_critical(self):
        """Both windows still CONFIRM the trade, and confidence is saturated
        in both -- so this finding degrades NORO's valuation quality without,
        on this scenario, reversing its vote."""
        from tests.audit.noro_fixtures import opinion_for

        for window in (10.0, 10.001):
            opinion = opinion_for(self._pair(), config(window), "A", "B")
            assert opinion.signal > 0
            assert "FAIR_VALUE_CONFIRMS_DISLOCATION" in opinion.reason_codes
            assert opinion.confidence == pytest.approx(1.0)


# ======================================================================
# Section 9 — what min(bid, ask) means
# ======================================================================


class TestLiquidityWeightIsTwoSidedCapacity:
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
        assert usable_liquidity(state, 10.0) == pytest.approx(expected)

    def test_a_deep_one_sided_book_is_weighted_like_a_tiny_one(self):
        """The architectural question, made concrete.

        B quotes a million dollars of bid and five thousand of ask. As a
        price-discovery signal that is a very well-observed venue. As
        executable two-sided capacity it is thin. NORO weights it as thin --
        the executability reading -- which is the quantity ZEPHR is
        separately responsible for.
        """
        deep_one_sided = venue("B", 101.0, bid_liquidity=1_000_000.0, ask_liquidity=5_000.0)
        genuinely_tiny = venue("C", 101.0, bid_liquidity=5_000.0, ask_liquidity=5_000.0)
        window = 10.0
        assert usable_liquidity(deep_one_sided, window) == usable_liquidity(
            genuinely_tiny, window
        )

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

    def test_alternatives_would_weight_these_books_very_differently(self):
        """Audit-only comparison, no recommendation attached."""
        bid, ask = 1_000_000.0, 5_000.0
        minimum = min(bid, ask)
        total = bid + ask
        geometric = (bid * ask) ** 0.5
        harmonic = 2 * bid * ask / (bid + ask)
        assert minimum == 5_000.0
        assert total == pytest.approx(1_005_000.0)
        assert geometric == pytest.approx(70_710.7, rel=1e-4)
        assert harmonic == pytest.approx(9_950.2, rel=1e-4)
        assert total / minimum == pytest.approx(201.0), (
            "the choice of statistic spans two orders of magnitude on this book"
        )


class TestLiquidityIsUsedTwice:
    """Section 21 question 8, established here because it starts in the window.

    The same ``usable_liquidity`` number both decides a venue's WEIGHT in the
    benchmark and, summed, drives NORO's CONFIDENCE. So a config change that
    moves the window moves both at once.
    """

    def test_one_window_change_moves_weight_and_total_liquidity_together(self):
        states = [
            cliff_venue("A", 100.0, near_notional=NEAR, far_notional=FAR),
            venue("B", 100.1, liquidity=50_000.0),
        ]
        at_bucket = fair_of(states, config(10.0))
        past_bucket = fair_of(states, config(10.001))
        assert past_bucket.total_liquidity > at_bucket.total_liquidity * 10
        assert weights(past_bucket)["A"] > weights(at_bucket)["A"] * 5
