"""Consensus: weighting, freshness handling, and the missing-agent rule."""

from __future__ import annotations

import pytest

from core.clock import ManualClock
from core.config import ConsensusConfig
from core.models.agent import AgentOpinion
from core.models.common import AgentId, DataQuality
from core.state import OpinionSlot
from strategies.consensus import ConsensusEngine
from tests.conftest import START_MS


def slot(agent: AgentId, signal: float, confidence: float, quality=DataQuality.FRESH):
    return OpinionSlot(
        opinion=AgentOpinion(
            agent_id=agent,
            symbol="BTC-USD",
            created_at=START_MS,
            signal=signal,
            confidence=confidence,
            expires_at=START_MS + 1_000,
            model_version="t",
        ),
        quality=quality,
    )


@pytest.fixture
def engine(clock: ManualClock) -> ConsensusEngine:
    return ConsensusEngine(ConsensusConfig(), clock)


def combine(engine, opinions):
    return engine.combine(symbol="BTC-USD", strategy="cross_venue", opinions=opinions)


class TestWeighting:
    def test_unanimous_agreement_scores_near_the_signal(self, engine):
        result = combine(
            engine,
            {
                AgentId.TIDAL: slot(AgentId.TIDAL, 1.0, 1.0),
                AgentId.NORO: slot(AgentId.NORO, 1.0, 1.0),
                AgentId.ZEPHR: slot(AgentId.ZEPHR, 1.0, 1.0),
            },
        )
        assert result.score == pytest.approx(1.0)
        assert result.complete

    def test_higher_weight_agents_move_the_score_more(self, engine):
        # ZEPHR carries weight 1.5, LUMEN 0.5; flipping ZEPHR must move the
        # score further than flipping LUMEN.
        base = {
            AgentId.TIDAL: slot(AgentId.TIDAL, 1.0, 1.0),
            AgentId.NORO: slot(AgentId.NORO, 1.0, 1.0),
            AgentId.ZEPHR: slot(AgentId.ZEPHR, 1.0, 1.0),
            AgentId.LUMEN: slot(AgentId.LUMEN, 1.0, 1.0),
        }
        flip_lumen = dict(base, **{AgentId.LUMEN: slot(AgentId.LUMEN, -1.0, 1.0)})
        flip_zephr = dict(base, **{AgentId.ZEPHR: slot(AgentId.ZEPHR, -1.0, 1.0)})
        assert combine(engine, flip_zephr).score < combine(engine, flip_lumen).score

    def test_confidence_scales_influence(self, engine):
        confident = combine(
            engine,
            {
                AgentId.NORO: slot(AgentId.NORO, 1.0, 1.0),
                AgentId.ZEPHR: slot(AgentId.ZEPHR, -1.0, 1.0),
                AgentId.TIDAL: slot(AgentId.TIDAL, 0.0, 1.0),
            },
        )
        # Same signals, but ZEPHR barely believes its own view.
        unsure = combine(
            engine,
            {
                AgentId.NORO: slot(AgentId.NORO, 1.0, 1.0),
                AgentId.ZEPHR: slot(AgentId.ZEPHR, -1.0, 0.1),
                AgentId.TIDAL: slot(AgentId.TIDAL, 0.0, 1.0),
            },
        )
        assert unsure.score > confident.score

    def test_contribution_shares_sum_to_one(self, engine):
        result = combine(
            engine,
            {
                AgentId.TIDAL: slot(AgentId.TIDAL, 0.4, 0.9),
                AgentId.NORO: slot(AgentId.NORO, -0.8, 0.7),
                AgentId.ZEPHR: slot(AgentId.ZEPHR, 0.6, 0.5),
            },
        )
        assert sum(c.contribution_share for c in result.contributions) == pytest.approx(1.0)

    def test_dissent_still_shows_as_contribution(self, engine):
        result = combine(
            engine,
            {
                AgentId.TIDAL: slot(AgentId.TIDAL, 0.1, 0.5),
                AgentId.NORO: slot(AgentId.NORO, 1.0, 1.0),
                AgentId.ZEPHR: slot(AgentId.ZEPHR, -1.0, 1.0),
            },
        )
        zephr = next(c for c in result.contributions if c.agent_id is AgentId.ZEPHR)
        assert zephr.contribution_share > 0
        assert zephr.weighted_signal < 0


class TestFreshness:
    def test_degraded_agents_are_down_weighted_not_dropped(self, engine):
        fresh = combine(
            engine,
            {
                AgentId.TIDAL: slot(AgentId.TIDAL, -1.0, 1.0),
                AgentId.NORO: slot(AgentId.NORO, 1.0, 1.0),
                AgentId.ZEPHR: slot(AgentId.ZEPHR, 1.0, 1.0),
            },
        )
        degraded = combine(
            engine,
            {
                AgentId.TIDAL: slot(AgentId.TIDAL, -1.0, 1.0, DataQuality.DEGRADED),
                AgentId.NORO: slot(AgentId.NORO, 1.0, 1.0),
                AgentId.ZEPHR: slot(AgentId.ZEPHR, 1.0, 1.0),
            },
        )
        # TIDAL still counts against the trade, but less.
        assert degraded.score > fresh.score
        assert AgentId.TIDAL in degraded.degraded_agents
        assert AgentId.TIDAL not in degraded.missing_agents

    def test_stale_agents_are_excluded_and_reported_missing(self, engine):
        result = combine(
            engine,
            {
                AgentId.TIDAL: slot(AgentId.TIDAL, -1.0, 1.0, DataQuality.STALE),
                AgentId.NORO: slot(AgentId.NORO, 1.0, 1.0),
                AgentId.ZEPHR: slot(AgentId.ZEPHR, 1.0, 1.0),
            },
        )
        assert AgentId.TIDAL in result.missing_agents
        assert not result.complete
        assert all(c.agent_id is not AgentId.TIDAL for c in result.contributions)

    def test_a_stale_agent_is_not_a_neutral_vote(self, engine):
        """The key invariant: excluding a stale agent must not be the same as
        it having voted zero."""
        stale = combine(
            engine,
            {
                AgentId.TIDAL: slot(AgentId.TIDAL, 1.0, 1.0, DataQuality.STALE),
                AgentId.NORO: slot(AgentId.NORO, 1.0, 1.0),
                AgentId.ZEPHR: slot(AgentId.ZEPHR, 1.0, 1.0),
            },
        )
        neutral = combine(
            engine,
            {
                AgentId.TIDAL: slot(AgentId.TIDAL, 0.0, 1.0),
                AgentId.NORO: slot(AgentId.NORO, 1.0, 1.0),
                AgentId.ZEPHR: slot(AgentId.ZEPHR, 1.0, 1.0),
            },
        )
        assert stale.score != pytest.approx(neutral.score)
        # And crucially, only one of them is safe to act on.
        assert not stale.complete and neutral.complete


class TestCompleteness:
    def test_missing_required_agent_makes_the_result_incomplete(self, engine):
        result = combine(
            engine,
            {
                AgentId.NORO: slot(AgentId.NORO, 1.0, 1.0),
                AgentId.ZEPHR: slot(AgentId.ZEPHR, 1.0, 1.0),
            },
        )
        assert not result.complete
        assert AgentId.TIDAL in result.missing_agents

    def test_missing_optional_agent_is_fine(self, engine):
        result = combine(
            engine,
            {
                AgentId.TIDAL: slot(AgentId.TIDAL, 1.0, 1.0),
                AgentId.NORO: slot(AgentId.NORO, 1.0, 1.0),
                AgentId.ZEPHR: slot(AgentId.ZEPHR, 1.0, 1.0),
            },
        )
        # LUMEN is absent and that is acceptable: it is not required.
        assert result.complete
        assert AgentId.LUMEN in result.missing_agents

    def test_no_opinions_at_all_scores_zero_but_is_incomplete(self, engine):
        result = combine(engine, {})
        assert result.score == 0.0
        assert not result.complete

    def test_entry_needs_completeness_as_well_as_score(self, engine):
        strong_but_incomplete = combine(
            engine,
            {
                AgentId.NORO: slot(AgentId.NORO, 1.0, 1.0),
                AgentId.ZEPHR: slot(AgentId.ZEPHR, 1.0, 1.0),
            },
        )
        assert strong_but_incomplete.agreement > engine.config.entry_threshold
        assert not engine.entry_allowed(strong_but_incomplete)


class TestThresholds:
    def test_exit_threshold_is_below_entry(self, engine):
        assert engine.config.exit_threshold < engine.config.entry_threshold

    def test_hysteresis_band_holds_a_position(self, engine):
        opinions = {
            AgentId.TIDAL: slot(AgentId.TIDAL, 0.0, 1.0),
            AgentId.NORO: slot(AgentId.NORO, 0.75, 1.0),
            AgentId.ZEPHR: slot(AgentId.ZEPHR, 0.75, 1.0),
        }
        result = combine(engine, opinions)
        # Inside the band a position is held even though it would not be
        # opened again from scratch.
        if engine.config.exit_threshold <= result.agreement < engine.config.entry_threshold:
            assert engine.continuation_allowed(result)
            assert not engine.entry_allowed(result)
