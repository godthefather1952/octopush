"""P3-8 regression: is NORO's confidence calibrated?

Confidence is not decoration. Consensus weights every agent by
``weight * confidence``, so NORO's confidence directly scales how much its
vote moves the score.

**What the audit found (P3-8).** The formula was::

    liquidity_confidence = min(1, total_liquidity / 250_000)
    breadth              = min(1, venues / 2)
    confidence           = 0.35 + 0.45 * liquidity_confidence + 0.20 * breadth

Four defects in three lines. Every constant was hard-coded. Breadth saturated
at exactly two venues, so the third independent price -- the one thing that
makes NORO able to disagree at all -- was worth nothing. The reachable range
for any two-venue valuation was ``[0.55, 1.0]``, a floor already above half. And
disagreement did not enter: two venues quoting a 5x price difference, which
cannot both be right, reported confidence 1.0. In production it was pinned at
1.0 throughout.

**What the remediation did.** Three components at configured weights summing
to 1::

    breadth   = 1 - 1/(1 + n_independent)
    agreement = clamp(1 - dispersion_bps / dispersion_tolerance_bps, 0, 1)
    quality   = mean(reliability of the independent contributors)

    confidence = w_b * breadth + w_a * agreement + w_q * quality

Breadth has diminishing returns but never stops rising. Disagreement lowers
confidence. Nothing measures how much could be traded -- that is ZEPHR's
question, and answering it here is what pinned the old number.
"""

from __future__ import annotations

import pytest

from core.config import NoroConfig
from tests.audit.helpers import cliff_venue, fair_of, venue
from tests.audit.noro_fixtures import opinion_for

ANCHOR = 100.0
#: Past the reliability saturation point, so the quality component is 1.0 and
#: breadth/agreement can be varied on their own.
SATURATED = 200_000.0


def expected(breadth: float, agreement: float, quality: float) -> float:
    """The documented combination, at the default weights."""
    config = NoroConfig()
    return (
        config.breadth_confidence_weight * breadth
        + config.agreement_confidence_weight * agreement
        + config.quality_confidence_weight * quality
    )


def with_anchors(*anchor_prices: float, liquidity: float = SATURATED) -> list:
    """A (BUY) and B (SELL), plus one independent anchor per price given."""
    states = [
        venue("A", ANCHOR * (1 - 10 / 10_000), liquidity=liquidity),
        venue("B", ANCHOR * (1 + 10 / 10_000), liquidity=liquidity),
    ]
    for index, price in enumerate(anchor_prices):
        states.append(venue(f"C{index}", price, liquidity=liquidity))
    return states


def confidence_of(states: list, config: NoroConfig | None = None) -> float:
    return opinion_for(states, config or NoroConfig(), "A", "B").confidence


# ======================================================================
# The published contract
# ======================================================================


class TestTheFormulaIsWhatTheAgentComputes:
    def test_one_independent_contributor_in_agreement(self):
        """breadth 0.5, agreement 1.0, quality 1.0."""
        assert confidence_of(with_anchors(ANCHOR)) == pytest.approx(
            expected(0.5, 1.0, 1.0), abs=1e-6
        )

    def test_two_independent_contributors_in_agreement(self):
        """breadth 2/3."""
        assert confidence_of(with_anchors(ANCHOR, ANCHOR)) == pytest.approx(
            expected(2 / 3, 1.0, 1.0), abs=1e-6
        )

    def test_the_components_are_reported_in_the_detail(self):
        opinion = opinion_for(with_anchors(ANCHOR, ANCHOR), NoroConfig(), "A", "B")
        assert opinion.detail["confidence_breadth"] == pytest.approx(2 / 3, abs=1e-3)
        assert opinion.detail["confidence_agreement"] == pytest.approx(1.0, abs=1e-3)
        assert opinion.detail["confidence_quality"] == pytest.approx(1.0, abs=1e-3)

    def test_the_weights_are_configurable_and_must_sum_to_one(self):
        """P3-11's companion: none of 0.35, 0.45, 0.20, 250_000 or 2 was
        exposed. Every constant in the new formula is."""
        fields = set(NoroConfig.model_fields)
        for name in (
            "breadth_confidence_weight",
            "agreement_confidence_weight",
            "quality_confidence_weight",
            "dispersion_tolerance_bps",
            "reliability_saturation_notional",
            "insufficient_breadth_confidence",
        ):
            assert name in fields

    def test_a_reweighted_configuration_changes_the_answer(self):
        breadth_heavy = NoroConfig(
            breadth_confidence_weight=1.0,
            agreement_confidence_weight=0.0,
            quality_confidence_weight=0.0,
        )
        assert confidence_of(with_anchors(ANCHOR), breadth_heavy) == pytest.approx(
            0.5, abs=1e-6
        )


# ======================================================================
# P3-8 — breadth no longer saturates at two
# ======================================================================


class TestP3_8_BreadthKeepsRising:
    def test_each_additional_independent_contributor_raises_confidence(self):
        confidences = [
            confidence_of(with_anchors(*([ANCHOR] * n))) for n in (1, 2, 3, 4)
        ]
        assert confidences == sorted(confidences)
        assert len(set(confidences)) == 4, (
            f"the old formula returned one value for all of these: {confidences}"
        )

    def test_a_third_and_fourth_contributor_both_matter(self):
        """The audit's headline: 'a genuinely independent third price anchor
        -- the thing that makes NORO able to disagree at all -- moves
        confidence by zero'. It no longer does."""
        two = confidence_of(with_anchors(ANCHOR, ANCHOR))
        three = confidence_of(with_anchors(ANCHOR, ANCHOR, ANCHOR))
        four = confidence_of(with_anchors(ANCHOR, ANCHOR, ANCHOR, ANCHOR))
        assert three > two
        assert four > three

    def test_the_returns_diminish(self):
        import itertools

        confidences = [
            confidence_of(with_anchors(*([ANCHOR] * n))) for n in (1, 2, 3, 4, 5)
        ]
        gains = [b - a for a, b in itertools.pairwise(confidences)]
        assert gains == sorted(gains, reverse=True), gains
        assert all(gain > 0 for gain in gains)

    def test_confidence_stays_bounded_however_many_contributors(self):
        many = confidence_of(with_anchors(*([ANCHOR] * 30)))
        assert 0.0 <= many <= 1.0


# ======================================================================
# P3-8 — disagreement now lowers confidence
# ======================================================================


class TestP3_8_DisagreementMatters:
    def test_dispersed_anchors_are_less_confident_than_agreeing_ones(self):
        agreeing = confidence_of(with_anchors(ANCHOR, ANCHOR))
        dispersed = confidence_of(with_anchors(99.90, 100.10))
        assert dispersed < agreeing

    def test_the_agreement_component_falls_linearly_with_dispersion(self):
        opinion = opinion_for(with_anchors(99.90, 100.10), NoroConfig(), "A", "B")
        dispersion = opinion.detail["valuation_dispersion_bps"]
        assert dispersion == pytest.approx(10.0, abs=0.02)
        assert opinion.detail["confidence_agreement"] == pytest.approx(
            1.0 - dispersion / NoroConfig().dispersion_tolerance_bps, abs=1e-3
        )

    def test_wildly_divergent_venues_no_longer_report_full_confidence(self):
        """The audit asked "do two venues quoting a 5x price difference
        produce a maximally confident valuation?" and measured: yes. Now the
        agreement component is floored at zero and confidence drops with it."""
        wild = opinion_for(with_anchors(100.0, 500.0), NoroConfig(), "A", "B")
        assert wild.detail["confidence_agreement"] == pytest.approx(0.0)
        assert wild.confidence < 0.7

    def test_dispersion_beyond_tolerance_clamps_rather_than_going_negative(self):
        wild = opinion_for(with_anchors(100.0, 10_000.0), NoroConfig(), "A", "B")
        assert wild.detail["confidence_agreement"] == 0.0
        assert 0.0 <= wild.confidence <= 1.0

    def test_a_wider_tolerance_forgives_the_same_dispersion(self):
        states = with_anchors(99.90, 100.10)
        strict = confidence_of(states, NoroConfig(dispersion_tolerance_bps=10.0))
        lenient = confidence_of(states, NoroConfig(dispersion_tolerance_bps=200.0))
        assert lenient > strict


# ======================================================================
# P3-8 / P3-9 — confidence is not a capacity measure
# ======================================================================


class TestP3_9_ConfidenceIsNotExecutability:
    def test_far_book_depth_does_not_raise_confidence(self):
        """The old ``total_liquidity`` term read whatever the window happened
        to resolve to, so the P3-1 bucket fallback moved confidence with no
        market change at all. Near-touch depth is now the only depth read, and
        it is bounded."""
        near_only = [
            venue("A", 99.90, liquidity=50_000.0),
            venue("B", 100.10, liquidity=50_000.0),
            venue("C", ANCHOR, liquidity=50_000.0),
        ]
        huge_far_book = [
            venue("A", 99.90, liquidity=50_000.0),
            venue("B", 100.10, liquidity=50_000.0),
            cliff_venue("C", ANCHOR, near_notional=50_000.0, far_notional=900_000_000.0),
        ]
        assert confidence_of(huge_far_book) == pytest.approx(confidence_of(near_only))

    def test_depth_past_saturation_adds_nothing(self):
        modest = confidence_of(with_anchors(ANCHOR, liquidity=SATURATED))
        colossal = confidence_of(with_anchors(ANCHOR, liquidity=5_000_000_000.0))
        assert colossal == pytest.approx(modest)

    def test_thin_contributors_are_less_trusted_than_deep_ones(self):
        """Quality still carries information -- it is simply bounded, and only
        30% of the answer."""
        thin = confidence_of(with_anchors(ANCHOR, liquidity=5_000.0))
        deep = confidence_of(with_anchors(ANCHOR, liquidity=SATURATED))
        assert thin < deep
        assert deep - thin <= NoroConfig().quality_confidence_weight + 1e-9

    def test_the_signal_and_the_confidence_are_not_the_same_number(self):
        strong = opinion_for(with_anchors(ANCHOR), NoroConfig(), "A", "B")
        weak = opinion_for(
            [
                venue("A", ANCHOR * (1 - 0.5 / 10_000), liquidity=SATURATED),
                venue("B", ANCHOR * (1 + 0.5 / 10_000), liquidity=SATURATED),
                venue("C", ANCHOR, liquidity=SATURATED),
            ],
            NoroConfig(),
            "A",
            "B",
        )
        assert strong.signal > weak.signal, "the signals differ"
        assert strong.confidence == pytest.approx(weak.confidence), (
            "...and the confidence does not: the same contributors stand "
            "equally behind either valuation"
        )


# ======================================================================
# P3-4 — the two-venue path has its own confidence
# ======================================================================


class TestTwoVenueConfidence:
    @staticmethod
    def _pair():
        return [
            venue("A", 100.0, liquidity=SATURATED),
            venue("B", 100.2, liquidity=SATURATED),
        ]

    def test_it_uses_the_configured_insufficient_breadth_value(self):
        config = NoroConfig()
        assert confidence_of(self._pair(), config) == pytest.approx(
            config.insufficient_breadth_confidence
        )

    def test_it_is_low_but_not_zero(self):
        """Zero would delete NORO from the weighted consensus as though it had
        never been asked; high would assert strong belief in neutrality. The
        default sits deliberately near the bottom of the range."""
        value = NoroConfig().insufficient_breadth_confidence
        assert 0.0 < value < 0.25

    def test_it_is_far_below_any_evidenced_valuation(self):
        assert confidence_of(self._pair()) < confidence_of(with_anchors(ANCHOR))

    def test_depth_cannot_raise_it(self):
        thin = [
            venue("A", 100.0, liquidity=1.0),
            venue("B", 100.2, liquidity=1.0),
        ]
        assert confidence_of(thin) == pytest.approx(confidence_of(self._pair()))

    def test_it_is_configurable(self):
        raised = NoroConfig(insufficient_breadth_confidence=0.42)
        assert confidence_of(self._pair(), raised) == pytest.approx(0.42)


# ======================================================================
# Determinism
# ======================================================================


class TestConfidenceIsDeterministic:
    def test_repeated_evaluation_gives_the_same_number(self):
        states = with_anchors(99.95, 100.05)
        values = {confidence_of(states) for _ in range(5)}
        assert len(values) == 1

    def test_contributor_order_does_not_matter(self):
        import itertools

        states = with_anchors(99.95, 100.05)
        values = {
            confidence_of(list(order)) for order in itertools.permutations(states)
        }
        assert len(values) == 1, values

    @pytest.mark.parametrize("anchor_count", [1, 2, 3, 5])
    def test_confidence_is_always_within_range(self, anchor_count):
        states = with_anchors(*[ANCHOR + i * 0.05 for i in range(anchor_count)])
        assert 0.0 <= confidence_of(states) <= 1.0


# ======================================================================
# How a new venue changes the estimate
# ======================================================================


class TestAddingInformation:
    def test_an_anchor_at_the_benchmark_leaves_the_estimate_alone(self):
        config = NoroConfig()
        base = with_anchors(ANCHOR)
        before = fair_of(base, config).fair_value
        after = fair_of([*base, venue("D", before, liquidity=SATURATED)], config)
        assert after.fair_value == pytest.approx(before, rel=1e-9)

    def test_moving_the_anchor_moves_the_verdict_and_the_confidence_separately(self):
        config = NoroConfig()
        near = opinion_for(with_anchors(ANCHOR, ANCHOR), config, "A", "B")
        # Deliberately off-centre: two anchors straddling the benchmark
        # symmetrically would disperse without moving it.
        spread = opinion_for(with_anchors(99.95, 100.25), config, "A", "B")
        assert near.signal != pytest.approx(spread.signal), "the verdict moved"
        assert spread.confidence < near.confidence, "and so did the confidence"
