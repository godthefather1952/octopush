"""Phase 3 audit, Sections 12-14 / 26-27 / 38-39: does NORO add information?

The question this phase exists to answer. NORO is a REQUIRED consensus agent
with weight 1.5, so its vote can suspend or carry a trade. That is only worth
paying for if its answer is not already implied by the detector's question.

**H4 -- the two-venue structure.** With exactly two contributors and positive
weights, fair value is a convex combination of the two prices, so it lies
strictly between them. If the detector's buy venue is also the cheaper venue
by NORO's price measure, both confirmations are positive *by construction* and
NORO cannot disagree. This suite derives the closed form, then measures how
often the escape hatch -- the detector uses touch prices, NORO uses
mid/microprice -- actually fires.

**H5 -- self-inclusion.** Every venue is judged against a benchmark it is
itself part of, weighted by its own liquidity. A deep venue therefore helps
decide whether it is an outlier.

The audit-only comparators in ``helpers`` (leave-one-out, median, weighted
median, trimmed mean) exist to size those effects. None is a recommendation.
"""

from __future__ import annotations

import pytest

from core.config import NoroConfig
from tests.audit.helpers import (
    SYMBOL,
    contributors,
    fair_of,
    leave_one_out,
    median_price,
    trimmed_mean,
    venue,
    weighted_mean,
    weighted_median,
    weights,
)
from tests.audit.noro_fixtures import opinion_for


@pytest.fixture
def config() -> NoroConfig:
    return NoroConfig()


def confirmations_for(fair, buy_venue: str, sell_venue: str) -> tuple[float, float]:
    """Exactly what ``Noro.evaluate`` computes for a BUY/SELL pair."""
    return (-fair.deviation(buy_venue), fair.deviation(sell_venue))


# ======================================================================
# Section 12 — H4: the two-venue closed form
# ======================================================================


class TestH4_TwoVenueClosedForm:
    def test_fair_value_lies_strictly_between_two_contributors(self, config):
        for gap in (0.5, 1.0, 5.0, 50.0, 500.0):
            states = [
                venue("A", 100.0, liquidity=17_000.0),
                venue("B", 100.0 * (1 + gap / 10_000), liquidity=83_000.0),
            ]
            fair = fair_of(states, config)
            assert 100.0 < fair.fair_value < states[1].metrics.mid

    def test_both_confirmations_are_positive_whenever_buy_price_is_lower(self, config):
        """The tautology, stated exactly: when NORO's own price ordering
        agrees with the trade direction, NORO cannot disagree."""
        for w_a in (0.001, 0.1, 0.5, 0.9, 0.999):
            liquidity = 1_000_000.0
            states = [
                venue("A", 100.0, liquidity=liquidity * w_a),
                venue("B", 100.10, liquidity=liquidity * (1 - w_a)),
            ]
            fair = fair_of(states, config)
            buy_conf, sell_conf = confirmations_for(fair, "A", "B")
            assert buy_conf > 0 and sell_conf > 0, (
                f"w_A={w_a}: ({buy_conf:.3f}, {sell_conf:.3f})"
            )

    def test_the_confirmations_are_exactly_the_gap_times_the_other_weight(
        self, config
    ):
        """``-dev_A = w_B * G`` and ``dev_B = w_A * G``, where G is the raw
        price gap in bps measured against fair value. Everything NORO adds in
        the two-venue case is contained in that pair of weights."""
        states = [
            venue("A", 100.0, liquidity=250_000.0),
            venue("B", 100.20, liquidity=750_000.0),
        ]
        fair = fair_of(states, config)
        gap_bps = (states[1].metrics.mid - states[0].metrics.mid) / fair.fair_value * 10_000
        w = weights(fair)
        buy_conf, sell_conf = confirmations_for(fair, "A", "B")
        assert buy_conf == pytest.approx(w["B"] * gap_bps, rel=1e-9)
        assert sell_conf == pytest.approx(w["A"] * gap_bps, rel=1e-9)

    def test_the_mean_term_is_exactly_half_the_gap_regardless_of_weighting(
        self, config
    ):
        """The structural result. ``mean(confirmations) = G/2`` for every
        weighting, so half of NORO's edge is the detector's own price gap,
        re-expressed. Only ``min(confirmations) = min(w_A, w_B) * G`` carries
        any liquidity information at all."""
        for w_a in (0.01, 0.25, 0.5, 0.75, 0.99):
            liquidity = 1_000_000.0
            states = [
                venue("A", 100.0, liquidity=liquidity * w_a),
                venue("B", 100.20, liquidity=liquidity * (1 - w_a)),
            ]
            fair = fair_of(states, config)
            gap = (states[1].metrics.mid - states[0].metrics.mid) / fair.fair_value * 10_000
            buy_conf, sell_conf = confirmations_for(fair, "A", "B")
            assert (buy_conf + sell_conf) / 2 == pytest.approx(gap / 2, rel=1e-9)

    def test_the_edge_is_bounded_between_one_and_two_times_half_the_gap(
        self, config
    ):
        """``edge = G * (min(w_A, w_B) + 1/2)``, so it spans exactly
        ``[G/2, G]``. NORO's entire two-venue contribution is a factor-of-two
        modulation of the detector's number."""
        for w_a in (0.001, 0.1, 0.3, 0.5, 0.7, 0.9, 0.999):
            liquidity = 1_000_000.0
            states = [
                venue("A", 100.0, liquidity=liquidity * w_a),
                venue("B", 100.20, liquidity=liquidity * (1 - w_a)),
            ]
            fair = fair_of(states, config)
            gap = (states[1].metrics.mid - states[0].metrics.mid) / fair.fair_value * 10_000
            w = weights(fair)
            opinion = opinion_for(states, config, "A", "B")
            edge = opinion.detail["confirmed_edge_bps"]
            assert edge == pytest.approx(gap * (min(w.values()) + 0.5), rel=1e-6)
            assert gap * 0.5 - 1e-9 <= edge <= gap * 1.0 + 1e-9

    def test_a_two_venue_signal_is_never_negative_when_prices_order_correctly(
        self, config
    ):
        for gap in (0.1, 1.0, 4.0, 10.0, 100.0):
            for w_a in (0.01, 0.5, 0.99):
                liquidity = 1_000_000.0
                states = [
                    venue("A", 100.0, liquidity=liquidity * w_a),
                    venue("B", 100.0 * (1 + gap / 10_000),
                          liquidity=liquidity * (1 - w_a)),
                ]
                assert opinion_for(states, config, "A", "B").signal > 0


class TestH4_TheTautologyIsComplete:
    """There is NO escape hatch for a two-venue detector opportunity.

    The hope was that the detector and NORO measure different prices -- the
    detector picks the cheapest ``best_ask`` and the richest ``best_bid``,
    NORO uses a mid/microprice blend -- so a wide or lopsided spread might
    put the detector's buy venue ABOVE the sell venue by NORO's measure, and
    NORO could then genuinely disagree.

    It cannot happen, and the reason is a two-line proof:

    1. The detector only emits an opportunity when
       ``sell_bid - buy_ask > 0``. So the two venues' quote intervals are
       DISJOINT, with the buy venue's entirely below the sell venue's.
    2. ``venue_price`` is a convex blend of ``mid`` and ``microprice``, and
       both of those lie within ``[best_bid, best_ask]`` (the microprice is
       ``(bid*ask_size + ask*bid_size)/(bid_size+ask_size)``, a convex
       combination of the two touch prices).

    Therefore ``price_buy < price_sell`` for every opportunity the detector
    can emit, fair value lies strictly between them, and both confirmations
    are strictly positive. NORO's two-venue vote is positive by construction.
    """

    def test_a_positive_detector_edge_means_disjoint_quote_intervals(self, config):
        from strategies.cross_venue.detector import find_dislocation

        a = venue("A", 100.0, best_bid=99.90, best_ask=100.00, liquidity=100_000.0)
        b = venue("B", 100.2, best_bid=100.10, best_ask=100.30, liquidity=100_000.0)
        dislocation = find_dislocation(SYMBOL, [a, b], None)
        assert dislocation.gross_edge_bps > 0
        assert a.metrics.best_ask < b.metrics.best_bid, (
            "a positive edge IS the statement that the intervals are disjoint"
        )

    def test_venue_price_always_lies_inside_the_touch(self, config):
        """Step 2 of the proof, checked against the production function."""
        from agents.noro.fair_value import venue_price

        for micro_position in (0.0, 0.25, 0.5, 0.75, 1.0):
            bid, ask = 99.0, 100.0
            micro = bid + micro_position * (ask - bid)
            state = venue(
                "A", (bid + ask) / 2, best_bid=bid, best_ask=ask, microprice=micro
            )
            for weight in (0.0, 0.3, 0.5, 0.9, 1.0):
                price = venue_price(state, weight)
                assert bid <= price <= ask

    def test_the_microprice_weight_cannot_break_it_either(self, config):
        """Even at ``microprice_weight=1`` -- the most aggressive setting --
        the buy venue's price stays below the sell venue's."""
        from agents.noro.fair_value import venue_price

        buy = venue("A", 99.95, best_bid=99.90, best_ask=100.00, microprice=100.00)
        sell = venue("B", 100.20, best_bid=100.10, best_ask=100.30, microprice=100.10)
        for weight in (0.0, 0.5, 1.0):
            assert venue_price(buy, weight) < venue_price(sell, weight)

    @pytest.mark.parametrize("microprice_weight", [0.0, 0.5, 1.0])
    def test_an_exhaustive_random_search_finds_no_rejection(
        self, microprice_weight
    ):
        """The empirical companion to the proof.

        Random two-venue books, filtered to those the detector would actually
        emit (edge >= ``min_dislocation_bps``), across the full range of
        spreads and microprice positions. Not one produces a non-positive
        NORO signal.
        """
        import random

        from strategies.cross_venue.detector import find_dislocation

        config = NoroConfig(microprice_weight=microprice_weight)
        rng = random.Random(11)
        emitted = 0
        rejected = 0
        for _ in range(4_000):
            bid_a = rng.uniform(99.0, 101.0)
            spread_a = rng.uniform(0.0005, 0.6)
            bid_b = rng.uniform(99.0, 101.0)
            spread_b = rng.uniform(0.0005, 0.6)
            ask_a, ask_b = bid_a + spread_a, bid_b + spread_b
            a = venue(
                "A", (bid_a + ask_a) / 2, best_bid=bid_a, best_ask=ask_a,
                microprice=bid_a + rng.random() * spread_a,
                liquidity=rng.uniform(1e4, 1e6),
            )
            b = venue(
                "B", (bid_b + ask_b) / 2, best_bid=bid_b, best_ask=ask_b,
                microprice=bid_b + rng.random() * spread_b,
                liquidity=rng.uniform(1e4, 1e6),
            )
            dislocation = find_dislocation(SYMBOL, [a, b], None)
            if dislocation is None or dislocation.gross_edge_bps < 4.0:
                continue
            emitted += 1
            opinion = opinion_for(
                [a, b], config, dislocation.buy_venue, dislocation.sell_venue
            )
            if opinion is not None and opinion.signal <= 0:
                rejected += 1
        assert emitted > 1_000, f"only {emitted} opportunities generated"
        assert rejected == 0, (
            f"{rejected}/{emitted} rejections -- the proof above says zero"
        )

    def test_noro_can_still_reject_when_the_detector_would_not_have_asked(
        self, config
    ):
        """Completeness: NORO's rejection machinery works. It is simply
        unreachable for a two-venue opportunity, because the only inputs that
        trigger it are inputs the detector filters out first."""
        from strategies.cross_venue.detector import find_dislocation

        a = venue("A", 99.5, best_bid=99.0, best_ask=100.0, microprice=100.0,
                  liquidity=100_000.0)
        b = venue("B", 99.76, best_bid=99.51, best_ask=100.01, microprice=99.51,
                  liquidity=100_000.0)
        dislocation = find_dislocation(SYMBOL, [a, b], None)
        assert dislocation.gross_edge_bps < 0, (
            "overlapping quotes -- the detector would never emit this"
        )
        opinion = opinion_for([a, b], config, dislocation.buy_venue,
                              dislocation.sell_venue)
        assert opinion.signal < 0, "...and NORO would have rejected it"


# ======================================================================
# Section 14 — three or more venues
# ======================================================================


class TestThreePlusVenuesCanDisagree:
    def test_a_third_venue_clustering_with_the_sell_side_kills_the_trade(
        self, config
    ):
        """Two venues: confirmed. Add a third that says the "rich" venue is
        the normal one and the "cheap" venue is the outlier -- and NORO
        reverses."""
        a = venue("A", 100.00, liquidity=100_000.0)
        b = venue("B", 100.20, liquidity=100_000.0)
        assert opinion_for([a, b], config, "A", "B").signal > 0

        c = venue("C", 100.50, liquidity=800_000.0)
        with_third = opinion_for([a, b, c], config, "A", "B")
        assert with_third.signal < 0, (
            f"the third venue turned a confirmed trade into a rejection: "
            f"{with_third.signal:+.3f}"
        )

    def test_a_third_venue_between_them_leaves_it_confirmed(self, config):
        a = venue("A", 100.00, liquidity=100_000.0)
        b = venue("B", 100.20, liquidity=100_000.0)
        c = venue("C", 100.10, liquidity=800_000.0)
        assert opinion_for([a, b, c], config, "A", "B").signal > 0

    def test_a_third_venue_clustering_with_the_buy_side_strengthens_it(
        self, config
    ):
        a = venue("A", 100.00, liquidity=100_000.0)
        b = venue("B", 100.20, liquidity=100_000.0)
        two = opinion_for([a, b], config, "A", "B").signal
        c = venue("C", 100.00, liquidity=800_000.0)
        three = opinion_for([a, b, c], config, "A", "B").signal
        assert three < two, (
            "the sell leg is now far above fair value while the buy leg is "
            "at it, so min(confirmations) collapses -- the edge falls even "
            "though the third venue AGREES the buy venue is cheap"
        )

    def test_rejection_needs_a_third_venue_or_a_spread_inversion(self, config):
        """The headline contrast for Section 14: with matched spreads, a
        two-venue NORO cannot reject; a three-venue NORO can."""
        a = venue("A", 100.00, spread_bps=2.0, liquidity=100_000.0)
        b = venue("B", 100.20, spread_bps=2.0, liquidity=100_000.0)
        outlier_anchor = venue("C", 100.50, spread_bps=2.0, liquidity=900_000.0)
        assert opinion_for([a, b], config, "A", "B").signal > 0
        assert opinion_for([a, b, outlier_anchor], config, "A", "B").signal < 0


class TestRejectionIsReachableButVanishinglyRare:
    """The measured answer to "can NORO ever reject a real opportunity?".

    ``scripts/audit_noro_information_value.py`` runs the real detector and
    the real agent over seeded random books:

        venues   opportunities   rejected      rate
             2          25,027          0    0.000%
             3          35,311         13    0.037%
             4          38,699          6    0.016%
             5          39,650          0    0.000%
             8          39,994          0    0.000%

    Two venues: never, and provably so. Three or four: about one in three
    thousand. Five or more: not observed -- with more venues the extremes get
    more extreme and fair value falls back inside the interval.
    """

    def test_a_concrete_detector_driven_three_venue_rejection(self, config):
        """One of the rare cases, pinned exactly.

        V2 has a WIDE spread straddling both other venues: its bid does not
        win the sell leg and its ask does not win the buy leg, so the detector
        still targets V0 -> V1 -- but its mid pulls fair value ABOVE the sell
        venue, so the sell leg lands on the wrong side.
        """
        from strategies.cross_venue.detector import find_dislocation

        states = [
            venue("V0", 100.3488, best_bid=100.3461, best_ask=100.3515,
                  liquidity=3_475_515.0),
            venue("V1", 100.4560, best_bid=100.3980, best_ask=100.5140,
                  liquidity=362_187.0),
            venue("V2", 100.7945, best_bid=100.3963, best_ask=101.1926,
                  liquidity=4_210_864.0),
        ]
        dislocation = find_dislocation(SYMBOL, states, None)
        assert (dislocation.buy_venue, dislocation.sell_venue) == ("V0", "V1"), (
            "the wide-spread anchor wins neither extreme"
        )
        assert dislocation.gross_edge_bps > 4.0, "the detector would emit this"

        fair = fair_of(states, config)
        assert fair.fair_value > 100.46, (
            "the anchor pulled fair value ABOVE the sell venue"
        )
        opinion = opinion_for(states, config, "V0", "V1")
        assert opinion.signal < 0, (
            f"NORO rejected a real detector opportunity: {opinion.signal:+.3f}"
        )
        assert "FAIR_VALUE_CONTRADICTS_DISLOCATION" in opinion.reason_codes

    def test_the_same_market_without_the_anchor_is_confirmed(self, config):
        """Isolating the anchor's contribution: remove it and NORO agrees."""
        pair = [
            venue("V0", 100.3488, best_bid=100.3461, best_ask=100.3515,
                  liquidity=3_475_515.0),
            venue("V1", 100.4560, best_bid=100.3980, best_ask=100.5140,
                  liquidity=362_187.0),
        ]
        assert opinion_for(pair, config, "V0", "V1").signal > 0

    def test_the_anchor_must_win_neither_extreme_to_have_any_effect(self, config):
        """Why the rate is so low: a TIGHT venue priced outside the interval
        simply becomes the new extreme, and the detector re-targets onto it --
        producing a fresh pair that NORO confirms as usual."""
        from strategies.cross_venue.detector import find_dislocation

        a = venue("A", 100.00, spread_bps=2.0, liquidity=100_000.0)
        b = venue("B", 100.20, spread_bps=2.0, liquidity=100_000.0)
        tight_anchor = venue("C", 100.60, spread_bps=2.0, liquidity=900_000.0)
        dislocation = find_dislocation(SYMBOL, [a, b, tight_anchor], None)
        assert dislocation.sell_venue == "C", "the detector re-targeted"
        assert opinion_for(
            [a, b, tight_anchor], config, dislocation.buy_venue,
            dislocation.sell_venue,
        ).signal > 0


# ======================================================================
# Section 15 / 38 — H5: self-inclusion
# ======================================================================


class TestH5_SelfInclusion:
    def test_the_deepest_venue_pulls_the_benchmark_onto_itself(self, config):
        deep = venue("A", 100.0, liquidity=1_000_000.0)
        thin = venue("B", 101.0, liquidity=10_000.0)
        fair = fair_of([deep, thin], config)
        assert abs(fair.deviation("A")) == pytest.approx(0.99, abs=0.05), (
            "the deep venue looks nearly fair -- it IS the benchmark"
        )
        assert abs(fair.deviation("B")) == pytest.approx(99.0, abs=0.5), (
            "the thin one absorbs essentially the whole 100 bps gap"
        )
        assert abs(fair.deviation("B")) > 90 * abs(fair.deviation("A")), (
            "a 100:1 liquidity ratio becomes a 100:1 blame ratio"
        )

    def test_reversing_the_liquidity_reverses_who_looks_like_the_outlier(
        self, config
    ):
        """Identical prices, opposite depth: the SAME price pair produces
        opposite verdicts about which venue is mispriced."""
        thin_cheap = fair_of(
            [venue("A", 100.0, liquidity=10_000.0), venue("B", 101.0, liquidity=1_000_000.0)],
            config,
        )
        deep_cheap = fair_of(
            [venue("A", 100.0, liquidity=1_000_000.0), venue("B", 101.0, liquidity=10_000.0)],
            config,
        )
        # 98.04 rather than 99.0: deviations are measured against fair value,
        # which sits at the DEEP venue's price in each case, so the two are
        # mirror images scaled by slightly different denominators.
        assert abs(thin_cheap.deviation("A")) == pytest.approx(98.04, abs=0.5), (
            "thin and cheap: A is the outlier"
        )
        assert abs(deep_cheap.deviation("A")) == pytest.approx(0.99, abs=0.05), (
            "deep and cheap: the SAME price is now the benchmark"
        )

    @pytest.mark.parametrize("share", [0.01, 0.10, 0.50, 0.90, 0.99])
    def test_an_outliers_own_weight_shrinks_its_measured_deviation(
        self, config, share
    ):
        """Section 27: how easily can one deep outlier move the benchmark it
        is judged by?"""
        total = 1_000_000.0
        states = [
            venue("A", 100.0, liquidity=total * (1 - share) / 2),
            venue("B", 100.0, liquidity=total * (1 - share) / 2),
            venue("C", 150.0, liquidity=total * share),
        ]
        fair = fair_of(states, config)
        self_inclusive = abs(fair.deviation("C"))
        items = contributors(states, config)
        loo = leave_one_out(items, "C")
        left_out = abs((150.0 - loo) / loo * 10_000)
        assert left_out > self_inclusive, (
            "excluding itself always makes an outlier look more extreme"
        )
        if share >= 0.9:
            assert self_inclusive < left_out / 5, (
                f"at {share:.0%} of the liquidity the outlier's own deviation "
                f"is measured as {self_inclusive:.0f} bps instead of "
                f"{left_out:.0f} bps"
            )

    def test_at_extreme_dominance_the_outlier_becomes_the_benchmark(self, config):
        states = [
            venue("A", 100.0, liquidity=1_000.0),
            venue("B", 100.0, liquidity=1_000.0),
            venue("C", 1_000.0, liquidity=10_000_000.0),
        ]
        fair = fair_of(states, config)
        assert fair.fair_value > 999, "the outlier IS fair value now"
        assert abs(fair.deviation("C")) < 20
        assert abs(fair.deviation("A")) > 8_000, (
            "the two agreeing venues are reported as the outliers"
        )


class TestLeaveOneOutComparison:
    """Section 38: audit-only comparator, quantified. No migration implied."""

    SCENARIOS = [
        ("balanced three", [(100.0, 100_000.0), (100.0, 100_000.0), (100.5, 100_000.0)]),
        ("deep outlier", [(100.0, 50_000.0), (100.0, 50_000.0), (150.0, 900_000.0)]),
        ("thin outlier", [(100.0, 500_000.0), (100.0, 500_000.0), (150.0, 1_000.0)]),
        ("two cheap one rich", [(100.0, 100_000.0), (100.1, 100_000.0), (100.4, 300_000.0)]),
        ("five venues", [(100.0, 100_000.0), (100.05, 100_000.0), (100.1, 100_000.0),
                         (100.15, 100_000.0), (101.0, 600_000.0)]),
    ]

    @pytest.mark.parametrize(("name", "spec"), SCENARIOS)
    def test_leave_one_out_always_reports_a_larger_deviation(self, config, name, spec):
        states = [
            venue(f"V{i}", price, liquidity=liq) for i, (price, liq) in enumerate(spec)
        ]
        fair = fair_of(states, config)
        items = contributors(states, config)
        for item in items:
            loo = leave_one_out(items, item.venue)
            loo_dev = (item.price - loo) / loo * 10_000
            self_dev = fair.deviation(item.venue)
            assert abs(loo_dev) >= abs(self_dev) - 1e-9, (
                f"{name}/{item.venue}: LOO {loo_dev:.2f} vs self {self_dev:.2f}"
            )
            assert loo_dev * self_dev >= -1e-9, "the sign never flips"

    def test_the_magnitude_difference_is_large_for_a_dominant_venue(self, config):
        states = [
            venue("A", 100.0, liquidity=50_000.0),
            venue("B", 100.0, liquidity=50_000.0),
            venue("C", 150.0, liquidity=900_000.0),
        ]
        fair = fair_of(states, config)
        items = contributors(states, config)
        loo = leave_one_out(items, "C")
        assert abs(fair.deviation("C")) == pytest.approx(345, abs=15)
        assert abs((150.0 - loo) / loo * 10_000) == pytest.approx(5_000, abs=50)


class TestRobustBenchmarkComparison:
    """Section 39: how differently would other benchmarks behave?"""

    @pytest.mark.parametrize("count", [2, 3, 5, 10])
    def test_without_an_outlier_every_benchmark_agrees(self, config, count):
        states = [
            venue(f"V{i}", 100.0 + i * 0.001, liquidity=100_000.0)
            for i in range(count)
        ]
        items = contributors(states, config)
        assert weighted_mean(items) == pytest.approx(median_price(items), abs=0.01)
        assert weighted_median(items) == pytest.approx(median_price(items), abs=0.01)

    def test_with_a_dominant_outlier_they_diverge_sharply(self, config):
        states = [
            venue("A", 100.0, liquidity=50_000.0),
            venue("B", 100.0, liquidity=50_000.0),
            venue("C", 100.0, liquidity=50_000.0),
            venue("D", 100.0, liquidity=50_000.0),
            venue("E", 150.0, liquidity=1_000_000.0),
        ]
        items = contributors(states, config)
        assert weighted_mean(items) > 140, "production follows the deep outlier"
        assert median_price(items) == pytest.approx(100.0), "the median ignores it"
        assert weighted_median(items) == pytest.approx(150.0), (
            "the weighted median follows the liquidity mass instead"
        )
        assert trimmed_mean(items) == pytest.approx(100.0), (
            "trimming discards the outlier entirely"
        )

    def test_trimming_needs_at_least_five_venues_to_trim_anything(self, config):
        """Recorded as a property of the audit comparator, not of production:
        ``int(n * 0.2) >= 1`` first holds at n = 5, so a 4-venue trimmed mean
        trims nothing and equals the plain mean."""
        states = [
            venue("A", 100.0), venue("B", 100.0), venue("C", 100.0),
            venue("D", 150.0),
        ]
        items = contributors(states, config)
        assert trimmed_mean(items) == pytest.approx(112.5)

    def test_the_spread_between_benchmarks_is_the_size_of_the_finding(
        self, config
    ):
        states = [
            venue("A", 100.0, liquidity=50_000.0),
            venue("B", 100.0, liquidity=50_000.0),
            venue("C", 150.0, liquidity=900_000.0),
        ]
        items = contributors(states, config)
        candidates = {
            "weighted_mean (production)": weighted_mean(items),
            "median": median_price(items),
            "weighted_median": weighted_median(items),
            "trimmed_mean": trimmed_mean(items),
        }
        assert max(candidates.values()) - min(candidates.values()) > 45, (
            f"benchmarks disagree by more than 45 price units: {candidates}"
        )
