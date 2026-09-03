"""Binance depth synchronisation: the snapshot handshake and its recovery.

Covers the three findings remediated in Phase 1 batch 1:

* TIDAL-C1 — a book is built from a REST checkpoint joined onto buffered
  stream updates, instead of never being built at all.
* TIDAL-H1 — ``U``/``u`` are treated as the span they are, so the documented
  post-snapshot rule ``U <= lastUpdateId + 1 <= u`` is satisfiable.
* TIDAL-C2 — a gap produces a resync request that actually reaches the feed
  and actually restores the book.

Nothing here touches the network: the checkpoint fetch is injected.
"""

from __future__ import annotations

import asyncio

import pytest

from agents.tidal.book import BookDesyncError, LocalOrderBook
from core.clock import ManualClock
from core.models.market import OrderBookSnapshot, PriceLevel
from venues.base.adapter import ReconnectPolicy
from venues.base.messages import BookDelta
from venues.venue_a.sync import DepthSynchronizer, SyncState

START_MS = 1_788_000_000_000
VENUE = "VENUE_A"


def depth_delta(
    *, first: int, final: int, bids=(), asks=(), symbol="BTC-USD", ts=START_MS
) -> BookDelta:
    """A Binance-shaped delta covering update ids ``first..final``."""
    return BookDelta(
        venue=VENUE,
        symbol=symbol,
        exchange_ts=ts,
        received_ts=ts,
        first_sequence=first,
        sequence=final,
        bids=[PriceLevel(price=p, size=s) for p, s in bids],
        asks=[PriceLevel(price=p, size=s) for p, s in asks],
    )


def checkpoint(
    last_update_id: int, *, bids=((100.0, 1.0),), asks=((101.0, 1.0),), symbol="BTC-USD"
) -> OrderBookSnapshot:
    return OrderBookSnapshot(
        venue=VENUE,
        symbol=symbol,
        exchange_ts=START_MS,
        received_ts=START_MS,
        sequence=last_update_id,
        bids=[PriceLevel(price=p, size=s) for p, s in bids],
        asks=[PriceLevel(price=p, size=s) for p, s in asks],
        is_checkpoint=True,
    )


def synced_book(last_update_id: int = 1000) -> LocalOrderBook:
    book = LocalOrderBook(venue=VENUE, symbol="BTC-USD", max_depth=10)
    book.apply_snapshot(checkpoint(last_update_id))
    return book


# ======================================================================
# B. First-event overlap — the documented handshake rule
# ======================================================================


class TestFirstEventOverlap:
    """``U <= lastUpdateId + 1 <= u`` for the first update after a snapshot.

    Every case here failed before the fix: the parser had collapsed the span
    to ``prev_sequence = U - 1`` and the book demanded exact equality, so only
    ``U == L + 1`` survived.
    """

    @pytest.mark.parametrize(
        ("first", "final", "why"),
        [
            (998, 1005, "span starts before L and ends after it"),
            (1000, 1003, "span starts exactly at L"),
            (1001, 1004, "span starts exactly at L + 1"),
            (1001, 1001, "span is a single id, exactly L + 1"),
        ],
    )
    def test_a_straddling_first_event_is_accepted(self, first, final, why):
        book = synced_book(1000)
        book.apply_delta(depth_delta(first=first, final=final, bids=[(100.0, 7.0)]))
        assert book.synced, why
        assert book.sequence == final
        assert book.bids[100.0] == 7.0

    def test_a_true_gap_desyncs(self):
        """1002 after 1000 means 1001 was never delivered."""
        book = synced_book(1000)
        with pytest.raises(BookDesyncError, match=r"covers 1002\.\.1004"):
            book.apply_delta(depth_delta(first=1002, final=1004))
        assert not book.synced
        assert not book.usable
        assert book.needs_resync
        assert book.sequence_gaps == 1

    def test_a_gap_of_exactly_one_id_still_desyncs(self):
        """The off-by-one case is the one a loose rule would let through."""
        book = synced_book(1000)
        with pytest.raises(BookDesyncError):
            book.apply_delta(depth_delta(first=1002, final=1002))
        assert not book.usable

    def test_continuity_still_required_after_the_first_event(self):
        book = synced_book(1000)
        book.apply_delta(depth_delta(first=998, final=1005))
        assert book.awaiting_first_delta is False
        # 1007 leaves 1006 undelivered.
        with pytest.raises(BookDesyncError):
            book.apply_delta(depth_delta(first=1007, final=1009))

    def test_contiguous_stream_applies_indefinitely(self):
        book = synced_book(1000)
        seq = 1000
        for i in range(50):
            book.apply_delta(
                depth_delta(first=seq + 1, final=seq + 2, bids=[(100.0, float(i))])
            )
            seq += 2
        assert book.synced
        assert book.sequence == seq
        assert book.bids[100.0] == 49.0
        assert book.sequence_gaps == 0


# ======================================================================
# C. Duplicate and superseded events
# ======================================================================


class TestDuplicateAndOldEvents:
    def test_a_fully_covered_event_is_ignored(self):
        book = synced_book(1000)
        book.apply_delta(depth_delta(first=1001, final=1010, bids=[(100.0, 5.0)]))
        # Re-delivery of a span entirely at or below what we hold.
        book.apply_delta(depth_delta(first=995, final=1000, bids=[(100.0, 99.0)]))
        assert book.bids[100.0] == 5.0, "a superseded event must not rewrite a level"
        assert book.sequence == 1010
        assert book.synced

    def test_an_exactly_repeated_event_is_ignored(self):
        book = synced_book(1000)
        event = depth_delta(first=1001, final=1005, bids=[(100.0, 5.0)])
        book.apply_delta(event)
        book.apply_delta(event)
        assert book.sequence == 1005
        assert book.updates_applied == 2  # snapshot + one delta
        assert book.synced

    def test_an_overlapping_event_is_applied_and_counted(self):
        """Overlap outside the first position is lossless but not normal.

        These deltas carry absolute quantities, so re-covering applied ids
        rewrites levels to the same values. It is tolerated rather than
        treated as a gap — but counted, so it cannot hide.
        """
        book = synced_book(1000)
        book.apply_delta(depth_delta(first=1001, final=1005))
        book.apply_delta(depth_delta(first=1003, final=1008, bids=[(100.0, 3.0)]))
        assert book.sequence == 1008
        assert book.bids[100.0] == 3.0
        assert book.overlapping_updates == 1
        assert book.sequence_gaps == 0

    def test_the_point_convention_still_works(self):
        """Feeds that number updates one at a time are unaffected."""
        book = synced_book(10)
        book.apply_delta(
            BookDelta(
                venue=VENUE,
                symbol="BTC-USD",
                exchange_ts=START_MS,
                received_ts=START_MS,
                sequence=11,
                prev_sequence=10,
                bids=[PriceLevel(price=100.0, size=4.0)],
            )
        )
        assert book.sequence == 11 and book.bids[100.0] == 4.0
        with pytest.raises(BookDesyncError):
            book.apply_delta(
                BookDelta(
                    venue=VENUE,
                    symbol="BTC-USD",
                    exchange_ts=START_MS,
                    received_ts=START_MS,
                    sequence=20,
                    prev_sequence=19,
                )
            )


# ======================================================================
# A. Initial synchronisation
# ======================================================================


class Recorder:
    """Collects what the synchronizer emits, in order."""

    def __init__(self) -> None:
        self.messages: list[OrderBookSnapshot | BookDelta] = []

    async def __call__(self, message) -> None:
        self.messages.append(message)

    @property
    def snapshots(self):
        return [m for m in self.messages if isinstance(m, OrderBookSnapshot)]

    @property
    def deltas(self):
        return [m for m in self.messages if isinstance(m, BookDelta)]


class FakeFeed:
    """A scripted checkpoint endpoint. Counts calls; never touches a socket."""

    def __init__(self, *results) -> None:
        #: Each entry is either a snapshot to return or an exception to raise.
        self.results = list(results)
        self.calls = 0
        self.gate: asyncio.Event | None = None

    async def __call__(self, symbol: str):
        self.calls += 1
        if self.gate is not None:
            await self.gate.wait()
        result = self.results.pop(0) if len(self.results) > 1 else self.results[0]
        if isinstance(result, Exception):
            raise result
        return result


def build_sync(feed, recorder, clock=None, **kwargs) -> DepthSynchronizer:
    return DepthSynchronizer(
        "BTC-USD",
        feed,
        recorder,
        clock or ManualClock(START_MS),
        min_interval_s=kwargs.pop("min_interval_s", 0.0),
        **kwargs,
    )


async def settle() -> None:
    """Let the synchronizer's background task run to completion."""
    for _ in range(20):
        await asyncio.sleep(0)


class TestInitialSynchronisation:
    async def test_deltas_before_the_snapshot_are_buffered_then_replayed(self):
        feed = FakeFeed(checkpoint(1000))
        feed.gate = asyncio.Event()
        out = Recorder()
        sync = build_sync(feed, out)

        # Stream updates arrive first, as they always do in reality.
        await sync.on_delta(depth_delta(first=990, final=995, bids=[(100.0, 1.0)]))
        await sync.on_delta(depth_delta(first=996, final=1002, bids=[(100.0, 2.0)]))
        await sync.on_delta(depth_delta(first=1003, final=1004, bids=[(100.0, 3.0)]))
        await settle()

        assert out.messages == [], "nothing may be emitted before the checkpoint lands"
        assert sync.state is SyncState.SYNCING

        feed.gate.set()
        await settle()

        # The 990..995 event is entirely below lastUpdateId and is discarded;
        # 996..1002 straddles 1001 and is the join; 1003..1004 follows it.
        assert isinstance(out.messages[0], OrderBookSnapshot)
        assert out.messages[0].sequence == 1000
        assert [(d.first_sequence, d.sequence) for d in out.deltas] == [
            (996, 1002),
            (1003, 1004),
        ]
        assert sync.buffered_discarded == 1
        assert sync.buffered_replayed == 2
        assert sync.state is SyncState.LIVE

    async def test_the_replayed_stream_produces_a_correct_synced_book(self):
        """End to end: what the synchronizer emits must build a valid book."""
        feed = FakeFeed(checkpoint(1000, bids=[(100.0, 1.0)], asks=[(101.0, 1.0)]))
        feed.gate = asyncio.Event()
        out = Recorder()
        sync = build_sync(feed, out)

        await sync.on_delta(depth_delta(first=980, final=999, bids=[(100.0, 42.0)]))
        await sync.on_delta(depth_delta(first=1000, final=1003, bids=[(99.0, 5.0)]))
        await sync.on_delta(depth_delta(first=1004, final=1006, asks=[(101.0, 0.0)]))
        feed.gate.set()
        await settle()

        book = LocalOrderBook(venue=VENUE, symbol="BTC-USD", max_depth=10)
        for message in out.messages:
            if isinstance(message, OrderBookSnapshot):
                book.apply_snapshot(message)
            else:
                book.apply_delta(message)

        assert book.synced
        assert book.sequence == 1006
        assert book.bids == {100.0: 1.0, 99.0: 5.0}, "the pre-snapshot event is not applied"
        assert book.asks == {}, "the deletion at 1004..1006 was replayed"
        assert book.sequence_gaps == 0

    async def test_a_checkpoint_older_than_the_buffer_is_refetched(self):
        """A stale snapshot leaves a hole, so it is retried, not accepted."""
        feed = FakeFeed(checkpoint(500), checkpoint(1000))
        out = Recorder()
        sync = build_sync(feed, out, backoff=ReconnectPolicy(initial_s=0.0, max_s=0.0))

        # Buffered updates start at 1001; a checkpoint at 500 cannot join them.
        await sync.on_delta(depth_delta(first=1001, final=1002, bids=[(100.0, 1.0)]))
        await settle()

        assert feed.calls == 2, "the stale checkpoint must be replaced, not used"
        assert sync.stale_snapshots == 1
        assert out.snapshots[0].sequence == 1000
        assert sync.state is SyncState.LIVE

    async def test_no_checkpoint_is_fetched_per_update_once_live(self):
        feed = FakeFeed(checkpoint(1000))
        out = Recorder()
        sync = build_sync(feed, out)
        await sync.on_delta(depth_delta(first=1001, final=1002))
        await settle()
        for i in range(200):
            await sync.on_delta(depth_delta(first=1003 + i, final=1003 + i))
        await settle()
        assert feed.calls == 1
        assert len(out.deltas) == 201

    async def test_the_buffer_is_bounded_and_drops_whole_not_middle(self):
        """Overflow must not leave a buffer with a hole in it.

        Dropping the oldest entry would keep the buffer the right size and
        make it unreplayable: the snapshot joins onto the *front*, so losing
        the front loses the proof of contiguity. The whole attempt is thrown
        away instead, and what is finally emitted is contiguous.
        """
        feed = FakeFeed(checkpoint(1015))
        feed.gate = asyncio.Event()
        out = Recorder()
        sync = build_sync(feed, out, max_buffer=16)

        for i in range(20):  # ids 1000..1019, one per message
            await sync.on_delta(
                depth_delta(first=1000 + i, final=1000 + i, bids=[(100.0, float(i))])
            )
        assert sync.buffer_overflows == 1
        assert len(sync._buffer) == 4, "cleared at the 17th, then 1016..1019"

        feed.gate.set()
        await settle()

        assert [d.sequence for d in out.deltas] == [1016, 1017, 1018, 1019]
        book = LocalOrderBook(venue=VENUE, symbol="BTC-USD", max_depth=10)
        for message in out.messages:
            if isinstance(message, OrderBookSnapshot):
                book.apply_snapshot(message)
            else:
                book.apply_delta(message)
        assert book.synced and book.sequence_gaps == 0
        assert book.bids[100.0] == 19.0


# ======================================================================
# F. Failed resynchronisation
# ======================================================================


class TestFailedSynchronisation:
    async def test_fetch_failures_are_retried_with_backoff_then_bounded(self):
        clock = ManualClock(START_MS)
        feed = FakeFeed(RuntimeError("HTTP 429"))
        out = Recorder()
        sync = build_sync(
            feed,
            out,
            clock=clock,
            max_attempts=4,
            backoff=ReconnectPolicy(initial_s=0.0, factor=1.0, max_s=0.0),
        )

        await sync.on_delta(depth_delta(first=1001, final=1002))
        await settle()

        assert feed.calls == 4, "retries are bounded, not endless"
        assert sync.fetch_failures == 4
        assert sync.state is SyncState.FAILED
        assert out.messages == [], "no market data is emitted from a failed handshake"
        assert sync.last_error is not None and "429" in sync.last_error

    async def test_a_failed_book_emits_nothing_rather_than_something_doubtful(self):
        feed = FakeFeed(RuntimeError("timeout"))
        out = Recorder()
        sync = build_sync(
            feed, out, max_attempts=1, backoff=ReconnectPolicy(initial_s=0.0, max_s=0.0)
        )
        await sync.on_delta(depth_delta(first=1001, final=1002))
        await settle()
        # Further updates keep arriving; none of them may leak out.
        for i in range(10):
            await sync.on_delta(depth_delta(first=1010 + i, final=1010 + i))
        await settle()
        assert out.messages == []
        assert sync.state is SyncState.FAILED

    async def test_a_reconnect_clears_a_failed_state_and_starts_again(self):
        feed = FakeFeed(RuntimeError("boom"), checkpoint(2000))
        out = Recorder()
        sync = build_sync(
            feed, out, max_attempts=1, backoff=ReconnectPolicy(initial_s=0.0, max_s=0.0)
        )
        await sync.on_delta(depth_delta(first=1001, final=1002))
        await settle()
        assert sync.state is SyncState.FAILED

        sync.reset()
        assert sync.state is SyncState.IDLE
        await sync.on_delta(depth_delta(first=2001, final=2002, bids=[(100.0, 1.0)]))
        await settle()
        assert sync.state is SyncState.LIVE
        assert out.snapshots[0].sequence == 2000

    async def test_the_checkpoint_rate_is_floored(self):
        """Repeated resyncs cannot become a REST request storm."""
        clock = ManualClock(START_MS)
        feed = FakeFeed(checkpoint(1000))
        out = Recorder()
        sync = build_sync(feed, out, clock=clock, min_interval_s=5.0)

        await sync.on_delta(depth_delta(first=1001, final=1002))
        await settle()
        assert feed.calls == 1

        await sync.request_resync("gap")
        await settle()
        # The clock has not moved, so the second fetch is still waiting out
        # the floor rather than hitting the endpoint.
        assert feed.calls == 1
        clock.advance(5_000)
        await settle()
        assert feed.calls == 2


# ======================================================================
# The adapter: real wire payloads through the whole handshake
# ======================================================================


class OfflineVenueA:
    """VenueAAdapter with the network replaced, and nothing else changed."""

    def __new__(cls, config, clock, symbols, checkpoints):
        from venues.venue_a.adapter import VenueAAdapter

        class _Offline(VenueAAdapter):
            def __init__(self, *args, **kwargs):
                self.fetch_calls: list[str] = []
                self._checkpoints = dict(checkpoints)
                super().__init__(*args, **kwargs)

            async def fetch_checkpoint(self, symbol: str):
                self.fetch_calls.append(symbol)
                return self._checkpoints[symbol]

        return _Offline(config, clock, symbols)


def depth_payload(symbol_venue: str, first: int, final: int, bids=(), asks=()) -> str:
    """A real combined-stream ``depthUpdate`` envelope."""
    import json

    return json.dumps(
        {
            "stream": f"{symbol_venue.lower()}@depth@100ms",
            "data": {
                "e": "depthUpdate",
                "E": START_MS,
                "s": symbol_venue,
                "U": first,
                "u": final,
                "b": [[str(p), str(s)] for p, s in bids],
                "a": [[str(p), str(s)] for p, s in asks],
            },
        }
    )


class TestAdapterIntegration:
    @pytest.fixture
    def config(self):
        from core.config import VenueConfig

        return VenueConfig(
            name=VENUE,
            display_name="A",
            adapter="binance_public",
            depth_sync_min_interval_s=0.0,
        )

    async def test_streamed_payloads_produce_a_synchronised_book(self, config):
        clock = ManualClock(START_MS)
        out = Recorder()
        adapter = OfflineVenueA(
            config,
            clock,
            ["BTC-USD"],
            {"BTC-USD": checkpoint(1000, bids=[(100.0, 1.0)], asks=[(101.0, 1.0)])},
        )
        adapter.bind(out)

        # Wire-format updates, in the order a socket would deliver them.
        await adapter.handle_payload(depth_payload("BTCUSDT", 995, 999, bids=[(100.0, 9.0)]))
        await adapter.handle_payload(depth_payload("BTCUSDT", 1000, 1004, bids=[(99.0, 2.0)]))
        await adapter.handle_payload(
            depth_payload("BTCUSDT", 1005, 1006, asks=[(101.0, 0.0), (102.0, 3.0)])
        )
        await settle()

        book = LocalOrderBook(venue=VENUE, symbol="BTC-USD", max_depth=10)
        for message in out.messages:
            if isinstance(message, OrderBookSnapshot):
                book.apply_snapshot(message)
            else:
                book.apply_delta(message)

        assert adapter.fetch_calls == ["BTC-USD"], "one checkpoint, not one per update"
        assert book.synced and book.usable
        assert book.sequence == 1006
        # 995..999 is entirely below the checkpoint and must not have been
        # applied — if it had, the bid would read 9.0.
        assert book.bids == {100.0: 1.0, 99.0: 2.0}
        assert book.asks == {102.0: 3.0}, "the deletion and the insert both applied"

    async def test_a_resync_request_reaches_the_synchronizer(self, config):
        clock = ManualClock(START_MS)
        out = Recorder()
        adapter = OfflineVenueA(config, clock, ["BTC-USD"], {"BTC-USD": checkpoint(1000)})
        adapter.bind(out)
        await adapter.handle_payload(depth_payload("BTCUSDT", 1001, 1002))
        await settle()
        assert adapter.fetch_calls == ["BTC-USD"]

        await adapter.request_resync("BTC-USD", "sequence gap")
        await settle()
        assert adapter.fetch_calls == ["BTC-USD", "BTC-USD"]
        assert adapter.stats.sequence_gaps == 1

    async def test_each_symbol_synchronises_independently(self, config):
        clock = ManualClock(START_MS)
        out = Recorder()
        adapter = OfflineVenueA(
            config,
            clock,
            ["BTC-USD", "ETH-USD"],
            {"BTC-USD": checkpoint(1000), "ETH-USD": checkpoint(7000, symbol="ETH-USD")},
        )
        adapter.bind(out)

        await adapter.handle_payload(depth_payload("BTCUSDT", 1001, 1002))
        await adapter.handle_payload(depth_payload("ETHUSDT", 7001, 7002))
        await settle()

        states = {s: sync.state for s, sync in adapter.sync_stats.items()}
        assert states == {"BTC-USD": SyncState.LIVE, "ETH-USD": SyncState.LIVE}

        # Resyncing BTC leaves ETH exactly where it was.
        eth_before = adapter.sync_stats["ETH-USD"].snapshots_applied
        await adapter.request_resync("BTC-USD", "gap")
        await settle()
        assert adapter.sync_stats["ETH-USD"].snapshots_applied == eth_before
        assert adapter.sync_stats["ETH-USD"].resyncs == 0
        assert adapter.sync_stats["BTC-USD"].resyncs == 1

    async def test_a_reconnect_restarts_every_book(self, config):
        clock = ManualClock(START_MS)
        out = Recorder()
        adapter = OfflineVenueA(config, clock, ["BTC-USD"], {"BTC-USD": checkpoint(1000)})
        adapter.bind(out)
        await adapter.handle_payload(depth_payload("BTCUSDT", 1001, 1002))
        await settle()
        assert adapter.sync_stats["BTC-USD"].state is SyncState.LIVE

        await adapter.on_connected()
        assert adapter.sync_stats["BTC-USD"].state is SyncState.IDLE, (
            "a new socket is a new stream position; the old book cannot be kept"
        )

    async def test_trades_are_unaffected_by_the_handshake(self, config):
        """Trade prints do not depend on book state and must not be gated."""
        import json

        from core.models.market import TradeEvent

        clock = ManualClock(START_MS)
        out = Recorder()
        adapter = OfflineVenueA(config, clock, ["BTC-USD"], {"BTC-USD": checkpoint(1000)})
        adapter.bind(out)
        await adapter.handle_payload(
            json.dumps(
                {
                    "stream": "btcusdt@trade",
                    "data": {
                        "e": "trade",
                        "T": START_MS,
                        "s": "BTCUSDT",
                        "p": "100.0",
                        "q": "0.5",
                        "m": True,
                        "t": 7,
                    },
                }
            )
        )
        assert len(out.messages) == 1
        assert isinstance(out.messages[0], TradeEvent)
        assert adapter.fetch_calls == [], "a trade must not trigger a checkpoint"

    def test_the_checkpoint_limit_is_one_the_endpoint_accepts(self, config):
        clock = ManualClock(START_MS)
        adapter = OfflineVenueA(config, clock, ["BTC-USD"], {})
        allowed = {5, 10, 20, 50, 100, 500, 1000, 5000}
        for levels in (1, 3, 25, 60, 400, 2500, 5000):
            adapter.config = config.model_copy(update={"book_depth_levels": levels})
            limit = adapter._checkpoint_limit()
            assert limit in allowed
            assert limit >= min(levels * 2, 5000)


# ======================================================================
# D. Runtime resynchronisation
# ======================================================================


class TestRuntimeResync:
    async def test_a_resync_rebuilds_the_book_after_a_gap(self):
        feed = FakeFeed(checkpoint(1000), checkpoint(5000, bids=[(100.0, 8.0)]))
        out = Recorder()
        sync = build_sync(feed, out)

        await sync.on_delta(depth_delta(first=1001, final=1002, bids=[(100.0, 1.0)]))
        await settle()
        assert sync.state is SyncState.LIVE

        await sync.request_resync("sequence gap")
        await sync.on_delta(depth_delta(first=5001, final=5002, bids=[(99.0, 2.0)]))
        await settle()

        assert sync.resyncs == 1
        assert feed.calls == 2
        assert [s.sequence for s in out.snapshots] == [1000, 5000]
        assert out.messages[-1].first_sequence == 5001

        book = LocalOrderBook(venue=VENUE, symbol="BTC-USD", max_depth=10)
        for message in out.messages:
            if isinstance(message, OrderBookSnapshot):
                book.apply_snapshot(message)
            else:
                book.apply_delta(message)
        assert book.synced and book.sequence == 5002
        assert book.bids == {100.0: 8.0, 99.0: 2.0}

    async def test_concurrent_resync_requests_cost_one_fetch(self):
        feed = FakeFeed(checkpoint(1000), checkpoint(5000))
        feed.gate = asyncio.Event()
        out = Recorder()
        sync = build_sync(feed, out)
        await sync.on_delta(depth_delta(first=1001, final=1002))
        feed.gate.set()
        await settle()

        feed.gate = asyncio.Event()
        for _ in range(10):
            await sync.request_resync("gap")
        await settle()
        assert feed.calls == 2, "ten requests, one in-flight fetch"
        feed.gate.set()
        await settle()

    async def test_deltas_during_a_resync_are_buffered_not_emitted(self):
        feed = FakeFeed(checkpoint(1000), checkpoint(5000))
        feed.gate = asyncio.Event()
        out = Recorder()
        sync = build_sync(feed, out)
        await sync.on_delta(depth_delta(first=1001, final=1002))
        feed.gate.set()
        await settle()
        emitted_before = len(out.messages)

        feed.gate = asyncio.Event()
        await sync.request_resync("gap")
        await sync.on_delta(depth_delta(first=5001, final=5002))
        await settle()
        assert len(out.messages) == emitted_before, "buffered while resyncing"

        feed.gate.set()
        await settle()
        assert out.messages[emitted_before].sequence == 5000
        assert out.messages[emitted_before + 1].first_sequence == 5001
