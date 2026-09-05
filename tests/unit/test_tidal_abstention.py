"""TIDAL's microstructure deadband: when a weak read stops being a vote.

**What the calibration measured.** Across 206 two-venue opportunities TIDAL's
signal ran from -0.126 to +0.061, median -0.057, while ZEPHR independently
found 19-24 bps of post-cost edge on every single one. Consensus topped out at
0.571 against a 0.60 threshold, and reaching it would have needed a TIDAL
signal of +0.124 — twice the strongest reading observed. Noise around zero was
being counted as directional evidence against the trade.

**What changed.** Only participation. The microstructure score itself is
untouched: same book imbalance, same trade-flow term, same coefficients, same
volatility penalty. Below the deadband TIDAL now abstains — present, fresh,
healthy and inconclusive — instead of publishing a small negative vote.

Three states have to stay distinct, and the tests below pin all three:

* **Missing** — the book could not be read at all. ``evaluate`` returns
  ``None`` and a required TIDAL suspends the strategy.
* **Abstaining** — the book was read and says nothing directional.
* **Informative** — the book says something, in either direction. TIDAL keeps
  its full ability to oppose a trade.
"""

from __future__ import annotations

import pytest

from agents.tidal.agent import Tidal
from core.bus import InMemoryEventBus
from core.clock import ManualClock
from core.config import Settings, TidalConfig, load_settings, simulated_venues
from core.health import HealthRegistry
from core.models.common import DataQuality, Side
from core.models.market import BookMetrics, VenueMarketState
from core.models.opportunity import Opportunity, OpportunityKind, OpportunityLeg
from tests.conftest import START_MS

SYMBOL = "BTC-USD"
BUY_VENUE = "VENUE_A"
SELL_VENUE = "VENUE_B"

#: The production blend, restated so the tests can predict the raw score
#: exactly and then assert production agrees. Duplicated deliberately: a test
#: that reads the number back out of the agent could not detect the agent
#: computing the wrong one.
#: Trade flow (production's 0.3 term) is held at zero throughout this module,
#: so the deadband is exercised on book imbalance alone.
IMBALANCE_WEIGHT = 0.7


def expected_raw(
    imbalance_buy: float, imbalance_sell: float, *, vol_penalty: float = 0.0
) -> float:
    """``mean(0.7 * imbalance * side_sign) - vol_penalty``, clamped.

    Trade flow is held at zero throughout this module so the deadband is
    exercised on one variable at a time.
    """
    buy = IMBALANCE_WEIGHT * imbalance_buy
    sell = IMBALANCE_WEIGHT * -imbalance_sell
    return max(-1.0, min(1.0, (buy + sell) / 2 - vol_penalty))


def imbalances_for(target: float) -> tuple[float, float]:
    """Book imbalances that produce a raw signal of about ``target``.

    Symmetric about zero: the buy venue leans bid-heavy by as much as the sell
    venue leans ask-heavy, so ``0.35 * (imb_buy - imb_sell) == target``.
    """
    return target / IMBALANCE_WEIGHT, -target / IMBALANCE_WEIGHT


def venue_state(
    venue: str,
    *,
    imbalance: float,
    vol_bps: float = 0.0,
    quality: DataQuality = DataQuality.FRESH,
    symbol: str = SYMBOL,
) -> VenueMarketState:
    """One venue's state with the microstructure inputs set explicitly.

    Age is zero, so freshness is 1.0 and confidence is the full 0.90 the
    calibration observed — holding confidence constant while the deadband is
    the variable under test.
    """
    metrics = BookMetrics(
        best_bid=99.99,
        best_ask=100.01,
        mid=100.0,
        microprice=100.0,
        spread=0.02,
        spread_bps=2.0,
        bid_depth_notional=100_000.0,
        ask_depth_notional=100_000.0,
        imbalance=imbalance,
        short_vol_bps=vol_bps,
        buy_volume=0.0,
        sell_volume=0.0,
    )
    return VenueMarketState(
        venue=venue,
        symbol=symbol,
        metrics=metrics,
        book=None,
        exchange_ts=START_MS,
        last_update_ts=START_MS,
        as_of=START_MS,
        quality=quality,
        latency_ms=5.0,
        connected=True,
    )


def opportunity(*, gross_edge_bps: float = 10.0) -> Opportunity:
    opp = Opportunity(
        created_at=START_MS,
        source_data_timestamp=START_MS,
        kind=OpportunityKind.CROSS_VENUE_DISLOCATION,
        strategy="cross_venue",
        symbol=SYMBOL,
        legs=[
            OpportunityLeg(
                venue=BUY_VENUE, symbol=SYMBOL, side=Side.BUY, reference_price=100.0
            ),
            OpportunityLeg(
                venue=SELL_VENUE, symbol=SYMBOL, side=Side.SELL, reference_price=100.1
            ),
        ],
        gross_edge_bps=gross_edge_bps,
        expires_at=START_MS + 2_000,
        reason_codes=["CROSS_VENUE_DISLOCATION"],
    )
    opp.correlation_id = opp.opportunity_id
    return opp


def build_tidal(
    states: dict[tuple[str, str], VenueMarketState | None],
    config: TidalConfig | None = None,
) -> Tidal:
    """A real ``Tidal`` whose venue states are supplied rather than derived.

    ``venue_state`` recomputes metrics from live books, which makes an exact
    imbalance impossible to construct. Substituting the accessor — and only
    the accessor — leaves the whole opinion path under test while letting the
    microstructure inputs be set to the values the deadband is about.
    """
    clock = ManualClock(START_MS)
    settings: Settings = load_settings().model_copy(
        update={"venues": simulated_venues(), "tidal": config or TidalConfig()}
    )
    tidal = Tidal(
        bus=InMemoryEventBus(raise_on_handler_error=True),
        clock=clock,
        settings=settings,
        health=HealthRegistry(clock=clock),
    )
    tidal.venue_state = (  # type: ignore[method-assign]
        lambda venue, symbol, now_ms=None: states.get((venue, symbol))
    )
    return tidal


def opinion_at(target: float, config: TidalConfig | None = None):
    """The opinion TIDAL publishes for a raw microstructure score of
    ``target``."""
    buy, sell = imbalances_for(target)
    tidal = build_tidal(
        {
            (BUY_VENUE, SYMBOL): venue_state(BUY_VENUE, imbalance=buy),
            (SELL_VENUE, SYMBOL): venue_state(SELL_VENUE, imbalance=sell),
        },
        config,
    )
    return tidal.evaluate(opportunity(), START_MS)


# ======================================================================
# the builder is exact — prove it before relying on it
# ======================================================================


class TestTheScenarioBuilderMatchesProduction:
    @pytest.mark.parametrize("target", [-0.6, -0.3, -0.15, 0.15, 0.3, 0.6])
    def test_the_published_signal_is_the_predicted_raw_score(self, target):
        """Well outside the deadband, so the published signal IS the raw
        score. If this drifts, every deadband assertion below is measuring
        the wrong number."""
        opinion = opinion_at(target)
        buy, sell = imbalances_for(target)
        assert opinion.signal == pytest.approx(expected_raw(buy, sell), abs=1e-12)
        assert opinion.signal == pytest.approx(target, abs=1e-12)

    def test_confidence_is_the_full_freshness_value(self):
        """Held constant at 0.90 across every case here, so the deadband is
        the only variable."""
        assert opinion_at(0.5).confidence == pytest.approx(0.90)
        assert opinion_at(0.02).confidence == pytest.approx(0.90)


# ======================================================================
# A / B — weak evidence abstains
# ======================================================================


class TestWeakEvidenceAbstains:
    def test_a_weak_positive_reading_abstains(self):
        opinion = opinion_at(0.05)
        assert opinion is not None, "a readable book is never missing"
        assert opinion.abstain is True
        assert opinion.signal == 0.0
        assert "MICROSTRUCTURE_INCONCLUSIVE" in opinion.reason_codes

    def test_a_weak_negative_reading_abstains(self):
        opinion = opinion_at(-0.05)
        assert opinion.abstain is True
        assert opinion.signal == 0.0
        assert "MICROSTRUCTURE_INCONCLUSIVE" in opinion.reason_codes

    @pytest.mark.parametrize("target", [-0.0999, -0.057, -0.01, 0.0, 0.01, 0.0608])
    def test_the_measured_calibration_band_abstains_throughout(self, target):
        """Every value the calibration actually observed — median -0.057,
        maximum +0.0608 — falls inside the deadband."""
        opinion = opinion_at(target)
        assert opinion.abstain is True
        assert opinion.signal == 0.0

    def test_the_raw_score_is_retained_in_the_detail(self):
        """The reading is not discarded. Abstention withholds the vote, not
        the observation, and an operator must still be able to see what the
        book actually said."""
        opinion = opinion_at(-0.05)
        buy, sell = imbalances_for(-0.05)
        assert opinion.detail["raw_microstructure_signal"] == pytest.approx(
            expected_raw(buy, sell), abs=1e-6
        )
        assert opinion.detail["raw_microstructure_signal"] != 0.0

    def test_an_abstention_claims_neither_support_nor_opposition(self):
        opinion = opinion_at(-0.05)
        assert "MICROSTRUCTURE_SUPPORTS" not in opinion.reason_codes
        assert "MICROSTRUCTURE_UNSUPPORTIVE" not in opinion.reason_codes

    def test_the_configured_threshold_is_reported(self):
        opinion = opinion_at(-0.05)
        assert opinion.detail["informative_signal_threshold"] == pytest.approx(
            TidalConfig().informative_signal_threshold
        )


# ======================================================================
# C / D — strong evidence votes, in both directions
# ======================================================================


class TestStrongEvidenceVotes:
    def test_a_strong_positive_reading_votes(self):
        opinion = opinion_at(0.35)
        assert opinion.abstain is False
        assert opinion.signal > 0
        assert "MICROSTRUCTURE_SUPPORTS" in opinion.reason_codes
        assert "MICROSTRUCTURE_INCONCLUSIVE" not in opinion.reason_codes

    def test_a_strong_negative_reading_votes(self):
        """TIDAL must keep its veto. The deadband silences noise, not
        evidence."""
        opinion = opinion_at(-0.35)
        assert opinion.abstain is False
        assert opinion.signal < 0
        assert "MICROSTRUCTURE_UNSUPPORTIVE" in opinion.reason_codes
        assert "MICROSTRUCTURE_INCONCLUSIVE" not in opinion.reason_codes

    @pytest.mark.parametrize("target", [0.101, 0.2, 0.5, 0.9])
    def test_the_signal_is_the_raw_score_when_voting(self, target):
        for signed in (target, -target):
            opinion = opinion_at(signed)
            assert opinion.abstain is False
            assert opinion.signal == pytest.approx(signed, abs=1e-9)

    def test_tidal_does_not_become_an_always_abstaining_agent(self):
        votes = [opinion_at(t) for t in (-0.9, -0.4, 0.4, 0.9)]
        assert all(o.abstain is False for o in votes)
        assert [o.signal < 0 for o in votes] == [True, True, False, False]


# ======================================================================
# E / F — the boundary is deterministic
# ======================================================================


class TestBoundarySemantics:
    """``abs(raw) < threshold`` abstains; ``abs(raw) == threshold`` votes.

    The deadband is the OPEN interval ``(-threshold, +threshold)``. Each test
    sets the threshold to exactly the raw score the scenario produces, so the
    comparison lands on the boundary regardless of how the float rounds.
    """

    @staticmethod
    def _at_exactly(target: float):
        buy, sell = imbalances_for(target)
        boundary = abs(expected_raw(buy, sell))
        return opinion_at(
            target, TidalConfig(informative_signal_threshold=boundary)
        )

    def test_exactly_the_positive_threshold_votes(self):
        opinion = self._at_exactly(0.10)
        assert opinion.abstain is False
        assert opinion.signal > 0

    def test_exactly_the_negative_threshold_votes(self):
        opinion = self._at_exactly(-0.10)
        assert opinion.abstain is False
        assert opinion.signal < 0

    def test_just_inside_the_band_abstains(self):
        """The same scenario with the threshold nudged up by one ulp-scale
        step: now strictly inside, so it abstains."""
        buy, sell = imbalances_for(0.10)
        boundary = abs(expected_raw(buy, sell))
        opinion = opinion_at(
            0.10, TidalConfig(informative_signal_threshold=boundary + 1e-9)
        )
        assert opinion.abstain is True

    def test_a_zero_threshold_never_abstains(self):
        """``abs(raw) < 0`` is false for every real number, including zero, so
        a zero deadband restores the pre-change behaviour exactly."""
        for target in (-0.05, 0.0, 0.05):
            opinion = opinion_at(
                target, TidalConfig(informative_signal_threshold=0.0)
            )
            assert opinion.abstain is False


# ======================================================================
# G — missing is not abstaining
# ======================================================================


class TestMissingIsNotAbstention:
    def test_an_absent_book_returns_no_opinion(self):
        tidal = build_tidal({})
        assert tidal.evaluate(opportunity(), START_MS) is None

    @pytest.mark.parametrize(
        "quality",
        [DataQuality.DEGRADED, DataQuality.STALE, DataQuality.UNAVAILABLE],
    )
    def test_an_unusable_book_returns_no_opinion(self, quality):
        buy, sell = imbalances_for(0.5)
        tidal = build_tidal(
            {
                (BUY_VENUE, SYMBOL): venue_state(BUY_VENUE, imbalance=buy),
                (SELL_VENUE, SYMBOL): venue_state(
                    SELL_VENUE, imbalance=sell, quality=quality
                ),
            }
        )
        assert tidal.evaluate(opportunity(), START_MS) is None, (
            "an inability to inspect the market is MISSING, not abstention"
        )

    def test_one_missing_leg_is_enough_to_return_none(self):
        buy, _ = imbalances_for(0.5)
        tidal = build_tidal({(BUY_VENUE, SYMBOL): venue_state(BUY_VENUE, imbalance=buy)})
        assert tidal.evaluate(opportunity(), START_MS) is None


# ======================================================================
# volatility reason codes, and what does not control participation
# ======================================================================


class TestVolatilityReasonCode:
    """The penalty is applied BEFORE the deadband, exactly as before.

    This pass changed participation, not the score, so a fast market still
    subtracts from the reading and can carry it into the band on its own.
    """

    GROSS_EDGE_BPS = 10.0

    @classmethod
    def _penalty(cls, vol_bps: float) -> float:
        return min(0.8, vol_bps / cls.GROSS_EDGE_BPS * 0.25)

    @classmethod
    def _states(cls, support_target: float, vol_bps: float):
        buy, sell = imbalances_for(support_target)
        return {
            (BUY_VENUE, SYMBOL): venue_state(
                BUY_VENUE, imbalance=buy, vol_bps=vol_bps
            ),
            (SELL_VENUE, SYMBOL): venue_state(
                SELL_VENUE, imbalance=sell, vol_bps=vol_bps
            ),
        }

    @classmethod
    def _scenario(cls, final_raw: float, vol_bps: float):
        """A market whose raw score lands on ``final_raw`` AFTER the
        volatility penalty, so the two effects can be separated."""
        tidal = build_tidal(cls._states(final_raw + cls._penalty(vol_bps), vol_bps))
        return tidal.evaluate(
            opportunity(gross_edge_bps=cls.GROSS_EDGE_BPS), START_MS
        )

    def test_the_scenario_lands_where_intended(self):
        opinion = self._scenario(0.0, vol_bps=20.0)
        assert opinion.detail["vol_penalty"] == pytest.approx(0.5)
        assert opinion.detail["raw_microstructure_signal"] == pytest.approx(
            0.0, abs=1e-6
        )

    def test_a_fast_market_is_still_flagged_while_abstaining(self):
        """An opinion may be abstaining *and* carry a volatility warning: the
        directional read is inconclusive, and the market is still moving fast
        enough to be worth saying so."""
        opinion = self._scenario(0.0, vol_bps=20.0)
        assert opinion.detail["vol_penalty"] > 0.3
        assert "VOLATILITY_EXCEEDS_EDGE" in opinion.reason_codes
        assert "MICROSTRUCTURE_INCONCLUSIVE" in opinion.reason_codes
        assert opinion.abstain is True

    def test_a_reason_code_never_decides_participation(self):
        """Only ``abstain`` does. The volatility flag rides along on votes as
        readily as on abstentions."""
        strong = self._scenario(0.3, vol_bps=14.0)
        assert strong.detail["vol_penalty"] == pytest.approx(0.35)
        assert "VOLATILITY_EXCEEDS_EDGE" in strong.reason_codes
        assert strong.abstain is False
        assert strong.signal == pytest.approx(0.3, abs=1e-9)

    def test_the_volatility_penalty_can_push_a_reading_into_the_band(self):
        """Same book, two volatility regimes: the calm read votes, the fast
        one is dragged inside the deadband and abstains."""
        opinions = {
            vol_bps: build_tidal(self._states(0.2, vol_bps)).evaluate(
                opportunity(gross_edge_bps=self.GROSS_EDGE_BPS), START_MS
            )
            for vol_bps in (0.0, 6.0)
        }
        calm, fast = opinions[0.0], opinions[6.0]
        assert calm.abstain is False, "0.20 clears the 0.10 deadband"
        assert fast.detail["vol_penalty"] == pytest.approx(0.15)
        assert fast.detail["raw_microstructure_signal"] == pytest.approx(
            0.05, abs=1e-6
        )
        assert fast.abstain is True, "0.20 - 0.15 = 0.05 is inside the band"


# ======================================================================
# configuration
# ======================================================================


class TestTidalConfig:
    def test_the_default_threshold_is_the_documented_deadband(self):
        assert TidalConfig().informative_signal_threshold == pytest.approx(0.10)

    def test_it_is_not_tuned_to_the_algebraic_entry_requirement(self):
        """The calibration showed 0.1238 was the TIDAL signal needed to reach
        the entry threshold. Setting the deadband to that number would make it
        an entry threshold wearing a different name; it is an
        information-strength boundary and is deliberately below it."""
        assert TidalConfig().informative_signal_threshold < 0.1238

    @pytest.mark.parametrize("value", [-0.01, 1.0, 1.5, float("nan"), float("inf")])
    def test_meaningless_thresholds_are_refused(self, value):
        import pydantic

        with pytest.raises(pydantic.ValidationError):
            TidalConfig(informative_signal_threshold=value)

    @pytest.mark.parametrize("value", [0.0, 0.05, 0.10, 0.5, 0.99])
    def test_the_valid_range_is_accepted(self, value):
        assert TidalConfig(
            informative_signal_threshold=value
        ).informative_signal_threshold == pytest.approx(value)

    def test_the_threshold_is_wired_through_settings(self):
        settings = load_settings()
        assert isinstance(settings.tidal, TidalConfig)
        assert settings.tidal.informative_signal_threshold == pytest.approx(0.10)

    def test_a_wider_band_silences_more(self):
        wide = TidalConfig(informative_signal_threshold=0.5)
        assert opinion_at(0.3, wide).abstain is True
        assert opinion_at(0.3).abstain is False


# ======================================================================
# determinism and serialisation
# ======================================================================


class TestDeterminismAndSerialisation:
    def test_the_same_inputs_always_produce_the_same_decision(self):
        results = {
            (opinion_at(-0.05).abstain, opinion_at(-0.05).signal) for _ in range(5)
        }
        assert len(results) == 1

    def test_the_decision_survives_serialisation(self):
        payload = opinion_at(-0.05).to_json_dict()
        assert payload["abstain"] is True
        assert payload["signal"] == 0.0
        assert payload["detail"]["raw_microstructure_signal"] != 0.0

    def test_a_vote_serialises_as_a_vote(self):
        payload = opinion_at(0.4).to_json_dict()
        assert payload["abstain"] is False
        assert payload["signal"] == pytest.approx(0.4, abs=1e-9)

    def test_the_model_version_records_the_semantic_change(self):
        """A tidal-0.1 opinion and a tidal-0.2 opinion carrying the same
        signal do not mean the same thing to consensus."""
        assert opinion_at(0.4).model_version == "tidal-0.2"
