"""Valuation mathematics: the invariants NORO v0.2's estimator guarantees.

Audit-only. Nothing here changes production; these tests establish what
``compute_fair_value`` actually guarantees, so that findings can be stated
against a proved baseline rather than an assumed one.

The estimator changed in the Phase 3 remediation, and so did the invariant
set. The pre-remediation liquidity-weighted mean guaranteed that weights sum
to one, that fair value moves toward whichever venue gains liquidity, and that
a large enough outlier becomes the benchmark. Those were *properties of the
defect* -- P3-5 is precisely the observation that a deep venue could decide
whether it was an outlier -- and they are deliberately false of the
reliability-weighted median that replaced it.

What is asserted here is the set the new estimator is designed to hold:

    A  identical contributors price at the common price
    B  input order changes nothing
    C  the benchmark lies inside the contributor price range
    D  one extreme venue cannot drag it
    E  reliability is bounded at 1
    F  depth past saturation buys no further influence
    G  unusable venues are excluded
    H  a missing depth bucket excludes the contributor
    I  zero contributors gives None
    J  one contributor gives a diagnostic price, not a confirmation
    K  symbols never cross
    L  ties are broken deterministically
"""

from __future__ import annotations

import itertools
import math

import pytest

from agents.noro.fair_value import compute_fair_value
from core.config import NoroConfig
from core.models.common import DataQuality
from tests.audit.helpers import (
    PUBLISHED_BUCKETS,
    SYMBOL,
    deviations,
    fair_of,
    independent_of,
    reliabilities,
    venue,
)

#: Past the reliability saturation point, so every contributor weighs exactly
#: 1.0 and the weighted median reduces to the plain median. Most invariants
#: below are about ordering, and this holds weighting constant while they are
#: checked.
SATURATED = 200_000.0


@pytest.fixture
def config() -> NoroConfig:
    return NoroConfig()


# ======================================================================
# A — identical contributors
# ======================================================================


class TestA_IdenticalVenues:
    @pytest.mark.parametrize("count", [1, 2, 3, 4, 7])
    def test_identical_venues_price_at_the_common_price(self, config, count):
        states = [venue(f"V{i}", 100.0, liquidity=SATURATED) for i in range(count)]
        fair = fair_of(states, config)
        assert fair.fair_value == pytest.approx(100.0)
        assert fair.dispersion_bps == pytest.approx(0.0, abs=1e-9)

    def test_identical_venues_carry_identical_reliability(self, config):
        states = [venue(f"V{i}", 100.0, liquidity=50_000.0) for i in range(3)]
        weights = reliabilities(fair_of(states, config))
        assert len(set(weights.values())) == 1
        assert all(0.0 < w <= 1.0 for w in weights.values())


# ======================================================================
# B / L — determinism
# ======================================================================


class TestB_InputOrderInvariance:
    STATES = [(100.0, 50_000.0), (100.5, 900_000.0), (101.0, 1_000.0)]

    def _states(self):
        return [
            venue(f"V{i}", price, liquidity=liq)
            for i, (price, liq) in enumerate(self.STATES)
        ]

    def test_reordering_inputs_changes_nothing_economic(self, config):
        baseline = fair_of(self._states(), config)
        for order in itertools.permutations(self._states()):
            reordered = fair_of(list(order), config)
            assert reordered.fair_value == pytest.approx(baseline.fair_value)
            assert reordered.dispersion_bps == pytest.approx(baseline.dispersion_bps)
            assert deviations(reordered) == pytest.approx(deviations(baseline))

    def test_the_venues_tuple_is_sorted_by_venue_name(self, config):
        """Not input order. ``build_contributors`` sorts, so nothing
        downstream can depend on how the market dict happened to iterate."""
        for order in itertools.permutations(self._states()):
            fair = fair_of(list(order), config)
            assert [v.venue for v in fair.venues] == ["V0", "V1", "V2"]

    def test_the_same_inputs_produce_identical_output(self, config):
        states = self._states()
        results = [fair_of(states, config) for _ in range(10)]
        assert len({r.fair_value for r in results}) == 1
        assert len({r.dispersion_bps for r in results}) == 1


class TestL_DeterministicTieHandling:
    def test_equal_prices_are_ordered_by_venue_name(self, config):
        states = [
            venue("Z", 100.0, liquidity=SATURATED),
            venue("A", 100.0, liquidity=SATURATED),
            venue("M", 100.0, liquidity=SATURATED),
        ]
        fair = fair_of(states, config)
        assert [v.venue for v in fair.venues] == ["A", "M", "Z"]
        assert fair.fair_value == pytest.approx(100.0)

    def test_an_exact_weight_tie_averages_the_straddling_pair(self, config):
        """Two equally reliable contributors put exactly half the weight at
        or below each price, so the median is their midpoint rather than an
        arbitrary one of them."""
        fair = fair_of(
            [
                venue("A", 100.0, liquidity=SATURATED),
                venue("B", 102.0, liquidity=SATURATED),
            ],
            config,
        )
        assert fair.fair_value == pytest.approx(101.0)

    def test_the_midpoint_rule_is_symmetric_under_relabelling(self, config):
        first = fair_of(
            [
                venue("A", 100.0, liquidity=SATURATED),
                venue("B", 102.0, liquidity=SATURATED),
            ],
            config,
        )
        swapped = fair_of(
            [
                venue("A", 102.0, liquidity=SATURATED),
                venue("B", 100.0, liquidity=SATURATED),
            ],
            config,
        )
        assert first.fair_value == pytest.approx(swapped.fair_value)


# ======================================================================
# C / D — the benchmark cannot be dragged out of the cluster
# ======================================================================


class TestC_ConvexHull:
    @pytest.mark.parametrize(
        "prices",
        [
            [100.0, 100.0],
            [100.0, 101.0],
            [99.0, 100.0, 101.0],
            [100.0, 100.1, 150.0],
            [1.0, 2.0, 3.0, 4.0, 5.0],
            [100.0, 100.0, 100.0, 500.0],
        ],
    )
    def test_fair_value_lies_within_the_contributor_range(self, config, prices):
        states = [
            venue(f"V{i}", price, liquidity=SATURATED)
            for i, price in enumerate(prices)
        ]
        fair = fair_of(states, config)
        contributed = [v.price for v in fair.venues]
        assert min(contributed) <= fair.fair_value <= max(contributed)

    def test_it_holds_under_wildly_unequal_reliability_too(self, config):
        states = [
            venue("A", 100.0, liquidity=1.0),
            venue("B", 101.0, liquidity=50_000_000.0),
            venue("C", 500.0, liquidity=10.0),
        ]
        fair = fair_of(states, config)
        assert 100.0 <= fair.fair_value <= 500.0


class TestD_OneExtremeVenueCannotDragIt:
    @pytest.mark.parametrize("outlier", [150.0, 1_000.0, 1e6])
    def test_the_cluster_wins_however_far_the_outlier_is(self, config, outlier):
        states = [
            venue("A", 100.0, liquidity=SATURATED),
            venue("B", 100.1, liquidity=SATURATED),
            venue("C", outlier, liquidity=5_000_000.0),
        ]
        assert fair_of(states, config).fair_value == pytest.approx(100.1)

    def test_a_tiny_outlier_has_a_bounded_effect(self, config):
        base = [
            venue("A", 100.0, liquidity=SATURATED),
            venue("B", 100.1, liquidity=SATURATED),
            venue("C", 100.2, liquidity=SATURATED),
        ]
        before = fair_of(base, config).fair_value
        after = fair_of(
            [*base, venue("D", 100.05, liquidity=SATURATED)], config
        ).fair_value
        assert abs(after - before) < 0.1

    def test_adding_a_venue_at_the_benchmark_leaves_it_alone(self, config):
        base = [
            venue("A", 100.0, liquidity=SATURATED),
            venue("B", 100.1, liquidity=SATURATED),
            venue("C", 100.2, liquidity=SATURATED),
        ]
        before = fair_of(base, config).fair_value
        after = fair_of([*base, venue("D", before, liquidity=SATURATED)], config)
        assert after.fair_value == pytest.approx(before)

    def test_removing_an_already_excluded_venue_changes_nothing(self, config):
        base = [venue("A", 100.0), venue("B", 100.4)]
        excluded = venue("C", 500.0, quality=DataQuality.STALE)
        assert fair_of([*base, excluded], config).fair_value == pytest.approx(
            fair_of(base, config).fair_value
        )


# ======================================================================
# E / F — reliability is bounded
# ======================================================================


class TestE_ReliabilityIsBounded:
    @pytest.mark.parametrize(
        "notional", [1e-6, 1.0, 1_000.0, 99_999.0, 100_000.0, 1e9, 1e18]
    )
    def test_it_never_exceeds_one(self, config, notional):
        fair = fair_of([venue("V", 100.0, liquidity=notional)], config)
        assert 0.0 < fair.venues[0].reliability <= 1.0

    def test_it_reaches_one_exactly_at_the_saturation_notional(self, config):
        at = fair_of(
            [venue("V", 100.0, liquidity=config.reliability_saturation_notional)],
            config,
        )
        assert at.venues[0].reliability == pytest.approx(1.0)

    def test_mean_reliability_is_bounded_too(self, config):
        states = [venue(f"V{i}", 100.0, liquidity=1e12) for i in range(5)]
        assert fair_of(states, config).mean_reliability == pytest.approx(1.0)


class TestF_DepthPastSaturationBuysNothing:
    @pytest.mark.parametrize("multiple", [1, 10, 1_000, 1_000_000])
    def test_the_benchmark_is_unchanged_by_further_depth(self, config, multiple):
        saturation = config.reliability_saturation_notional
        states = [
            venue("A", 100.0, liquidity=saturation),
            venue("B", 100.1, liquidity=saturation),
            venue("C", 150.0, liquidity=saturation * multiple),
        ]
        assert fair_of(states, config).fair_value == pytest.approx(100.1)

    def test_two_saturated_venues_weigh_the_same(self, config):
        states = [
            venue("A", 100.0, liquidity=config.reliability_saturation_notional),
            venue("B", 100.0, liquidity=1e15),
        ]
        weights = reliabilities(fair_of(states, config))
        assert weights["A"] == weights["B"] == pytest.approx(1.0)

    def test_below_saturation_reliability_is_still_informative(self, config):
        weights = reliabilities(
            fair_of(
                [
                    venue("A", 100.0, liquidity=10_000.0),
                    venue("B", 100.0, liquidity=50_000.0),
                ],
                config,
            )
        )
        assert weights["A"] < weights["B"] < 1.0


# ======================================================================
# G / H / I / J — who contributes at all
# ======================================================================


class TestG_UnusableVenuesAreExcluded:
    @pytest.mark.parametrize(
        "quality",
        [DataQuality.DEGRADED, DataQuality.STALE, DataQuality.UNAVAILABLE],
    )
    def test_only_fresh_contributes(self, config, quality):
        fair = fair_of(
            [venue("A", 100.0), venue("B", 500.0, quality=quality)], config
        )
        assert [v.venue for v in fair.venues] == ["A"]

    def test_a_venue_with_no_mid_is_excluded(self, config):
        unpriceable = venue("B", 100.0)
        unpriceable.metrics.mid = None
        fair = fair_of([venue("A", 100.0), unpriceable], config)
        assert [v.venue for v in fair.venues] == ["A"]


class TestH_AMissingBucketExcludes:
    def test_a_venue_with_no_measured_bucket_is_excluded(self, config):
        no_bucket = venue("B", 130.0, buckets={}, full_book=(9_000_000.0, 9_000_000.0))
        fair = fair_of([venue("A", 100.0), no_bucket], config)
        assert [v.venue for v in fair.venues] == ["A"], (
            "no fallback to whole-book depth (P3-1)"
        )

    @pytest.mark.parametrize("bucket", PUBLISHED_BUCKETS)
    def test_a_venue_measured_only_elsewhere_is_excluded(self, config, bucket):
        if bucket == config.liquidity_window_bps:
            pytest.skip("this is the configured window, so it is measured")
        partial = venue("B", 130.0, buckets={bucket: (50_000.0, 50_000.0)})
        fair = fair_of([venue("A", 100.0), partial], config)
        assert [v.venue for v in fair.venues] == ["A"]

    def test_a_zero_depth_measurement_also_excludes(self, config):
        empty = venue("B", 130.0, bid_liquidity=0.0, ask_liquidity=0.0)
        fair = fair_of([venue("A", 100.0), empty], config)
        assert [v.venue for v in fair.venues] == ["A"]

    def test_a_one_sided_book_contributes_nothing(self, config):
        one_sided = venue("B", 130.0, bid_liquidity=9_000_000.0, ask_liquidity=0.0)
        fair = fair_of([venue("A", 100.0), one_sided], config)
        assert [v.venue for v in fair.venues] == ["A"]


class TestI_ZeroContributors:
    def test_no_states_at_all(self, config):
        assert compute_fair_value(SYMBOL, [], config) is None

    def test_no_usable_quality(self, config):
        states = [
            venue("A", 100.0, quality=DataQuality.STALE),
            venue("B", 101.0, quality=DataQuality.UNAVAILABLE),
        ]
        assert compute_fair_value(SYMBOL, states, config) is None

    def test_no_priceable_venue(self, config):
        state = venue("A", 100.0)
        state.metrics.mid = None
        assert compute_fair_value(SYMBOL, [state], config) is None

    def test_no_measured_depth_anywhere(self, config):
        states = [venue("A", 100.0, buckets={}), venue("B", 101.0, buckets={})]
        assert compute_fair_value(SYMBOL, states, config) is None


class TestJ_OneContributor:
    def test_one_venue_prices_at_its_own_price(self, config):
        fair = fair_of([venue("A", 100.0)], config)
        assert fair.fair_value == pytest.approx(100.0)
        assert fair.deviation_of(100.0) == pytest.approx(0.0)
        assert fair.contributor_count == 1

    def test_a_one_venue_valuation_is_tautological_and_says_so(self, config):
        """It is a price, not a second opinion: the venue is measured against
        itself and its deviation is zero by construction."""
        fair = fair_of([venue("A", 100.0)], config)
        assert fair.dispersion_bps == pytest.approx(0.0)
        assert deviations(fair) == {"A": pytest.approx(0.0)}

    def test_but_it_cannot_confirm_an_opportunity(self, config):
        """The remediation's boundary: a diagnostic valuation exists, and no
        independent benchmark does, so an opportunity on that venue gets no
        confirmation from it (P3-4)."""
        states = [venue("A", 100.0), venue("B", 100.2)]
        assert fair_of(states, config) is not None
        assert independent_of(states, config, exclude=("A", "B")) is None


class TestK_SymbolIsolation:
    def test_another_symbols_venues_never_contribute(self, config):
        states = [
            venue("A", 100.0, symbol=SYMBOL),
            venue("B", 3_000.0, symbol="ETH-USD", liquidity=9_000_000.0),
        ]
        fair = compute_fair_value(SYMBOL, states, config)
        assert [v.venue for v in fair.venues] == ["A"]
        assert fair.fair_value == pytest.approx(100.0)

    def test_every_contributor_records_the_symbol_it_priced(self, config):
        fair = compute_fair_value(
            SYMBOL, [venue("A", 100.0), venue("B", 100.2)], config
        )
        assert {v.symbol for v in fair.venues} == {SYMBOL}

    def test_a_stablecoin_variant_is_a_different_instrument(self, config):
        """``BTC-USDT`` is not ``BTC-USD``; letting one price the other values
        the stablecoin basis as a bitcoin dislocation."""
        states = [
            venue("A", 100.0, symbol="BTC-USD"),
            venue("A", 100.9, symbol="BTC-USDT", liquidity=9_000_000.0),
        ]
        fair = compute_fair_value("BTC-USD", states, config)
        assert fair.fair_value == pytest.approx(100.0)


# ======================================================================
# Scale, safety and purity
# ======================================================================


class TestPriceScaleInvariance:
    @pytest.mark.parametrize("scale", [0.001, 0.5, 2.0, 1_000.0])
    def test_scaling_every_price_scales_the_benchmark_and_leaves_bps_alone(
        self, config, scale
    ):
        prices = [100.0, 100.5, 103.0]
        base = fair_of(
            [venue(f"V{i}", p, liquidity=SATURATED) for i, p in enumerate(prices)],
            config,
        )
        scaled = fair_of(
            [
                venue(f"V{i}", p * scale, liquidity=SATURATED)
                for i, p in enumerate(prices)
            ],
            config,
        )
        assert scaled.fair_value == pytest.approx(base.fair_value * scale, rel=1e-9)
        assert scaled.dispersion_bps == pytest.approx(base.dispersion_bps, rel=1e-6)
        assert deviations(scaled) == pytest.approx(deviations(base), rel=1e-6)


class TestNonFiniteSafety:
    @pytest.mark.parametrize(
        ("price", "liquidity"),
        [
            (1e-6, 1e-6),
            (1e-6, 1e12),
            (1e12, 1e-6),
            (1e12, 1e12),
            (100.0, 1e18),
        ],
    )
    def test_extreme_finite_inputs_stay_finite(self, config, price, liquidity):
        fair = fair_of(
            [
                venue("A", price, liquidity=liquidity),
                venue("B", price * 1.01, liquidity=liquidity),
            ],
            config,
        )
        assert fair is not None
        assert math.isfinite(fair.fair_value) and fair.fair_value > 0
        assert math.isfinite(fair.dispersion_bps)
        for valuation in fair.venues:
            assert math.isfinite(valuation.price)
            assert math.isfinite(valuation.reliability)

    def test_prices_differing_below_float_resolution(self, config):
        fair = fair_of(
            [
                venue("A", 100.0, liquidity=SATURATED),
                venue("B", 100.0 * (1 + 1e-15), liquidity=SATURATED),
            ],
            config,
        )
        assert math.isfinite(fair.fair_value)
        assert fair.dispersion_bps == pytest.approx(0.0, abs=1e-6)

    @pytest.mark.parametrize("count", [10, 100, 500])
    def test_large_venue_counts_stay_sane(self, config, count):
        states = [
            venue(f"V{i:04d}", 100.0 + i * 0.001, liquidity=SATURATED)
            for i in range(count)
        ]
        fair = fair_of(states, config)
        assert fair.contributor_count == count
        assert 100.0 <= fair.fair_value <= 100.0 + count * 0.001
        assert math.isfinite(fair.dispersion_bps)


class TestPurity:
    def test_computing_does_not_mutate_its_inputs(self, config):
        states = [venue("A", 100.0), venue("B", 101.0)]
        before = [s.model_dump() for s in states]
        fair_of(states, config)
        assert [s.model_dump() for s in states] == before

    def test_computing_does_not_mutate_the_config(self, config):
        before = config.model_dump()
        fair_of([venue("A", 100.0), venue("B", 101.0)], config)
        assert config.model_dump() == before

    def test_computing_does_not_mutate_the_input_list(self, config):
        states = [venue("A", 100.0), venue("B", 101.0)]
        fair_of(states, config)
        assert [s.venue for s in states] == ["A", "B"]

    def test_venue_label_invariance(self, config):
        original = fair_of(
            [
                venue("alpha", 100.0, liquidity=SATURATED),
                venue("beta", 101.0, liquidity=SATURATED),
            ],
            config,
        )
        renamed = fair_of(
            [
                venue("gamma", 100.0, liquidity=SATURATED),
                venue("delta", 101.0, liquidity=SATURATED),
            ],
            config,
        )
        assert renamed.fair_value == pytest.approx(original.fair_value)
        assert renamed.dispersion_bps == pytest.approx(original.dispersion_bps)


class TestDuplicateVenues:
    def test_the_low_level_function_does_not_deduplicate(self, config):
        """Recorded, not defended against: ``compute_fair_value`` takes a
        list. Two states for one venue name both contribute."""
        states = [
            venue("A", 100.0, liquidity=SATURATED),
            venue("A", 200.0, liquidity=SATURATED),
        ]
        fair = compute_fair_value(SYMBOL, states, config)
        assert fair.contributor_count == 2
        assert [v.venue for v in fair.venues] == ["A", "A"]

    def test_production_cannot_produce_duplicate_venue_states(self, config):
        """``MarketState.venues`` is a dict keyed by ``venue:symbol``, so a
        duplicate is structurally impossible on the path that matters."""
        from tests.audit.helpers import market

        state = market(
            venue("A", 100.0), venue("A", 200.0), venue("B", 101.0)
        )
        assert len(state.states_for(SYMBOL)) == 2
        assert {s.venue for s in state.states_for(SYMBOL)} == {"A", "B"}

    def test_contributor_lookup_returns_the_first_match(self, config):
        states = [
            venue("A", 100.0, liquidity=SATURATED),
            venue("A", 200.0, liquidity=SATURATED),
        ]
        fair = compute_fair_value(SYMBOL, states, config)
        assert fair.contributor("A").price == pytest.approx(100.0)
