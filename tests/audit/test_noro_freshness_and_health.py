"""P3-6 / P3-7 regression: quality, timestamps, health, leg coverage.

Four questions, grouped because they all concern what NORO knows about the
age and breadth of its own inputs.

* **Quality (unchanged)** -- only FRESH contributes, and no stale valuation
  survives a later snapshot. This was already correct and stays asserted.
* **Source timestamp (P3-7)** -- ``evaluate`` used to stamp the MarketState's
  global ``source_data_timestamp``: the newest observation anywhere in the
  market, including symbols the valuation never touched. Phase 1 established
  (TIDAL-H4) that a multi-leg economic claim is only as fresh as its OLDEST
  contributor, and added ``MarketState.source_data_timestamp_for`` for exactly
  that. NORO now uses it, over the contributors the conclusion actually rests
  on, and fails closed when one has no exchange observation.
* **Health (P3-6)** -- ``_heartbeat`` used to count symbols that produced *a*
  valuation, which one venue was enough for, so NORO reported HEALTHY on a
  market where no cross-venue valuation was possible at all. Readiness now
  means at least one symbol with the two contributors a cross-venue valuation
  needs.
* **Leg coverage (unchanged)** -- how ``evaluate`` fails when a leg is not
  priceable.
"""

from __future__ import annotations

import pytest

from core.config import NoroConfig
from core.models.common import AgentId, DataQuality, Side
from core.models.opportunity import Opportunity, OpportunityKind, OpportunityLeg
from core.models.ops import HealthStatus
from tests.audit.helpers import START_MS, SYMBOL, market, opportunity, venue
from tests.audit.noro_fixtures import build_noro


@pytest.fixture
def config() -> NoroConfig:
    return NoroConfig()


# ======================================================================
# Section 17 — quality filtering
# ======================================================================


class TestOnlyFreshContributes:
    @pytest.mark.parametrize(
        ("quality", "contributes"),
        [
            (DataQuality.FRESH, True),
            (DataQuality.DEGRADED, False),
            (DataQuality.STALE, False),
            (DataQuality.UNAVAILABLE, False),
        ],
    )
    def test_each_quality_level(self, config, quality, contributes):
        noro = build_noro(config)
        noro.on_market_state(
            market(venue("A", 100.0), venue("B", 101.0, quality=quality))
        )
        fair = noro.fair_value(SYMBOL)
        names = [v.venue for v in fair.venues]
        assert ("B" in names) is contributes

    @pytest.mark.parametrize(
        ("first", "second", "expected"),
        [
            (DataQuality.FRESH, DataQuality.FRESH, 2),
            (DataQuality.FRESH, DataQuality.DEGRADED, 1),
            (DataQuality.FRESH, DataQuality.STALE, 1),
            (DataQuality.FRESH, DataQuality.UNAVAILABLE, 1),
            (DataQuality.DEGRADED, DataQuality.DEGRADED, 0),
            (DataQuality.STALE, DataQuality.STALE, 0),
        ],
    )
    def test_mixed_combinations(self, config, first, second, expected):
        noro = build_noro(config)
        noro.on_market_state(
            market(
                venue("A", 100.0, quality=first),
                venue("B", 101.0, quality=second),
            )
        )
        fair = noro.fair_value(SYMBOL)
        assert (0 if fair is None else len(fair.venues)) == expected

    def test_degraded_is_excluded_entirely_not_downweighted(self, config):
        """``DataQuality.is_usable`` is FRESH-only, so DEGRADED contributes
        nothing at all -- unlike consensus, which down-weights it."""
        noro = build_noro(config)
        noro.on_market_state(
            market(
                venue("A", 100.0, liquidity=1_000.0),
                venue("B", 200.0, liquidity=10_000_000.0, quality=DataQuality.DEGRADED),
            )
        )
        assert noro.fair_value(SYMBOL).fair_value == pytest.approx(100.0)


# ======================================================================
# Section 32 — no stale fair value survives
# ======================================================================


class TestFairValueLifecycle:
    def test_the_map_is_rebuilt_from_scratch_on_every_snapshot(self, config):
        noro = build_noro(config)
        noro.on_market_state(market(venue("A", 100.0), venue("B", 101.0)))
        assert noro.fair_value(SYMBOL) is not None

        noro.on_market_state(
            market(
                venue("A", 100.0, quality=DataQuality.STALE),
                venue("B", 101.0, quality=DataQuality.STALE),
            )
        )
        assert noro.fair_value(SYMBOL) is None, (
            "no valuation from the previous snapshot may survive"
        )

    def test_and_an_old_opportunity_then_gets_no_opinion(self, config):
        noro = build_noro(config)
        noro.on_market_state(market(venue("A", 100.0), venue("B", 101.0)))
        opp = opportunity("A", "B")
        assert noro.evaluate(opp, START_MS) is not None

        noro.on_market_state(
            market(
                venue("A", 100.0, quality=DataQuality.STALE),
                venue("B", 101.0, quality=DataQuality.STALE),
            )
        )
        assert noro.evaluate(opp, START_MS + 100) is None, (
            "a missing valuation must be missing, never a fabricated neutral"
        )

    def test_a_venue_dropping_out_removes_only_that_venue(self, config):
        noro = build_noro(config)
        noro.on_market_state(
            market(venue("A", 100.0), venue("B", 101.0), venue("C", 102.0))
        )
        assert len(noro.fair_value(SYMBOL).venues) == 3
        noro.on_market_state(
            market(
                venue("A", 100.0),
                venue("B", 101.0, quality=DataQuality.STALE),
                venue("C", 102.0),
            )
        )
        assert [v.venue for v in noro.fair_value(SYMBOL).venues] == ["A", "C"]

    def test_evaluate_returns_none_when_a_leg_venue_went_stale(self, config):
        noro = build_noro(config)
        noro.on_market_state(
            market(venue("A", 100.0), venue("B", 101.0), venue("C", 102.0))
        )
        noro.on_market_state(
            market(
                venue("A", 100.0),
                venue("B", 101.0, quality=DataQuality.STALE),
                venue("C", 102.0),
            )
        )
        assert noro.evaluate(opportunity("A", "B"), START_MS) is None, (
            "one leg missing from the benchmark fails the whole opinion closed"
        )


# ======================================================================
# Section 18 — symbol isolation
# ======================================================================


class TestSymbolIsolation:
    def test_one_symbols_venues_never_reach_another(self, config):
        noro = build_noro(config, symbols=[SYMBOL, "ETH-USD"])
        noro.on_market_state(
            market(
                venue("A", 100.0, symbol=SYMBOL, liquidity=50_000.0),
                venue("B", 100.2, symbol=SYMBOL, liquidity=50_000.0),
                venue("A", 3_000.0, symbol="ETH-USD", liquidity=9_000_000.0),
                venue("B", 3_050.0, symbol="ETH-USD", liquidity=9_000_000.0),
            )
        )
        assert noro.fair_value(SYMBOL).fair_value == pytest.approx(100.1)
        assert noro.fair_value("ETH-USD").fair_value == pytest.approx(3_025.0)

    def test_changing_one_symbol_leaves_the_other_untouched(self, config):
        noro = build_noro(config, symbols=[SYMBOL, "ETH-USD"])
        btc = [venue("A", 100.0, symbol=SYMBOL), venue("B", 100.2, symbol=SYMBOL)]
        noro.on_market_state(
            market(*btc, venue("A", 3_000.0, symbol="ETH-USD"),
                   venue("B", 3_010.0, symbol="ETH-USD"))
        )
        before = noro.fair_value(SYMBOL)
        noro.on_market_state(
            market(*btc, venue("A", 9_999.0, symbol="ETH-USD"),
                   venue("B", 1.0, symbol="ETH-USD"))
        )
        assert noro.fair_value(SYMBOL) == before

    def test_a_symbol_with_no_venues_simply_has_no_fair_value(self, config):
        noro = build_noro(config, symbols=[SYMBOL, "ETH-USD"])
        noro.on_market_state(market(venue("A", 100.0), venue("B", 100.2)))
        assert noro.fair_value(SYMBOL) is not None
        assert noro.fair_value("ETH-USD") is None


# ======================================================================
# Section 19 / H6 — source-data timestamp semantics
# ======================================================================


class TestP3_7_SourceTimestampIsTheOldestContributor:
    """The opinion can never claim to be fresher than the data behind it."""

    @staticmethod
    def _mixed_ages(config):
        """A at T-100, B at T-1000, independent C at T-500, unrelated ETH at T."""
        noro = build_noro(config, symbols=[SYMBOL, "ETH-USD"])
        state = market(
            venue("A", 100.0, exchange_ts=START_MS - 100),
            venue("B", 100.2, exchange_ts=START_MS - 1_000),
            venue("C", 100.1, exchange_ts=START_MS - 500),
            venue("A", 3_000.0, symbol="ETH-USD", exchange_ts=START_MS),
        )
        noro.on_market_state(state)
        return noro, state

    def test_the_stamp_is_the_oldest_contributor_used(self, config):
        noro, _ = self._mixed_ages(config)
        opinion = noro.evaluate(opportunity("A", "B"), START_MS)
        assert opinion.source_data_timestamp == START_MS - 1_000, (
            "B is the stalest venue the conclusion rests on"
        )

    def test_an_unrelated_symbol_has_no_effect(self, config):
        """The ETH venue is a full second fresher than anything in the BTC
        valuation. It used to make that valuation look current."""
        noro, state = self._mixed_ages(config)
        opinion = noro.evaluate(opportunity("A", "B"), START_MS)
        assert state.source_data_timestamp == START_MS, "ETH is the market's newest"
        assert opinion.source_data_timestamp == START_MS - 1_000

    def test_it_matches_the_helper_phase_1_added_for_this(self, config):
        noro, state = self._mixed_ages(config)
        opinion = noro.evaluate(opportunity("A", "B"), START_MS)
        honest = state.source_data_timestamp_for(
            [("A", SYMBOL), ("B", SYMBOL), ("C", SYMBOL)]
        )
        assert honest == START_MS - 1_000
        assert opinion.source_data_timestamp == honest

    def test_an_independent_contributor_can_be_the_oldest(self, config):
        """The stamp covers every venue the conclusion reads, not only the
        opportunity's own legs -- the benchmark is evidence too."""
        noro = build_noro(config)
        noro.on_market_state(
            market(
                venue("A", 100.0, exchange_ts=START_MS - 100),
                venue("B", 100.2, exchange_ts=START_MS - 200),
                venue("C", 100.1, exchange_ts=START_MS - 3_000),
            )
        )
        opinion = noro.evaluate(opportunity("A", "B"), START_MS)
        assert opinion.source_data_timestamp == START_MS - 3_000, (
            "the anchor judging the trade is three seconds old, and says so"
        )

    def test_the_newest_leg_no_longer_masks_the_oldest(self, config):
        noro = build_noro(config)
        noro.on_market_state(
            market(
                venue("A", 100.0, exchange_ts=START_MS),
                venue("B", 100.2, exchange_ts=START_MS - 1_900),
            )
        )
        opinion = noro.evaluate(opportunity("A", "B"), START_MS)
        assert opinion.source_data_timestamp == START_MS - 1_900

    def test_an_unknown_contributor_timestamp_fails_closed(self, config):
        """Unknown stays unknown. An opinion whose age cannot be checked is
        not publishable, so NORO returns nothing rather than a stamp it
        cannot justify."""
        noro = build_noro(config)
        noro.on_market_state(
            market(
                venue("A", 100.0, exchange_ts=START_MS),
                venue("B", 100.2, exchange_ts=None),
            )
        )
        assert noro.evaluate(opportunity("A", "B"), START_MS) is None

    def test_the_downstream_consumer_now_reads_an_honest_age(self, config):
        """``Envelope.data_age_ms`` is what age-based gates read, and it is
        computed from this stamp. It used to under-report by the spread
        between the newest and oldest contributor."""
        noro = build_noro(config)
        noro.on_market_state(
            market(
                venue("A", 100.0, exchange_ts=START_MS),
                venue("B", 100.2, exchange_ts=START_MS - 1_900),
            )
        )
        opinion = noro.evaluate(opportunity("A", "B"), START_MS + 50)
        assert opinion.data_age_ms == 1_950, "the true oldest-contributor age"

    def test_noro_no_longer_reads_the_market_wide_stamp(self):
        """The mechanism, asserted directly: the old call site is gone."""
        import inspect

        from agents.noro import agent as noro_agent
        from core.models.market import MarketState

        assert hasattr(MarketState, "source_data_timestamp_for")
        source = inspect.getsource(noro_agent.Noro.evaluate)
        assert "source_data_timestamp_for" in source
        assert "market.source_data_timestamp," not in source


# ======================================================================
# P3-6 — health expresses cross-venue valuation readiness
# ======================================================================


class TestP3_6_HealthExpressesValuationReadiness:
    def test_one_venue_per_symbol_is_no_longer_healthy(self, config):
        """The finding, closed. Two symbols, one venue each: a price exists
        for both, and a cross-venue valuation for neither."""
        noro = build_noro(config, symbols=[SYMBOL, "ETH-USD"])
        noro.on_market_state(
            market(
                venue("A", 100.0, symbol=SYMBOL),
                venue("B", 3_000.0, symbol="ETH-USD"),
            )
        )
        assert noro.health.status_of("NORO") is HealthStatus.DEGRADED

    def test_two_venues_on_one_symbol_is_healthy(self, config):
        noro = build_noro(config, symbols=[SYMBOL, "ETH-USD"])
        noro.on_market_state(market(venue("A", 100.0), venue("B", 100.2)))
        assert noro.health.status_of("NORO") is HealthStatus.HEALTHY

    def test_no_usable_data_is_offline(self, config):
        noro = build_noro(config, symbols=[SYMBOL])
        noro.on_market_state(market())
        assert noro.health.status_of("NORO") is HealthStatus.OFFLINE

    def test_stale_venues_are_not_usable_data(self, config):
        noro = build_noro(config, symbols=[SYMBOL])
        noro.on_market_state(
            market(
                venue("A", 100.0, quality=DataQuality.STALE),
                venue("B", 100.2, quality=DataQuality.STALE),
            )
        )
        assert noro.health.status_of("NORO") is HealthStatus.OFFLINE

    def test_health_tracks_the_symbol_that_is_ready_not_the_symbol_count(
        self, config
    ):
        """One symbol fully covered and one not present at all is HEALTHY:
        NORO can genuinely value something. The old rule reported DEGRADED
        here and HEALTHY on the unpriceable market above -- exactly backwards
        for a required component."""
        noro = build_noro(config, symbols=[SYMBOL, "ETH-USD"])
        noro.on_market_state(market(venue("A", 100.0), venue("B", 100.2)))
        assert noro.health.status_of("NORO") is HealthStatus.HEALTHY

    def test_the_detail_distinguishes_ready_from_informative(self, config):
        """A symbol at exactly two contributors is healthy and honest, but
        will only ever return the neutral verdict. An operator must be able to
        see that without reading opinions one by one."""
        two = build_noro(config, symbols=[SYMBOL])
        two.on_market_state(market(venue("A", 100.0), venue("B", 100.2)))
        three = build_noro(config, symbols=[SYMBOL])
        three.on_market_state(
            market(venue("A", 100.0), venue("B", 100.2), venue("C", 100.1))
        )

        a = two.health.snapshot(now_ms=START_MS).components["NORO"]
        b = three.health.snapshot(now_ms=START_MS).components["NORO"]
        assert a.status is b.status is HealthStatus.HEALTHY
        assert a.detail != b.detail, (
            "the two markets are differently capable and the detail says so"
        )
        assert "1/1" in a.detail and "1/1" in b.detail
        assert a.detail.startswith("1/1 symbols valuation-ready")

    def test_a_degraded_detail_names_the_shortfall(self, config):
        noro = build_noro(config, symbols=[SYMBOL])
        noro.on_market_state(market(venue("A", 100.0)))
        detail = noro.health.snapshot(now_ms=START_MS).components["NORO"].detail
        assert "valuation-ready" in detail
        assert "0/1" in detail

    def test_the_trading_path_still_fails_closed(self, config):
        """Unchanged mitigation: a missing NORO opinion makes consensus
        incomplete, which suspends the strategy."""
        from core.models.agent import ConsensusResult
        from strategies.consensus.engine import ConsensusEngine

        noro = build_noro(config, symbols=[SYMBOL])
        noro.on_market_state(market(venue("A", 100.0)))
        assert noro.evaluate(opportunity("A", "B"), START_MS) is None

        engine = ConsensusEngine(noro.settings.consensus, noro.clock)
        result: ConsensusResult = engine.combine(
            symbol=SYMBOL, strategy="cross_venue", opinions={}, now_ms=START_MS
        )
        assert AgentId.NORO in result.missing_agents
        assert result.complete is False
        assert engine.entry_allowed(result) is False


# ======================================================================
# Section 20 — opportunity leg coverage
# ======================================================================


def _legs(*specs, symbol: str = SYMBOL) -> Opportunity:
    opp = Opportunity(
        created_at=START_MS,
        source_data_timestamp=START_MS,
        kind=OpportunityKind.CROSS_VENUE_DISLOCATION,
        strategy="cross_venue",
        symbol=symbol,
        legs=[
            OpportunityLeg(venue=v, symbol=symbol, side=s, reference_price=100.0)
            for v, s in specs
        ],
        gross_edge_bps=10.0,
        expires_at=START_MS + 2_000,
        reason_codes=["CROSS_VENUE_DISLOCATION"],
    )
    opp.correlation_id = opp.opportunity_id
    return opp


class TestLegCoverage:
    @staticmethod
    def _noro(config):
        noro = build_noro(config)
        noro.on_market_state(market(venue("A", 100.0), venue("B", 100.2)))
        return noro

    def test_a_both_legs_present(self, config):
        assert self._noro(config).evaluate(
            _legs(("A", Side.BUY), ("B", Side.SELL)), START_MS
        ) is not None

    def test_b_one_leg_missing_fails_closed(self, config):
        assert self._noro(config).evaluate(
            _legs(("A", Side.BUY), ("Z", Side.SELL)), START_MS
        ) is None

    def test_c_both_legs_missing_fails_closed(self, config):
        assert self._noro(config).evaluate(
            _legs(("Y", Side.BUY), ("Z", Side.SELL)), START_MS
        ) is None

    def test_d_duplicate_venue_legs_cannot_be_confirmed(self, config):
        """Not reachable from the detector (it refuses a same-venue pair),
        but the function does not defend against it. Under the weakest-leg
        rule the two opposite confirmations are exact negatives, so the
        minimum is the negative one and the verdict is a rejection."""
        opinion = self._noro(config).evaluate(
            _legs(("A", Side.BUY), ("A", Side.SELL)), START_MS
        )
        assert opinion is not None
        assert opinion.detail["weakest_confirmation_bps"] == pytest.approx(
            -abs(opinion.detail["confirmation_bps_A"])
        )
        assert opinion.signal < 0, (
            "buying and selling the same venue can never be confirmed"
        )

    def test_e_a_single_leg_opportunity_is_accepted(self, config):
        """One leg, judged against the venue it does not participate in."""
        opinion = self._noro(config).evaluate(_legs(("A", Side.BUY)), START_MS)
        assert opinion is not None
        assert opinion.signal > 0, "A is below the independent benchmark, B"

    def test_f_a_three_leg_opportunity_is_accepted(self, config):
        noro = build_noro(config)
        noro.on_market_state(
            market(venue("A", 100.0), venue("B", 100.2), venue("C", 100.4))
        )
        opinion = noro.evaluate(
            _legs(("A", Side.BUY), ("B", Side.SELL), ("C", Side.SELL)), START_MS
        )
        assert opinion is not None

    def test_g_a_wrong_symbol_opportunity_has_no_fair_value(self, config):
        assert self._noro(config).evaluate(
            _legs(("A", Side.BUY), ("B", Side.SELL), symbol="ETH-USD"), START_MS
        ) is None

    def test_h_same_venue_both_sides_is_structurally_neutral(self, config):
        """The detector refuses this (``buy_state.venue == sell_state.venue``
        returns None), so it is unreachable in production."""
        from strategies.cross_venue.detector import find_dislocation

        single = [venue("A", 100.0)]
        assert find_dislocation(SYMBOL, single, None) is None

    def test_the_model_permits_an_empty_leg_list(self, config):
        """``confirmations`` would be empty and ``evaluate`` returns None
        before dividing by zero -- the guard is present and works."""
        opinion = self._noro(config).evaluate(_legs(), START_MS)
        assert opinion is None


# ======================================================================
# Section 34 — observability of the valuation itself
# ======================================================================


class TestOpinionObservability:
    """P3-4 / P3-8 observability: the opinion must explain the new model.

    The audit's complaint was that a bad valuation could not be diagnosed from
    the opinion alone -- nothing said which venues contributed, which were
    excluded, or how confident the agent was and why. The detail now names the
    contributor set, the independent subset, the benchmark, the per-leg
    confirmations, the dispersion and each confidence component.
    """

    @staticmethod
    def _three(config):
        noro = build_noro(config)
        noro.on_market_state(
            market(venue("A", 100.0), venue("B", 100.2), venue("C", 100.4))
        )
        return noro

    def test_the_detail_names_every_contributor(self, config):
        detail = self._three(config).evaluate(opportunity("A", "B"), START_MS).detail
        assert detail["contributors"] == 3
        assert detail["contributor_venues"] == "A,B,C"
        for name in ("A", "B", "C"):
            assert f"price_{name}" in detail
            assert f"reliability_{name}" in detail

    def test_the_detail_names_the_independent_subset(self, config):
        """The venue that actually judged the trade -- which the old opinion
        did not mention at all, even though it moved the verdict."""
        detail = self._three(config).evaluate(opportunity("A", "B"), START_MS).detail
        assert detail["independent_venues"] == "C"
        assert detail["independent_contributors"] == 1

    def test_the_detail_publishes_the_benchmark_and_the_weakest_leg(self, config):
        detail = self._three(config).evaluate(opportunity("A", "B"), START_MS).detail
        assert detail["valuation_benchmark"] == pytest.approx(100.4)
        assert detail["weakest_confirmation_bps"] is not None
        assert detail["valuation_dispersion_bps"] is not None

    def test_the_detail_publishes_each_confidence_component(self, config):
        detail = self._three(config).evaluate(opportunity("A", "B"), START_MS).detail
        for key in (
            "confidence_breadth",
            "confidence_agreement",
            "confidence_quality",
        ):
            assert key in detail

    def test_per_leg_deviations_and_confirmations_are_both_present(self, config):
        detail = self._three(config).evaluate(opportunity("A", "B"), START_MS).detail
        for name in ("A", "B"):
            assert f"deviation_bps_{name}" in detail
            assert f"confirmation_bps_{name}" in detail

    def test_an_excluded_venue_is_visible_by_its_absence_from_the_roll_call(
        self, config
    ):
        """A venue quoting 5x the price used to be silently dropped with
        nothing recorded. The contributor roll-call now makes the exclusion
        legible: C is not in it, and the count says so."""
        noro = build_noro(config)
        noro.on_market_state(
            market(
                venue("A", 100.0),
                venue("B", 100.2),
                venue("C", 500.0, quality=DataQuality.STALE),
            )
        )
        detail = noro.evaluate(opportunity("A", "B"), START_MS).detail
        assert detail["contributors"] == 2
        assert detail["contributor_venues"] == "A,B"
        assert "price_C" not in detail

    def test_the_neutral_path_reports_why_it_is_neutral(self, config):
        noro = build_noro(config)
        noro.on_market_state(market(venue("A", 100.0), venue("B", 100.2)))
        detail = noro.evaluate(opportunity("A", "B"), START_MS).detail
        assert detail["independent_contributors"] == 0
        assert detail["independent_venues"] == ""
        assert detail["valuation_benchmark"] is None
        assert detail["weakest_confirmation_bps"] is None

    def test_the_configured_thresholds_are_echoed(self, config):
        detail = self._three(config).evaluate(opportunity("A", "B"), START_MS).detail
        assert detail["saturation_bps"] == config.saturation_bps
        assert detail["liquidity_window_bps"] == config.liquidity_window_bps

    def test_the_model_version_records_the_semantic_change(self, config):
        """The signal's MEANING changed -- independent benchmark, weakest-leg
        governance, neutral on two venues -- so an 0.1 opinion and an 0.2
        opinion carrying the same number do not say the same thing."""
        noro = build_noro(config)
        noro.on_market_state(market(venue("A", 100.0), venue("B", 100.2)))
        assert noro.evaluate(opportunity("A", "B"), START_MS).model_version == (
            "noro-0.2"
        )
