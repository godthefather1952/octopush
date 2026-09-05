"""P3-4 / P3-5 regression: does NORO add information?

The question Phase 3 existed to answer. NORO is a REQUIRED consensus agent
with weight 1.5, so its vote can suspend or carry a trade. That is only worth
paying for if its answer is not already implied by the detector's question.

**What the audit found (P3-4).** It was implied. The detector emits an
opportunity only when the buy venue's ``best_ask`` is strictly below the sell
venue's ``best_bid`` -- that is what a positive gross edge *means* -- so the
two venues' quote intervals are disjoint. ``venue_price`` is a convex blend of
mid and microprice, both of which lie inside ``[best_bid, best_ask]``.
Therefore ``price_buy < price_sell`` for every opportunity the detector can
emit; a fair value formed from those two prices lies strictly between them;
and both confirmations are positive **by construction**. Measured: 0
rejections in 25,027 random two-venue opportunities, and 82 positive entry
votes out of 82 in production.

**What the audit found (P3-5).** Every venue was judged against a benchmark it
was itself part of, weighted by its own raw depth, so a deep venue helped
decide whether it was an outlier -- and at extreme dominance simply became the
benchmark.

**What the remediation did.** The benchmark is built from the venues *not*
participating in the opportunity, and the estimator is a reliability-weighted
median with weights bounded at 1.0. When no independent venue exists, NORO
returns an explicit neutral opinion instead of a confirmation it did not earn.

The audit-only comparators in ``helpers`` (leave-one-out, weighted mean,
median, trimmed mean) are retained to show what the pre-remediation estimator
would have done on the same inputs.
"""

from __future__ import annotations

import pytest

from agents.noro.agent import (
    FAIR_VALUE_CONFIRMS_DISLOCATION,
    FAIR_VALUE_CONTRADICTS_DISLOCATION,
    INSUFFICIENT_INDEPENDENT_VALUATION_BREADTH,
)
from core.config import NoroConfig
from tests.audit.helpers import (
    SYMBOL,
    contributors,
    fair_of,
    independent_of,
    median_price,
    trimmed_mean,
    venue,
    weighted_mean,
)
from tests.audit.noro_fixtures import opinion_for

ANCHOR = 100.0

#: Depth at which every venue is past the reliability saturation point, so
#: contributor weights are all exactly 1.0 and the weighted median reduces to
#: the plain median. Used where a test is about ORDERING, not weighting.
SATURATED = 200_000.0


@pytest.fixture
def config() -> NoroConfig:
    return NoroConfig()


def anchored(buy_bps: float, sell_bps: float, *, liquidity: float = 100_000.0) -> list:
    """A (BUY), B (SELL) and C, an independent anchor at ``ANCHOR``.

    Only C survives the opportunity-venue exclusion, so the benchmark is
    exactly C's price and ``confirmation(A) = buy_bps``,
    ``confirmation(B) = sell_bps``.
    """
    return [
        venue("A", ANCHOR * (1 - buy_bps / 10_000), liquidity=liquidity),
        venue("B", ANCHOR * (1 + sell_bps / 10_000), liquidity=liquidity),
        venue("C", ANCHOR, liquidity=liquidity),
    ]


# ======================================================================
# P3-4 — the two-venue tautology, closed
# ======================================================================


class TestP3_4_TwoVenuesProduceNoConfirmation:
    """The central regression of the whole remediation.

    Before: a two-venue opportunity was confirmed by construction, with a
    near-saturated signal and confidence around 1.0. Now NORO says it has no
    independent evidence, and neither confirms nor contradicts.
    """

    @staticmethod
    def _pair(gap_bps: float = 20.0, liquidity: float = 100_000.0):
        return [
            venue("A", ANCHOR, liquidity=liquidity),
            venue("B", ANCHOR * (1 + gap_bps / 10_000), liquidity=liquidity),
        ]

    def test_noro_still_answers(self, config):
        """Missing is not neutral. NORO is required, so silence would suspend
        the strategy on the commonest market there is."""
        assert opinion_for(self._pair(), config, "A", "B") is not None

    def test_the_signal_is_exactly_neutral(self, config):
        opinion = opinion_for(self._pair(), config, "A", "B")
        assert opinion.signal == 0.0

    def test_the_confidence_is_the_configured_insufficient_breadth_value(self, config):
        opinion = opinion_for(self._pair(), config, "A", "B")
        assert opinion.confidence == pytest.approx(config.insufficient_breadth_confidence)

    def test_the_breadth_reason_code_is_emitted(self, config):
        opinion = opinion_for(self._pair(), config, "A", "B")
        assert INSUFFICIENT_INDEPENDENT_VALUATION_BREADTH in opinion.reason_codes

    def test_no_confirmation_is_claimed(self, config):
        opinion = opinion_for(self._pair(), config, "A", "B")
        assert FAIR_VALUE_CONFIRMS_DISLOCATION not in opinion.reason_codes
        assert FAIR_VALUE_CONTRADICTS_DISLOCATION not in opinion.reason_codes

    @pytest.mark.parametrize("gap_bps", [0.1, 1.0, 4.0, 10.0, 50.0, 100.0, 500.0])
    def test_no_gap_however_wide_produces_a_positive_vote(self, config, gap_bps):
        """The old suite asserted the opposite of this line: that a two-venue
        signal was *never negative* when prices ordered correctly. It is now
        never positive either -- it carries no direction at all."""
        assert opinion_for(self._pair(gap_bps), config, "A", "B").signal == 0.0

    @pytest.mark.parametrize("liquidity", [1_000.0, 100_000.0, 10_000_000.0])
    def test_depth_cannot_manufacture_a_vote(self, config, liquidity):
        assert opinion_for(self._pair(20.0, liquidity), config, "A", "B").signal == 0.0

    def test_there_is_no_independent_benchmark_to_build(self, config):
        assert independent_of(self._pair(), config, exclude=("A", "B")) is None

    def test_the_diagnostic_valuation_still_exists(self, config):
        """The symbol-wide fair value is kept for health and observability;
        it is simply never what an opportunity is judged against."""
        fair = fair_of(self._pair(), config)
        assert fair is not None
        assert {v.venue for v in fair.venues} == {"A", "B"}

    def test_the_opinion_is_marked_as_an_abstention(self, config):
        """The flag consensus actually reads.

        A neutral signal at low confidence is not enough on its own: a
        weighted mean divides by the weights it summed, so an opinion carrying
        weight into the denominator and nothing into the numerator votes
        against whatever the other agents concluded. ``abstain`` is what
        removes it from both sides.
        """
        opinion = opinion_for(self._pair(), config, "A", "B")
        assert opinion.abstain is True

    def test_the_abstention_survives_serialisation(self, config):
        opinion = opinion_for(self._pair(), config, "A", "B")
        assert opinion.to_json_dict()["abstain"] is True


class TestP3_4_AgainstTheRealDetector:
    """The empirical companion, over the same random search the audit ran.

    The old assertion was ``rejected == 0`` -- the tautology, measured. The
    new one is stronger and opposite in spirit: not one of these detector-real
    opportunities produces a directional vote at all.
    """

    @pytest.mark.parametrize("microprice_weight", [0.0, 0.5, 1.0])
    def test_no_two_venue_opportunity_receives_a_directional_vote(
        self, microprice_weight
    ):
        import random

        from strategies.cross_venue.detector import find_dislocation

        config = NoroConfig(microprice_weight=microprice_weight)
        rng = random.Random(11)
        emitted = 0
        directional = 0
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
            if opinion is not None and opinion.signal != 0.0:
                directional += 1
        assert emitted > 1_000, f"only {emitted} opportunities generated"
        assert directional == 0, (
            f"{directional}/{emitted} two-venue opportunities produced a "
            "directional NORO vote; with no independent evidence there is "
            "nothing to be directional about"
        )


# ======================================================================
# P3-4 — with a third venue, NORO becomes informative
# ======================================================================


class TestP3_4_ThreeVenuesCarryInformation:
    def test_case_a_an_independent_anchor_can_confirm(self, config):
        """A below the anchor, B above it: both legs independently supported."""
        opinion = opinion_for(anchored(10.0, 10.0), config, "A", "B")
        assert opinion.signal > 0
        assert FAIR_VALUE_CONFIRMS_DISLOCATION in opinion.reason_codes
        assert opinion.detail["independent_venues"] == "C"

    @pytest.mark.parametrize(
        ("buy_bps", "sell_bps"),
        [(10.0, 10.0), (10.0, -1.0), (-5.0, -5.0), (10.0, 0.0)],
    )
    def test_an_evidenced_verdict_is_never_an_abstention(
        self, config, buy_bps, sell_bps
    ):
        """Confirming, contradicting, and a genuine zero reached on real
        evidence are all VOTES. Only the absence of an independent benchmark
        is an abstention, so each of these participates in consensus
        normally."""
        opinion = opinion_for(anchored(buy_bps, sell_bps), config, "A", "B")
        assert opinion.abstain is False
        assert INSUFFICIENT_INDEPENDENT_VALUATION_BREADTH not in opinion.reason_codes

    def test_a_genuine_zero_on_evidence_is_a_vote_not_an_abstention(self, config):
        """The sharpest case for the distinction: both opinions carry
        ``signal == 0``, and only one of them declines to participate."""
        evidenced = opinion_for(anchored(10.0, 0.0), config, "A", "B")
        no_evidence = opinion_for(
            [venue("A", ANCHOR, liquidity=SATURATED),
             venue("B", ANCHOR * 1.002, liquidity=SATURATED)],
            config,
            "A",
            "B",
        )
        assert evidenced.signal == pytest.approx(0.0, abs=1e-9)
        assert no_evidence.signal == 0.0
        assert evidenced.abstain is False
        assert no_evidence.abstain is True

    def test_case_b_an_independent_anchor_can_contradict(self, config):
        """The anchor sits above BOTH opportunity venues, so the sell leg is
        on the wrong side of it. NORO could not reach this verdict at all
        before the remediation."""
        states = [
            venue("A", 100.00, liquidity=100_000.0),
            venue("B", 100.20, liquidity=100_000.0),
            venue("C", 100.50, liquidity=100_000.0),
        ]
        opinion = opinion_for(states, config, "A", "B")
        assert opinion.signal < 0
        assert FAIR_VALUE_CONTRADICTS_DISLOCATION in opinion.reason_codes

    def test_case_c_a_strong_leg_cannot_outvote_a_weak_contradiction(self, config):
        """P3-2 and P3-4 meeting: the buy leg is confirmed by 30 bps, the sell
        leg contradicted by half a basis point, and the verdict is negative."""
        opinion = opinion_for(anchored(30.0, -0.5), config, "A", "B")
        assert opinion.detail["confirmation_bps_A"] == pytest.approx(30.0, abs=1e-2)
        assert opinion.detail["confirmation_bps_B"] == pytest.approx(-0.5, abs=1e-2)
        assert opinion.signal < 0

    def test_the_same_pair_flips_verdict_with_the_anchor_moved(self, config):
        """The information NORO now adds, isolated: identical opportunity
        venues, one independent venue moved, opposite verdicts."""
        pair = [
            venue("A", 100.00, liquidity=100_000.0),
            venue("B", 100.20, liquidity=100_000.0),
        ]
        below = opinion_for([*pair, venue("C", 100.10, liquidity=100_000.0)], config, "A", "B")
        above = opinion_for([*pair, venue("C", 100.50, liquidity=100_000.0)], config, "A", "B")
        assert below.signal > 0
        assert above.signal < 0

    def test_a_fourth_venue_still_judges_from_outside_the_opportunity(self, config):
        states = [
            venue("A", 100.00, liquidity=100_000.0),
            venue("B", 100.20, liquidity=100_000.0),
            venue("C", 100.08, liquidity=100_000.0),
            venue("D", 100.12, liquidity=100_000.0),
        ]
        opinion = opinion_for(states, config, "A", "B")
        assert opinion.detail["independent_venues"] == "C,D"
        assert opinion.detail["independent_contributors"] == 2
        assert opinion.signal > 0


# ======================================================================
# P3-5 — self-inclusion is structurally impossible
# ======================================================================


class TestP3_5_SelfInclusionIsClosed:
    @staticmethod
    def _four():
        return [
            venue("A", 100.00, liquidity=100_000.0),
            venue("B", 100.20, liquidity=100_000.0),
            venue("C", 100.08, liquidity=100_000.0),
            venue("D", 100.12, liquidity=100_000.0),
        ]

    def test_opportunity_venues_are_absent_from_the_benchmark(self, config):
        benchmark = independent_of(self._four(), config, exclude=("A", "B"))
        assert [v.venue for v in benchmark.venues] == ["C", "D"]

    def test_the_agent_reports_the_same_contributor_set(self, config):
        opinion = opinion_for(self._four(), config, "A", "B")
        independent = opinion.detail["independent_venues"].split(",")
        assert independent == ["C", "D"]
        assert "A" not in independent and "B" not in independent

    @pytest.mark.parametrize("depth", [1_000.0, 100_000.0, 50_000_000.0])
    def test_changing_an_opportunity_venues_depth_cannot_move_the_benchmark(
        self, config, depth
    ):
        """P3-5's core property. A venue under judgement has no influence on
        the number judging it, however deep it is."""
        states = [
            venue("A", 100.00, liquidity=depth),
            venue("B", 100.20, liquidity=depth),
            venue("C", 100.08, liquidity=100_000.0),
            venue("D", 100.12, liquidity=100_000.0),
        ]
        benchmark = independent_of(states, config, exclude=("A", "B"))
        reference = independent_of(self._four(), config, exclude=("A", "B"))
        assert benchmark.fair_value == pytest.approx(reference.fair_value)

    @pytest.mark.parametrize("price", [50.0, 100.0, 100.2, 500.0])
    def test_changing_an_opportunity_venues_price_cannot_move_it_either(
        self, config, price
    ):
        states = [
            venue("A", price, liquidity=100_000.0),
            venue("B", 100.20, liquidity=100_000.0),
            venue("C", 100.08, liquidity=100_000.0),
            venue("D", 100.12, liquidity=100_000.0),
        ]
        benchmark = independent_of(states, config, exclude=("A", "B"))
        assert benchmark.fair_value == pytest.approx(100.10, abs=1e-6), (
            "the benchmark is C and D, and only C and D"
        )

    def test_an_outlier_can_no_longer_become_its_own_benchmark(self, config):
        """The old extreme case: a venue holding 10M of depth against two
        venues holding 1k each *became* fair value, and the two agreeing
        venues were reported as the outliers.

        Bounded weights end that wherever the other venues are themselves
        credible. Note what the bound does and does not claim: it caps
        *influence*, so a 10,000x depth advantage buys the same single vote as
        anyone else past saturation. It does not promote a venue with almost
        no near-touch depth, which remains genuinely weak evidence.
        """
        states = [
            venue("A", 100.0, liquidity=SATURATED),
            venue("B", 100.0, liquidity=SATURATED),
            venue("C", 1_000.0, liquidity=10_000_000.0),
        ]
        fair = fair_of(states, config)
        assert fair.fair_value == pytest.approx(100.0), (
            "two agreeing venues outvote one enormous outlier"
        )
        assert abs(fair.deviation_of(1_000.0)) > 8_000, "C is the outlier, and says so"


# ======================================================================
# The estimator: a reliability-weighted median
# ======================================================================


class TestWeightedMedianRobustness:
    @staticmethod
    def _outlier_market():
        # Two venues clustered near 100 and one far outlier at 150 holding
        # 25x their depth. This is the shape that broke the weighted mean.
        return [
            venue("A", 100.0, liquidity=SATURATED),
            venue("B", 100.1, liquidity=SATURATED),
            venue("C", 150.0, liquidity=5_000_000.0),
        ]

    def test_the_benchmark_stays_with_the_cluster(self, config):
        fair = fair_of(self._outlier_market(), config)
        assert fair.fair_value == pytest.approx(100.1)

    def test_the_pre_remediation_estimator_would_have_followed_the_outlier(
        self, config
    ):
        """Evidence preserved: the old liquidity-weighted mean, run on the
        same inputs, lands near the outlier. That is P3-5, quantified."""
        items = contributors(self._outlier_market(), config)
        assert weighted_mean(items) > 140.0
        assert median_price(items) == pytest.approx(100.1)

    @pytest.mark.parametrize("outlier_depth", [1e5, 1e6, 1e8, 1e12])
    def test_raw_depth_cannot_drag_the_benchmark_continuously(
        self, config, outlier_depth
    ):
        """The mean moved with depth in proportion. The median does not move
        at all: past saturation the outlier's weight is 1.0, the same as
        everyone else's."""
        states = [
            venue("A", 100.0, liquidity=SATURATED),
            venue("B", 100.1, liquidity=SATURATED),
            venue("C", 150.0, liquidity=outlier_depth),
        ]
        assert fair_of(states, config).fair_value == pytest.approx(100.1)

    def test_the_result_is_invariant_under_input_permutation(self, config):
        import itertools

        states = self._outlier_market()
        values = {
            fair_of(list(order), config).fair_value
            for order in itertools.permutations(states)
        }
        assert len(values) == 1, f"order-dependent benchmark: {values}"

    def test_the_contributor_ordering_is_deterministic(self, config):
        import itertools

        states = self._outlier_market()
        orders = {
            tuple(v.venue for v in fair_of(list(order), config).venues)
            for order in itertools.permutations(states)
        }
        assert orders == {("A", "B", "C")}

    def test_ties_in_price_are_broken_by_venue_name(self, config):
        """Deterministic tie handling: identical prices cannot make the result
        depend on which venue happened to be seen first."""
        states = [
            venue("Z", 100.0, liquidity=SATURATED),
            venue("A", 100.0, liquidity=SATURATED),
            venue("M", 100.0, liquidity=SATURATED),
        ]
        fair = fair_of(states, config)
        assert [v.venue for v in fair.venues] == ["A", "M", "Z"]
        assert fair.fair_value == pytest.approx(100.0)

    def test_the_benchmark_always_lies_within_the_contributor_range(self, config):
        for spec in (
            [(100.0, SATURATED)],
            [(100.0, SATURATED), (101.0, SATURATED)],
            [(100.0, 1_000.0), (101.0, 5_000_000.0)],
            [(90.0, 1_000.0), (100.0, SATURATED), (150.0, 5_000_000.0)],
            [(100.0, 1e3), (100.5, 1e4), (101.0, 1e5), (200.0, 1e9)],
        ):
            states = [
                venue(f"V{i}", price, liquidity=liq)
                for i, (price, liq) in enumerate(spec)
            ]
            fair = fair_of(states, config)
            prices = [v.price for v in fair.venues]
            assert min(prices) <= fair.fair_value <= max(prices)

    def test_a_single_contributor_returns_its_own_price(self, config):
        fair = fair_of([venue("A", 123.45, liquidity=SATURATED)], config)
        assert fair.fair_value == pytest.approx(123.45)
        assert fair.dispersion_bps == pytest.approx(0.0)

    def test_two_equally_reliable_contributors_give_the_midpoint(self, config):
        fair = fair_of(
            [venue("A", 100.0, liquidity=SATURATED), venue("B", 102.0, liquidity=SATURATED)],
            config,
        )
        assert fair.fair_value == pytest.approx(101.0)


class TestComparatorBenchmarks:
    """Audit-only comparison, retained from the Phase 3 evidence.

    These measure how far apart the candidate estimators are on the shape that
    motivated the change. Production now uses the reliability-weighted median;
    none of the others may be wired in.
    """

    @pytest.mark.parametrize("count", [2, 3, 5, 10])
    def test_without_an_outlier_every_benchmark_agrees(self, config, count):
        states = [
            venue(f"V{i}", 100.0 + i * 0.001, liquidity=100_000.0)
            for i in range(count)
        ]
        items = contributors(states, config)
        assert weighted_mean(items) == pytest.approx(median_price(items), abs=0.01)
        assert fair_of(states, config).fair_value == pytest.approx(
            median_price(items), abs=0.01
        )

    def test_with_a_dominant_outlier_production_now_sides_with_the_median(
        self, config
    ):
        states = [
            venue("A", 100.0, liquidity=SATURATED),
            venue("B", 100.0, liquidity=SATURATED),
            venue("C", 100.0, liquidity=SATURATED),
            venue("D", 100.0, liquidity=SATURATED),
            venue("E", 150.0, liquidity=5_000_000.0),
        ]
        items = contributors(states, config)
        assert weighted_mean(items) > 140, "the pre-remediation estimator followed it"
        assert median_price(items) == pytest.approx(100.0)
        assert trimmed_mean(items) == pytest.approx(100.0)
        assert fair_of(states, config).fair_value == pytest.approx(100.0), (
            "production ignores it now"
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
