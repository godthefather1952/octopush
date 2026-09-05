"""Phase 3 audit, Sections 21 / 22 / 54: is NORO's confidence calibrated?

Confidence is not decoration. Consensus weights every agent by
``weight * confidence``, so NORO's confidence directly scales how much its
(structurally near-tautological) two-venue vote moves the score.

The formula::

    liquidity_confidence = min(1, total_liquidity / 250_000)
    breadth              = min(1, venues / 2)
    confidence           = 0.35 + 0.45 * liquidity_confidence + 0.20 * breadth

Every constant in it -- the 0.35 floor, the $250,000 saturation, the venue
count of 2 -- is hard-coded in ``agents/noro/agent.py``; none is configurable.
This suite measures what that produces rather than arguing about it.
"""

from __future__ import annotations

import pytest

from core.config import NoroConfig
from tests.audit.helpers import fair_of, venue
from tests.audit.noro_fixtures import opinion_for


def confidence_of(total_liquidity: float, venue_count: int) -> float:
    """The production formula, isolated for the calibration table."""
    liquidity_confidence = min(1.0, total_liquidity / 250_000.0)
    breadth = min(1.0, venue_count / 2.0)
    return max(0.0, min(1.0, 0.35 + 0.45 * liquidity_confidence + 0.2 * breadth))


def measured(total_liquidity: float, venue_count: int) -> float:
    """The same number, produced by the REAL agent.

    Liquidity is split evenly and every venue is priced identically, so the
    only thing varying is what the formula reads.
    """
    per_venue = total_liquidity / venue_count
    states = [
        venue(f"V{i}", 100.0, liquidity=per_venue) for i in range(venue_count)
    ]
    # A trade needs two distinct legs; with one venue NORO returns None, so the
    # single-venue row is measured through FairValue instead.
    if venue_count == 1:
        fair = fair_of(states, NoroConfig())
        return confidence_of(fair.total_liquidity, len(fair.venues))
    opinion = opinion_for(states, NoroConfig(), "V0", "V1")
    return opinion.confidence


class TestTheFormulaIsWhatTheAgentComputes:
    @pytest.mark.parametrize(
        ("total_liquidity", "venue_count"),
        [
            (1_000.0, 2), (100_000.0, 2), (250_000.0, 2), (1_000_000.0, 2),
            (100_000.0, 3), (100_000.0, 5),
        ],
    )
    def test_the_isolated_formula_matches_the_agent(self, total_liquidity, venue_count):
        assert measured(total_liquidity, venue_count) == pytest.approx(
            confidence_of(total_liquidity, venue_count)
        )


class TestCalibrationTable:
    """Section 21's full grid, as assertions on the shape it produces."""

    LIQUIDITY = [
        0.000001, 1.0, 10.0, 100.0, 1_000.0, 5_000.0, 10_000.0, 25_000.0,
        50_000.0, 100_000.0, 250_000.0, 500_000.0, 1_000_000.0, 10_000_000.0,
    ]
    VENUES = [1, 2, 3, 4, 10]

    def test_the_floor_is_reached_at_negligible_liquidity_with_one_venue(self):
        assert confidence_of(0.000001, 1) == pytest.approx(0.45, abs=1e-6), (
            "0.35 base + 0.10 half-breadth: a single venue with a millionth "
            "of a dollar behind it is already 0.45 confident"
        )

    def test_two_venues_with_negligible_liquidity_are_already_over_half(self):
        """Question 3: does almost-zero two-venue liquidity still yield ~0.55?"""
        assert confidence_of(0.000001, 2) == pytest.approx(0.55, abs=1e-6)
        assert confidence_of(1.0, 2) == pytest.approx(0.55, abs=1e-5)

    def test_the_measured_agent_agrees_that_a_dollar_is_worth_055(self):
        """Through the real agent, not the isolated formula."""
        states = [
            venue("A", 100.0, liquidity=0.5),
            venue("B", 100.2, liquidity=0.5),
        ]
        opinion = opinion_for(states, NoroConfig(), "A", "B")
        assert opinion.confidence == pytest.approx(0.55, abs=1e-4), (
            "one dollar of two-sided depth across two venues produces a "
            "0.55-confidence valuation carrying NORO's full consensus weight"
        )

    def test_breadth_saturates_at_two_venues(self):
        """Questions 1, 2 and 12: a third independent venue adds nothing."""
        for count in (2, 3, 4, 10):
            assert confidence_of(100_000.0, count) == pytest.approx(
                confidence_of(100_000.0, 2)
            )

    def test_a_third_venue_does_not_raise_confidence_at_all(self):
        """Measured through the agent, holding total liquidity constant --
        the case Section 54 asks about."""
        two = [
            venue("A", 100.0, liquidity=50_000.0),
            venue("B", 100.2, liquidity=50_000.0),
        ]
        three = [
            venue("A", 100.0, liquidity=33_333.3),
            venue("B", 100.2, liquidity=33_333.3),
            venue("C", 100.1, liquidity=33_333.4),
        ]
        c2 = opinion_for(two, NoroConfig(), "A", "B").confidence
        c3 = opinion_for(three, NoroConfig(), "A", "B").confidence
        assert c3 == pytest.approx(c2, abs=1e-6), (
            "a genuinely independent third price anchor -- the thing that "
            "makes NORO able to disagree at all -- moves confidence by zero"
        )

    def test_liquidity_saturates_at_a_quarter_million_dollars(self):
        """Questions 5 and 7: the constant is absolute, and shared by every
        symbol. $250k is a different fraction of the book for BTC than for a
        thin altcoin, and nothing scales it."""
        assert confidence_of(250_000.0, 2) == pytest.approx(1.0)
        assert confidence_of(1_000_000.0, 2) == pytest.approx(1.0)
        assert confidence_of(10_000_000.0, 2) == pytest.approx(1.0)

    def test_the_reachable_range_is_narrow(self):
        """The practical finding: for any two-venue valuation, confidence
        lives in [0.55, 1.0] -- a 0.45-wide band whose floor is already above
        half."""
        values = [
            confidence_of(liq, count)
            for liq in self.LIQUIDITY
            for count in self.VENUES
            if count >= 2
        ]
        assert min(values) == pytest.approx(0.55, abs=1e-5)
        assert max(values) == pytest.approx(1.0)

    def test_the_configuration_cannot_change_any_of_it(self):
        """Question 6: none of 0.35, 0.45, 0.20, 250_000 or 2 is exposed."""
        fields = set(NoroConfig.model_fields)
        assert fields == {
            "liquidity_window_bps", "microprice_weight", "ttl_ms", "saturation_bps"
        }
        for name in ("confidence", "liquidity_saturation", "breadth"):
            assert not any(name in field for field in fields)


class TestConfidenceIgnoresDisagreement:
    """Section 22: is confidence "enough data" or "a strong belief"?"""

    @staticmethod
    def _agreeing():
        return [
            venue("A", 100.000, liquidity=125_000.0),
            venue("B", 100.010, liquidity=125_000.0),
        ]

    @staticmethod
    def _disagreeing():
        return [
            venue("A", 100.0, liquidity=125_000.0),
            venue("B", 101.0, liquidity=125_000.0),
        ]

    def test_identical_liquidity_and_breadth_give_identical_confidence(self):
        config = NoroConfig()
        agree = opinion_for(self._agreeing(), config, "A", "B")
        disagree = opinion_for(self._disagreeing(), config, "A", "B")
        assert agree.confidence == pytest.approx(disagree.confidence)
        assert agree.confidence == pytest.approx(1.0)

    def test_even_though_the_valuations_disagree_by_a_hundred_times(self):
        config = NoroConfig()
        agree = fair_of(self._agreeing(), config)
        disagree = fair_of(self._disagreeing(), config)
        assert agree.widest_deviation_bps == pytest.approx(0.5, abs=0.01)
        assert disagree.widest_deviation_bps == pytest.approx(49.75, abs=0.5)
        assert disagree.widest_deviation_bps > 90 * agree.widest_deviation_bps

    def test_wildly_divergent_venues_still_report_full_confidence(self):
        """Question 10, answered: yes."""
        states = [
            venue("A", 100.0, liquidity=200_000.0),
            venue("B", 500.0, liquidity=200_000.0),
        ]
        opinion = opinion_for(states, NoroConfig(), "A", "B")
        assert opinion.confidence == pytest.approx(1.0), (
            "two venues quoting a 5x price difference -- which cannot both be "
            "right -- produce a maximally confident valuation"
        )

    def test_confidence_measures_data_volume_not_valuation_quality(self):
        """The finding stated as a property: confidence is a monotone
        function of (total_liquidity, venue_count) alone. Nothing about
        agreement, spread, staleness or contributor age enters it."""
        import inspect

        from agents.noro import agent as noro_agent

        source = inspect.getsource(noro_agent.Noro.evaluate)
        confidence_block = source[source.index("liquidity_confidence") :]
        confidence_block = confidence_block[: confidence_block.index("self.evaluations")]
        for forbidden in ("deviation", "widest", "spread", "quality", "age"):
            assert forbidden not in confidence_block, (
                f"confidence unexpectedly reads {forbidden!r}"
            )


class TestLiquidityIsCountedTwice:
    """Question 8: the same number weights the benchmark and rates it."""

    def test_one_liquidity_number_drives_both_weight_and_confidence(self):
        thin = [
            venue("A", 100.0, liquidity=1_000.0),
            venue("B", 100.2, liquidity=1_000.0),
        ]
        deep = [
            venue("A", 100.0, liquidity=500_000.0),
            venue("B", 100.2, liquidity=500_000.0),
        ]
        config = NoroConfig()
        thin_opinion = opinion_for(thin, config, "A", "B")
        deep_opinion = opinion_for(deep, config, "A", "B")
        # Identical prices and identical relative weights...
        assert thin_opinion.signal == pytest.approx(deep_opinion.signal, rel=1e-6)
        # ...but very different confidence, from the same inputs.
        assert thin_opinion.confidence == pytest.approx(0.5536, abs=1e-3)
        assert deep_opinion.confidence == pytest.approx(1.0)

    def test_so_a_window_change_moves_confidence_without_touching_prices(self):
        """Ties back to P3-1: the bucket-fallback discontinuity changes
        ``total_liquidity``, and therefore confidence, with no market change
        whatsoever."""
        from tests.audit.helpers import cliff_venue

        states = [
            cliff_venue("A", 100.0, near_notional=2_000.0, far_notional=900_000.0),
            cliff_venue("B", 100.2, near_notional=2_000.0, far_notional=900_000.0),
        ]
        at_bucket = opinion_for(states, NoroConfig(liquidity_window_bps=10.0), "A", "B")
        past_bucket = opinion_for(
            states, NoroConfig(liquidity_window_bps=10.001), "A", "B"
        )
        assert at_bucket.confidence == pytest.approx(0.5572, abs=1e-3)
        assert past_bucket.confidence == pytest.approx(1.0)
        assert past_bucket.signal == pytest.approx(at_bucket.signal, rel=1e-6), (
            "the prices did not move at all"
        )


class TestAddingInformation:
    """Section 54: how fair value responds to a new venue."""

    def test_a_third_venue_at_fair_value_barely_moves_it(self):
        config = NoroConfig()
        base = [
            venue("A", 100.0, liquidity=50_000.0),
            venue("B", 100.2, liquidity=50_000.0),
        ]
        before = fair_of(base, config).fair_value
        after = fair_of([*base, venue("C", before, liquidity=50_000.0)], config)
        assert after.fair_value == pytest.approx(before, rel=1e-12)

    def test_a_third_venue_above_pulls_it_up_and_below_pulls_it_down(self):
        config = NoroConfig()
        base = [
            venue("A", 100.0, liquidity=50_000.0),
            venue("B", 100.2, liquidity=50_000.0),
        ]
        before = fair_of(base, config).fair_value
        above = fair_of([*base, venue("C", 101.0, liquidity=50_000.0)], config)
        below = fair_of([*base, venue("C", 99.0, liquidity=50_000.0)], config)
        assert above.fair_value > before
        assert below.fair_value < before

    def test_but_none_of_it_changes_confidence(self):
        """The asymmetry worth recording: a third venue changes the ESTIMATE
        materially and its reported reliability not at all."""
        config = NoroConfig()
        base = [
            venue("A", 100.0, liquidity=125_000.0),
            venue("B", 100.2, liquidity=125_000.0),
        ]
        two = opinion_for(base, config, "A", "B")
        three = opinion_for(
            [*base, venue("C", 105.0, liquidity=125_000.0)], config, "A", "B"
        )
        assert two.confidence == pytest.approx(1.0)
        assert three.confidence == pytest.approx(1.0)
        assert three.signal != pytest.approx(two.signal), (
            "the third venue moved the verdict, not the confidence in it"
        )
