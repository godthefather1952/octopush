"""Consensus: weighting, freshness, missing agents, and abstention."""

from __future__ import annotations

import pytest

from core.clock import ManualClock
from core.config import ConsensusConfig
from core.models.agent import AgentOpinion
from core.models.common import AgentId, DataQuality
from core.state import OpinionSlot
from strategies.consensus import ConsensusEngine
from tests.conftest import START_MS


def slot(
    agent: AgentId,
    signal: float,
    confidence: float,
    quality=DataQuality.FRESH,
    *,
    abstain: bool = False,
):
    return OpinionSlot(
        opinion=AgentOpinion(
            agent_id=agent,
            symbol="BTC-USD",
            created_at=START_MS,
            signal=signal,
            confidence=confidence,
            abstain=abstain,
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


class TestAbstention:
    """An agent that answered and declined to vote.

    Three states have to stay distinct. MISSING is a hole in the data and
    suspends a required agent's strategy. INFORMATIVE is a vote, including a
    signal of exactly zero when the evidence genuinely supports neutrality.
    ABSTAINING is neither: the agent is present, usable and complete, and
    supplies no scoring mass on either side of the weighted mean.

    The distinction is arithmetic, not cosmetic. A weighted mean divides by
    the weights it summed, so an agent contributing 0 to the numerator while
    contributing its weight to the denominator is voting against whatever
    everyone else concluded, in proportion to its own weight.
    """

    @staticmethod
    def _informative():
        return {
            AgentId.TIDAL: slot(AgentId.TIDAL, 0.2, 1.0),
            AgentId.ZEPHR: slot(AgentId.ZEPHR, 1.0, 1.0),
        }

    def _expected_without_noro(self, engine):
        weights = engine.config.weights
        numerator = weights[AgentId.TIDAL] * 1.0 * 0.2 + weights[AgentId.ZEPHR] * 1.0
        denominator = weights[AgentId.TIDAL] * 1.0 + weights[AgentId.ZEPHR] * 1.0
        return numerator / denominator

    # -- A: a required agent abstains ------------------------------------

    def test_a_required_abstention_leaves_the_result_complete(self, engine):
        result = combine(
            engine,
            {
                **self._informative(),
                AgentId.NORO: slot(AgentId.NORO, 0.0, 0.1, abstain=True),
            },
        )
        assert result.complete is True

    def test_an_abstaining_agent_is_reported_as_abstained_not_missing(self, engine):
        result = combine(
            engine,
            {
                **self._informative(),
                AgentId.NORO: slot(AgentId.NORO, 0.0, 0.1, abstain=True),
            },
        )
        assert AgentId.NORO in result.abstained_agents
        assert AgentId.NORO not in result.missing_agents
        assert AgentId.NORO not in result.degraded_agents

    def test_the_score_is_the_weighted_mean_of_the_voting_agents_only(self, engine):
        result = combine(
            engine,
            {
                **self._informative(),
                AgentId.NORO: slot(AgentId.NORO, 0.0, 0.1, abstain=True),
            },
        )
        assert result.score == pytest.approx(self._expected_without_noro(engine))

    def test_the_denominator_excludes_the_abstaining_agent(self, engine):
        """Stated directly: the score is identical to the one produced when
        NORO is not in the input at all. Only ``complete`` differs."""
        with_abstention = combine(
            engine,
            {
                **self._informative(),
                AgentId.NORO: slot(AgentId.NORO, 0.0, 0.1, abstain=True),
            },
        )
        without_noro = combine(engine, self._informative())
        assert with_abstention.score == pytest.approx(without_noro.score)
        assert with_abstention.complete is True
        assert without_noro.complete is False

    def test_an_abstaining_agent_gets_no_contribution_row(self, engine):
        result = combine(
            engine,
            {
                **self._informative(),
                AgentId.NORO: slot(AgentId.NORO, 0.0, 0.1, abstain=True),
            },
        )
        assert AgentId.NORO not in {c.agent_id for c in result.contributions}
        assert {c.agent_id for c in result.contributions} == {
            AgentId.TIDAL,
            AgentId.ZEPHR,
        }

    def test_the_abstaining_agents_confidence_cannot_change_the_score(self, engine):
        scores = {
            confidence: combine(
                engine,
                {
                    **self._informative(),
                    AgentId.NORO: slot(
                        AgentId.NORO, 0.0, confidence, abstain=True
                    ),
                },
            ).score
            for confidence in (0.0, 0.1, 0.5, 1.0)
        }
        assert len(set(scores.values())) == 1, scores

    def test_the_abstaining_agents_signal_cannot_change_the_score(self, engine):
        """It carries no mass, so even a nonsensical signal on an abstaining
        opinion is inert."""
        scores = {
            signal: combine(
                engine,
                {
                    **self._informative(),
                    AgentId.NORO: slot(AgentId.NORO, signal, 1.0, abstain=True),
                },
            ).score
            for signal in (-1.0, 0.0, 1.0)
        }
        assert len(set(scores.values())) == 1, scores

    # -- B: a required agent is missing ----------------------------------

    def test_a_missing_required_agent_is_still_incomplete(self, engine):
        result = combine(engine, self._informative())
        assert result.complete is False
        assert AgentId.NORO in result.missing_agents
        assert AgentId.NORO not in result.abstained_agents

    def test_a_stale_required_agent_is_missing_not_abstaining(self, engine):
        result = combine(
            engine,
            {
                **self._informative(),
                AgentId.NORO: slot(
                    AgentId.NORO, 1.0, 1.0, DataQuality.STALE, abstain=True
                ),
            },
        )
        assert AgentId.NORO in result.missing_agents
        assert AgentId.NORO not in result.abstained_agents
        assert result.complete is False

    # -- C: abstention is not a directional neutral ----------------------

    def test_abstention_and_a_neutral_vote_score_differently(self, engine):
        """The whole point of the new field, in one comparison. Both opinions
        carry ``signal=0``; only one of them declines to participate."""
        neutral = combine(
            engine,
            {
                **self._informative(),
                AgentId.NORO: slot(AgentId.NORO, 0.0, 1.0, abstain=False),
            },
        )
        abstaining = combine(
            engine,
            {
                **self._informative(),
                AgentId.NORO: slot(AgentId.NORO, 0.0, 1.0, abstain=True),
            },
        )
        assert neutral.score != pytest.approx(abstaining.score)
        assert abstaining.score > neutral.score, (
            "the neutral vote drags the positive consensus toward zero; the "
            "abstention leaves it to the agents that had evidence"
        )
        assert neutral.complete is abstaining.complete is True

    def test_a_neutral_vote_still_earns_a_contribution_row(self, engine):
        result = combine(
            engine,
            {
                **self._informative(),
                AgentId.NORO: slot(AgentId.NORO, 0.0, 1.0, abstain=False),
            },
        )
        assert AgentId.NORO in {c.agent_id for c in result.contributions}
        assert AgentId.NORO not in result.abstained_agents

    def test_a_low_confidence_neutral_vote_still_suppresses(self, engine):
        """The defect this build fixes, preserved as evidence: NORO's honest
        zero at confidence 0.1 was counted in the denominator, so it pulled
        the score down even though it claimed nothing."""
        suppressed = combine(
            engine,
            {
                **self._informative(),
                AgentId.NORO: slot(AgentId.NORO, 0.0, 0.1, abstain=False),
            },
        )
        assert suppressed.score < self._expected_without_noro(engine)

    # -- D: everyone abstains --------------------------------------------

    def test_when_every_agent_abstains_the_score_is_zero(self, engine):
        result = combine(
            engine,
            {
                agent: slot(agent, 1.0, 1.0, abstain=True)
                for agent in (AgentId.TIDAL, AgentId.NORO, AgentId.ZEPHR)
            },
        )
        assert result.score == 0.0
        assert result.agreement == 0.0
        assert result.contributions == []
        assert set(result.abstained_agents) == {
            AgentId.TIDAL,
            AgentId.NORO,
            AgentId.ZEPHR,
        }

    def test_an_all_abstain_result_is_complete_but_never_entered(self, engine):
        """Completeness is about presence, so it holds. Entry is about
        agreement, and an empty weighted mean is zero -- far below any
        threshold. This must not be special-cased into a trade."""
        result = combine(
            engine,
            {
                agent: slot(agent, 1.0, 1.0, abstain=True)
                for agent in (AgentId.TIDAL, AgentId.NORO, AgentId.ZEPHR)
            },
        )
        assert result.complete is True
        assert engine.entry_allowed(result) is False

    # -- E: a non-required agent abstains --------------------------------

    def test_a_non_required_abstention_leaves_the_score_untouched(self, engine):
        """LUMEN is optional. Whether it abstains or is simply absent, the
        agents with evidence decide."""
        assert AgentId.LUMEN not in engine.config.required_agents
        with_lumen = combine(
            engine,
            {
                **self._informative(),
                AgentId.NORO: slot(AgentId.NORO, 0.5, 1.0),
                AgentId.LUMEN: slot(AgentId.LUMEN, -1.0, 1.0, abstain=True),
            },
        )
        without_lumen = combine(
            engine,
            {
                **self._informative(),
                AgentId.NORO: slot(AgentId.NORO, 0.5, 1.0),
            },
        )
        assert with_lumen.score == pytest.approx(without_lumen.score)
        assert AgentId.LUMEN in with_lumen.abstained_agents

    # -- F: a degraded abstention ----------------------------------------

    def test_a_degraded_abstention_is_reported_in_both_lists(self, engine):
        """Degradation describes freshness and abstention describes
        participation; they are orthogonal, and a degraded agent that
        abstained is honestly both."""
        result = combine(
            engine,
            {
                **self._informative(),
                AgentId.NORO: slot(
                    AgentId.NORO, 0.0, 0.1, DataQuality.DEGRADED, abstain=True
                ),
            },
        )
        assert AgentId.NORO in result.degraded_agents
        assert AgentId.NORO in result.abstained_agents
        assert AgentId.NORO not in result.missing_agents

    def test_a_degraded_abstention_still_contributes_no_mass(self, engine):
        result = combine(
            engine,
            {
                **self._informative(),
                AgentId.NORO: slot(
                    AgentId.NORO, 0.0, 0.1, DataQuality.DEGRADED, abstain=True
                ),
            },
        )
        assert result.score == pytest.approx(self._expected_without_noro(engine))
        assert AgentId.NORO not in {c.agent_id for c in result.contributions}


class TestAbstentionDefaults:
    def test_an_opinion_is_informative_unless_it_says_otherwise(self):
        """Backward compatibility: every agent that never abstains, and every
        payload serialised before the field existed, keeps voting normally."""
        opinion = AgentOpinion(
            agent_id=AgentId.TIDAL,
            symbol="BTC-USD",
            created_at=START_MS,
            signal=0.5,
            confidence=0.9,
            expires_at=START_MS + 1_000,
            model_version="t",
        )
        assert opinion.abstain is False

    def test_a_payload_without_the_field_still_validates(self):
        legacy = {
            "agent_id": "TIDAL",
            "symbol": "BTC-USD",
            "created_at": START_MS,
            "signal": 0.5,
            "confidence": 0.9,
            "expires_at": START_MS + 1_000,
            "model_version": "t",
        }
        assert AgentOpinion.model_validate(legacy).abstain is False

    def test_the_field_round_trips_through_serialisation(self):
        opinion = AgentOpinion(
            agent_id=AgentId.NORO,
            symbol="BTC-USD",
            created_at=START_MS,
            signal=0.0,
            confidence=0.1,
            abstain=True,
            expires_at=START_MS + 1_000,
            model_version="t",
        )
        payload = opinion.to_json_dict()
        assert payload["abstain"] is True
        assert AgentOpinion.model_validate(payload).abstain is True

    def test_a_consensus_result_without_the_field_still_validates(self):
        from core.models.agent import ConsensusResult

        legacy = {
            "created_at": START_MS,
            "symbol": "BTC-USD",
            "strategy": "cross_venue",
            "score": 0.5,
            "agreement": 0.5,
        }
        assert ConsensusResult.model_validate(legacy).abstained_agents == []


class TestTidalAndNoroBothAbstain:
    """The two-venue shape after the TIDAL deadband landed.

    NORO has no independent valuation benchmark and abstains. TIDAL reads a
    book that says nothing directional and abstains. ZEPHR has real execution
    economics and votes. The trade is then decided on the one agent that
    actually has evidence -- which is the correct outcome, and the whole
    reason abstention had to be distinguishable from a missing agent.
    """

    @staticmethod
    def _opinions(tidal_signal: float | None = None, tidal_confidence: float = 0.9):
        opinions = {
            AgentId.NORO: slot(AgentId.NORO, 0.0, 0.1, abstain=True),
            AgentId.ZEPHR: slot(AgentId.ZEPHR, 1.0, 1.0),
        }
        opinions[AgentId.TIDAL] = (
            slot(AgentId.TIDAL, 0.0, tidal_confidence, abstain=True)
            if tidal_signal is None
            else slot(AgentId.TIDAL, tidal_signal, tidal_confidence)
        )
        return opinions

    def test_two_abstentions_still_leave_the_result_complete(self, engine):
        """Neither abstention is a hole in the data, so the strategy is not
        suspended."""
        result = combine(engine, self._opinions())
        assert result.complete is True

    def test_both_abstainers_are_reported_as_abstaining(self, engine):
        result = combine(engine, self._opinions())
        assert set(result.abstained_agents) == {AgentId.TIDAL, AgentId.NORO}

    def test_neither_abstainer_is_reported_as_missing(self, engine):
        result = combine(engine, self._opinions())
        assert AgentId.TIDAL not in result.missing_agents
        assert AgentId.NORO not in result.missing_agents

    def test_zephr_is_the_only_directional_contribution(self, engine):
        result = combine(engine, self._opinions())
        assert {c.agent_id for c in result.contributions} == {AgentId.ZEPHR}

    def test_the_score_is_zephrs_own_score(self, engine):
        """With one voter the weighted mean is that voter's signal -- no
        renormalisation artefact, no residual mass from the abstainers."""
        result = combine(engine, self._opinions())
        assert result.score == pytest.approx(1.0)
        assert result.agreement == pytest.approx(1.0)

    def test_entry_is_allowed_on_the_unchanged_threshold(self, engine):
        """No bypass and no special two-venue shortcut: 1.0 clears 0.60 the
        ordinary way, and RUNE still runs afterward."""
        result = combine(engine, self._opinions())
        assert engine.config.entry_threshold == pytest.approx(0.60)
        assert engine.entry_allowed(result) is True

    def test_a_weaker_lone_zephr_still_has_to_clear_the_threshold(self, engine):
        """Abstention does not lower the bar. If the only agent with evidence
        is unconvinced, the trade does not happen."""
        opinions = self._opinions()
        opinions[AgentId.ZEPHR] = slot(AgentId.ZEPHR, 0.4, 1.0)
        result = combine(engine, opinions)
        assert result.complete is True
        assert result.score == pytest.approx(0.4)
        assert engine.entry_allowed(result) is False


class TestTidalRetainsItsVeto:
    """The deadband silences noise, not evidence.

    A materially adverse microstructure read is above the deadband, so TIDAL
    votes, carries its full weight, and can pull an otherwise strong ZEPHR
    consensus below the entry threshold.
    """

    @staticmethod
    def _with_tidal(signal: float, confidence: float = 0.9):
        return {
            AgentId.TIDAL: slot(AgentId.TIDAL, signal, confidence),
            AgentId.NORO: slot(AgentId.NORO, 0.0, 0.1, abstain=True),
            AgentId.ZEPHR: slot(AgentId.ZEPHR, 1.0, 1.0),
        }

    def test_a_voting_tidal_appears_in_the_contributions(self, engine):
        result = combine(engine, self._with_tidal(-0.5))
        assert {c.agent_id for c in result.contributions} == {
            AgentId.TIDAL,
            AgentId.ZEPHR,
        }
        assert AgentId.TIDAL not in result.abstained_agents

    def test_an_adverse_tidal_lowers_the_score_below_zephr_alone(self, engine):
        alone = combine(
            engine,
            {
                AgentId.TIDAL: slot(AgentId.TIDAL, 0.0, 0.9, abstain=True),
                AgentId.NORO: slot(AgentId.NORO, 0.0, 0.1, abstain=True),
                AgentId.ZEPHR: slot(AgentId.ZEPHR, 1.0, 1.0),
            },
        )
        opposed = combine(engine, self._with_tidal(-0.5))
        assert opposed.score < alone.score

    def test_a_strongly_adverse_tidal_blocks_the_trade(self, engine):
        """The property that matters: TIDAL keeps the ability to stop a trade
        its own evidence argues against."""
        result = combine(engine, self._with_tidal(-0.5))
        assert result.complete is True
        assert engine.entry_allowed(result) is False

    @pytest.mark.parametrize("signal", [-1.0, -0.75, -0.5, -0.25, -0.11])
    def test_every_informative_negative_reading_suppresses_the_score(
        self, engine, signal
    ):
        alone = combine(
            engine,
            {
                AgentId.TIDAL: slot(AgentId.TIDAL, 0.0, 0.9, abstain=True),
                AgentId.NORO: slot(AgentId.NORO, 0.0, 0.1, abstain=True),
                AgentId.ZEPHR: slot(AgentId.ZEPHR, 1.0, 1.0),
            },
        )
        assert combine(engine, self._with_tidal(signal)).score < alone.score

    def test_a_supportive_tidal_is_not_penalised_for_voting(self, engine):
        """Symmetry check: an informative positive read participates too, and
        a lone ZEPHR at 1.0 cannot be improved on, so the score stays at the
        ceiling rather than being dragged down by the extra weight."""
        result = combine(engine, self._with_tidal(1.0))
        assert result.score == pytest.approx(1.0)
        assert engine.entry_allowed(result) is True
