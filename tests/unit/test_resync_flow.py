"""The recovery path end to end: gap -> request -> adapter -> book restored.

TIDAL-C2 was not that recovery was broken but that it did not exist: a gap was
detected, reported, and then nothing happened, because TIDAL has no way to
reach a venue and nothing bridged the two. These tests drive the whole chain
and assert the *outcome* — a usable book again — rather than that a flag was
set, which is what the previous test asserted and why the gap went unnoticed.
"""

from __future__ import annotations

import pytest

from agents.tidal import Tidal
from agents.tidal.book import LocalOrderBook
from apps.orchestrator.wiring import ResyncBridge
from core.bus import InMemoryEventBus
from core.clock import ManualClock
from core.config import Settings, VenueConfig
from core.events import EventType
from core.health import HealthRegistry
from core.models.common import DataQuality
from core.models.market import OrderBookSnapshot, PriceLevel
from storage.base import SessionStatus
from tests.conftest import START_MS
from venues.base.adapter import VenueAdapter
from venues.base.messages import BookDelta, ResyncRequest


def snapshot(venue: str, symbol: str, last_id: int, *, bid=100.0, ask=101.0, ts=START_MS):
    return OrderBookSnapshot(
        venue=venue,
        symbol=symbol,
        exchange_ts=ts,
        received_ts=ts,
        sequence=last_id,
        bids=[PriceLevel(price=bid, size=1.0)],
        asks=[PriceLevel(price=ask, size=1.0)],
        is_checkpoint=True,
    )


def delta(venue: str, symbol: str, first: int, final: int, *, ts=START_MS, bids=()):
    return BookDelta(
        venue=venue,
        symbol=symbol,
        exchange_ts=ts,
        received_ts=ts,
        first_sequence=first,
        sequence=final,
        bids=[PriceLevel(price=p, size=s) for p, s in bids],
    )


class RecordingAdapter(VenueAdapter):
    """A venue that answers resync requests from a scripted checkpoint.

    Deliberately a plain adapter, not the Binance one: the point being tested
    is that the *bridge* delivers the request to whoever owns the feed.
    """

    def __init__(self, config, clock, symbols, next_id: int = 5000) -> None:
        super().__init__(config, clock, symbols)
        self.requests: list[tuple[str, str]] = []
        self.next_id = next_id

    async def run(self) -> None:  # pragma: no cover - never started
        raise AssertionError("this adapter is driven directly")

    async def request_resync(self, symbol: str, reason: str = "") -> None:
        self.requests.append((symbol, reason))


@pytest.fixture
def venue_config() -> VenueConfig:
    return VenueConfig(name="VENUE_A", display_name="A", adapter="simulated")


@pytest.fixture
def tidal(bus: InMemoryEventBus, clock: ManualClock, settings: Settings) -> Tidal:
    agent = Tidal(bus, clock, settings, HealthRegistry(clock=clock))
    agent.subscribe()
    return agent


class TestResyncRequestIsEmitted:
    async def test_a_gap_publishes_a_resync_request(self, tidal, bus, clock):
        requests: list[ResyncRequest] = []

        async def spy(event):
            requests.append(ResyncRequest.model_validate(event.payload))

        bus.subscribe(spy, types=[EventType.BOOK_RESYNC_REQUESTED], name="spy")

        await tidal.on_snapshot(snapshot("VENUE_A", "BTC-USD", 1000))
        await tidal.on_delta(delta("VENUE_A", "BTC-USD", 1002, 1003))
        await bus.drain()

        assert len(requests) == 1, "a gap must ask for recovery, not just report one"
        assert requests[0].venue == "VENUE_A"
        assert requests[0].symbol == "BTC-USD"
        assert "sequence gap" in requests[0].reason
        assert tidal.resync_requests == 1

    async def test_the_book_is_unusable_the_moment_the_gap_is_seen(self, tidal, clock):
        await tidal.on_snapshot(snapshot("VENUE_A", "BTC-USD", 1000))
        await tidal.on_delta(delta("VENUE_A", "BTC-USD", 1002, 1003))
        state = tidal.venue_state("VENUE_A", "BTC-USD")
        assert state is not None
        assert state.quality is DataQuality.UNAVAILABLE
        assert not state.quality.is_usable

    async def test_a_burst_of_gapped_deltas_asks_once(self, tidal, bus, clock):
        """Every delta after a gap hits an unsynced book. That is one problem."""
        count = 0

        async def spy(_event):
            nonlocal count
            count += 1

        bus.subscribe(spy, types=[EventType.BOOK_RESYNC_REQUESTED], name="spy")

        await tidal.on_snapshot(snapshot("VENUE_A", "BTC-USD", 1000))
        for i in range(50):
            await tidal.on_delta(delta("VENUE_A", "BTC-USD", 1002 + i, 1003 + i))
        await bus.drain()

        assert count == 1, "50 reports of one gap must not be 50 requests"
        assert tidal.desyncs == 50, "each one is still recorded"

    async def test_the_rate_limit_lifts_with_time(self, tidal, bus, clock):
        count = 0

        async def spy(_event):
            nonlocal count
            count += 1

        bus.subscribe(spy, types=[EventType.BOOK_RESYNC_REQUESTED], name="spy")

        await tidal.on_snapshot(snapshot("VENUE_A", "BTC-USD", 1000))
        await tidal.on_delta(delta("VENUE_A", "BTC-USD", 1002, 1003))
        clock.advance(tidal.resync_request_interval_ms + 1)
        await tidal.on_delta(delta("VENUE_A", "BTC-USD", 1004, 1005))
        await bus.drain()
        assert count == 2, "a gap that persists must be asked about again"


class TestBridgeRouting:
    async def test_the_request_reaches_the_owning_adapter(
        self, tidal, bus, clock, venue_config
    ):
        adapter = RecordingAdapter(venue_config, clock, ["BTC-USD"])
        bus.subscribe(
            ResyncBridge({"VENUE_A": adapter}),
            types=[EventType.BOOK_RESYNC_REQUESTED],
            name="venue-resync",
        )

        await tidal.on_snapshot(snapshot("VENUE_A", "BTC-USD", 1000))
        await tidal.on_delta(delta("VENUE_A", "BTC-USD", 1002, 1003))
        await bus.drain()

        assert adapter.requests, "the adapter never heard about the gap"
        symbol, reason = adapter.requests[0]
        assert symbol == "BTC-USD"
        assert "sequence gap" in reason

    async def test_a_request_for_an_unknown_venue_is_dropped(self, bus, clock, venue_config):
        adapter = RecordingAdapter(venue_config, clock, ["BTC-USD"])
        bridge = ResyncBridge({"VENUE_A": adapter})
        await bridge(
            type(
                "E", (), {"payload": {"venue": "VENUE_Z", "symbol": "BTC-USD", "requested_at": 0}}
            )()
        )
        assert adapter.requests == []
        assert bridge.unknown_venue == 1

    async def test_tidal_holds_no_adapter_reference(self, tidal):
        """The decoupling is the design, so it is asserted, not assumed."""
        for value in vars(tidal).values():
            assert not isinstance(value, VenueAdapter)
            if isinstance(value, dict):
                assert not any(isinstance(v, VenueAdapter) for v in value.values())


class TestRecoveryCompletes:
    async def test_the_book_returns_to_fresh_after_a_resync(self, tidal, bus, clock):
        """The whole chain: gap -> request -> checkpoint -> usable book.

        This is the assertion the old test should have made. `needs_resync` on
        its own was true for a book that would never recover.
        """

        async def answer(event):
            request = ResyncRequest.model_validate(event.payload)
            # What a real adapter does: fetch a fresh public checkpoint and
            # put it back on the bus through the normal path.
            await tidal.on_snapshot(snapshot(request.venue, request.symbol, 5000, bid=100.5))

        bus.subscribe(answer, types=[EventType.BOOK_RESYNC_REQUESTED], name="feed")

        await tidal.on_snapshot(snapshot("VENUE_A", "BTC-USD", 1000))
        await tidal.on_delta(delta("VENUE_A", "BTC-USD", 1001, 1002, bids=[(100.0, 4.0)]))
        assert tidal.venue_state("VENUE_A", "BTC-USD").quality is DataQuality.FRESH

        await tidal.on_delta(delta("VENUE_A", "BTC-USD", 1004, 1005))
        assert tidal.venue_state("VENUE_A", "BTC-USD").quality is DataQuality.UNAVAILABLE

        await bus.drain()

        book = tidal.books[("VENUE_A", "BTC-USD")]
        assert book.synced and book.sequence == 5000
        assert not book.needs_resync
        state = tidal.venue_state("VENUE_A", "BTC-USD")
        assert state.quality is DataQuality.FRESH
        assert state.quality.is_usable
        assert state.metrics.best_bid == pytest.approx(100.5)

    async def test_the_first_delta_after_recovery_may_straddle(self, tidal, bus, clock):
        """Recovery is only real if the stream can rejoin the new checkpoint."""
        await tidal.on_snapshot(snapshot("VENUE_A", "BTC-USD", 1000))
        await tidal.on_delta(delta("VENUE_A", "BTC-USD", 1005, 1006))  # gap
        await tidal.on_snapshot(snapshot("VENUE_A", "BTC-USD", 5000))
        await tidal.on_delta(delta("VENUE_A", "BTC-USD", 4998, 5004, bids=[(100.0, 9.0)]))

        book = tidal.books[("VENUE_A", "BTC-USD")]
        assert book.synced, "U <= L+1 <= u must hold after a resync too"
        assert book.sequence == 5004
        assert book.bids[100.0] == 9.0


class TestIsolation:
    async def test_one_symbol_desyncing_leaves_the_other_alone(self, tidal, bus, clock):
        for symbol in ("BTC-USD", "ETH-USD"):
            await tidal.on_snapshot(snapshot("VENUE_A", symbol, 1000))
            await tidal.on_delta(delta("VENUE_A", symbol, 1001, 1002))

        await tidal.on_delta(delta("VENUE_A", "BTC-USD", 2000, 2001))  # gap on BTC
        await bus.drain()

        btc = tidal.venue_state("VENUE_A", "BTC-USD")
        eth = tidal.venue_state("VENUE_A", "ETH-USD")
        assert btc.quality is DataQuality.UNAVAILABLE
        assert eth.quality is DataQuality.FRESH and eth.quality.is_usable
        assert tidal.books[("VENUE_A", "ETH-USD")].sequence_gaps == 0

        # And ETH keeps advancing while BTC is down.
        await tidal.on_delta(delta("VENUE_A", "ETH-USD", 1003, 1004, bids=[(100.0, 6.0)]))
        assert tidal.books[("VENUE_A", "ETH-USD")].bids[100.0] == 6.0

    async def test_one_venue_desyncing_leaves_the_other_alone(self, tidal, bus, clock):
        for venue in ("VENUE_A", "VENUE_B"):
            await tidal.on_snapshot(snapshot(venue, "BTC-USD", 1000))
            await tidal.on_delta(delta(venue, "BTC-USD", 1001, 1002))

        await tidal.on_delta(delta("VENUE_A", "BTC-USD", 2000, 2001))
        await bus.drain()

        assert tidal.venue_state("VENUE_A", "BTC-USD").quality is DataQuality.UNAVAILABLE
        assert tidal.venue_state("VENUE_B", "BTC-USD").quality is DataQuality.FRESH

    async def test_the_resync_request_names_only_the_affected_book(
        self, tidal, bus, clock, venue_config
    ):
        adapter = RecordingAdapter(venue_config, clock, ["BTC-USD", "ETH-USD"])
        bus.subscribe(
            ResyncBridge({"VENUE_A": adapter}),
            types=[EventType.BOOK_RESYNC_REQUESTED],
            name="venue-resync",
        )
        for symbol in ("BTC-USD", "ETH-USD"):
            await tidal.on_snapshot(snapshot("VENUE_A", symbol, 1000))
        await tidal.on_delta(delta("VENUE_A", "BTC-USD", 2000, 2001))
        await bus.drain()

        assert [s for s, _ in adapter.requests] == ["BTC-USD"]


class TestNoQuestionableDataWhileRecovering:
    async def test_a_recovering_book_never_reports_fresh(self, tidal, clock):
        """Between the gap and the checkpoint there is no usable state."""
        await tidal.on_snapshot(snapshot("VENUE_A", "BTC-USD", 1000))
        await tidal.on_delta(delta("VENUE_A", "BTC-USD", 2000, 2001))

        for _ in range(10):
            clock.advance(100)
            state = tidal.venue_state("VENUE_A", "BTC-USD")
            assert state.quality is DataQuality.UNAVAILABLE
            # Deltas keep arriving and keep being refused.
            await tidal.on_delta(delta("VENUE_A", "BTC-USD", 2100, 2101))
            assert not tidal.books[("VENUE_A", "BTC-USD")].synced

    async def test_a_desynced_book_is_excluded_from_consolidation(self, tidal, clock):
        await tidal.on_snapshot(snapshot("VENUE_A", "BTC-USD", 1000, bid=100.0, ask=101.0))
        await tidal.on_snapshot(snapshot("VENUE_B", "BTC-USD", 1000, bid=110.0, ask=111.0))
        await tidal.on_delta(delta("VENUE_A", "BTC-USD", 2000, 2001))

        state = tidal.build_state()
        view = state.consolidated["BTC-USD"]
        assert view.best_bid_venue != "VENUE_A"
        assert view.best_ask_venue != "VENUE_A"
        assert view.usable_venues == ["VENUE_B"]


class TestPaperBoundaryUnchanged:
    def test_request_resync_adds_no_trading_surface(self):
        """The new method is the only addition to the venue interface."""
        import inspect

        signature = inspect.signature(VenueAdapter.request_resync)
        assert list(signature.parameters) == ["self", "symbol", "reason"]
        assert VenueAdapter.capabilities.order_submission is False
        assert VenueAdapter.capabilities.authenticated is False

    def test_the_default_implementation_does_nothing(self, venue_config, clock):
        """A feed with no checkpoint endpoint must not be forced to invent one."""

        class Minimal(VenueAdapter):
            async def run(self) -> None:  # pragma: no cover
                raise AssertionError

        import asyncio

        adapter = Minimal(venue_config, clock, ["BTC-USD"])
        assert asyncio.run(adapter.request_resync("BTC-USD", "gap")) is None

    def test_no_order_verb_appears_on_the_binance_adapter(self):
        from venues.venue_a.adapter import VenueAAdapter

        banned = ("order", "cancel", "withdraw", "sign", "auth", "key", "secret")
        for name in dir(VenueAAdapter):
            if name.startswith("_"):
                continue
            assert not any(word in name.lower() for word in banned), name


class TestWiredIntoThePlatform:
    """The bridge has to exist in the real composition root, not just in tests.

    Every other test here builds the chain by hand, which proves the parts fit
    but not that anything assembled them. This one asks the actual platform.
    """

    async def test_the_platform_subscribes_the_bridge(self, platform):
        assert platform.resync_bridge is not None
        names = [sub.name for sub in platform.bus._subs]
        assert "venue-resync" in names
        assert set(platform.resync_bridge.adapters) == set(platform.adapters)

    async def test_a_gap_in_a_running_platform_reaches_the_adapter(self, platform):
        seen: list[tuple[str, str]] = []

        for adapter in platform.adapters.values():

            async def record(symbol, reason="", _venue=adapter.name):
                seen.append((_venue, symbol))

            adapter.request_resync = record  # type: ignore[method-assign]

        tidal = platform.tidal
        await tidal.on_snapshot(snapshot("VENUE_A", "BTC-USD", 1000))
        await tidal.on_delta(delta("VENUE_A", "BTC-USD", 2000, 2001))
        await platform.bus.drain()

        assert seen == [("VENUE_A", "BTC-USD")]


class TestReplaySafety:
    """A replay must never reach out to a venue.

    Resync requests are recorded like every other event. If replay re-published
    them, reading back an old session would fire live REST calls against a
    public endpoint — turning an offline, deterministic replay into a network
    client, and making the run non-reproducible.
    """

    def test_resync_requests_are_not_replayable_market_input(self):
        from core.events import MARKET_INPUT_TYPES

        assert EventType.BOOK_RESYNC_REQUESTED not in MARKET_INPUT_TYPES

    async def test_replaying_a_recording_never_asks_a_venue_for_anything(
        self, bus, clock, store, venue_config
    ):
        """The behavioural version: record one, replay it, watch the adapter."""
        from core.events import Event
        from replay.engine import ReplaySession

        session_id = "sess-replay-guard"
        await store.open()
        # Storage no longer creates sessions implicitly (Phase 2 Batch 2):
        # an append to an unknown id is a wiring mistake, not a second,
        # invisible session.
        await store.start_session(session_id, START_MS)
        await store.append_many(
            session_id,
            [
                Event(
                    type=EventType.BOOK_SNAPSHOT,
                    ts_ms=START_MS,
                    source="VENUE_A",
                    schema_name="OrderBookSnapshot",
                    payload=snapshot("VENUE_A", "BTC-USD", 1000).to_json_dict(),
                ),
                Event(
                    type=EventType.BOOK_RESYNC_REQUESTED,
                    ts_ms=START_MS + 1,
                    source="TIDAL",
                    schema_name="ResyncRequest",
                    payload=ResyncRequest(
                        venue="VENUE_A",
                        symbol="BTC-USD",
                        requested_at=START_MS + 1,
                        reason="recorded gap",
                    ).to_json_dict(),
                ),
            ],
        )
        # Exact replay requires a session whose recording integrity was
        # verified (Phase 2 Batch 2); a hand-built recording must say so.
        await store.finalize_session(
            session_id, START_MS + 2, status=SessionStatus.COMPLETE
        )

        adapter = RecordingAdapter(venue_config, clock, ["BTC-USD"])
        bus.subscribe(
            ResyncBridge({"VENUE_A": adapter}),
            types=[EventType.BOOK_RESYNC_REQUESTED],
            name="venue-resync",
        )

        # Hand-built session, never produced by a real Orchestrator.tick(),
        # so it carries no ORCHESTRATOR_TICK markers -- irrelevant to what
        # this test checks (resync safety, not tick cadence).
        session = ReplaySession(
            store=store, bus=bus, clock=clock, session_id=session_id, legacy_timeline=True
        )
        await session.open()
        stats = await session.run()
        session.close()

        assert stats.events_published == 1, "only the snapshot is a market input"
        assert adapter.requests == [], (
            "a replay that re-fires resync requests would hit the live endpoint"
        )


class TestBookInvariantsHold:
    def test_an_unsynced_book_refuses_every_delta(self):
        book = LocalOrderBook(venue="VENUE_A", symbol="BTC-USD")
        assert not book.synced
        with pytest.raises(Exception, match="no snapshot yet"):
            book.apply_delta(delta("VENUE_A", "BTC-USD", 1, 2))
