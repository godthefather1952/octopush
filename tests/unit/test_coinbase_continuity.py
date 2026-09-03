"""Coinbase L2 book integrity: TIDAL-H2.

The audit's finding was that Coinbase's ``level2_batch`` handling had *no*
effective gap detection, and that the parser's own docstring claiming a
"timestamp monotonicity fallback" was false — no such check existed.

Before writing a fix, the actual public protocol was checked against current
documentation rather than against the stale comment:

* ``snapshot`` (on ``level2_batch``) carries neither a sequence number nor a
  timestamp. It is a full replace, and it is the only way this channel
  re-establishes ground truth.
* ``l2update`` carries a ``time`` (ISO-8601) but **no sequence number**. There
  is no counter to compare, so there is nothing to detect a gap *with*.
* ``heartbeat`` does carry a ``sequence``, but it is the matching engine's
  per-product counter — the same space ``full``/``matches`` events draw from.
  ``level2_batch`` updates never touch that counter, so heartbeat sequence
  numbers say nothing about whether an ``l2update`` was missed. They could, in
  principle, detect a missed ``match`` (trade), but that is a different
  finding (trade-print continuity) and out of scope here; implementing it
  would also risk implying, by proximity, that the same signal says something
  about the book. It does not, so this batch does not touch it.
* A reconnect is the one thing this channel *does* prove: subscribing on a
  fresh connection always yields a new full snapshot. There is no way to ask
  for one over an existing connection.

Given that, what a client can actually prove about one ``l2update`` is
narrow: whether its ``time`` is older than the newest one already applied.
That proves the message is not new information — sufficient to justify
dropping it — and nothing more. It does **not** prove completeness: a message
could vanish between two we did receive, in either direction of time, with
no trace at all. That residual gap is real and is documented, not papered
over with a heavier-sounding check.
"""

from __future__ import annotations

import json

import pytest

from agents.tidal import Tidal
from agents.tidal.book import BookDesyncError, LocalOrderBook
from core.clock import ManualClock
from core.health import HealthRegistry
from core.models.common import DataQuality
from core.models.market import OrderBookSnapshot, PriceLevel
from tests.conftest import START_MS
from venues.base.messages import BookDelta, VenueStatus, VenueStatusKind
from venues.venue_b import parser as parser_b
from venues.venue_b.adapter import VenueBAdapter

VENUE = "VENUE_B"


def cb_snapshot(
    symbol: str = "BTC-USD",
    exchange_ts: int = START_MS,
    *,
    bid=100.0,
    ask=101.0,
    ts: int = START_MS,
) -> OrderBookSnapshot:
    """A Coinbase-shaped snapshot: no sequence number, ordinary exchange_ts."""
    return OrderBookSnapshot(
        venue=VENUE,
        symbol=symbol,
        exchange_ts=exchange_ts,
        received_ts=ts,
        sequence=None,
        bids=[PriceLevel(price=bid, size=1.0)],
        asks=[PriceLevel(price=ask, size=1.0)],
        is_checkpoint=True,
    )


def cb_delta(
    symbol: str = "BTC-USD",
    *,
    exchange_ts: int,
    bids=(),
    asks=(),
    ts: int = START_MS,
) -> BookDelta:
    """A Coinbase-shaped l2update delta: no sequence number of any kind."""
    return BookDelta(
        venue=VENUE,
        symbol=symbol,
        exchange_ts=exchange_ts,
        received_ts=ts,
        sequence=None,
        first_sequence=None,
        bids=[PriceLevel(price=p, size=s) for p, s in bids],
        asks=[PriceLevel(price=p, size=s) for p, s in asks],
    )


# ======================================================================
# 2. What the protocol actually exposes — pinned as executable fact
# ======================================================================


class TestProtocolCapabilities:
    def test_real_snapshot_payloads_carry_no_sequence(self):
        """The real wire shape: no "sequence" key on snapshot at all."""
        snapshot = parser_b.parse_snapshot(
            {"type": "snapshot", "product_id": "BTC-USD", "bids": [], "asks": []},
            START_MS,
        )
        assert snapshot.sequence is None

    def test_real_l2update_payloads_carry_no_sequence(self):
        delta = parser_b.parse_l2update(
            {
                "type": "l2update",
                "product_id": "BTC-USD",
                "time": "2024-01-01T00:00:00.000Z",
                "changes": [["buy", "100.0", "1.0"]],
            },
            START_MS,
        )
        assert delta.sequence is None
        assert delta.first_sequence is None
        assert not delta.covers_range

    def test_heartbeat_carries_a_sequence_the_book_never_sees(self):
        """heartbeat.sequence exists but is not in level2_batch's data at all.

        It is the matching-engine's per-product counter, shared with
        ``full``/``matches`` events this client does not subscribe to for the
        book. There is no field on ``l2update`` for it to correspond to, so
        there is nothing to compare it against for L2 continuity.
        """
        payload = {
            "type": "heartbeat",
            "sequence": 4321,
            "last_trade_id": 77,
            "product_id": "BTC-USD",
            "time": "2024-01-01T00:00:00.000Z",
        }
        # The parser has nowhere to route this for book purposes: it carries
        # no bids/asks/changes, so it correctly produces nothing.
        assert parser_b.parse_message(payload, START_MS) is None

    def test_heartbeat_sequencing_is_not_wired_into_l2_continuity(self):
        """Pinned by name so this cannot be quietly "improved" into a false
        claim later: :class:`LocalOrderBook` has no notion of heartbeat at
        all, and that is deliberate, not an oversight.
        """
        import inspect

        source = inspect.getsource(LocalOrderBook)
        assert "heartbeat" not in source.lower()


# ======================================================================
# A / B / C. Snapshot, ordering, duplicates — at the book level
# ======================================================================


class TestBookLevelOrdering:
    def test_snapshot_then_valid_updates_apply_in_order(self):
        book = LocalOrderBook(venue=VENUE, symbol="BTC-USD", max_depth=10)
        book.apply_snapshot(cb_snapshot(exchange_ts=START_MS))
        book.apply_delta(cb_delta(exchange_ts=START_MS + 10, bids=[(100.0, 5.0)]))
        book.apply_delta(cb_delta(exchange_ts=START_MS + 20, asks=[(101.0, 3.0)]))
        assert book.synced and book.usable
        assert book.bids[100.0] == 5.0
        assert book.asks[101.0] == 3.0
        assert book.exchange_ts == START_MS + 20
        assert book.out_of_order_dropped == 0

    def test_an_out_of_order_update_is_dropped_not_applied(self):
        book = LocalOrderBook(venue=VENUE, symbol="BTC-USD", max_depth=10)
        book.apply_snapshot(cb_snapshot(exchange_ts=START_MS, bid=100.0))
        book.apply_delta(cb_delta(exchange_ts=START_MS + 20, bids=[(100.0, 9.0)]))
        # Arrives late: its exchange_ts is behind what we already accepted.
        book.apply_delta(cb_delta(exchange_ts=START_MS + 10, bids=[(100.0, 1.0)]))
        assert book.bids[100.0] == 9.0, "the stale update must not overwrite newer data"
        assert book.exchange_ts == START_MS + 20, "the book's clock must not move backwards"
        assert book.out_of_order_dropped == 1
        assert book.synced, "dropping is not the same as invalidating"

    def test_a_duplicate_old_message_does_not_corrupt_the_book(self):
        book = LocalOrderBook(venue=VENUE, symbol="BTC-USD", max_depth=10)
        book.apply_snapshot(cb_snapshot(exchange_ts=START_MS, bid=100.0))
        first = cb_delta(exchange_ts=START_MS + 10, bids=[(100.0, 5.0)])
        book.apply_delta(first)
        book.apply_delta(cb_delta(exchange_ts=START_MS + 20, bids=[(100.0, 8.0)]))
        # The first message, replayed — a real-world duplicate delivery.
        book.apply_delta(first)
        assert book.bids[100.0] == 8.0
        assert book.out_of_order_dropped == 1

    def test_equal_timestamps_are_applied_not_dropped(self):
        """level2_batch batches several changes under one shared ``time``.

        Two messages sharing a timestamp is the *normal* shape of that
        batching, not evidence of anything wrong — so equality must not be
        treated as "older."
        """
        book = LocalOrderBook(venue=VENUE, symbol="BTC-USD", max_depth=10)
        book.apply_snapshot(cb_snapshot(exchange_ts=START_MS))
        book.apply_delta(cb_delta(exchange_ts=START_MS + 10, bids=[(100.0, 1.0)]))
        book.apply_delta(cb_delta(exchange_ts=START_MS + 10, bids=[(100.0, 2.0)]))
        assert book.bids[100.0] == 2.0
        assert book.out_of_order_dropped == 0

    def test_a_large_backward_jump_is_dropped_not_escalated(self):
        """H. A "suspicious" ordering condition still just drops the message.

        Deliberately proving the negative here: there is no threshold in this
        code that turns a big out-of-order gap into book invalidation. Doing
        that would manufacture confidence the timestamp signal does not
        support — we cannot tell a big reorder from a big, harmless replay of
        very old data, and guessing wrong in the direction of "invalidate"
        would make the book flap on ordinary batching jitter. Dropping is
        safe regardless of which one it is, so dropping is all that happens.
        """
        book = LocalOrderBook(venue=VENUE, symbol="BTC-USD", max_depth=10)
        book.apply_snapshot(cb_snapshot(exchange_ts=START_MS, bid=100.0))
        book.apply_delta(cb_delta(exchange_ts=START_MS + 10, bids=[(100.0, 9.0)]))
        book.apply_delta(cb_delta(exchange_ts=START_MS - 60_000, bids=[(100.0, 1.0)]))  # a minute "old"
        assert book.synced, "a suspicious-but-unprovable signal must not tear down the book"
        assert book.usable
        assert book.bids[100.0] == 9.0
        assert book.out_of_order_dropped == 1

    def test_the_snapshot_itself_is_the_initial_reference(self):
        """Unlike Binance's range check, there is no first-delta grace window.

        Binance needs one because its sequence space is separate from the
        snapshot (``lastUpdateId`` vs. the stream's ``U``/``u``), so the first
        delta has to be allowed to straddle. Coinbase has no separate
        sequencing at all — the snapshot's own ``exchange_ts`` *is* the
        reference the very next message is compared against, immediately.
        """
        book = LocalOrderBook(venue=VENUE, symbol="BTC-USD", max_depth=10)
        book.apply_snapshot(cb_snapshot(exchange_ts=START_MS, bid=100.0))
        # Strictly before the snapshot's own time: dropped.
        book.apply_delta(cb_delta(exchange_ts=START_MS - 500, bids=[(100.0, 4.0)]))
        assert book.bids[100.0] == 1.0, "the snapshot's own value must survive"
        assert book.out_of_order_dropped == 1
        # At or after it: applied normally.
        book.apply_delta(cb_delta(exchange_ts=START_MS, bids=[(100.0, 7.0)]))
        assert book.bids[100.0] == 7.0
        assert book.out_of_order_dropped == 1

    def test_a_delta_before_any_snapshot_is_still_rejected(self):
        """Unchanged from Batch 1: no channel skips needing a base state."""
        book = LocalOrderBook(venue=VENUE, symbol="BTC-USD", max_depth=10)
        with pytest.raises(BookDesyncError, match="no snapshot yet"):
            book.apply_delta(cb_delta(exchange_ts=START_MS))


# ======================================================================
# D / E / F. Disconnect / reconnect, through TIDAL
# ======================================================================


class TestReconnectFlow:
    """healthy -> disconnect -> UNAVAILABLE -> reconnect -> new snapshot -> FRESH.

    CONNECTED != SYNCHRONIZED: the middle of this flow must not let the old
    book become usable merely because a socket says it reconnected.
    """

    @pytest.fixture
    def tidal(self, bus, clock, settings):
        agent = Tidal(bus, clock, settings, HealthRegistry(clock=clock))
        agent.subscribe()
        return agent

    async def test_the_full_disconnect_reconnect_cycle(self, tidal, clock):
        # Healthy snapshot + valid updates.
        await tidal.on_snapshot(cb_snapshot(exchange_ts=clock.now_ms()))
        await tidal.on_delta(cb_delta(exchange_ts=clock.now_ms() + 10, bids=[(100.0, 3.0)]))
        assert tidal.venue_state(VENUE, "BTC-USD").quality is DataQuality.FRESH

        # Disconnect: immediately unusable, not merely "will expire eventually."
        await tidal.on_event(
            _event(VenueStatusKind.DISCONNECTED, clock.now_ms())
        )
        assert tidal.venue_state(VENUE, "BTC-USD").quality is DataQuality.UNAVAILABLE
        assert not tidal.books[(VENUE, "BTC-USD")].synced

        # Socket reports CONNECTED again — but that is not the same as
        # SYNCHRONIZED. No snapshot has arrived yet.
        await tidal.on_event(_event(VenueStatusKind.CONNECTED, clock.now_ms()))
        assert tidal.venue_state(VENUE, "BTC-USD").quality is DataQuality.UNAVAILABLE
        assert not tidal.books[(VENUE, "BTC-USD")].synced

        # A stray update from before the fresh snapshot must still be refused.
        with pytest.raises(BookDesyncError):
            await _apply_and_raise(tidal, cb_delta(exchange_ts=clock.now_ms()))

        # The fresh snapshot Coinbase always sends on a new subscription.
        await tidal.on_snapshot(cb_snapshot(exchange_ts=clock.now_ms(), bid=105.0, ask=106.0))
        assert tidal.books[(VENUE, "BTC-USD")].synced
        await tidal.on_delta(cb_delta(exchange_ts=clock.now_ms() + 5, bids=[(105.0, 2.0)]))
        assert tidal.venue_state(VENUE, "BTC-USD").quality is DataQuality.FRESH

    async def test_reconnect_alone_never_restores_usability(self, tidal, clock):
        """The CONNECTED event by itself must do nothing to book state."""
        await tidal.on_snapshot(cb_snapshot(exchange_ts=clock.now_ms()))
        await tidal.on_event(_event(VenueStatusKind.DISCONNECTED, clock.now_ms()))
        before = tidal.venue_state(VENUE, "BTC-USD").quality
        await tidal.on_event(_event(VenueStatusKind.CONNECTED, clock.now_ms()))
        after = tidal.venue_state(VENUE, "BTC-USD").quality
        assert before is after is DataQuality.UNAVAILABLE


async def _apply_and_raise(tidal: Tidal, delta: BookDelta) -> None:
    """Apply a delta through the book directly, surfacing BookDesyncError.

    ``Tidal.on_delta`` catches the error to drive resync bookkeeping; this
    bypasses that so the test can assert the error itself was raised.
    """
    tidal.books[(delta.venue, delta.symbol)].apply_delta(delta)


def _event(kind: VenueStatusKind, ts: int):
    from core.events import Event, EventType

    topic = {
        VenueStatusKind.CONNECTED: EventType.VENUE_CONNECTED,
        VenueStatusKind.DISCONNECTED: EventType.VENUE_DISCONNECTED,
    }[kind]
    status = VenueStatus(venue=VENUE, kind=kind, received_ts=ts)
    return Event(type=topic, ts_ms=ts, source=VENUE, schema_name="VenueStatus",
                 payload=status.model_dump(mode="json"))


# ======================================================================
# G. Multi-symbol isolation
# ======================================================================


class TestMultiSymbolIsolation:
    async def test_a_btc_ordering_issue_leaves_eth_untouched(self, bus, clock, settings):
        tidal = Tidal(bus, clock, settings, HealthRegistry(clock=clock))
        await tidal.on_snapshot(cb_snapshot("BTC-USD", exchange_ts=clock.now_ms(), bid=100.0))
        await tidal.on_snapshot(cb_snapshot("ETH-USD", exchange_ts=clock.now_ms(), bid=50.0))
        await tidal.on_delta(cb_delta("BTC-USD", exchange_ts=clock.now_ms() + 20, bids=[(100.0, 9.0)]))
        await tidal.on_delta(cb_delta("ETH-USD", exchange_ts=clock.now_ms() + 20, bids=[(50.0, 9.0)]))

        # Feed BTC a stream of out-of-order garbage.
        for i in range(20):
            await tidal.on_delta(
                cb_delta("BTC-USD", exchange_ts=clock.now_ms() - 60_000, bids=[(100.0, float(i))])
            )

        btc = tidal.books[(VENUE, "BTC-USD")]
        eth = tidal.books[(VENUE, "ETH-USD")]
        assert btc.out_of_order_dropped == 20
        assert eth.out_of_order_dropped == 0
        assert eth.bids[50.0] == 9.0, "ETH's book must be unaffected by BTC's garbage"
        assert tidal.venue_state(VENUE, "ETH-USD").quality is DataQuality.FRESH

    async def test_disconnect_invalidates_only_that_venues_books(
        self, bus, clock, settings
    ):
        tidal = Tidal(bus, clock, settings, HealthRegistry(clock=clock))
        tidal.subscribe()
        await tidal.on_snapshot(cb_snapshot("BTC-USD", exchange_ts=clock.now_ms()))
        await tidal.on_snapshot(
            OrderBookSnapshot(
                venue="VENUE_A", symbol="BTC-USDT", exchange_ts=clock.now_ms(),
                received_ts=clock.now_ms(), sequence=1000,
                bids=[PriceLevel(price=100.0, size=1.0)],
                asks=[PriceLevel(price=101.0, size=1.0)], is_checkpoint=True,
            )
        )
        await tidal.on_event(_event(VenueStatusKind.DISCONNECTED, clock.now_ms()))
        assert not tidal.books[("VENUE_B", "BTC-USD")].synced
        assert tidal.books[("VENUE_A", "BTC-USDT")].synced, (
            "a Coinbase disconnect must not touch Binance's book"
        )


# ======================================================================
# J. No Binance regression from this batch's dispatch change
# ======================================================================


class TestNoBinanceRegression:
    def test_binance_range_deltas_are_unaffected_by_the_new_branch(self):
        book = LocalOrderBook(venue="VENUE_A", symbol="BTC-USDT", max_depth=10)
        book.apply_snapshot(
            OrderBookSnapshot(
                venue="VENUE_A", symbol="BTC-USDT", exchange_ts=START_MS,
                received_ts=START_MS, sequence=1000,
                bids=[PriceLevel(price=100.0, size=1.0)],
                asks=[PriceLevel(price=101.0, size=1.0)], is_checkpoint=True,
            )
        )
        book.apply_delta(
            BookDelta(
                venue="VENUE_A", symbol="BTC-USDT", exchange_ts=START_MS,
                received_ts=START_MS, first_sequence=1001, sequence=1002,
                bids=[PriceLevel(price=100.0, size=5.0)],
            )
        )
        assert book.synced and book.sequence == 1002 and book.bids[100.0] == 5.0
        assert book.out_of_order_dropped == 0


# ======================================================================
# Adapter-level: real wire payloads, end to end
# ======================================================================


class TestAdapterEndToEnd:
    @pytest.fixture
    def adapter(self):
        from core.config import VenueConfig

        config = VenueConfig(name=VENUE, display_name="B", adapter="coinbase_public")
        return VenueBAdapter(config, ManualClock(START_MS), ["BTC-USD"])

    async def test_a_real_snapshot_then_l2update_payload_round_trips(self, adapter):
        emitted = []

        async def emit(msg):
            emitted.append(msg)

        adapter.bind(emit)
        await adapter.handle_payload(
            json.dumps(
                {
                    "type": "snapshot",
                    "product_id": "BTC-USD",
                    "bids": [["100.00", "1.0"]],
                    "asks": [["101.00", "1.0"]],
                }
            )
        )
        await adapter.handle_payload(
            json.dumps(
                {
                    "type": "l2update",
                    "product_id": "BTC-USD",
                    "time": "2024-01-01T00:00:00.100Z",
                    "changes": [["buy", "100.00", "2.5"]],
                }
            )
        )
        assert len(emitted) == 2
        assert isinstance(emitted[0], OrderBookSnapshot) and emitted[0].sequence is None
        assert isinstance(emitted[1], BookDelta) and emitted[1].sequence is None

    async def test_heartbeat_payload_produces_no_book_message(self, adapter):
        emitted = []
        adapter.bind(lambda m: emitted.append(m) or _noop())
        await adapter.handle_payload(
            json.dumps(
                {
                    "type": "heartbeat",
                    "sequence": 99,
                    "last_trade_id": 5,
                    "product_id": "BTC-USD",
                    "time": "2024-01-01T00:00:00Z",
                }
            )
        )
        assert emitted == []


async def _noop():
    return None
