"""Phase 3 audit, Sections 16-20 / 32-34: quality, timestamps, health, coverage.

Four separate questions, grouped because they all concern what NORO knows
about the age and breadth of its own inputs.

* **Quality (17)** -- only FRESH contributes, and no stale fair value survives
  a later snapshot.
* **Source timestamp (19, H6)** -- ``evaluate`` stamps the MarketState's
  global ``source_data_timestamp``, which is the newest observation anywhere
  in the market. Phase 1 established (TIDAL-H4) that a multi-leg economic
  claim must be as old as its OLDEST contributing leg, and added
  ``MarketState.source_data_timestamp_for`` for exactly that.
* **Health (16, 33, H7)** -- ``_heartbeat`` counts symbols that produced *a*
  fair value, which one venue is enough for.
* **Leg coverage (20)** -- how ``evaluate`` fails when a leg is not in the
  benchmark.
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


class TestH6_SourceTimestampLaunderscontributorAge:
    def test_noro_stamps_the_market_wide_newest_timestamp(self, config):
        """The mechanism: ``self.market.source_data_timestamp``, not the
        opportunity's own legs."""
        noro = build_noro(config, symbols=[SYMBOL, "ETH-USD"])
        state = market(
            venue("A", 100.0, exchange_ts=START_MS - 100),
            venue("B", 100.2, exchange_ts=START_MS - 1_000),
            # An unrelated symbol, updated right now.
            venue("A", 3_000.0, symbol="ETH-USD", exchange_ts=START_MS),
        )
        noro.on_market_state(state)
        opinion = noro.evaluate(opportunity("A", "B"), START_MS)
        assert opinion.source_data_timestamp == START_MS, (
            "an unrelated ETH update made the BTC valuation look current"
        )

    def test_the_oldest_contributing_leg_is_a_full_second_older(self, config):
        noro = build_noro(config, symbols=[SYMBOL, "ETH-USD"])
        state = market(
            venue("A", 100.0, exchange_ts=START_MS - 100),
            venue("B", 100.2, exchange_ts=START_MS - 1_000),
            venue("A", 3_000.0, symbol="ETH-USD", exchange_ts=START_MS),
        )
        noro.on_market_state(state)
        opinion = noro.evaluate(opportunity("A", "B"), START_MS)
        honest = state.source_data_timestamp_for([("A", SYMBOL), ("B", SYMBOL)])
        assert honest == START_MS - 1_000
        assert opinion.source_data_timestamp - honest == 1_000, (
            "NORO's opinion claims to be 1000ms fresher than its own inputs"
        )

    def test_the_same_symbols_newer_leg_also_masks_the_older_one(self, config):
        """Even without an unrelated symbol: the newest leg of THIS symbol
        hides the oldest."""
        noro = build_noro(config)
        state = market(
            venue("A", 100.0, exchange_ts=START_MS),
            venue("B", 100.2, exchange_ts=START_MS - 1_900),
        )
        noro.on_market_state(state)
        opinion = noro.evaluate(opportunity("A", "B"), START_MS)
        assert opinion.source_data_timestamp == START_MS
        assert state.source_data_timestamp_for(
            [("A", SYMBOL), ("B", SYMBOL)]
        ) == START_MS - 1_900

    def test_three_contributors_report_the_newest_not_the_oldest(self, config):
        noro = build_noro(config)
        noro.on_market_state(
            market(
                venue("A", 100.0, exchange_ts=START_MS - 100),
                venue("B", 100.2, exchange_ts=START_MS - 500),
                venue("C", 100.1, exchange_ts=START_MS - 2_000),
            )
        )
        opinion = noro.evaluate(opportunity("A", "B"), START_MS)
        assert opinion.source_data_timestamp == START_MS - 100, (
            "the fair value used all three, and reports the age of the newest"
        )

    def test_the_helper_phase_1_added_for_this_exists_and_is_unused_by_noro(self):
        """Phase 1 (TIDAL-H4) added ``source_data_timestamp_for`` and moved
        the three TradeIntent sites onto it, explicitly scoping AgentOpinion
        out. That scope note is what this finding is about."""
        import inspect

        from agents.noro import agent as noro_agent
        from core.models.market import MarketState

        assert hasattr(MarketState, "source_data_timestamp_for")
        source = inspect.getsource(noro_agent.Noro.evaluate)
        assert "self.market.source_data_timestamp" in source
        assert "source_data_timestamp_for" not in source

    def test_the_downstream_consumer_of_this_field(self, config):
        """Where it matters: ``Envelope.data_age_ms`` is what age-based gates
        read, and it is computed from this stamp."""
        noro = build_noro(config)
        noro.on_market_state(
            market(
                venue("A", 100.0, exchange_ts=START_MS),
                venue("B", 100.2, exchange_ts=START_MS - 1_900),
            )
        )
        opinion = noro.evaluate(opportunity("A", "B"), START_MS + 50)
        assert opinion.data_age_ms == 50, "reported age"
        assert (START_MS + 50) - (START_MS - 1_900) == 1_950, "true oldest-leg age"


# ======================================================================
# Sections 16 / 33 / H7 — health semantics
# ======================================================================


class TestH7_HealthDoesNotExpressCrossVenueReadiness:
    def test_one_venue_per_symbol_reports_healthy(self, config):
        noro = build_noro(config, symbols=[SYMBOL, "ETH-USD"])
        noro.on_market_state(
            market(
                venue("A", 100.0, symbol=SYMBOL),
                venue("B", 3_000.0, symbol="ETH-USD"),
            )
        )
        assert noro.health.status_of("NORO") is HealthStatus.HEALTHY, (
            "priced 2/2 symbols -- with one venue each, and therefore no "
            "cross-venue valuation for either"
        )

    def test_although_neither_symbol_can_produce_an_opinion(self, config):
        noro = build_noro(config, symbols=[SYMBOL, "ETH-USD"])
        noro.on_market_state(
            market(
                venue("A", 100.0, symbol=SYMBOL),
                venue("B", 3_000.0, symbol="ETH-USD"),
            )
        )
        assert noro.health.status_of("NORO") is HealthStatus.HEALTHY
        assert noro.evaluate(opportunity("A", "B"), START_MS) is None, (
            "the second leg is not in the benchmark, so no opinion exists"
        )

    def test_the_trading_path_still_fails_closed(self, config):
        """The mitigation that keeps this an observability finding rather
        than a trading-safety one: a missing NORO opinion makes consensus
        incomplete, which suspends the strategy."""
        from core.models.agent import ConsensusResult
        from strategies.consensus.engine import ConsensusEngine

        noro = build_noro(config, symbols=[SYMBOL])
        noro.on_market_state(market(venue("A", 100.0)))
        assert noro.evaluate(opportunity("A", "B"), START_MS) is None

        engine = ConsensusEngine(
            noro.settings.consensus, noro.clock
        )
        result: ConsensusResult = engine.combine(
            symbol=SYMBOL, strategy="cross_venue", opinions={}, now_ms=START_MS
        )
        assert AgentId.NORO in result.missing_agents
        assert result.complete is False
        assert engine.entry_allowed(result) is False

    def test_health_degrades_only_on_symbol_count_not_venue_breadth(self, config):
        noro = build_noro(config, symbols=[SYMBOL, "ETH-USD"])
        noro.on_market_state(market(venue("A", 100.0, symbol=SYMBOL)))
        assert noro.health.status_of("NORO") is HealthStatus.DEGRADED, (
            "1 of 2 symbols priced"
        )
        noro.on_market_state(market())
        assert noro.health.status_of("NORO") is HealthStatus.OFFLINE

    def test_a_single_venue_symbol_is_indistinguishable_from_a_healthy_one(
        self, config
    ):
        """The observability gap in one assertion: the health record carries
        no venue count, so an operator cannot tell these two apart."""
        one_venue = build_noro(config, symbols=[SYMBOL])
        one_venue.on_market_state(market(venue("A", 100.0)))
        two_venue = build_noro(config, symbols=[SYMBOL])
        two_venue.on_market_state(market(venue("A", 100.0), venue("B", 100.2)))

        a = one_venue.health.snapshot(now_ms=START_MS).components["NORO"]
        b = two_venue.health.snapshot(now_ms=START_MS).components["NORO"]
        assert a.status is b.status is HealthStatus.HEALTHY
        assert a.detail == b.detail == ""


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

    def test_d_duplicate_venue_legs_are_evaluated_twice(self, config):
        """Not reachable from the detector (it refuses a same-venue pair),
        but the function does not defend against it: the same deviation is
        counted on both sides."""
        opinion = self._noro(config).evaluate(
            _legs(("A", Side.BUY), ("A", Side.SELL)), START_MS
        )
        assert opinion is not None
        # confirmations are (-dev_A, +dev_A), so the mean is exactly zero and
        # the edge is the negative one alone: min + 0 = -|dev_A|.
        assert opinion.detail["confirmed_edge_bps"] == pytest.approx(
            -abs(opinion.detail["deviation_bps_A"])
        )
        assert opinion.signal < 0, (
            "buying and selling the same venue can never be confirmed"
        )

    def test_e_a_single_leg_opportunity_is_accepted(self, config):
        """``min + mean`` of one element is just ``2x`` that element, so a
        one-leg opportunity produces a full-strength opinion."""
        opinion = self._noro(config).evaluate(_legs(("A", Side.BUY)), START_MS)
        assert opinion is not None
        assert opinion.signal > 0

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
    def test_the_detail_names_every_leg_but_not_every_contributor(self, config):
        noro = build_noro(config)
        noro.on_market_state(
            market(venue("A", 100.0), venue("B", 100.2), venue("C", 100.4))
        )
        opinion = noro.evaluate(opportunity("A", "B"), START_MS)
        detail = opinion.detail
        assert detail["venues_priced"] == 3
        assert "deviation_bps_A" in detail and "deviation_bps_B" in detail
        assert "deviation_bps_C" not in detail, (
            "the third venue moved fair value and is not named in the opinion"
        )

    def test_no_weight_or_contributor_timestamp_is_published(self, config):
        noro = build_noro(config)
        noro.on_market_state(market(venue("A", 100.0), venue("B", 100.2)))
        detail = noro.evaluate(opportunity("A", "B"), START_MS).detail
        assert set(detail) == {
            "fair_value", "total_liquidity", "venues_priced",
            "deviation_bps_A", "deviation_bps_B", "confirmed_edge_bps",
        }
        for absent in ("weight", "excluded", "exchange_ts", "quality"):
            assert not any(absent in key for key in detail)

    def test_a_bad_valuation_cannot_be_diagnosed_from_the_opinion_alone(
        self, config
    ):
        """Two very different markets produce indistinguishable details apart
        from the numbers -- nothing says WHICH venues were excluded or why."""
        noro = build_noro(config)
        noro.on_market_state(
            market(
                venue("A", 100.0),
                venue("B", 100.2),
                venue("C", 500.0, quality=DataQuality.STALE),
            )
        )
        detail = noro.evaluate(opportunity("A", "B"), START_MS).detail
        assert detail["venues_priced"] == 2
        assert not any("C" in key for key in detail), (
            "a venue quoting 5x the price was silently excluded and the "
            "opinion records nothing about it"
        )

    def test_the_model_version_is_present_for_future_changes(self, config):
        """Section 57: the field exists, so a future algorithm change can be
        made observable by bumping it."""
        noro = build_noro(config)
        noro.on_market_state(market(venue("A", 100.0), venue("B", 100.2)))
        assert noro.evaluate(opportunity("A", "B"), START_MS).model_version == (
            "noro-0.1"
        )
