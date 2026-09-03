"""Opportunity / intent data age: TIDAL-H4.

The defect was that ``MarketState.source_data_timestamp`` took the *newest*
timestamp across every venue and symbol, and every downstream consumer
(``Opportunity``, three separate ``TradeIntent`` construction sites in the
orchestrator) simply inherited that one global number. A two-leg trade whose
buy side was 100ms old and whose sell side was 1,900ms old could therefore be
reported as 100ms old — or, just as easily, a completely unrelated symbol's
fresh venue could make a stale pair look current.

The fix is not "take the global minimum instead of the maximum" — section 11
of the remediation brief explicitly warns against that, because it would let
one unrelated stale venue (say, an ETH book nobody is trading) poison every
opportunity on every other symbol. The fix is per-record: each ``Opportunity``
and each ``TradeIntent`` now carries the oldest exchange observation among
*its own* legs, computed by ``MarketState.source_data_timestamp_for``. The
global ``MarketState.source_data_timestamp`` field is untouched and keeps its
own, different meaning: "has anything at all updated recently" — a
monitoring signal, not a per-trade freshness gate.
"""

from __future__ import annotations

import inspect

import pytest

from agents.tidal import Tidal
from core.health import HealthRegistry
from core.models.common import Side
from core.models.market import MarketState, OrderBookSnapshot, PriceLevel
from core.models.opportunity import OpportunityLeg, TradeIntent
from risk.limits import gate_data_age
from strategies.cross_venue.detector import CrossVenueDetector
from tests.conftest import START_MS


def snap(venue: str, symbol: str, exchange_ts: int, *, bid=100.0, ask=101.0):
    return OrderBookSnapshot(
        venue=venue, symbol=symbol, exchange_ts=exchange_ts, received_ts=exchange_ts,
        sequence=1000,
        bids=[PriceLevel(price=bid, size=1.0)], asks=[PriceLevel(price=ask, size=1.0)],
        is_checkpoint=True,
    )


async def market_with(bus, clock, settings, *books):
    """A real MarketState built by TIDAL from the given (venue, symbol, exchange_ts) books."""
    tidal = Tidal(bus, clock, settings, HealthRegistry(clock=clock))
    for venue, symbol, exchange_ts, kwargs in books:
        await tidal.on_snapshot(snap(venue, symbol, exchange_ts, **kwargs))
    return tidal.build_state()


class TestSourceDataTimestampFor:
    """The primitive both Opportunity and TradeIntent now build on."""

    async def test_oldest_leg_wins(self, bus, clock, settings):
        market = await market_with(
            bus, clock, settings,
            ("VENUE_A", "BTC-USD", START_MS - 100, {}),
            ("VENUE_B", "BTC-USD", START_MS - 1_900, {}),
        )
        ts = market.source_data_timestamp_for([("VENUE_A", "BTC-USD"), ("VENUE_B", "BTC-USD")])
        assert ts == START_MS - 1_900

    async def test_a_fresh_leg_cannot_mask_a_stale_one(self, bus, clock, settings):
        """However fresh one leg is, the pair is only as fresh as the other."""
        market = await market_with(
            bus, clock, settings,
            ("VENUE_A", "BTC-USD", START_MS, {}),          # 0ms old
            ("VENUE_B", "BTC-USD", START_MS - 5_000, {}),  # 5s old
        )
        ts = market.source_data_timestamp_for([("VENUE_A", "BTC-USD"), ("VENUE_B", "BTC-USD")])
        assert ts == START_MS - 5_000, "the stale leg must govern, not the fresh one"

    async def test_an_unrelated_symbol_does_not_affect_the_result(self, bus, clock, settings):
        """A 10s-old ETH venue must not poison a fresh BTC pair's timestamp."""
        market = await market_with(
            bus, clock, settings,
            ("VENUE_A", "BTC-USD", START_MS - 100, {}),
            ("VENUE_B", "BTC-USD", START_MS - 200, {}),
            ("VENUE_C", "ETH-USD", START_MS - 10_000, {}),
        )
        ts = market.source_data_timestamp_for([("VENUE_A", "BTC-USD"), ("VENUE_B", "BTC-USD")])
        assert ts == START_MS - 200, "only the BTC legs may govern a BTC opportunity"

    async def test_a_stale_unrelated_venue_does_not_poison_the_global_field_either(
        self, bus, clock, settings
    ):
        """The reverse of the H4 defect is also a defect: don't flip max->min
        globally, or one unrelated stale venue would flag everything stale.
        ``MarketState.source_data_timestamp`` (the whole-snapshot field) keeps
        its original "newest anywhere" meaning; only the per-leg helper is new.
        """
        market = await market_with(
            bus, clock, settings,
            ("VENUE_A", "BTC-USD", START_MS, {}),
            ("VENUE_C", "ETH-USD", START_MS - 10_000, {}),
        )
        assert market.source_data_timestamp == START_MS, (
            "the global field is a liveness signal, not a per-trade gate — "
            "flipping it to min() was explicitly the wrong fix (see module docstring)"
        )

    async def test_a_missing_leg_makes_the_result_unknown_not_omitted(
        self, bus, clock, settings
    ):
        market = await market_with(bus, clock, settings, ("VENUE_A", "BTC-USD", START_MS, {}))
        ts = market.source_data_timestamp_for([("VENUE_A", "BTC-USD"), ("VENUE_B", "BTC-USD")])
        assert ts is None, "a leg TIDAL has no state for must fail closed, not be skipped"

    def test_no_legs_is_unknown(self):
        market = MarketState(created_at=START_MS, source_data_timestamp=None)
        assert market.source_data_timestamp_for([]) is None

    async def test_the_result_is_a_pure_function_of_the_snapshot(self, bus, clock, settings):
        """E. Determinism: the same MarketState answers the same question the
        same way every time — there is no hidden clock read or mutable state
        inside the helper, which is what makes it safe to call once at
        detection time and trust again later (e.g. on replay).
        """
        market = await market_with(
            bus, clock, settings,
            ("VENUE_A", "BTC-USD", START_MS - 100, {}),
            ("VENUE_B", "BTC-USD", START_MS - 1_900, {}),
        )
        legs = [("VENUE_A", "BTC-USD"), ("VENUE_B", "BTC-USD")]
        first = market.source_data_timestamp_for(legs)
        for _ in range(50):
            assert market.source_data_timestamp_for(legs) == first
        # Order must not matter either — it is a min(), not a "first wins".
        assert market.source_data_timestamp_for(list(reversed(legs))) == first


class TestOpportunityUsesOldestLeg:
    """The audit's literal example, through the real detector."""

    async def test_a_two_leg_dislocation_carries_its_oldest_legs_timestamp(
        self, bus, clock, settings
    ):
        market = await market_with(
            bus, clock, settings,
            ("VENUE_A", "BTC-USD", START_MS - 100, {"ask": 100_000.0}),
            ("VENUE_B", "BTC-USD", START_MS - 1_900, {"bid": 100_200.0, "ask": 100_201.0}),
        )
        detector = CrossVenueDetector(
            settings.model_copy(update={"symbols": ["BTC-USD"]}), clock
        )
        opportunities = detector.detect(market)
        assert len(opportunities) == 1
        opp = opportunities[0]
        assert opp.source_data_timestamp == START_MS - 1_900
        assert opp.data_age_ms == pytest.approx(clock.now_ms() - (START_MS - 1_900))

    async def test_an_unrelated_stale_eth_venue_does_not_poison_the_btc_opportunity(
        self, bus, clock, settings
    ):
        market = await market_with(
            bus, clock, settings,
            ("VENUE_A", "BTC-USD", START_MS - 100, {"ask": 100_000.0}),
            ("VENUE_B", "BTC-USD", START_MS - 200, {"bid": 100_200.0, "ask": 100_201.0}),
            ("VENUE_C", "ETH-USD", START_MS - 10_000, {}),
        )
        detector = CrossVenueDetector(
            settings.model_copy(update={"symbols": ["BTC-USD", "ETH-USD"]}), clock
        )
        opportunities = detector.detect(market)
        assert len(opportunities) == 1
        assert opportunities[0].source_data_timestamp == START_MS - 200


class TestFinalRiskGateUsesTheCorrectAge:
    """D. The gate the whole chain feeds into."""

    def _intent(self, **overrides):
        legs = [
            OpportunityLeg(venue="VENUE_A", symbol="BTC-USD", side=Side.BUY, reference_price=100.0),
            OpportunityLeg(venue="VENUE_B", symbol="BTC-USD", side=Side.SELL, reference_price=100.2),
        ]
        from core.models.opportunity import CostBreakdown

        defaults = dict(
            created_at=START_MS,
            source_data_timestamp=START_MS,
            opportunity_id="opp-1",
            strategy="cross_venue",
            symbol="BTC-USD",
            legs=legs,
            notional=1_000.0,
            gross_edge_bps=5.0,
            costs=CostBreakdown(),
            expected_net_edge_bps=3.0,
            consensus_score=1.0,
            consensus_agreement=1.0,
            max_slippage_bps=5.0,
            deadline_ms=START_MS + 10_000,
            urgency=0.5,
        )
        defaults.update(overrides)
        return TradeIntent(**defaults)

    def test_oldest_leg_governs_the_gate(self, settings):
        """Buy leg 100ms old, sell leg 1,900ms old -> gated on ~1,900ms."""
        now = START_MS + 1_900
        intent = self._intent(source_data_timestamp=START_MS)  # the 1,900ms-old leg
        check = gate_data_age(intent, settings.risk, now)
        # max_data_age_ms defaults to 2000; 1900 <= 2000 still passes...
        assert check.observed == pytest.approx(1_900)
        assert check.result.value == "PASS"

    def test_the_gate_rejects_once_the_oldest_leg_exceeds_the_limit(self, settings):
        now = START_MS + 2_500  # > default max_data_age_ms (2000)
        intent = self._intent(source_data_timestamp=START_MS)
        check = gate_data_age(intent, settings.risk, now)
        assert check.result.value == "FAIL"
        assert check.observed == pytest.approx(2_500)
        assert check.limit == settings.risk.max_data_age_ms

    def test_a_fresh_venue_cannot_rescue_a_stale_intent_at_the_gate(self, settings):
        """If the intent's own source_data_timestamp reflects the stale leg
        (as it now does), no amount of "but VENUE_A was fresh" changes the
        gate's verdict — there is no second, fresher timestamp for it to
        accidentally read instead.
        """
        now = START_MS + 5_000
        intent = self._intent(source_data_timestamp=START_MS)  # oldest leg: 5000ms old
        check = gate_data_age(intent, settings.risk, now)
        assert check.result.value == "FAIL"

    def test_missing_source_timestamp_is_unknown_and_blocks(self, settings):
        intent = self._intent(source_data_timestamp=None)
        check = gate_data_age(intent, settings.risk, START_MS)
        assert check.result.value == "UNKNOWN"


class TestOrchestratorSitesUseThePerLegHelper:
    """A regression guard on the three call sites the audit named.

    ``_build_intent``, the exit-intent path, and the hedge-intent path each
    used to read ``market.source_data_timestamp`` (the global newest-anywhere
    value) directly. All three must now derive from the actual legs of the
    record they are building, via ``source_data_timestamp_for``. Reading the
    source is deliberately brittle here: a future edit that reintroduces
    ``market.source_data_timestamp`` on any of these lines is exactly the
    regression this batch fixes, and should fail loudly.
    """

    def test_no_intent_construction_site_reads_the_global_timestamp_directly(self):
        import apps.orchestrator.orchestrator as orch

        source = inspect.getsource(orch)
        # Every remaining use of the raw global field must be for something
        # other than a TradeIntent's own source_data_timestamp (e.g. AgentOpinion
        # elsewhere is out of this batch's scope). The three TradeIntent(...)
        # constructions specifically must use the helper.
        for block in source.split("TradeIntent(")[1:]:
            call_body = block[: block.index("\n\n")]
            assert "source_data_timestamp_for(" in call_body, (
                "a TradeIntent is being built from the global market timestamp "
                "again — this is the TIDAL-H4 regression"
            )
            assert "market.source_data_timestamp," not in call_body

    def test_market_state_still_exposes_the_helper_orchestrator_relies_on(self):
        assert hasattr(MarketState, "source_data_timestamp_for")
