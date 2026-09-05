"""Phase 3 audit, Sections 10 / 11 / 35 / 36 / 53: what the signal means.

Three questions about one line of ``Noro.evaluate``::

    edge_bps = min(confirmations) + sum(confirmations) / len(confirmations)

whose comment says:

    "The trade is only confirmed to the extent that *both* legs agree; the
     weakest leg governs, so a single rich venue cannot carry the trade."

**H2 -- does the weakest leg govern?** Adding the mean to the minimum means a
strong leg contributes regardless of what the weak one says. This suite finds
the exact point where a contradicting leg stops being able to veto.

**H3 -- what does ``saturation_bps`` parameterise?** It is documented as "the
deviation, in bps, at which the signal saturates to |1|". For symmetric
confirmations the formula returns ``2X``, so saturation arrives at half the
configured value.

**Reason codes and sign symmetry** are audited alongside, because both are
consequences of the same expression.
"""

from __future__ import annotations

import pytest

from core.config import NoroConfig
from tests.audit.helpers import fair_of, venue
from tests.audit.noro_fixtures import opinion_for


def edge_of(confirmations: list[float]) -> float:
    """The production expression, isolated for the algebraic cases."""
    return min(confirmations) + sum(confirmations) / len(confirmations)


# ======================================================================
# Section 10 — H2: does the weakest leg actually govern?
# ======================================================================


class TestH2_TheEdgeFormula:
    @pytest.mark.parametrize(
        ("confirmations", "expected_edge"),
        [
            ([10.0, 10.0], 20.0),
            ([10.0, 5.0], 12.5),
            ([10.0, 1.0], 6.5),
            ([10.0, 0.0], 5.0),
            ([10.0, -0.1], 4.85),
            ([10.0, -1.0], 3.5),
            ([10.0, -2.0], 2.0),
            ([10.0, -5.0], -2.5),
            ([10.0, -10.0], -10.0),
            ([20.0, -1.0], 8.5),
            ([20.0, -5.0], 2.5),
            ([20.0, -10.0], -5.0),
        ],
    )
    def test_the_edge_is_min_plus_mean(self, confirmations, expected_edge):
        assert edge_of(confirmations) == pytest.approx(expected_edge)

    def test_the_headline_case_one_leg_contradicts_and_the_edge_is_positive(self):
        """[-1, +10]: one leg is on the WRONG side of fair value, and NORO
        still reports a positive edge."""
        confirmations = [-1.0, 10.0]
        assert min(confirmations) == -1.0, "the weakest leg contradicts"
        assert sum(confirmations) / 2 == 4.5
        assert edge_of(confirmations) == pytest.approx(3.5), (
            "a contradicting leg produced a POSITIVE confirmed edge"
        )

    @pytest.mark.parametrize("strong", [5.0, 10.0, 20.0, 50.0, 100.0])
    def test_a_contradicting_leg_is_outvoted_up_to_a_computable_point(self, strong):
        """The exact veto threshold.

        With two legs, edge = w + (w + s)/2 where w is the weak (negative)
        leg. That is positive while w > -s/3. So a strong leg of size s
        tolerates a contradiction up to a THIRD of its own magnitude before
        the combined edge turns negative -- the weak leg does not govern; it
        is merely down-weighted.
        """
        threshold = -strong / 3.0
        just_inside = threshold + 1e-6
        just_outside = threshold - 1e-6
        assert edge_of([just_inside, strong]) > 0, "still confirmed"
        assert edge_of([just_outside, strong]) < 0, "finally contradicted"

    def test_a_true_weakest_leg_rule_would_reject_all_of_these(self):
        """Contrast: what ``min`` alone -- the documented behaviour -- gives."""
        for weak, strong in ((-0.1, 10.0), (-1.0, 10.0), (-3.0, 10.0)):
            assert min([weak, strong]) < 0, "the documented rule rejects"
            assert edge_of([weak, strong]) > 0, "the implemented one confirms"


class TestH2_FromRealMarketState:
    """The same contradiction, built from venue states rather than by hand.

    Hand-entered confirmation vectors prove the arithmetic. These prove the
    condition is reachable through ``compute_fair_value`` and
    ``Noro.evaluate`` on states the production types accept.
    """

    @staticmethod
    def _states():
        # Three venues. C is richer and reasonably deep, dragging fair value
        # just ABOVE the venue the detector wants to sell -- so the SELL leg
        # lands on the wrong side of fair value while the BUY leg is strongly
        # confirmed. C is deliberately mild: drag fair value far enough and
        # BOTH legs contradict and NORO correctly rejects, which is a
        # different (and healthy) case.
        return [
            venue("A", 100.00, liquidity=50_000.0),
            venue("B", 100.20, liquidity=50_000.0),
            venue("C", 100.50, liquidity=40_000.0),
        ]

    def test_the_sell_leg_is_on_the_wrong_side_of_fair_value(self):
        fair = fair_of(self._states(), NoroConfig())
        assert fair.fair_value == pytest.approx(100.2143, abs=1e-3)
        assert fair.fair_value > 100.20, (
            "the richer venue pulled fair value above the sell venue"
        )
        assert fair.deviation("A") == pytest.approx(-21.38, abs=0.1), (
            "buying A is strongly confirmed"
        )
        assert fair.deviation("B") == pytest.approx(-1.43, abs=0.1), (
            "selling B is CONTRADICTED -- B is below fair value"
        )

    def test_noro_still_reports_a_positive_signal(self):
        opinion = opinion_for(self._states(), NoroConfig(), "A", "B")
        assert opinion is not None
        assert opinion.signal > 0, (
            f"one leg contradicts fair value and NORO still votes "
            f"{opinion.signal:+.3f} for the trade"
        )
        assert opinion.signal == pytest.approx(0.570, abs=0.01)
        assert opinion.detail["confirmed_edge_bps"] == pytest.approx(8.55, abs=0.05)

    def test_it_simultaneously_confirms_and_reports_a_leg_against(self):
        """The semantic contradiction, in the reason codes themselves."""
        opinion = opinion_for(self._states(), NoroConfig(), "A", "B")
        assert "FAIR_VALUE_CONFIRMS_DISLOCATION" in opinion.reason_codes
        assert "LEG_AGAINST_FAIR_VALUE" in opinion.reason_codes
        assert opinion.signal > 0

    def test_the_contradiction_survives_into_consensus_weight(self):
        """It is not a labelling curiosity: this opinion is a real positive
        vote carrying NORO's full weight."""
        opinion = opinion_for(self._states(), NoroConfig(), "A", "B")
        # 0.802: 0.35 base + 0.45 x (140k/250k) + 0.20 x full breadth.
        assert opinion.confidence == pytest.approx(0.802, abs=1e-3)
        assert opinion.signal * opinion.confidence > 0.45, (
            "a contradicting leg still delivers a substantial positive vote"
        )


# ======================================================================
# Section 11 — H3: what does saturation_bps saturate?
# ======================================================================


class TestH3_SaturationSemantics:
    @pytest.mark.parametrize(
        ("symmetric", "expected_edge"),
        [
            (0.0, 0.0),
            (1.0, 2.0),
            (2.0, 4.0),
            (5.0, 10.0),
            (7.49, 14.98),
            (7.5, 15.0),
            (7.51, 15.02),
            (10.0, 20.0),
            (15.0, 30.0),
            (20.0, 40.0),
            (30.0, 60.0),
        ],
    )
    def test_symmetric_confirmations_double(self, symmetric, expected_edge):
        assert edge_of([symmetric, symmetric]) == pytest.approx(expected_edge)

    def test_the_signal_saturates_at_half_the_configured_value(self):
        """``saturation_bps=15`` saturates at a 7.5 bps per-leg deviation."""
        config = NoroConfig(saturation_bps=15.0)
        assert edge_of([7.49, 7.49]) / config.saturation_bps < 1.0
        assert edge_of([7.5, 7.5]) / config.saturation_bps == pytest.approx(1.0)
        assert edge_of([7.51, 7.51]) / config.saturation_bps > 1.0

    def test_the_documented_reading_would_saturate_at_fifteen(self):
        """The config comment says "deviation, in bps, at which the signal
        saturates". Read that way, a 15 bps deviation is the saturation
        point; measured, 15 bps saturates twice over."""
        config = NoroConfig(saturation_bps=15.0)
        documented_point = 15.0
        assert edge_of([documented_point, documented_point]) == pytest.approx(30.0)
        assert edge_of([documented_point, documented_point]) / config.saturation_bps == (
            pytest.approx(2.0)
        )

    def test_what_the_parameter_actually_divides(self):
        """Not a per-leg deviation. The AGGREGATE ``min + mean``, whose range
        for two agreeing legs is [gap, 2 x gap] depending on weighting."""
        assert edge_of([10.0, 10.0]) == 20.0, "balanced: 2x the per-leg value"
        assert edge_of([0.1, 19.9]) == pytest.approx(10.1), "lopsided: ~1x"

    def test_the_saturation_reached_through_the_real_agent(self):
        """Two venues 15 bps apart with equal weight saturate the signal --
        i.e. a per-leg deviation of 7.5 bps, not 15."""
        states = [
            venue("A", 100.0, liquidity=100_000.0),
            venue("B", 100.15, liquidity=100_000.0),
        ]
        fair = fair_of(states, NoroConfig())
        # 7.494, not exactly 7.5: deviations are measured against fair value
        # (100.075), not against either venue's own price.
        assert abs(fair.deviation("A")) == pytest.approx(7.494, abs=0.01)
        opinion = opinion_for(states, NoroConfig(saturation_bps=15.0), "A", "B")
        assert opinion.signal == pytest.approx(0.9993, abs=1e-3), (
            "essentially saturated at a ~7.5 bps per-leg deviation, with "
            "saturation_bps=15"
        )
        wider = [
            venue("A", 100.0, liquidity=100_000.0),
            venue("B", 100.16, liquidity=100_000.0),
        ]
        assert opinion_for(wider, NoroConfig(saturation_bps=15.0), "A", "B").signal == (
            pytest.approx(1.0)
        )


# ======================================================================
# Section 35 — reason-code consistency
# ======================================================================


class TestReasonCodes:
    @pytest.mark.parametrize(
        ("confirmations", "expect_confirms", "expect_leg_against"),
        [
            ([10.0, 10.0], True, False),
            ([10.0, 0.0], True, False),
            ([10.0, -1.0], True, True),
            ([10.0, -5.0], False, True),
            ([-1.0, -1.0], False, True),
        ],
    )
    def test_codes_follow_the_edge_and_the_legs_independently(
        self, confirmations, expect_confirms, expect_leg_against
    ):
        edge = edge_of(confirmations)
        confirms = edge > 0
        leg_against = any(c < 0 for c in confirmations)
        assert confirms is expect_confirms
        assert leg_against is expect_leg_against

    def test_confirms_and_leg_against_can_both_be_true(self):
        """Documented so it is not mistaken for an inconsistency: the two
        codes answer different questions -- "is the aggregate positive" and
        "did any leg disagree". Both being present is the reachable, and
        confusing, combination that P3-2 is about."""
        confirmations = [10.0, -1.0]
        assert edge_of(confirmations) > 0
        assert any(c < 0 for c in confirmations)

    def test_a_zero_edge_is_labelled_contradicts(self):
        """``signal > 0`` is the test, so exactly zero falls to the negative
        branch. Defensible, but worth stating: a perfectly neutral valuation
        is reported as CONTRADICTS, not as neutral."""
        opinion = opinion_for(
            [venue("A", 100.0, liquidity=50_000.0), venue("B", 100.0, liquidity=50_000.0)],
            NoroConfig(),
            "A",
            "B",
        )
        assert opinion.signal == pytest.approx(0.0)
        assert "FAIR_VALUE_CONTRADICTS_DISLOCATION" in opinion.reason_codes
        assert "FAIR_VALUE_CONFIRMS_DISLOCATION" not in opinion.reason_codes


# ======================================================================
# Section 36 — sign symmetry
# ======================================================================


class TestSignSymmetry:
    @staticmethod
    def _states():
        return [
            venue("A", 100.0, liquidity=60_000.0),
            venue("B", 100.2, liquidity=40_000.0),
        ]

    def test_reversing_both_legs_reverses_the_signal(self):
        config = NoroConfig()
        forward = opinion_for(self._states(), config, "A", "B")
        reversed_ = opinion_for(self._states(), config, "B", "A")
        assert reversed_.signal == pytest.approx(-forward.signal, abs=1e-9), (
            "confirmations negate exactly, so min+mean negates exactly"
        )

    def test_confidence_is_unchanged_by_direction(self):
        """Confidence depends on liquidity and breadth only -- never on
        whether the trade is confirmed. Recorded as a property."""
        config = NoroConfig()
        forward = opinion_for(self._states(), config, "A", "B")
        reversed_ = opinion_for(self._states(), config, "B", "A")
        assert reversed_.confidence == pytest.approx(forward.confidence)

    def test_symmetry_holds_under_saturation_only_in_magnitude(self):
        """Once clamped, both directions saturate and the exact antisymmetry
        becomes |1| vs |-1| -- still symmetric, but no longer informative."""
        states = [
            venue("A", 100.0, liquidity=50_000.0),
            venue("B", 101.0, liquidity=50_000.0),
        ]
        config = NoroConfig()
        assert opinion_for(states, config, "A", "B").signal == pytest.approx(1.0)
        assert opinion_for(states, config, "B", "A").signal == pytest.approx(-1.0)


# ======================================================================
# Section 53 — signal monotonicity in the raw dislocation
# ======================================================================


class TestSignalMonotonicity:
    def test_the_signal_rises_monotonically_with_the_gap(self):
        config = NoroConfig()
        previous = -2.0
        for gap_bps in range(1, 51):
            states = [
                venue("A", 100.0, liquidity=100_000.0),
                venue("B", 100.0 * (1 + gap_bps / 10_000), liquidity=100_000.0),
            ]
            signal = opinion_for(states, config, "A", "B").signal
            assert signal >= previous - 1e-12, (
                f"signal fell at gap={gap_bps}bps: {signal} < {previous}"
            )
            previous = signal

    def test_it_saturates_and_then_stops_carrying_information(self):
        """Above the saturation point every dislocation looks identical."""
        config = NoroConfig()
        signals = {}
        for gap_bps in (16, 30, 60, 200, 1_000):
            states = [
                venue("A", 100.0, liquidity=100_000.0),
                venue("B", 100.0 * (1 + gap_bps / 10_000), liquidity=100_000.0),
            ]
            signals[gap_bps] = opinion_for(states, config, "A", "B").signal
        assert all(s == pytest.approx(1.0) for s in signals.values()), signals

    def test_monotonicity_holds_under_a_fixed_liquidity_imbalance(self):
        config = NoroConfig()
        previous = -2.0
        for gap_bps in range(1, 31):
            states = [
                venue("A", 100.0, liquidity=10_000.0),
                venue("B", 100.0 * (1 + gap_bps / 10_000), liquidity=900_000.0),
            ]
            signal = opinion_for(states, config, "A", "B").signal
            assert signal >= previous - 1e-12
            previous = signal

    def test_the_saturation_point_moves_with_the_liquidity_balance(self):
        """Not a monotonicity break, but worth recording: how big a gap is
        needed to saturate depends on how balanced the venues' liquidity is,
        because ``min(confirmations)`` scales with the smaller weight."""
        config = NoroConfig()

        def signal(gap_bps, liq_a, liq_b):
            states = [
                venue("A", 100.0, liquidity=liq_a),
                venue("B", 100.0 * (1 + gap_bps / 10_000), liquidity=liq_b),
            ]
            return opinion_for(states, config, "A", "B").signal

        balanced = signal(10.0, 100_000.0, 100_000.0)
        lopsided = signal(10.0, 1_000.0, 999_000.0)
        assert balanced > lopsided, (
            f"same 10 bps gap: balanced={balanced:.3f} lopsided={lopsided:.3f}"
        )
        assert lopsided == pytest.approx(0.3337, abs=0.005), (
            "the lopsided limit is the mean term alone: edge -> gap/2 = 5 bps, "
            "so signal -> 5/15 = 1/3 however extreme the imbalance becomes"
        )
