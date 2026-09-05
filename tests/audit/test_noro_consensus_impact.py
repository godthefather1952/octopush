"""Phase 3 audit, Sections 23 / 24 / 42: how much can NORO move a decision?

Every mathematical finding in this audit is only as serious as NORO's ability
to change what the platform does. NORO is a REQUIRED agent carrying weight
1.5 of a 4.9 total, so this suite measures the two things that matter:

* the **threshold map** -- which (signal, confidence) pairs flip a decision
  while TIDAL and ZEPHR are held fixed, and
* the **overlap** -- whether NORO's vote is close to a restatement of the
  detector's own number, in which case a high weight buys correlation rather
  than independence.
"""

from __future__ import annotations

import pytest

from core.config import NoroConfig, load_settings
from core.models.agent import AgentOpinion
from core.models.common import AgentId, DataQuality, Millis
from core.state import OpinionSlot
from strategies.consensus.engine import ConsensusEngine
from tests.audit.helpers import START_MS, SYMBOL, venue
from tests.audit.noro_fixtures import opinion_for


@pytest.fixture
def settings():
    return load_settings()


@pytest.fixture
def engine(settings):
    from core.clock import ManualClock

    return ConsensusEngine(settings.consensus, ManualClock(START_MS))


def slot(
    agent: AgentId,
    signal: float,
    confidence: float,
    *,
    quality: DataQuality = DataQuality.FRESH,
) -> OpinionSlot:
    return OpinionSlot(
        opinion=AgentOpinion(
            agent_id=agent,
            symbol=SYMBOL,
            created_at=START_MS,
            signal=signal,
            confidence=confidence,
            expires_at=START_MS + 5_000,
            model_version="audit",
        ),
        quality=quality,
    )


def score(engine, opinions: dict[AgentId, OpinionSlot], now_ms: Millis = START_MS):
    return engine.combine(
        symbol=SYMBOL, strategy="cross_venue", opinions=opinions, now_ms=now_ms
    )


# ======================================================================
# Section 23 — the configured weights, recorded exactly
# ======================================================================


class TestTheConfiguredWeights:
    def test_the_weights_and_thresholds(self, settings):
        weights = settings.consensus.weights
        assert weights[AgentId.TIDAL] == 1.4
        assert weights[AgentId.NORO] == 1.5
        assert weights[AgentId.ZEPHR] == 1.5
        assert weights[AgentId.LUMEN] == 0.5
        assert settings.consensus.entry_threshold == 0.60
        assert settings.consensus.exit_threshold == 0.45
        assert settings.consensus.degraded_weight_factor == 0.35

    def test_noro_is_required(self, settings):
        assert AgentId.NORO in settings.consensus.required_agents

    def test_noros_share_of_the_total_weight(self, settings):
        total = sum(settings.consensus.weights.values())
        assert total == pytest.approx(4.9)
        assert settings.consensus.weights[AgentId.NORO] / total == pytest.approx(
            0.306, abs=1e-3
        ), "NORO carries just under a third of the weighted vote"

    def test_noro_and_zephr_are_the_joint_heaviest(self, settings):
        weights = settings.consensus.weights
        assert weights[AgentId.NORO] == max(weights.values())


# ======================================================================
# Section 23 — the threshold map
# ======================================================================


class TestNoroCanFlipTheDecision:
    """TIDAL and ZEPHR held constant; only NORO's vote varies."""

    BASE = {
        AgentId.TIDAL: (0.20, 0.90),
        AgentId.ZEPHR: (0.80, 0.90),
    }

    def _with_noro(self, engine, signal, confidence):
        opinions = {a: slot(a, s, c) for a, (s, c) in self.BASE.items()}
        opinions[AgentId.NORO] = slot(AgentId.NORO, signal, confidence)
        return score(engine, opinions)

    def test_without_noro_the_result_is_incomplete_and_blocked(self, engine):
        opinions = {a: slot(a, s, c) for a, (s, c) in self.BASE.items()}
        result = score(engine, opinions)
        assert AgentId.NORO in result.missing_agents
        assert not result.complete
        assert not engine.entry_allowed(result)

    @pytest.mark.parametrize(
        "signal", [-1.0, -0.75, -0.5, -0.25, 0.0, 0.25, 0.5, 0.75, 0.9, 1.0]
    )
    def test_the_score_is_monotone_in_noros_signal(self, engine, signal):
        result = self._with_noro(engine, signal, 1.0)
        assert -1.0 <= result.score <= 1.0
        stronger = self._with_noro(engine, min(1.0, signal + 0.1), 1.0)
        assert stronger.score >= result.score - 1e-12

    def test_noro_alone_crosses_the_entry_threshold(self, engine):
        """The headline: identical TIDAL and ZEPHR, and NORO decides."""
        blocked = self._with_noro(engine, 0.0, 1.0)
        allowed = self._with_noro(engine, 1.0, 1.0)
        assert not engine.entry_allowed(blocked)
        assert engine.entry_allowed(allowed)
        assert blocked.agreement < 0.60 <= allowed.agreement

    def test_the_exact_signal_at_which_entry_becomes_allowed(self, engine):
        crossing = None
        for step in range(0, 201):
            signal = -1.0 + step * 0.01
            if engine.entry_allowed(self._with_noro(engine, signal, 1.0)):
                crossing = signal
                break
        assert crossing == pytest.approx(0.76, abs=0.011), (
            f"with TIDAL 0.20 and ZEPHR 0.80, NORO must vote >= {crossing:.2f} "
            "for entry"
        )

    @pytest.mark.parametrize("confidence", [0.1, 0.25, 0.5, 0.55, 0.75, 1.0])
    def test_confidence_scales_noros_influence(self, engine, confidence):
        """Confidence multiplies the weight, so a low-confidence NORO both
        pulls less AND lets the others dominate."""
        strong = self._with_noro(engine, 1.0, confidence)
        weak = self._with_noro(engine, -1.0, confidence)
        assert strong.score > weak.score
        assert strong.score - weak.score > 0

    def test_the_influence_spread_grows_with_confidence(self, engine):
        spreads = {}
        for confidence in (0.1, 0.55, 1.0):
            strong = self._with_noro(engine, 1.0, confidence).score
            weak = self._with_noro(engine, -1.0, confidence).score
            spreads[confidence] = strong - weak
        assert spreads[0.1] < spreads[0.55] < spreads[1.0]
        assert spreads[1.0] > 0.55, (
            "at full confidence NORO commands more than half the score range"
        )

    def test_noros_realistic_confidence_floor_is_already_influential(self, engine):
        """0.55 is the minimum a two-venue NORO can report (see the
        confidence suite), so this is its *weakest possible* influence."""
        strong = self._with_noro(engine, 1.0, 0.55).score
        weak = self._with_noro(engine, -1.0, 0.55).score
        assert strong - weak > 0.4

    def test_a_negative_noro_blocks_an_otherwise_strong_consensus(self, engine):
        opinions = {
            AgentId.TIDAL: slot(AgentId.TIDAL, 0.9, 0.9),
            AgentId.ZEPHR: slot(AgentId.ZEPHR, 0.9, 0.9),
            AgentId.NORO: slot(AgentId.NORO, -1.0, 1.0),
        }
        result = score(engine, opinions)
        assert not engine.entry_allowed(result), (
            "NORO's veto works -- which is why its inability to reject a "
            "two-venue opportunity matters"
        )


# ======================================================================
# Section 24 — missing is not neutral
# ======================================================================


class TestMissingIsNotNeutral:
    BASE = {
        AgentId.TIDAL: (0.9, 0.9),
        AgentId.ZEPHR: (0.9, 0.9),
    }

    def _score(self, engine, noro: tuple[float, float] | None, **kwargs):
        opinions = {a: slot(a, s, c) for a, (s, c) in self.BASE.items()}
        if noro is not None:
            opinions[AgentId.NORO] = slot(AgentId.NORO, *noro, **kwargs)
        return score(engine, opinions)

    def test_a_missing_noro_suspends_the_strategy(self, engine):
        result = self._score(engine, None)
        assert not result.complete
        assert not engine.entry_allowed(result)
        assert not engine.continuation_allowed(result)

    def test_a_neutral_noro_does_not(self, engine):
        result = self._score(engine, (0.0, 1.0))
        assert result.complete
        assert result.agreement > 0

    def test_the_two_are_materially_different_outcomes(self, engine):
        missing = self._score(engine, None)
        neutral = self._score(engine, (0.0, 1.0))
        assert missing.complete is False and neutral.complete is True
        # The scores differ too: a missing agent is left out of the weighted
        # mean entirely, while a neutral one dilutes it.
        assert missing.agreement == pytest.approx(0.9)
        assert neutral.agreement == pytest.approx(0.5715, abs=1e-3)

    def test_and_the_difference_can_decide_entry(self, engine):
        """With a strong enough base, a NEUTRAL NORO still permits entry
        while a MISSING one blocks it outright -- despite the missing case
        having the higher raw agreement."""
        opinions = {
            AgentId.TIDAL: slot(AgentId.TIDAL, 1.0, 0.9),
            AgentId.ZEPHR: slot(AgentId.ZEPHR, 1.0, 0.9),
        }
        missing = score(engine, opinions)
        opinions[AgentId.NORO] = slot(AgentId.NORO, 0.0, 1.0)
        neutral = score(engine, opinions)
        assert missing.agreement > neutral.agreement
        assert engine.entry_allowed(missing) is False, "incomplete blocks"
        assert engine.entry_allowed(neutral) is True

    @pytest.mark.parametrize(
        ("label", "noro", "quality"),
        [
            ("positive low-confidence", (0.8, 0.55), DataQuality.FRESH),
            ("strongly positive", (1.0, 1.0), DataQuality.FRESH),
            ("negative", (-1.0, 1.0), DataQuality.FRESH),
        ],
    )
    def test_each_present_case_is_complete(self, engine, label, noro, quality):
        result = self._score(engine, noro, quality=quality)
        assert result.complete, label

    def test_a_stale_noro_is_reported_missing_and_blocks(self, engine):
        result = self._score(engine, (1.0, 1.0), quality=DataQuality.STALE)
        assert AgentId.NORO in result.missing_agents
        assert not result.complete

    def test_a_degraded_noro_still_counts_at_reduced_weight(self, engine, settings):
        fresh = self._score(engine, (1.0, 1.0), quality=DataQuality.FRESH)
        degraded = self._score(engine, (1.0, 1.0), quality=DataQuality.DEGRADED)
        assert degraded.complete
        assert degraded.score < fresh.score
        assert AgentId.NORO in degraded.degraded_agents


# ======================================================================
# Section 42 — double-counting the same dislocation
# ======================================================================


class TestSignalOverlap:
    """Do TIDAL, NORO and ZEPHR carry independent information?

    NORO's two-venue edge is ``G * (min(w) + 1/2)`` where G is the price gap
    the detector already measured (proved in the information-value suite). So
    NORO's signal is a monotone function of the detector's own number,
    modulated by at most a factor of two.
    """

    def test_noros_signal_is_a_monotone_function_of_the_detector_gap(self):
        config = NoroConfig()
        signals = []
        for gap_bps in (1, 2, 4, 6, 8, 10, 12, 14, 16, 20):
            states = [
                venue("A", 100.0, liquidity=100_000.0),
                venue("B", 100.0 * (1 + gap_bps / 10_000), liquidity=100_000.0),
            ]
            signals.append(opinion_for(states, config, "A", "B").signal)
        assert signals == sorted(signals)

    def test_the_correlation_with_the_raw_gap_is_essentially_perfect(self):
        """Below saturation, and with balanced liquidity, NORO's signal IS
        the detector's gap on a different scale."""
        config = NoroConfig()
        gaps, signals = [], []
        for tenth in range(1, 71):
            gap_bps = tenth / 10
            states = [
                venue("A", 100.0, liquidity=100_000.0),
                venue("B", 100.0 * (1 + gap_bps / 10_000), liquidity=100_000.0),
            ]
            gaps.append(gap_bps)
            signals.append(opinion_for(states, config, "A", "B").signal)

        mean_g = sum(gaps) / len(gaps)
        mean_s = sum(signals) / len(signals)
        cov = sum((g - mean_g) * (s - mean_s) for g, s in zip(gaps, signals, strict=True))
        var_g = sum((g - mean_g) ** 2 for g in gaps)
        var_s = sum((s - mean_s) ** 2 for s in signals)
        correlation = cov / (var_g * var_s) ** 0.5
        assert correlation > 0.999, (
            f"NORO signal vs raw detector gap: r={correlation:.6f}"
        )

    def test_liquidity_balance_is_the_only_independent_input(self):
        """The residual information: holding the gap fixed, the signal still
        varies with how balanced the two venues' liquidity is -- by exactly
        a factor of two, end to end."""
        config = NoroConfig()
        gap_bps = 6.0
        extremes = []
        for w_a in (0.5, 0.999):
            liquidity = 1_000_000.0
            states = [
                venue("A", 100.0, liquidity=liquidity * w_a),
                venue("B", 100.0 * (1 + gap_bps / 10_000),
                      liquidity=liquidity * (1 - w_a)),
            ]
            extremes.append(opinion_for(states, config, "A", "B").signal)
        balanced, lopsided = extremes
        assert balanced == pytest.approx(2 * lopsided, rel=0.01), (
            "the full range of NORO's liquidity information is one factor of 2"
        )

    def test_a_third_venue_is_what_makes_the_signal_independent(self):
        """Contrast: with three venues the signal is no longer a function of
        the A-B gap alone -- the same gap gives opposite votes."""
        config = NoroConfig()
        a = venue("A", 100.0, liquidity=100_000.0)
        b = venue("B", 100.2, liquidity=100_000.0)
        near_buy = opinion_for(
            [a, b, venue("C", 100.0, liquidity=800_000.0)], config, "A", "B"
        ).signal
        near_sell = opinion_for(
            [a, b, venue("C", 100.5, liquidity=800_000.0)], config, "A", "B"
        ).signal
        assert near_sell < 0 < near_buy, (
            f"identical A-B gap, opposite verdicts: {near_buy:+.3f} vs "
            f"{near_sell:+.3f}"
        )
