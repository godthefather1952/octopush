"""Phase 3 audit, Sections 5 / 29 / 30 / 31: fair-value mathematics.

Audit-only. Nothing here changes production; these tests establish what
``compute_fair_value`` actually guarantees, so that later findings can be
stated against a proved baseline rather than an assumed one.

Where an invariant holds, it is recorded as a positive property worth keeping.
Where it does not, the test says so precisely rather than being softened until
it passes.
"""

from __future__ import annotations

import math

import pytest

from agents.noro.fair_value import compute_fair_value
from core.config import NoroConfig
from core.models.common import DataQuality
from tests.audit.helpers import SYMBOL, deviations, fair_of, venue, weights


@pytest.fixture
def config() -> NoroConfig:
    return NoroConfig()


# ======================================================================
# Section 5 — basic invariants
# ======================================================================


class TestA_IdenticalVenues:
    def test_identical_venues_price_at_the_common_price(self, config):
        states = [venue("A", 100.0), venue("B", 100.0)]
        fair = fair_of(states, config)
        assert fair.fair_value == pytest.approx(100.0)
        for deviation in deviations(fair).values():
            assert deviation == pytest.approx(0.0, abs=1e-9)

    def test_identical_venues_split_weight_evenly(self, config):
        fair = fair_of([venue("A", 100.0), venue("B", 100.0)], config)
        assert list(weights(fair).values()) == pytest.approx([0.5, 0.5])


class TestB_ConvexHull:
    @pytest.mark.parametrize(
        "prices",
        [
            (100.0, 101.0),
            (100.0, 100.0),
            (99.0, 100.0, 105.0),
            (100.0, 100.0, 100.0, 250.0),
            (1.0, 1_000_000.0),
        ],
    )
    def test_fair_value_lies_within_the_contributor_range(self, config, prices):
        states = [
            venue(f"V{i}", price, liquidity=10_000.0 * (i + 1))
            for i, price in enumerate(prices)
        ]
        fair = fair_of(states, config)
        assert min(prices) <= fair.fair_value <= max(prices)


class TestC_WeightsSumToOne:
    @pytest.mark.parametrize("count", [1, 2, 3, 5, 10, 50])
    def test_weights_sum_to_one(self, config, count):
        states = [
            venue(f"V{i}", 100.0 + i * 0.01, liquidity=1_000.0 * (i + 1))
            for i in range(count)
        ]
        fair = fair_of(states, config)
        assert sum(weights(fair).values()) == pytest.approx(1.0)
        assert all(w > 0 for w in weights(fair).values())


class TestD_LiquidityMonotonicity:
    def test_fair_value_moves_toward_the_venue_gaining_liquidity(self, config):
        previous = None
        for liquidity in (10_000.0, 50_000.0, 250_000.0, 1_000_000.0):
            fair = fair_of(
                [venue("A", 100.0, liquidity=liquidity), venue("B", 101.0)],
                config,
            )
            if previous is not None:
                assert fair.fair_value < previous, (
                    "more liquidity on the cheap venue must pull fair value "
                    "toward it, monotonically"
                )
            previous = fair.fair_value

    def test_the_limit_is_the_dominant_venues_own_price(self, config):
        fair = fair_of(
            [venue("A", 100.0, liquidity=1e12), venue("B", 101.0, liquidity=1.0)],
            config,
        )
        assert fair.fair_value == pytest.approx(100.0, abs=1e-6)


class TestE_InputOrderInvariance:
    def test_reordering_inputs_changes_nothing_economic(self, config):
        states = [
            venue("A", 100.0, liquidity=10_000.0),
            venue("B", 101.0, liquidity=90_000.0),
            venue("C", 100.5, liquidity=50_000.0),
        ]
        baseline = fair_of(states, config)
        for permutation in ([2, 0, 1], [1, 2, 0], [2, 1, 0]):
            reordered = fair_of([states[i] for i in permutation], config)
            assert reordered.fair_value == pytest.approx(baseline.fair_value)
            assert reordered.total_liquidity == pytest.approx(
                baseline.total_liquidity
            )
            assert deviations(reordered) == pytest.approx(deviations(baseline))
            assert weights(reordered) == pytest.approx(weights(baseline))

    def test_the_venues_tuple_follows_input_order(self, config):
        """Documented, not a defect: the tuple mirrors the input order, and
        every economic quantity above is order-independent."""
        states = [venue("A", 100.0), venue("B", 101.0)]
        assert [v.venue for v in fair_of(states, config).venues] == ["A", "B"]
        assert [v.venue for v in fair_of(states[::-1], config).venues] == ["B", "A"]


class TestF_PriceScaleInvariance:
    @pytest.mark.parametrize("k", [0.01, 1.0, 100.0, 10_000.0])
    def test_scaling_every_price_scales_fair_value_and_leaves_bps_alone(
        self, config, k
    ):
        base = [
            venue("A", 100.0, liquidity=30_000.0),
            venue("B", 101.0, liquidity=70_000.0),
        ]
        scaled = [
            venue("A", 100.0 * k, liquidity=30_000.0),
            venue("B", 101.0 * k, liquidity=70_000.0),
        ]
        reference = fair_of(base, config)
        result = fair_of(scaled, config)
        assert result.fair_value == pytest.approx(reference.fair_value * k, rel=1e-9)
        assert deviations(result) == pytest.approx(deviations(reference), rel=1e-6)


class TestG_LiquidityScaleInvariance:
    @pytest.mark.parametrize("k", [1e-6, 0.5, 1.0, 1_000.0, 1e9])
    def test_scaling_all_liquidity_moves_neither_fair_value_nor_weights(
        self, config, k
    ):
        reference = fair_of(
            [venue("A", 100.0, liquidity=30_000.0), venue("B", 101.0, liquidity=70_000.0)],
            config,
        )
        scaled = fair_of(
            [
                venue("A", 100.0, liquidity=30_000.0 * k),
                venue("B", 101.0, liquidity=70_000.0 * k),
            ],
            config,
        )
        assert scaled.fair_value == pytest.approx(reference.fair_value)
        assert weights(scaled) == pytest.approx(weights(reference))

    def test_but_total_liquidity_does_scale(self, config):
        """...which is what feeds confidence -- see the confidence suite."""
        small = fair_of([venue("A", 100.0, liquidity=1.0)], config)
        large = fair_of([venue("A", 100.0, liquidity=1e9)], config)
        assert small.total_liquidity < large.total_liquidity


class TestH_ZeroContributors:
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


class TestI_OneContributor:
    def test_one_venue_prices_at_its_own_price_with_zero_deviation(self, config):
        fair = fair_of([venue("A", 100.0)], config)
        assert fair is not None
        assert fair.fair_value == pytest.approx(100.0)
        assert fair.venues[0].deviation_bps == pytest.approx(0.0)
        assert fair.venues[0].weight == pytest.approx(1.0)

    def test_a_one_venue_fair_value_is_tautological(self, config):
        """Mathematically fine, economically empty: a venue compared with
        itself is always exactly at fair value, so it can never be an outlier.
        Recorded here because ``Noro.on_market_state`` stores this and
        ``_heartbeat`` counts the symbol as priced -- see the health suite
        (P3-6)."""
        for price in (1.0, 100.0, 50_000.0):
            fair = fair_of([venue("A", price)], config)
            assert fair.widest_deviation_bps == pytest.approx(0.0)


class TestJ_ZeroLiquidity:
    def test_a_priced_venue_with_no_usable_liquidity_is_excluded(self, config):
        states = [
            venue("A", 100.0, liquidity=50_000.0),
            venue("B", 101.0, liquidity=0.0),
        ]
        fair = fair_of(states, config)
        assert [v.venue for v in fair.venues] == ["A"]
        assert fair.fair_value == pytest.approx(100.0)

    def test_a_one_sided_book_contributes_nothing(self, config):
        """min(bid, ask) is zero when either side is empty -- see the
        liquidity-weighting suite for what that choice means."""
        states = [
            venue("A", 100.0, liquidity=50_000.0),
            venue("B", 101.0, bid_liquidity=1e9, ask_liquidity=0.0),
        ]
        fair = fair_of(states, config)
        assert [v.venue for v in fair.venues] == ["A"]

    def test_every_venue_unusable_returns_none(self, config):
        states = [venue("A", 100.0, liquidity=0.0), venue("B", 101.0, liquidity=0.0)]
        assert compute_fair_value(SYMBOL, states, config) is None


class TestK_NonFiniteSafety:
    @pytest.mark.parametrize(
        ("price", "liquidity"),
        [
            (1e-6, 1e-12),
            (1e12, 1e15),
            (1e-6, 1e15),
            (1e12, 1e-12),
        ],
    )
    def test_extreme_finite_inputs_stay_finite(self, config, price, liquidity):
        fair = fair_of(
            [
                venue("A", price, liquidity=liquidity),
                venue("B", price * 1.001, liquidity=liquidity),
            ],
            config,
        )
        assert fair is not None
        assert math.isfinite(fair.fair_value)
        assert math.isfinite(fair.total_liquidity)
        for valuation in fair.venues:
            assert math.isfinite(valuation.deviation_bps)
            assert math.isfinite(valuation.weight)
            assert valuation.weight > 0, "a weight is a liquidity share; never <= 0"

    def test_prices_differing_below_float_resolution(self, config):
        fair = fair_of(
            [venue("A", 100.0), venue("B", 100.0 * (1 + 1e-9))], config
        )
        assert math.isfinite(fair.fair_value)
        assert all(math.isfinite(v.deviation_bps) for v in fair.venues)

    @pytest.mark.parametrize("count", [2, 50, 100, 500])
    def test_large_venue_counts_stay_sane(self, config, count):
        states = [
            venue(f"V{i}", 100.0 + (i % 7) * 0.01, liquidity=1_000.0 + i)
            for i in range(count)
        ]
        fair = fair_of(states, config)
        assert len(fair.venues) == count
        assert math.isfinite(fair.fair_value)
        assert sum(weights(fair).values()) == pytest.approx(1.0)


class TestL_DeterministicRepeatability:
    def test_the_same_inputs_produce_byte_identical_output(self, config):
        states = [
            venue("A", 100.0, liquidity=33_333.0),
            venue("B", 101.7, liquidity=66_667.0),
            venue("C", 100.9, liquidity=12_345.0),
        ]
        first = fair_of(states, config)
        for _ in range(5):
            again = fair_of(states, config)
            assert again == first, "FairValue is a frozen dataclass; equality is exact"


# ======================================================================
# Section 29 — metamorphic properties
# ======================================================================


class TestMetamorphic:
    def test_adding_a_venue_at_fair_value_barely_moves_it(self, config):
        states = [
            venue("A", 100.0, liquidity=40_000.0),
            venue("B", 101.0, liquidity=60_000.0),
        ]
        before = fair_of(states, config).fair_value
        after = fair_of(
            [*states, venue("C", before, liquidity=50_000.0)], config
        ).fair_value
        assert after == pytest.approx(before, rel=1e-12)

    def test_a_tiny_outlier_has_a_tiny_effect(self, config):
        states = [
            venue("A", 100.0, liquidity=500_000.0),
            venue("B", 100.0, liquidity=500_000.0),
        ]
        before = fair_of(states, config).fair_value
        after = fair_of([*states, venue("C", 200.0, liquidity=1.0)], config).fair_value
        assert abs(after - before) < 1e-3

    def test_a_huge_outlier_moves_fair_value_toward_itself(self, config):
        states = [
            venue("A", 100.0, liquidity=10_000.0),
            venue("B", 100.0, liquidity=10_000.0),
        ]
        before = fair_of(states, config).fair_value
        after = fair_of(
            [*states, venue("C", 200.0, liquidity=10_000_000.0)], config
        ).fair_value
        assert after > before + 90, "a dominant outlier essentially becomes the benchmark"

    def test_removing_an_already_excluded_venue_changes_nothing(self, config):
        good = [venue("A", 100.0), venue("B", 101.0)]
        excluded = venue("C", 500.0, quality=DataQuality.STALE)
        assert fair_of([*good, excluded], config) == fair_of(good, config)

    def test_venue_label_invariance(self, config):
        """Section 37: renaming venues must change labels and nothing else."""
        original = fair_of(
            [venue("VENUE_A", 100.0, liquidity=20_000.0),
             venue("VENUE_B", 101.0, liquidity=80_000.0)],
            config,
        )
        renamed = fair_of(
            [venue("X", 100.0, liquidity=20_000.0),
             venue("Y", 101.0, liquidity=80_000.0)],
            config,
        )
        assert renamed.fair_value == pytest.approx(original.fair_value)
        assert [v.weight for v in renamed.venues] == pytest.approx(
            [v.weight for v in original.venues]
        )
        assert [v.deviation_bps for v in renamed.venues] == pytest.approx(
            [v.deviation_bps for v in original.venues]
        )

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
        assert len(states) == 2


# ======================================================================
# Section 31 — duplicate venue input
# ======================================================================


class TestDuplicateVenues:
    def test_the_low_level_function_double_counts_a_duplicate(self, config):
        """``compute_fair_value`` takes a list and does not deduplicate.

        Recorded as a property of the FUNCTION, not a production defect --
        the next test proves production cannot reach this state.
        """
        single = fair_of([venue("A", 100.0), venue("B", 102.0)], config)
        duplicated = fair_of(
            [venue("A", 100.0), venue("A", 100.0), venue("B", 102.0)], config
        )
        assert duplicated.fair_value < single.fair_value, (
            "the repeated venue got twice the weight"
        )
        assert len(duplicated.venues) == 3

    def test_production_cannot_produce_duplicate_venue_states(self, config):
        """``MarketState.venues`` is a dict keyed ``venue:symbol``, so
        ``states_for()`` yields at most one state per venue. The duplicate
        case above is unreachable from the production path."""
        from tests.audit.helpers import market

        state = market(venue("A", 100.0), venue("A", 105.0), venue("B", 102.0))
        venues = [s.venue for s in state.states_for(SYMBOL)]
        assert sorted(venues) == ["A", "B"]
        assert len(venues) == len(set(venues))
        # ...and the later state wins, which is dict semantics, not a choice
        # NORO makes.
        assert state.venue_state("A", SYMBOL).metrics.mid == pytest.approx(105.0)

    def test_deviation_lookup_returns_the_first_match(self, config):
        """``FairValue.deviation`` scans linearly and returns the first hit,
        so a duplicated venue's second valuation is unreachable by lookup."""
        fair = fair_of([venue("A", 100.0), venue("A", 100.0)], config)
        assert fair.deviation("A") == pytest.approx(fair.venues[0].deviation_bps)
        assert fair.deviation("NOT_PRESENT") is None
