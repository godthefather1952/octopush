"""Full-book storage safety bound (Batch 5, FULL-BOOK STORAGE BOUND).

TIDAL-M1 (Batch 4) correctly stopped ``LocalOrderBook`` from destructively
trimming storage to ``max_depth`` on every write — trimming on write silently
discarded levels the venue never said were gone. But the fix's necessary
consequence is that storage is now genuinely unbounded: a pathological
insert-only stream (measured at ~200,000 levels / ~15-19MB for one book) has
nothing capping it.

This is a different problem from TIDAL-M1 and is not solved by reintroducing
any form of trimming, lossy or otherwise — that would just be M1 again with
extra steps. The fix here is a hard safety *ceiling*
(``max_book_levels_per_side``) that does not change how the book behaves
below it: exactly at the ceiling is a perfectly normal book. Only crossing it
is failure, and failure here means the same thing a sequence gap already
means — the book cannot be trusted, so it is marked unsynchronised and a
fresh, bounded snapshot is requested through the exact recovery path
TIDAL-C2/TIDAL-M4 already built. No level is ever silently evicted to make
storage fit.
"""

from __future__ import annotations

import asyncio
import contextlib
from unittest.mock import patch

import pytest

from agents.tidal import Tidal
from agents.tidal.book import BookDesyncError, BookOverflowError, LocalOrderBook
from apps.orchestrator.wiring import ResyncBridge
from core.bus import InMemoryEventBus
from core.clock import ManualClock
from core.config import Settings, VenueConfig
from core.events import EventType
from core.health import HealthRegistry
from core.models.common import DataQuality, Side
from core.models.market import OrderBookSnapshot, PriceLevel
from tests.conftest import START_MS
from venues.base.adapter import VenueAdapter
from venues.base.messages import BookDelta, ResyncRequest
from venues.venue_b.adapter import VenueBAdapter


def snapshot(venue: str, symbol: str, last_id, *, bids=(), asks=(), ts=START_MS):
    return OrderBookSnapshot(
        venue=venue,
        symbol=symbol,
        exchange_ts=ts,
        received_ts=ts,
        sequence=last_id,
        bids=[PriceLevel(price=p, size=s) for p, s in bids] or [PriceLevel(price=100.0, size=1.0)],
        asks=[PriceLevel(price=p, size=s) for p, s in asks] or [PriceLevel(price=101.0, size=1.0)],
        is_checkpoint=True,
    )


def range_delta(*, first, final, bids=(), asks=(), venue="VENUE_A", symbol="BTC-USD", ts=START_MS):
    return BookDelta(
        venue=venue, symbol=symbol, exchange_ts=ts, received_ts=ts,
        first_sequence=first, sequence=final,
        bids=[PriceLevel(price=p, size=s) for p, s in bids],
        asks=[PriceLevel(price=p, size=s) for p, s in asks],
    )


def cb_delta(*, exchange_ts, bids=(), asks=(), venue="VENUE_B", symbol="BTC-USD"):
    return BookDelta(
        venue=venue, symbol=symbol, exchange_ts=exchange_ts, received_ts=exchange_ts,
        sequence=None, first_sequence=None,
        bids=[PriceLevel(price=p, size=s) for p, s in bids],
        asks=[PriceLevel(price=p, size=s) for p, s in asks],
    )


class RecordingAdapter(VenueAdapter):
    """Same test double used throughout ``test_resync_flow.py``."""

    def __init__(self, config, clock, symbols) -> None:
        super().__init__(config, clock, symbols)
        self.requests: list[tuple[str, str]] = []

    async def run(self) -> None:  # pragma: no cover - never started
        raise AssertionError("this adapter is driven directly")

    async def request_resync(self, symbol: str, reason: str = "") -> None:
        self.requests.append((symbol, reason))


def bounded_settings(base: Settings, *, max_levels: int, book_depth: int = 2) -> Settings:
    """A copy of ``base`` with a small, deterministic storage bound.

    The production default (10,000) would make these tests correct but slow
    to construct; a small explicit bound exercises the exact same code path
    without needing thousands of synthetic levels per test.
    """
    venues = [
        v.model_copy(
            update={"max_book_levels_per_side": max_levels, "book_depth_levels": book_depth}
        )
        for v in base.venues
    ]
    return base.model_copy(update={"venues": venues})


# ======================================================================
# A / C / D / E — the bound itself, directly on LocalOrderBook
# ======================================================================


class TestNormalBookUnderLimitIsExact:
    def test_a_full_book_comfortably_under_the_limit_is_unaffected(self):
        book = LocalOrderBook(venue="V", symbol="BTC-USD", max_depth=2, max_levels_per_side=10)
        book.apply_snapshot(
            snapshot(
                "V", "BTC-USD", 1,
                bids=[(100.0 - i, 1.0) for i in range(5)],
                asks=[(101.0 + i, 1.0) for i in range(5)],
            )
        )
        book.apply_delta(range_delta(first=2, final=2, bids=[(80.0, 3.0)], venue="V"))
        assert len(book.bids) == 6
        assert book.bids[80.0] == 3.0
        assert book.synced


class TestBuriedLevelStillResurfacesWithABoundConfigured:
    """M1 regression check: a finite storage bound must not reintroduce the
    destructive top-N trimming M1 removed, as long as the book stays under it.
    """

    def test_a_level_outside_the_read_depth_still_resurfaces(self):
        book = LocalOrderBook(venue="V", symbol="BTC-USD", max_depth=2, max_levels_per_side=20)
        book.apply_snapshot(
            snapshot("V", "BTC-USD", 1, bids=[(100.0 - i, 1.0) for i in range(5)])
        )
        book.apply_delta(range_delta(first=2, final=2, bids=[(50.0, 9.0)], venue="V"))
        assert 50.0 not in [lvl.price for lvl in book.levels(Side.BUY)]
        assert book.bids[50.0] == 9.0, "storage must hold it even though it is buried"
        for price in [p for p in list(book.bids) if p != 50.0]:
            book.apply_delta(
                range_delta(first=book.sequence + 1, final=book.sequence + 1, bids=[(price, 0.0)], venue="V")
            )
        assert book.bids == {50.0: 9.0}
        assert book.synced, "staying under the bound the whole time must not fail the book"


class TestExactlyAtTheLimitIsValid:
    def test_hitting_the_bound_precisely_does_not_fail_the_book(self):
        book = LocalOrderBook(venue="V", symbol="BTC-USD", max_depth=2, max_levels_per_side=5)
        book.apply_snapshot(
            snapshot("V", "BTC-USD", 1, bids=[(100.0 - i, 1.0) for i in range(5)])
        )
        assert len(book.bids) == 5
        assert book.synced
        assert book.overflow_count == 0
        # A delta that only rewrites existing levels stays exactly at 5.
        book.apply_delta(range_delta(first=2, final=2, bids=[(100.0, 2.0)], venue="V"))
        assert len(book.bids) == 5
        assert book.synced


class TestExceedingByOneFailsClosed:
    def test_one_level_past_the_bound_raises_and_invalidates(self):
        book = LocalOrderBook(venue="V", symbol="BTC-USD", max_depth=2, max_levels_per_side=5)
        book.apply_snapshot(
            snapshot("V", "BTC-USD", 1, bids=[(100.0 - i, 1.0) for i in range(5)])
        )
        with pytest.raises(BookOverflowError):
            book.apply_delta(range_delta(first=2, final=2, bids=[(50.0, 1.0)], venue="V"))
        assert not book.synced
        assert book.needs_resync
        assert book.overflow_count == 1

    def test_overflow_error_is_a_book_desync_error(self):
        """So it reaches the existing recovery path (``on_delta``'s except
        clause) without TIDAL needing to know a new exception type exists.
        """
        assert issubclass(BookOverflowError, BookDesyncError)

    def test_the_ask_side_alone_can_also_trigger_it(self):
        book = LocalOrderBook(venue="V", symbol="BTC-USD", max_depth=2, max_levels_per_side=5)
        book.apply_snapshot(
            snapshot("V", "BTC-USD", 1, asks=[(101.0 + i, 1.0) for i in range(5)])
        )
        with pytest.raises(BookOverflowError):
            book.apply_delta(range_delta(first=2, final=2, asks=[(200.0, 1.0)], venue="V"))
        assert not book.synced


class TestNoLevelIsSilentlyDiscardedOnOverflow:
    def test_every_level_is_still_present_after_the_failure(self):
        """The fail-closed design must never quietly evict levels to fit —
        that would just be TIDAL-M1's destructive trimming again. All 6
        levels remain in storage; only ``synced`` changes.
        """
        book = LocalOrderBook(venue="V", symbol="BTC-USD", max_depth=2, max_levels_per_side=5)
        book.apply_snapshot(
            snapshot("V", "BTC-USD", 1, bids=[(100.0 - i, 1.0) for i in range(5)])
        )
        with pytest.raises(BookOverflowError):
            book.apply_delta(range_delta(first=2, final=2, bids=[(50.0, 4.0)], venue="V"))
        assert len(book.bids) == 6, "no level was evicted to bring storage back under the bound"
        assert book.bids[50.0] == 4.0


# ======================================================================
# F / H / I / J / K — through TIDAL, the real recovery wiring
# ======================================================================


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock(START_MS)


@pytest.fixture
def bus() -> InMemoryEventBus:
    return InMemoryEventBus(raise_on_handler_error=True)


@pytest.fixture
def bounded(settings: Settings) -> Settings:
    return bounded_settings(settings, max_levels=5, book_depth=2)


@pytest.fixture
def tidal(bus: InMemoryEventBus, clock: ManualClock, bounded: Settings) -> Tidal:
    agent = Tidal(bus, clock, bounded, HealthRegistry(clock=clock))
    agent.subscribe()
    return agent


@pytest.fixture
def venue_config() -> VenueConfig:
    return VenueConfig(name="VENUE_A", display_name="A", adapter="simulated")


def overflow_delta(venue: str, symbol: str, book: LocalOrderBook, *, price: float = 999.0):
    """One delta guaranteed to push ``book`` one level past its bound."""
    return range_delta(
        first=book.sequence + 1, final=book.sequence + 1, bids=[(price, 1.0)],
        venue=venue, symbol=symbol,
    )


class TestBinanceOverflowTriggersOnlyAffectedSymbolResync:
    async def test_only_the_overflowing_symbol_is_asked_for(
        self, tidal, bus, clock, venue_config
    ):
        adapter = RecordingAdapter(venue_config, clock, ["BTC-USD", "ETH-USD"])
        bus.subscribe(
            ResyncBridge({"VENUE_A": adapter}),
            types=[EventType.BOOK_RESYNC_REQUESTED],
            name="venue-resync",
        )
        for symbol in ("BTC-USD", "ETH-USD"):
            await tidal.on_snapshot(
                snapshot("VENUE_A", symbol, 1, bids=[(100.0 - i, 1.0) for i in range(5)])
            )

        btc_book = tidal.books[("VENUE_A", "BTC-USD")]
        await tidal.on_delta(overflow_delta("VENUE_A", "BTC-USD", btc_book))
        await bus.drain()

        assert [s for s, _ in adapter.requests] == ["BTC-USD"]
        assert tidal.books[("VENUE_A", "ETH-USD")].overflow_count == 0


class TestCoinbaseOverflowForcesAFullReconnect:
    """Unlike Binance, Coinbase has no per-symbol checkpoint (Batch 3): the
    only verified recovery is a full reconnect, which necessarily carries
    every symbol on that connection with it, not just the one that
    overflowed. This is the documented blast radius, identical to the one
    ``_contain()`` already accepts for a malformed book-affecting message.
    """

    async def test_request_resync_closes_the_live_socket_forcing_reconnect(self):
        clock = ManualClock(START_MS)
        config = VenueConfig(name="VENUE_B", display_name="B", adapter="venue_b")
        adapter = VenueBAdapter(config, clock, ["BTC-USD", "ETH-USD"])

        class ClosableSocket:
            def __init__(self) -> None:
                self._closed = asyncio.Event()
                self.sent: list[str] = []
                self.close_calls = 0

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def recv(self) -> str:
                await self._closed.wait()
                raise ConnectionResetError("closed by request_resync")

            async def send(self, data: str) -> None:
                self.sent.append(data)

            async def close(self) -> None:
                self.close_calls += 1
                self._closed.set()

        socket = ClosableSocket()
        with patch("websockets.connect", lambda *a, **kw: socket):
            session_task = asyncio.create_task(adapter._session())
            for _ in range(50):
                await asyncio.sleep(0)
            assert adapter.stats.connected is True, "sanity: the session actually connected"

            await adapter.request_resync("BTC-USD", "book overflow: max_book_levels_per_side")

            with pytest.raises(ConnectionResetError):
                await asyncio.wait_for(session_task, timeout=5)

        assert socket.close_calls == 1
        # The forced close tears down the one socket every subscribed symbol
        # shares -- there is no way to reconnect "just BTC-USD" on this feed.
        assert adapter.symbols == ["BTC-USD", "ETH-USD"]


class TestOneSymbolOverflowingLeavesTheOtherAlone:
    async def test_btc_overflow_does_not_corrupt_eth(self, tidal, bus, clock):
        for symbol in ("BTC-USD", "ETH-USD"):
            await tidal.on_snapshot(
                snapshot("VENUE_A", symbol, 1, bids=[(100.0 - i, 1.0) for i in range(5)])
            )
        btc_book = tidal.books[("VENUE_A", "BTC-USD")]
        with contextlib.suppress(BookOverflowError):
            btc_book.apply_delta(overflow_delta("VENUE_A", "BTC-USD", btc_book))

        eth_book = tidal.books[("VENUE_A", "ETH-USD")]
        assert eth_book.synced
        assert eth_book.overflow_count == 0
        assert tidal.venue_state("VENUE_A", "ETH-USD").quality is DataQuality.FRESH
        await eth_book_advances(tidal, eth_book)


async def eth_book_advances(tidal, eth_book):
    seq = eth_book.sequence
    await tidal.on_delta(
        range_delta(first=seq + 1, final=seq + 1, bids=[(100.0, 5.0)], venue="VENUE_A", symbol="ETH-USD")
    )
    assert tidal.books[("VENUE_A", "ETH-USD")].bids[100.0] == 5.0


class TestOverflowingBookCannotRemainFresh:
    async def test_quality_is_unavailable_immediately_after_overflow(self, tidal, bus, clock):
        await tidal.on_snapshot(
            snapshot("VENUE_A", "BTC-USD", 1, bids=[(100.0 - i, 1.0) for i in range(5)])
        )
        assert tidal.venue_state("VENUE_A", "BTC-USD").quality is DataQuality.FRESH
        book = tidal.books[("VENUE_A", "BTC-USD")]
        await tidal.on_delta(overflow_delta("VENUE_A", "BTC-USD", book))
        state = tidal.venue_state("VENUE_A", "BTC-USD")
        assert state.quality is DataQuality.UNAVAILABLE
        assert not state.quality.is_usable


class TestRecoveryReturnsTheBookToFresh:
    async def test_a_fresh_bounded_snapshot_restores_fresh_quality(self, tidal, bus, clock):
        async def answer(event):
            request = ResyncRequest.model_validate(event.payload)
            await tidal.on_snapshot(
                snapshot(
                    request.venue, request.symbol, 5000,
                    bids=[(100.5 - i, 1.0) for i in range(3)],
                )
            )

        bus.subscribe(answer, types=[EventType.BOOK_RESYNC_REQUESTED], name="feed")

        await tidal.on_snapshot(
            snapshot("VENUE_A", "BTC-USD", 1, bids=[(100.0 - i, 1.0) for i in range(5)])
        )
        book = tidal.books[("VENUE_A", "BTC-USD")]
        await tidal.on_delta(overflow_delta("VENUE_A", "BTC-USD", book))
        assert tidal.venue_state("VENUE_A", "BTC-USD").quality is DataQuality.UNAVAILABLE

        await bus.drain()

        recovered = tidal.books[("VENUE_A", "BTC-USD")]
        assert recovered.synced
        assert not recovered.needs_resync
        assert len(recovered.bids) == 3, "the new snapshot is small again -- bounded, not inherited"
        state = tidal.venue_state("VENUE_A", "BTC-USD")
        assert state.quality is DataQuality.FRESH
        assert state.quality.is_usable


class TestRepeatedOverflowCannotStorm:
    async def test_a_burst_of_post_overflow_deltas_asks_once(self, tidal, bus, clock, venue_config):
        adapter = RecordingAdapter(venue_config, clock, ["BTC-USD"])
        bus.subscribe(
            ResyncBridge({"VENUE_A": adapter}),
            types=[EventType.BOOK_RESYNC_REQUESTED],
            name="venue-resync",
        )
        await tidal.on_snapshot(
            snapshot("VENUE_A", "BTC-USD", 1, bids=[(100.0 - i, 1.0) for i in range(5)])
        )
        book = tidal.books[("VENUE_A", "BTC-USD")]
        await tidal.on_delta(overflow_delta("VENUE_A", "BTC-USD", book))
        # Every further delta hits an unsynced book and reports again, but the
        # rate limiter (the same one gap recovery already relies on) must
        # still cap this to one outbound request, not a storm.
        for i in range(50):
            await tidal.on_delta(
                range_delta(first=9000 + i, final=9000 + i, bids=[(1.0, 1.0)], venue="VENUE_A")
            )
        await bus.drain()

        assert len(adapter.requests) == 1, "50 reports of one overflow must not be 50 requests"
        assert book.overflow_count == 1, "only the original delta actually overflowed anything"


# ======================================================================
# L — replay of an overflow condition is deterministic, no live network
# ======================================================================


class TestReplayOfOverflowIsDeterministicAndOffline:
    async def test_replaying_the_same_overflow_twice_gives_the_same_outcome(
        self, bus, clock, bounded, venue_config
    ):
        """Two independent Tidal instances, fed the exact same recorded
        snapshot + overflowing delta, must land in the exact same state --
        and neither may ever call a real network endpoint. ``RecordingAdapter``
        stands in for the venue precisely so a network call would be a
        programming error caught here, not a silent possibility.
        """

        def build():
            local_bus = InMemoryEventBus(raise_on_handler_error=True)
            agent = Tidal(local_bus, ManualClock(START_MS), bounded, HealthRegistry(clock=clock))
            agent.subscribe()
            adapter = RecordingAdapter(venue_config, clock, ["BTC-USD"])
            local_bus.subscribe(
                ResyncBridge({"VENUE_A": adapter}),
                types=[EventType.BOOK_RESYNC_REQUESTED],
                name="venue-resync",
            )
            return local_bus, agent, adapter

        async def run_once():
            local_bus, agent, adapter = build()
            await agent.on_snapshot(
                snapshot("VENUE_A", "BTC-USD", 1, bids=[(100.0 - i, 1.0) for i in range(5)])
            )
            book = agent.books[("VENUE_A", "BTC-USD")]
            await agent.on_delta(overflow_delta("VENUE_A", "BTC-USD", book))
            await local_bus.drain()
            return (
                book.synced,
                book.overflow_count,
                len(book.bids),
                [s for s, _ in adapter.requests],
            )

        first = await run_once()
        second = await run_once()
        assert first == second == (False, 1, 6, ["BTC-USD"])


# ======================================================================
# Pre-commit correction, Issue 3: the bound applies to snapshots too
# ======================================================================


class TestSnapshotExactlyAtTheLimitIsValid:
    def test_a_snapshot_with_exactly_the_bound_is_accepted(self):
        book = LocalOrderBook(venue="V", symbol="BTC-USD", max_depth=2, max_levels_per_side=5)
        book.apply_snapshot(
            snapshot("V", "BTC-USD", 1, bids=[(100.0 - i, 1.0) for i in range(5)])
        )
        assert book.synced
        assert len(book.bids) == 5
        assert book.overflow_count == 0


class TestSnapshotOneOverTheLimitIsUnusable:
    def test_a_snapshot_one_level_past_the_bound_raises_and_stays_unsynced(self):
        book = LocalOrderBook(venue="V", symbol="BTC-USD", max_depth=2, max_levels_per_side=5)
        with pytest.raises(BookOverflowError):
            book.apply_snapshot(
                snapshot("V", "BTC-USD", 1, bids=[(100.0 - i, 1.0) for i in range(6)])
            )
        assert not book.synced
        assert book.needs_resync
        assert book.overflow_count == 1

    def test_an_oversized_snapshot_is_never_marked_synced_even_as_the_first_ever_snapshot(self):
        """A book with no prior state at all must not become usable just
        because there was nothing to compare the oversized snapshot against.
        """
        book = LocalOrderBook(venue="V", symbol="BTC-USD", max_levels_per_side=3)
        assert not book.synced
        with pytest.raises(BookOverflowError):
            book.apply_snapshot(
                snapshot("V", "BTC-USD", 1, bids=[(100.0 - i, 1.0) for i in range(4)])
            )
        assert not book.synced


class TestOversizedSnapshotNeverBecomesResidentInTheBook:
    """Final pre-commit correction: unlike a delta (whose possible overshoot
    is tiny, so applying it first and checking after is cheap and keeps
    every level visible for diagnosis -- see ``_check_storage_bound``), a
    snapshot can legitimately be enormous. Copying an oversized snapshot's
    full content into authoritative storage before checking it would defeat
    the entire point of the bound: a 100x-oversized snapshot must never leave
    100x-the-limit levels resident in this book, not even momentarily.
    """

    def test_a_wildly_oversized_snapshot_leaves_the_book_empty_not_full(self):
        book = LocalOrderBook(venue="V", symbol="BTC-USD", max_depth=2, max_levels_per_side=5)
        n = 500
        with pytest.raises(BookOverflowError):
            book.apply_snapshot(
                snapshot("V", "BTC-USD", 1, bids=[(float(n - i), 1.0) for i in range(n)])
            )
        assert book.bids == {}, "an oversized snapshot's content must never be copied in at all"
        assert book.asks == {}

    def test_a_prior_valid_book_is_left_completely_untouched_by_a_rejected_snapshot(self):
        """Rejecting the entire snapshot is not the destructive trimming
        TIDAL-M1 removed (trimming silently keeps *some* levels while hiding
        that others existed): the whole message is refused, and whatever the
        book held before is exactly what it holds after -- not overwritten
        with any part of the oversized content, and not emptied either.
        """
        book = LocalOrderBook(venue="V", symbol="BTC-USD", max_depth=2, max_levels_per_side=5)
        book.apply_snapshot(snapshot("V", "BTC-USD", 1, bids=[(100.0, 1.0), (99.0, 2.0)]))
        prior_bids = dict(book.bids)

        n = 500
        with pytest.raises(BookOverflowError):
            book.apply_snapshot(
                snapshot("V", "BTC-USD", 2, bids=[(float(n - i), 1.0) for i in range(n)])
            )

        assert book.bids == prior_bids, "the prior snapshot's content must be untouched"

    def test_the_error_reports_the_incoming_count_not_a_built_dict_size(self):
        """The check runs against the incoming message's own level count,
        not against a dict built from it -- this is what makes it safe to
        reject before ever materializing the oversized structure.
        """
        book = LocalOrderBook(venue="V", symbol="BTC-USD", max_levels_per_side=3)
        n = 10_000
        huge = snapshot("V", "BTC-USD", 1, bids=[(float(n - i), 1.0) for i in range(n)])
        with pytest.raises(BookOverflowError, match="incoming bids=10000"):
            book.apply_snapshot(huge)
        assert len(book.bids) == 0


class TestOversizedBinanceCheckpointCannotBecomeFresh:
    async def test_quality_stays_unavailable_and_a_resync_is_requested(
        self, tidal, bus, clock, venue_config
    ):
        adapter = RecordingAdapter(venue_config, clock, ["BTC-USD"])
        bus.subscribe(
            ResyncBridge({"VENUE_A": adapter}),
            types=[EventType.BOOK_RESYNC_REQUESTED],
            name="venue-resync",
        )
        # bounded settings fixture uses max_levels_per_side=5.
        await tidal.on_snapshot(
            snapshot("VENUE_A", "BTC-USD", 1, bids=[(100.0 - i, 1.0) for i in range(6)])
        )
        await bus.drain()
        state = tidal.venue_state("VENUE_A", "BTC-USD")
        assert state.quality is DataQuality.UNAVAILABLE
        assert not state.quality.is_usable
        assert [s for s, _ in adapter.requests] == ["BTC-USD"]
        assert tidal.books[("VENUE_A", "BTC-USD")].overflow_count == 1


class TestOversizedCoinbaseSnapshotCannotBecomeFresh:
    async def test_quality_stays_unavailable_and_reconnect_is_requested(self, tidal, bus, clock):
        clock2 = ManualClock(START_MS)
        config = VenueConfig(name="VENUE_B", display_name="B", adapter="venue_b")
        adapter = VenueBAdapter(config, clock2, ["BTC-USD"])
        bus.subscribe(
            ResyncBridge({"VENUE_B": adapter}),
            types=[EventType.BOOK_RESYNC_REQUESTED],
            name="venue-resync",
        )
        await tidal.on_snapshot(
            snapshot("VENUE_B", "BTC-USD", None, bids=[(100.0 - i, 1.0) for i in range(6)])
        )
        await bus.drain()
        state = tidal.venue_state("VENUE_B", "BTC-USD")
        assert state.quality is DataQuality.UNAVAILABLE
        assert not state.quality.is_usable
        # VenueBAdapter.request_resync closed no socket (none open in this
        # test), but it did run and count the attempt -- proving the same
        # generic on_snapshot -> request_resync path fires for Coinbase too.
        assert adapter.stats.sequence_gaps == 1


class TestRepeatedOversizedSnapshotsDoNotStorm:
    async def test_retries_stay_rate_limited_and_escalate_to_a_health_error(
        self, tidal, bus, clock, venue_config
    ):
        adapter = RecordingAdapter(venue_config, clock, ["BTC-USD"])
        bus.subscribe(
            ResyncBridge({"VENUE_A": adapter}),
            types=[EventType.BOOK_RESYNC_REQUESTED],
            name="venue-resync",
        )
        alerts: list[str] = []

        async def spy(event):
            if event.payload.get("kind") == "BOOK_OVERFLOW_PERSISTENT":
                alerts.append(event.payload["message"])

        bus.subscribe(spy, types=[EventType.SYSTEM_EVENT], name="alert-spy")

        # The venue keeps sending an oversized checkpoint every time recovery
        # is attempted -- a structurally incompatible configuration, not a
        # transient gap.
        for _ in range(5):
            await tidal.on_snapshot(
                snapshot("VENUE_A", "BTC-USD", 1, bids=[(100.0 - i, 1.0) for i in range(6)])
            )
        await bus.drain()

        assert len(adapter.requests) == 1, (
            "5 oversized snapshots in a row must not be 5 outbound resync requests"
        )
        assert any("misconfigured" in msg for msg in alerts), (
            "a persistent mismatch must surface a distinct, visible health error"
        )
        assert tidal.books[("VENUE_A", "BTC-USD")].overflow_count == 5, (
            "every overflow is still individually counted even though requests are rate-limited"
        )


class TestNormalRecoverySnapshotBelowTheLimitRestoresFresh:
    async def test_a_bounded_recovery_snapshot_returns_the_book_to_fresh(
        self, tidal, bus, clock
    ):
        async def answer(event):
            request = ResyncRequest.model_validate(event.payload)
            # The venue corrects itself on the next attempt -- a small,
            # in-bound checkpoint.
            await tidal.on_snapshot(
                snapshot(request.venue, request.symbol, 5000, bids=[(100.5, 1.0), (100.0, 1.0)])
            )

        bus.subscribe(answer, types=[EventType.BOOK_RESYNC_REQUESTED], name="feed")

        await tidal.on_snapshot(
            snapshot("VENUE_A", "BTC-USD", 1, bids=[(100.0 - i, 1.0) for i in range(6)])
        )
        assert tidal.venue_state("VENUE_A", "BTC-USD").quality is DataQuality.UNAVAILABLE

        await bus.drain()

        book = tidal.books[("VENUE_A", "BTC-USD")]
        assert book.synced
        assert not book.needs_resync
        assert len(book.bids) == 2
        state = tidal.venue_state("VENUE_A", "BTC-USD")
        assert state.quality is DataQuality.FRESH
        assert state.quality.is_usable
