"""P3-2 / P3-3 regression: what the signal means, and what saturates it.

**What the audit found (P3-2).** ``Noro.evaluate`` computed::

    edge_bps = min(confirmations) + sum(confirmations) / len(confirmations)

under a comment claiming "the weakest leg governs, so a single rich venue
cannot carry the trade". It did not. Adding the mean let a strong leg pay for
a contradicting one: ``[-1, +10]`` came out at ``+3.5``, positive, and with
two legs a strong leg of size *s* outvoted any contradiction down to ``-s/3``.

**What the audit found (P3-3).** ``saturation_bps`` was documented as "the
deviation, in bps, at which the signal saturates to |1|", but the quantity it
divided was that doubled aggregate, so symmetric confirmations saturated at
*half* the configured value -- 7.5 bps per leg with ``saturation_bps=15``.

**What the remediation did.** ``confirmed_edge_bps = min(confirmations)``.
Both findings close together: the weakest leg governs outright with no
compensation available, and because the aggregate is now a single leg's
confirmation, ``saturation_bps`` finally means what its name says.

Every scenario here uses THREE venues. Two-venue opportunities no longer
produce a directional signal at all -- see P3-4 in
``test_noro_information_value.py`` -- so a signal test built on two venues
would be testing the neutral path by accident.
"""

from __future__ import annotations

import pytest

from agents.noro.agent import (
    FAIR_VALUE_CONFIRMS_DISLOCATION,
    FAIR_VALUE_CONTRADICTS_DISLOCATION,
    FAIR_VALUE_NEUTRAL_ON_DISLOCATION,
    INSUFFICIENT_INDEPENDENT_VALUATION_BREADTH,
    LEG_AGAINST_FAIR_VALUE,
)
from core.config import NoroConfig
from tests.audit.helpers import independent_of, venue
from tests.audit.noro_fixtures import opinion_for

ANCHOR = 100.0


def three_venue(
    buy_bps: float, sell_bps: float, *, liquidity: float = 100_000.0
) -> list:
    """A (BUY leg), B (SELL leg) and C, an independent anchor at ``ANCHOR``.

    Only C survives the opportunity-venue exclusion, so the benchmark is
    exactly C's price and the arithmetic is exact:

        confirmation(A) = +buy_bps      (A sits ``buy_bps`` BELOW the anchor)
        confirmation(B) = +sell_bps     (B sits ``sell_bps`` ABOVE it)

    A negative offset therefore puts that leg on the wrong side.
    """
    return [
        venue("A", ANCHOR * (1 - buy_bps / 10_000), liquidity=liquidity),
        venue("B", ANCHOR * (1 + sell_bps / 10_000), liquidity=liquidity),
        venue("C", ANCHOR, liquidity=liquidity),
    ]


def signal_of(buy_bps: float, sell_bps: float, config: NoroConfig | None = None) -> float:
    opinion = opinion_for(
        three_venue(buy_bps, sell_bps), config or NoroConfig(), "A", "B"
    )
    return opinion.signal


# ======================================================================
# The scenario builder is exact -- prove it before relying on it
# ======================================================================


class TestTheBenchmarkIsTheIndependentAnchor:
    def test_only_the_third_venue_forms_the_benchmark(self):
        states = three_venue(10.0, 10.0)
        benchmark = independent_of(states, NoroConfig(), exclude=("A", "B"))
        assert [v.venue for v in benchmark.venues] == ["C"]
        assert benchmark.fair_value == pytest.approx(ANCHOR)

    @pytest.mark.parametrize("offset", [0.5, 1.0, 7.5, 15.0, 40.0])
    def test_the_offsets_land_exactly_where_intended(self, offset):
        opinion = opinion_for(three_venue(offset, offset), NoroConfig(), "A", "B")
        assert opinion.detail["confirmation_bps_A"] == pytest.approx(offset, abs=1e-3)
        assert opinion.detail["confirmation_bps_B"] == pytest.approx(offset, abs=1e-3)


# ======================================================================
# P3-2 — the weakest leg governs, outright
# ======================================================================


class TestP3_2_WeakestLegGoverns:
    @pytest.mark.parametrize(
        ("buy_bps", "sell_bps", "expected_edge"),
        [
            (10.0, 10.0, 10.0),
            (10.0, 5.0, 5.0),
            (10.0, 1.0, 1.0),
            (10.0, 0.0, 0.0),
            (10.0, -0.1, -0.1),
            (10.0, -1.0, -1.0),
            (10.0, -5.0, -5.0),
            (20.0, -1.0, -1.0),
            (100.0, -1.0, -1.0),
        ],
    )
    def test_the_confirmed_edge_is_the_minimum(self, buy_bps, sell_bps, expected_edge):
        """Under ``min + mean`` these rows were 20.0, 12.5, 6.5, 5.0, 4.85,
        3.5, -2.5, 8.5 and 48.5 -- five of them positive despite a
        contradicting leg."""
        opinion = opinion_for(three_venue(buy_bps, sell_bps), NoroConfig(), "A", "B")
        assert opinion.detail["weakest_confirmation_bps"] == pytest.approx(
            expected_edge, abs=1e-3
        )

    def test_p3_2_the_headline_case_is_now_negative(self):
        """``[-1, +10]``: one leg is on the wrong side of the independent
        benchmark, so the aggregate is negative. It used to be ``+3.5``."""
        opinion = opinion_for(three_venue(10.0, -1.0), NoroConfig(), "A", "B")
        assert opinion.detail["weakest_confirmation_bps"] == pytest.approx(-1.0, abs=1e-3)
        assert opinion.signal < 0, (
            f"a contradicting leg must not produce a positive vote: "
            f"{opinion.signal:+.4f}"
        )

    @pytest.mark.parametrize("strong", [5.0, 10.0, 20.0, 50.0, 100.0])
    def test_no_strong_leg_can_outvote_any_contradiction(self, strong):
        """The old veto threshold was ``-strong/3``: a contradiction smaller
        than a third of the strong leg was simply absorbed. There is no
        threshold now -- any negative leg carries the whole aggregate."""
        for weak in (-0.01, -0.1, -1.0, -strong / 3.0, -strong):
            assert signal_of(strong, weak) < 0, (
                f"strong={strong} weak={weak} produced a non-negative signal"
            )

    def test_a_neutral_leg_caps_the_aggregate_at_neutral(self):
        for strong in (1.0, 10.0, 100.0):
            assert signal_of(strong, 0.0) == pytest.approx(0.0, abs=1e-9)

    def test_only_two_positive_legs_give_a_positive_signal(self):
        assert signal_of(5.0, 5.0) > 0
        assert signal_of(0.1, 30.0) > 0
        assert signal_of(-0.1, 30.0) < 0
        assert signal_of(30.0, -0.1) < 0

    def test_the_documented_rule_and_the_implemented_one_now_agree(self):
        """The audit's contrast test, inverted. ``min`` alone was what the
        comment described; it is now also what the code does."""
        for weak, strong in ((-0.1, 10.0), (-1.0, 10.0), (-3.0, 10.0)):
            assert min([weak, strong]) < 0, "the documented rule rejects"
            assert signal_of(strong, weak) < 0, "and so does the implemented one"


# ======================================================================
# P3-3 — saturation_bps means what it says
# ======================================================================


class TestP3_3_SaturationSemantics:
    @pytest.mark.parametrize(
        ("weakest_bps", "expected_signal"),
        [
            (15.0, 1.0),
            (7.5, 0.5),
            (3.0, 0.2),
            (0.0, 0.0),
            (-3.0, -0.2),
            (-7.5, -0.5),
            (-15.0, -1.0),
        ],
    )
    def test_the_signal_is_the_weakest_leg_over_saturation_bps(
        self, weakest_bps, expected_signal
    ):
        """``saturation_bps=15`` now saturates at a 15 bps weakest-leg
        confirmation. It used to saturate at 7.5."""
        config = NoroConfig(saturation_bps=15.0)
        # The other leg is held well clear at +40 bps so the parametrised one
        # is unambiguously the weakest.
        assert signal_of(weakest_bps, 40.0, config) == pytest.approx(
            expected_signal, abs=2e-3
        )

    def test_fifteen_bps_no_longer_saturates_twice_over(self):
        """The documented reading is now the measured one: a 15 bps per-leg
        confirmation is exactly the saturation point, not double it."""
        config = NoroConfig(saturation_bps=15.0)
        assert signal_of(14.9, 40.0, config) < 1.0
        assert signal_of(15.0, 40.0, config) == pytest.approx(1.0, abs=2e-3)
        assert signal_of(20.0, 40.0, config) == pytest.approx(1.0)

    def test_the_signal_is_clamped_at_one(self):
        config = NoroConfig(saturation_bps=15.0)
        for offset in (16.0, 30.0, 100.0, 500.0):
            assert signal_of(offset, offset, config) == pytest.approx(1.0)
            assert signal_of(-offset, -offset, config) == pytest.approx(-1.0)

    def test_a_smaller_saturation_makes_the_signal_more_responsive(self):
        tight = signal_of(5.0, 40.0, NoroConfig(saturation_bps=10.0))
        loose = signal_of(5.0, 40.0, NoroConfig(saturation_bps=30.0))
        assert tight == pytest.approx(0.5, abs=2e-3)
        assert loose == pytest.approx(1 / 6, abs=2e-3)
        assert tight > loose

    def test_the_saturation_point_no_longer_moves_with_liquidity(self):
        """P3-3's companion defect: under ``min + mean`` how big a gap was
        needed to saturate depended on how balanced the venues' liquidity was,
        because ``min`` scaled with the smaller weight. The weakest leg is now
        a pure price comparison against an independent benchmark, so it does
        not move with depth at all."""
        config = NoroConfig(saturation_bps=15.0)
        balanced = opinion_for(
            three_venue(10.0, 10.0, liquidity=100_000.0), config, "A", "B"
        )
        lopsided = [
            venue("A", ANCHOR * (1 - 10.0 / 10_000), liquidity=1_000.0),
            venue("B", ANCHOR * (1 + 10.0 / 10_000), liquidity=999_000.0),
            venue("C", ANCHOR, liquidity=50_000.0),
        ]
        assert opinion_for(lopsided, config, "A", "B").signal == pytest.approx(
            balanced.signal, abs=1e-6
        )


# ======================================================================
# Reason codes are now mutually coherent
# ======================================================================


class TestReasonCodeCoherence:
    def test_a_confirming_opinion_never_reports_a_leg_against(self):
        """The semantic contradiction P3-2 produced in the reason codes
        themselves: ``FAIR_VALUE_CONFIRMS_DISLOCATION`` and
        ``LEG_AGAINST_FAIR_VALUE`` together. Unreachable now, because
        ``LEG_AGAINST_FAIR_VALUE`` fires exactly when the minimum is negative,
        which is exactly when the verdict is CONTRADICTS."""
        opinion = opinion_for(three_venue(10.0, 8.0), NoroConfig(), "A", "B")
        assert FAIR_VALUE_CONFIRMS_DISLOCATION in opinion.reason_codes
        assert LEG_AGAINST_FAIR_VALUE not in opinion.reason_codes

    def test_a_contradicting_leg_produces_both_negative_codes(self):
        opinion = opinion_for(three_venue(10.0, -1.0), NoroConfig(), "A", "B")
        assert FAIR_VALUE_CONTRADICTS_DISLOCATION in opinion.reason_codes
        assert LEG_AGAINST_FAIR_VALUE in opinion.reason_codes
        assert FAIR_VALUE_CONFIRMS_DISLOCATION not in opinion.reason_codes

    @pytest.mark.parametrize(
        ("buy_bps", "sell_bps", "expected"),
        [
            (10.0, 10.0, FAIR_VALUE_CONFIRMS_DISLOCATION),
            (10.0, 0.1, FAIR_VALUE_CONFIRMS_DISLOCATION),
            (10.0, -1.0, FAIR_VALUE_CONTRADICTS_DISLOCATION),
            (-1.0, -1.0, FAIR_VALUE_CONTRADICTS_DISLOCATION),
        ],
    )
    def test_exactly_one_verdict_code_is_emitted(self, buy_bps, sell_bps, expected):
        opinion = opinion_for(three_venue(buy_bps, sell_bps), NoroConfig(), "A", "B")
        verdicts = {
            FAIR_VALUE_CONFIRMS_DISLOCATION,
            FAIR_VALUE_CONTRADICTS_DISLOCATION,
            FAIR_VALUE_NEUTRAL_ON_DISLOCATION,
        }
        emitted = verdicts.intersection(opinion.reason_codes)
        assert emitted == {expected}

    def test_a_zero_edge_is_labelled_neutral_not_contradicting(self):
        """The audit noted that ``signal > 0`` sent exactly-zero to the
        negative branch, so a perfectly neutral valuation was reported as
        CONTRADICTS. It now has its own code."""
        opinion = opinion_for(three_venue(10.0, 0.0), NoroConfig(), "A", "B")
        assert opinion.signal == pytest.approx(0.0, abs=1e-9)
        assert FAIR_VALUE_NEUTRAL_ON_DISLOCATION in opinion.reason_codes
        assert FAIR_VALUE_CONTRADICTS_DISLOCATION not in opinion.reason_codes
        assert FAIR_VALUE_CONFIRMS_DISLOCATION not in opinion.reason_codes

    def test_the_neutral_breadth_code_never_accompanies_a_verdict(self):
        """A two-venue opportunity carries the breadth code alone: NORO is
        saying it has no basis for a verdict, not delivering one."""
        two = [venue("A", 100.0), venue("B", 100.2)]
        opinion = opinion_for(two, NoroConfig(), "A", "B")
        assert opinion.reason_codes == [INSUFFICIENT_INDEPENDENT_VALUATION_BREADTH]


# ======================================================================
# Sign symmetry
# ======================================================================


class TestSignSymmetry:
    def test_reversing_both_legs_reverses_the_signal(self):
        """A and B swap roles against an unchanged benchmark, so both
        confirmations negate and so does their minimum."""
        states = three_venue(6.0, 6.0)
        config = NoroConfig()
        forward = opinion_for(states, config, "A", "B")
        reversed_ = opinion_for(states, config, "B", "A")
        assert reversed_.signal == pytest.approx(-forward.signal, abs=1e-9)

    def test_confidence_is_unchanged_by_direction(self):
        """Confidence describes the valuation, not the trade: the same
        contributors judged in either direction stand equally behind it."""
        states = three_venue(6.0, 6.0)
        config = NoroConfig()
        forward = opinion_for(states, config, "A", "B")
        reversed_ = opinion_for(states, config, "B", "A")
        assert reversed_.confidence == pytest.approx(forward.confidence)

    def test_symmetry_holds_under_saturation_only_in_magnitude(self):
        states = three_venue(60.0, 60.0)
        config = NoroConfig()
        assert opinion_for(states, config, "A", "B").signal == pytest.approx(1.0)
        assert opinion_for(states, config, "B", "A").signal == pytest.approx(-1.0)


# ======================================================================
# Monotonicity in the dislocation
# ======================================================================


class TestSignalMonotonicity:
    def test_the_signal_rises_monotonically_with_the_gap(self):
        config = NoroConfig()
        previous = -2.0
        for gap_bps in range(1, 51):
            signal = signal_of(float(gap_bps), float(gap_bps), config)
            assert signal >= previous - 1e-12, (
                f"signal fell at gap={gap_bps}bps: {signal} < {previous}"
            )
            previous = signal

    def test_it_saturates_and_then_stops_carrying_information(self):
        config = NoroConfig()
        signals = {
            gap: signal_of(float(gap), float(gap), config)
            for gap in (16, 30, 60, 200, 1_000)
        }
        assert all(s == pytest.approx(1.0) for s in signals.values()), signals

    def test_monotonicity_holds_when_only_the_weaker_leg_moves(self):
        config = NoroConfig()
        previous = -2.0
        for gap_bps in range(-10, 21):
            signal = signal_of(40.0, float(gap_bps), config)
            assert signal >= previous - 1e-12
            previous = signal
