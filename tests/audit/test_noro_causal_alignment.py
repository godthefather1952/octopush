"""Phase 3 audit, Sections 45 / 46 / 55 / 56: is NORO judging the right snapshot?

H10, and the only hypothesis in this phase that could be CRITICAL.

``Noro.evaluate`` reads ``self.fair_values`` -- whatever the LAST
``MARKET_STATE`` event it handled produced. The opportunity it is judging
carries no reference to the snapshot that created it. If a newer MarketState
could reach NORO between an opportunity being published and NORO handling it,
NORO would judge that opportunity against a benchmark from a different instant:
a causal misalignment that no amount of correct arithmetic could repair, and
one that Phase 2's replay determinism would faithfully reproduce rather than
reveal.

This suite traces the actual event order rather than assuming the bus prevents
it, then tests what happens in the cases where the platform genuinely does
re-evaluate an older opportunity against newer state.
"""

from __future__ import annotations

import inspect

import pytest

from core.config import NoroConfig
from core.events import Event, EventType
from core.models.common import AgentId
from tests.audit.helpers import START_MS, SYMBOL, market, opportunity, venue
from tests.audit.noro_fixtures import build_noro


@pytest.fixture
def config() -> NoroConfig:
    return NoroConfig()


# ======================================================================
# Section 45 — who can publish a MARKET_STATE, and when
# ======================================================================


class TestTheOnlyPublisherIsTheTick:
    def test_market_state_has_exactly_one_producer(self):
        """If any other component could publish a MarketState, the ordering
        argument below would not hold."""
        import agents.tidal.agent as tidal
        import apps.orchestrator.orchestrator as orch
        import apps.orchestrator.wiring as wiring

        producers = []
        for module in (tidal, orch, wiring):
            source = inspect.getsource(module)
            for block in source.split("bus.publish(")[1:]:
                head = block[:200]
                if "EventType.MARKET_STATE" in head:
                    producers.append(module.__name__)
        assert producers == ["agents.tidal.agent"], producers

    def test_tidal_publishes_it_only_from_publish_state(self):
        import agents.tidal.agent as tidal

        source = inspect.getsource(tidal.Tidal.publish_state)
        assert "EventType.MARKET_STATE" in source

    def test_and_publish_state_is_called_only_from_observe(self):
        import apps.orchestrator.orchestrator as orch

        source = inspect.getsource(orch.Orchestrator)
        assert source.count("tidal.publish_state()") == 1
        observe = inspect.getsource(orch.Orchestrator._observe)
        assert "await self.tidal.publish_state()" in observe

    def test_observe_drains_before_the_tick_body_runs(self):
        """The ordering guarantee, read from the code: the snapshot is
        published and DRAINED (so NORO has recomputed) before anything in the
        tick can detect an opportunity."""
        import apps.orchestrator.orchestrator as orch

        observe = inspect.getsource(orch.Orchestrator._observe)
        publish_at = observe.index("publish_state()")
        drain_at = observe.index("bus.drain()")
        assert publish_at < drain_at

    def test_detection_publishes_and_drains_inside_the_same_tick_body(self):
        import apps.orchestrator.orchestrator as orch

        seek = inspect.getsource(orch.Orchestrator._seek)
        assert "EventType.OPPORTUNITY_DETECTED" in seek
        assert "await self.bus.drain()" in seek


class TestNoroSeesTheSnapshotThatCreatedTheOpportunity:
    """Directly, through NORO's own handler, in the production event order."""

    async def test_the_in_tick_order_keeps_them_aligned(self, config):
        """The opinion is formed from the snapshot the tick delivered.

        Built on THREE venues so the alignment is observable. With only the
        opportunity's own two, NORO abstains and publishes no benchmark at
        all, and the test would be measuring the neutral path rather than
        causal alignment. The independent anchor C is what NORO judges the
        trade against, so it is the value that must match the snapshot in
        force at detection.
        """
        noro = build_noro(config)
        first = market(
            venue("A", 100.0, liquidity=100_000.0),
            venue("B", 100.2, liquidity=100_000.0),
            venue("C", 100.1, liquidity=100_000.0),
        )
        await noro.on_event(
            Event(
                type=EventType.MARKET_STATE,
                ts_ms=START_MS,
                source="TIDAL",
                schema_name="MarketState",
                payload=first.to_json_dict(),
            )
        )
        anchor_at_detection = 100.1

        published: list[Event] = []
        noro.bus.subscribe(
            lambda e: published.append(e) or _noop(),
            types=[EventType.AGENT_OPINION],
            name="capture",
        )
        await noro.on_event(
            Event(
                type=EventType.OPPORTUNITY_DETECTED,
                ts_ms=START_MS,
                source="ORCHESTRATOR",
                schema_name="Opportunity",
                payload=opportunity("A", "B").to_json_dict(),
            )
        )
        await noro.bus.drain()
        assert len(published) == 1
        detail = published[0].payload["detail"]
        assert detail["independent_venues"] == "C"
        assert detail["valuation_benchmark"] == pytest.approx(anchor_at_detection)
        assert published[0].payload["abstain"] is False, (
            "an independent anchor exists, so this is a real vote"
        )

    async def test_a_later_snapshot_does_not_retro_change_the_published_opinion(
        self, config
    ):
        """Causal alignment stated as the property that matters: an opinion
        already published carries the benchmark of the snapshot that produced
        it, and a later snapshot cannot reach back and alter it."""
        noro = build_noro(config)
        published: list[Event] = []
        noro.bus.subscribe(
            lambda e: published.append(e) or _noop(),
            types=[EventType.AGENT_OPINION],
            name="capture",
        )

        for anchor, ts in ((100.1, START_MS), (100.5, START_MS + 100)):
            await noro.on_event(
                Event(
                    type=EventType.MARKET_STATE,
                    ts_ms=ts,
                    source="TIDAL",
                    schema_name="MarketState",
                    payload=market(
                        venue("A", 100.0, liquidity=100_000.0),
                        venue("B", 100.2, liquidity=100_000.0),
                        venue("C", anchor, liquidity=100_000.0),
                        created_at=ts,
                    ).to_json_dict(),
                )
            )
            await noro.on_event(
                Event(
                    type=EventType.OPPORTUNITY_DETECTED,
                    ts_ms=ts,
                    source="ORCHESTRATOR",
                    schema_name="Opportunity",
                    payload=opportunity("A", "B").to_json_dict(),
                )
            )
        await noro.bus.drain()

        assert len(published) == 2
        assert published[0].payload["detail"]["valuation_benchmark"] == (
            pytest.approx(100.1)
        )
        assert published[1].payload["detail"]["valuation_benchmark"] == (
            pytest.approx(100.5)
        )

    async def test_an_interleaved_newer_snapshot_would_be_visible(self, config):
        """The adversarial case, forced by hand: if a newer MarketState DID
        arrive between publication and evaluation, NORO would use it.

        This is what makes the alignment a property of the ORCHESTRATOR's
        ordering rather than of NORO itself -- NORO has no way to detect it.
        """
        noro = build_noro(config)
        noro.on_market_state(
            market(venue("A", 100.0, liquidity=100_000.0),
                   venue("B", 100.2, liquidity=100_000.0))
        )
        opp = opportunity("A", "B")
        at_detection = noro.evaluate(opp, START_MS)

        # A later snapshot arrives before the opinion is formed.
        noro.on_market_state(
            market(
                venue("A", 100.0, liquidity=100_000.0),
                venue("B", 100.2, liquidity=100_000.0),
                venue("C", 100.6, liquidity=900_000.0),
                created_at=START_MS + 100,
            )
        )
        after = noro.evaluate(opp, START_MS)

        assert at_detection.signal == 0.0, (
            "two venues, no independent evidence: NORO declines to vote"
        )
        assert after.signal < 0, (
            "the SAME opportunity is judged differently depending only on "
            "which snapshot NORO last handled -- the third venue turns a "
            "declined vote into a contradiction"
        )
        assert at_detection.detail["valuation_benchmark"] is None
        assert after.detail["valuation_benchmark"] is not None

    def test_the_opportunity_carries_no_snapshot_reference(self, config):
        """Why NORO cannot defend itself: nothing on the Opportunity says
        which MarketState produced it."""
        opp = opportunity("A", "B")
        fields = set(opp.model_dump())
        assert "market_created_at" not in fields
        assert "market_id" not in fields
        assert "snapshot" not in fields
        # created_at is a TIME, not an identity -- and NORO does not read it.
        source = inspect.getsource(build_noro(config).evaluate)
        assert "opportunity.created_at" not in source


# ======================================================================
# Section 46 — the one place production DOES re-evaluate
# ======================================================================


class TestContinuousReevaluationIsDeliberate:
    """``_monitor`` re-publishes an OPEN opportunity every tick.

    So an opportunity born at tick N is evaluated again at tick N+5 against
    tick N+5's fair values. That is not a misalignment: it is the design --
    "agents keep voting after entry". The evidence is that the re-publish
    carries the CURRENT tick's time, and NORO stamps its opinion with it.
    """

    def test_monitor_republishes_with_the_current_tick_time(self):
        import apps.orchestrator.orchestrator as orch

        monitor = inspect.getsource(orch.Orchestrator._monitor)
        assert "EventType.OPPORTUNITY_DETECTED" in monitor
        assert "ts_ms=self.tick_time" in monitor, (
            "the re-publish is stamped NOW, not at the opportunity's birth"
        )

    def test_noro_stamps_the_opinion_with_the_event_time_not_the_birth_time(
        self, config
    ):
        noro = build_noro(config)
        noro.on_market_state(market(venue("A", 100.0), venue("B", 100.2)))
        opp = opportunity("A", "B", created_at=START_MS)
        later = noro.evaluate(opp, START_MS + 5_000)
        assert later.created_at == START_MS + 5_000
        assert later.expires_at == START_MS + 5_000 + config.ttl_ms

    def test_noro_reads_only_the_leg_venues_never_the_recorded_leg_prices(
        self, config
    ):
        """Which is what makes re-evaluation coherent: NORO asks "is venue A
        cheap NOW", not "was it cheap at birth". A stale reference price on
        the opportunity cannot mislead it."""
        noro = build_noro(config)
        # Three venues, so the opinion is directional and the comparison has
        # something to compare.
        noro.on_market_state(
            market(venue("A", 100.0), venue("B", 100.2), venue("C", 100.1))
        )
        realistic = opportunity("A", "B", buy_price=100.0, sell_price=100.2)
        nonsense = opportunity("A", "B", buy_price=1.0, sell_price=999_999.0)
        assert noro.evaluate(realistic, START_MS).signal == pytest.approx(
            noro.evaluate(nonsense, START_MS).signal
        )
        assert noro.evaluate(realistic, START_MS).signal != 0.0

    def test_the_source_confirms_leg_prices_are_unread(self, config):
        source = inspect.getsource(type(build_noro(config)))
        assert "leg.venue" in source
        assert "leg.side" in source
        assert "reference_price" not in source


# ======================================================================
# Sections 55 / 56 — correlation and concurrent opportunities
# ======================================================================


class TestCorrelationIntegrity:
    def test_the_opinion_carries_the_opportunitys_id(self, config):
        noro = build_noro(config)
        noro.on_market_state(market(venue("A", 100.0), venue("B", 100.2)))
        opp = opportunity("A", "B")
        opinion = noro.evaluate(opp, START_MS)
        assert opinion.correlation_id == opp.opportunity_id

    async def test_the_published_event_carries_it_too(self, config):
        noro = build_noro(config)
        noro.on_market_state(market(venue("A", 100.0), venue("B", 100.2)))
        seen: list[Event] = []
        noro.bus.subscribe(
            lambda e: seen.append(e) or _noop(),
            types=[EventType.AGENT_OPINION],
            name="capture",
        )
        opp = opportunity("A", "B")
        await noro.on_event(
            Event(
                type=EventType.OPPORTUNITY_DETECTED,
                ts_ms=START_MS,
                source="ORCHESTRATOR",
                schema_name="Opportunity",
                correlation_id=opp.opportunity_id,
                payload=opp.to_json_dict(),
            )
        )
        await noro.bus.drain()
        assert seen[0].correlation_id == opp.opportunity_id
        assert seen[0].source == "NORO"

    def test_two_symbols_get_their_own_fair_values(self, config):
        noro = build_noro(config, symbols=[SYMBOL, "ETH-USD"])
        noro.on_market_state(
            market(
                venue("A", 100.0, symbol=SYMBOL, liquidity=50_000.0),
                venue("B", 100.2, symbol=SYMBOL, liquidity=50_000.0),
                venue("A", 3_000.0, symbol="ETH-USD", liquidity=50_000.0),
                venue("B", 3_030.0, symbol="ETH-USD", liquidity=50_000.0),
            )
        )
        btc = noro.evaluate(opportunity("A", "B", symbol=SYMBOL), START_MS)
        eth = noro.evaluate(opportunity("A", "B", symbol="ETH-USD"), START_MS)
        assert btc.symbol == SYMBOL and eth.symbol == "ETH-USD"
        assert btc.detail["price_A"] == pytest.approx(100.0)
        assert eth.detail["price_A"] == pytest.approx(3_000.0)
        assert btc.detail["contributor_venues"] == eth.detail["contributor_venues"]
        assert btc.correlation_id != eth.correlation_id

    def test_back_to_back_opportunities_do_not_contaminate_each_other(
        self, config
    ):
        noro = build_noro(config, symbols=[SYMBOL, "ETH-USD"])
        noro.on_market_state(
            market(
                venue("A", 100.0, symbol=SYMBOL),
                venue("B", 100.2, symbol=SYMBOL),
                venue("A", 3_000.0, symbol="ETH-USD"),
                venue("B", 3_030.0, symbol="ETH-USD"),
            )
        )
        opinions = [
            noro.evaluate(opportunity("A", "B", symbol=s), START_MS)
            for s in (SYMBOL, "ETH-USD", SYMBOL, "ETH-USD")
        ]
        assert opinions[0].detail == opinions[2].detail
        assert opinions[1].detail == opinions[3].detail
        assert opinions[0].detail["price_A"] != opinions[1].detail["price_A"], (
            "the two symbols are priced from their own venues, not each other's"
        )

    def test_every_opinion_is_stamped_noro(self, config):
        noro = build_noro(config)
        noro.on_market_state(market(venue("A", 100.0), venue("B", 100.2)))
        opinion = noro.evaluate(opportunity("A", "B"), START_MS)
        assert opinion.agent_id is AgentId.NORO


async def _noop() -> None:
    return None
