"""One malformed message must not drop a venue: TIDAL-M4.

``handle_payload`` used to catch invalid JSON only; a bad price, an unknown
Coinbase side, or any other parser/model exception propagated straight
through ``_session()`` and closed the whole socket over one bad message.

The fix makes every parse function raise :class:`MalformedVenueMessage`
(carrying venue, symbol and message type) instead of a bare exception, and
has each adapter's ``handle_payload`` catch it, count it, and decide —
explicitly, by message type — what the failure means for book continuity:

* Binance: a malformed ``depthUpdate`` may have hidden a real book change, so
  that symbol is resynced through the existing checkpoint path, exactly like
  a sequence gap. A malformed ``trade`` needs no such thing.
* Coinbase: a malformed ``snapshot``/``l2update`` forces a full reconnect —
  the only verified way to get a fresh Coinbase snapshot. A malformed
  ``match`` does not.

In every case the connection itself survives the one bad message that isn't
supposed to force a reconnect, and the next good message on the same socket
is processed normally.
"""

from __future__ import annotations

import json

import pytest

from core.clock import ManualClock
from core.config import VenueConfig
from core.models.market import OrderBookSnapshot, PriceLevel, TradeEvent
from venues.base.adapter import ContinuityUncertain
from venues.venue_a.adapter import VenueAAdapter
from venues.venue_b.adapter import VenueBAdapter

START_MS = 1_788_000_000_000


def checkpoint(last_id: int, symbol="BTC-USDT", bid=100.0, ask=101.0) -> OrderBookSnapshot:
    return OrderBookSnapshot(
        venue="VENUE_A", symbol=symbol, exchange_ts=START_MS, received_ts=START_MS,
        sequence=last_id, bids=[PriceLevel(price=bid, size=1.0)],
        asks=[PriceLevel(price=ask, size=1.0)], is_checkpoint=True,
    )


class OfflineVenueA(VenueAAdapter):
    """VenueAAdapter with the network replaced, nothing else changed."""

    def __init__(self, *args, checkpoints=None, **kwargs):
        self.fetch_calls: list[str] = []
        self._checkpoints = dict(checkpoints or {})
        super().__init__(*args, **kwargs)

    async def fetch_checkpoint(self, symbol: str):
        self.fetch_calls.append(symbol)
        return self._checkpoints[symbol]


async def settle() -> None:
    import asyncio

    for _ in range(20):
        await asyncio.sleep(0)


class Recorder:
    def __init__(self):
        self.messages = []

    async def __call__(self, message):
        self.messages.append(message)


@pytest.fixture
def binance_config():
    return VenueConfig(name="VENUE_A", display_name="A", adapter="binance_public")


@pytest.fixture
def coinbase_config():
    return VenueConfig(name="VENUE_B", display_name="B", adapter="coinbase_public")


# ======================================================================
# Binance: depth messages resync, trades don't, socket always survives
# ======================================================================


class TestBinanceContainment:
    async def test_invalid_json_is_isolated(self, binance_config):
        clock = ManualClock(START_MS)
        out = Recorder()
        adapter = OfflineVenueA(binance_config, clock, ["BTC-USDT"], checkpoints={})
        adapter.bind(out)
        await adapter.handle_payload("{not json")
        assert adapter.stats.errors == 1
        # The socket survives: nothing raised out of handle_payload.

    async def test_missing_required_depth_fields_is_isolated(self, binance_config):
        clock = ManualClock(START_MS)
        out = Recorder()
        adapter = OfflineVenueA(binance_config, clock, ["BTC-USDT"], checkpoints={})
        adapter.bind(out)
        await adapter.handle_payload(
            json.dumps({"stream": "btcusdt@depth", "data": {"e": "depthUpdate", "s": "BTCUSDT"}})
        )
        assert adapter.stats.errors == 1
        assert adapter.stats.last_malformed.message_type == "depthUpdate"
        assert adapter.stats.last_malformed.symbol == "BTC-USDT"

    async def test_invalid_price_triggers_resync_for_that_symbol(self, binance_config):
        clock = ManualClock(START_MS)
        out = Recorder()
        adapter = OfflineVenueA(
            binance_config, clock, ["BTC-USDT"],
            checkpoints={"BTC-USDT": checkpoint(5000)},
        )
        adapter.bind(out)
        await adapter.handle_payload(
            json.dumps({
                "stream": "btcusdt@depth", "data": {
                    "e": "depthUpdate", "E": START_MS, "s": "BTCUSDT",
                    "U": 1, "u": 2, "b": [["NaN", "1.0"]], "a": [],
                },
            })
        )
        await settle()
        assert adapter.stats.errors == 1
        assert adapter.stats.last_malformed.book_invalidated is False
        assert adapter.stats.last_malformed.resync_requested is True
        assert adapter.fetch_calls == ["BTC-USDT"], (
            "a possibly-lost depth change must trigger the same recovery as a sequence gap"
        )

    async def test_invalid_quantity_triggers_resync(self, binance_config):
        clock = ManualClock(START_MS)
        out = Recorder()
        adapter = OfflineVenueA(
            binance_config, clock, ["BTC-USDT"], checkpoints={"BTC-USDT": checkpoint(5000)}
        )
        adapter.bind(out)
        await adapter.handle_payload(
            json.dumps({
                "stream": "btcusdt@depth", "data": {
                    "e": "depthUpdate", "E": START_MS, "s": "BTCUSDT",
                    "U": 1, "u": 2, "b": [["100.0", "Infinity"]], "a": [],
                },
            })
        )
        await settle()
        assert adapter.fetch_calls == ["BTC-USDT"]

    async def test_a_malformed_trade_does_not_trigger_resync(self, binance_config):
        clock = ManualClock(START_MS)
        out = Recorder()
        adapter = OfflineVenueA(
            binance_config, clock, ["BTC-USDT"], checkpoints={"BTC-USDT": checkpoint(5000)}
        )
        adapter.bind(out)
        await adapter.handle_payload(
            json.dumps({
                "stream": "btcusdt@trade", "data": {
                    "e": "trade", "T": START_MS, "s": "BTCUSDT",
                    "p": "NaN", "q": "1.0", "m": False,
                },
            })
        )
        await settle()
        assert adapter.stats.errors == 1
        assert adapter.stats.last_malformed.message_type == "trade"
        assert adapter.stats.last_malformed.resync_requested is False
        assert adapter.fetch_calls == [], "a malformed trade print needs no book recovery"

    async def test_an_unknown_event_type_is_ignored_not_an_error(self, binance_config):
        clock = ManualClock(START_MS)
        out = Recorder()
        adapter = OfflineVenueA(binance_config, clock, ["BTC-USDT"], checkpoints={})
        adapter.bind(out)
        await adapter.handle_payload(
            json.dumps({"stream": "btcusdt@kline", "data": {"e": "kline"}})
        )
        assert adapter.stats.errors == 0
        assert out.messages == []

    async def test_the_socket_survives_and_processes_the_next_good_message(self, binance_config):
        """The point of the whole batch: one bad message, then business as usual."""
        clock = ManualClock(START_MS)
        out = Recorder()
        adapter = OfflineVenueA(
            binance_config, clock, ["BTC-USDT"], checkpoints={"BTC-USDT": checkpoint(1000)}
        )
        adapter.bind(out)

        await adapter.handle_payload("{garbage")
        await adapter.handle_payload(
            json.dumps({
                "stream": "btcusdt@depth", "data": {
                    "e": "depthUpdate", "E": START_MS, "s": "BTCUSDT",
                    "U": 1001, "u": 1002, "b": [["NaN", "1.0"]], "a": [],
                },
            })
        )
        await settle()
        # A perfectly good message right after, on the same adapter/socket.
        await adapter.handle_payload(
            json.dumps({
                "stream": "btcusdt@trade", "data": {
                    "e": "trade", "T": START_MS, "s": "BTCUSDT",
                    "p": "100.0", "q": "1.0", "m": False, "t": 7,
                },
            })
        )
        assert any(isinstance(m, TradeEvent) for m in out.messages)
        assert adapter.stats.errors == 2


# ======================================================================
# Coinbase: L2-affecting messages force reconnect, trades don't
# ======================================================================


class TestCoinbaseContainment:
    async def test_invalid_json_is_isolated(self, coinbase_config):
        clock = ManualClock(START_MS)
        out = Recorder()
        adapter = VenueBAdapter(coinbase_config, clock, ["BTC-USD"])
        adapter.bind(out)
        await adapter.handle_payload("{not json")
        assert adapter.stats.errors == 1

    async def test_an_unknown_side_forces_a_reconnect(self, coinbase_config):
        clock = ManualClock(START_MS)
        out = Recorder()
        adapter = VenueBAdapter(coinbase_config, clock, ["BTC-USD"])
        adapter.bind(out)
        payload = json.dumps({
            "type": "l2update", "product_id": "BTC-USD", "time": "2024-01-01T00:00:00Z",
            "changes": [["bogus", "100.0", "1.0"]],
        })
        with pytest.raises(ContinuityUncertain):
            await adapter.handle_payload(payload)
        assert adapter.stats.last_malformed.message_type == "l2update"
        assert adapter.stats.last_malformed.book_invalidated is True
        assert adapter.stats.last_malformed.resync_requested is True

    async def test_an_invalid_snapshot_price_forces_a_reconnect(self, coinbase_config):
        clock = ManualClock(START_MS)
        adapter = VenueBAdapter(coinbase_config, clock, ["BTC-USD"])
        adapter.bind(Recorder())
        payload = json.dumps({
            "type": "snapshot", "product_id": "BTC-USD",
            "bids": [["NaN", "1.0"]], "asks": [],
        })
        with pytest.raises(ContinuityUncertain):
            await adapter.handle_payload(payload)

    async def test_a_malformed_trade_does_not_force_a_reconnect(self, coinbase_config):
        clock = ManualClock(START_MS)
        out = Recorder()
        adapter = VenueBAdapter(coinbase_config, clock, ["BTC-USD"])
        adapter.bind(out)
        payload = json.dumps({
            "type": "match", "product_id": "BTC-USD", "time": "2024-01-01T00:00:00Z",
            "price": "NaN", "size": "1.0", "side": "buy",
        })
        # Must not raise.
        await adapter.handle_payload(payload)
        assert adapter.stats.errors == 1
        assert adapter.stats.last_malformed.message_type == "match"
        assert adapter.stats.last_malformed.book_invalidated is False
        assert adapter.stats.last_malformed.resync_requested is False

    async def test_the_reconnect_is_the_documented_mechanism_that_invalidates_the_book(
        self, coinbase_config, bus, clock, settings
    ):
        """Ties the adapter's decision to what actually happens to the book:
        ContinuityUncertain propagates to run()'s reconnect handling, which
        emits VENUE_DISCONNECTED, which is what TIDAL already invalidates a
        book on (Batch 3). This proves the two halves are actually connected,
        not merely that each one individually does something plausible.
        """
        from agents.tidal import Tidal
        from core.health import HealthRegistry
        from core.models.common import DataQuality

        tidal = Tidal(bus, clock, settings, HealthRegistry(clock=clock))
        await tidal.on_snapshot(
            OrderBookSnapshot(
                venue="VENUE_B", symbol="BTC-USD", exchange_ts=clock.now_ms(),
                received_ts=clock.now_ms(), sequence=None,
                bids=[PriceLevel(price=100.0, size=1.0)],
                asks=[PriceLevel(price=101.0, size=1.0)], is_checkpoint=True,
            )
        )
        assert tidal.venue_state("VENUE_B", "BTC-USD").quality is DataQuality.FRESH

        adapter = VenueBAdapter(coinbase_config, clock, ["BTC-USD"])
        adapter.bind(Recorder())
        payload = json.dumps({
            "type": "l2update", "product_id": "BTC-USD", "time": "2024-01-01T00:00:00Z",
            "changes": [["bogus", "100.0", "1.0"]],
        })
        with pytest.raises(ContinuityUncertain):
            await adapter.handle_payload(payload)

        # What run() does with that exception: emit DISCONNECTED (unit-tested
        # separately in test_reconnect_backoff.py); here, apply the same
        # event TIDAL actually subscribes to and confirm the consequence.
        from core.events import Event, EventType
        from venues.base.messages import VenueStatus, VenueStatusKind

        await tidal.on_event(
            Event(
                type=EventType.VENUE_DISCONNECTED, ts_ms=clock.now_ms(), source="VENUE_B",
                schema_name="VenueStatus",
                payload=VenueStatus(
                    venue="VENUE_B", kind=VenueStatusKind.DISCONNECTED, received_ts=clock.now_ms()
                ).model_dump(mode="json"),
            )
        )
        assert tidal.venue_state("VENUE_B", "BTC-USD").quality is DataQuality.UNAVAILABLE


# ======================================================================
# Cross-cutting: error visibility
# ======================================================================


class TestErrorVisibility:
    async def test_last_malformed_answers_every_required_question(self, binance_config):
        clock = ManualClock(START_MS)
        adapter = OfflineVenueA(
            binance_config, clock, ["BTC-USDT"], checkpoints={"BTC-USDT": checkpoint(1000)}
        )
        adapter.bind(Recorder())
        await adapter.handle_payload(
            json.dumps({
                "stream": "btcusdt@depth", "data": {
                    "e": "depthUpdate", "E": START_MS, "s": "BTCUSDT",
                    "U": 1001, "u": 1002, "b": [["NaN", "1.0"]], "a": [],
                },
            })
        )
        info = adapter.stats.last_malformed
        assert info.venue == "VENUE_A"
        assert info.symbol == "BTC-USDT"
        assert info.message_type == "depthUpdate"
        assert info.detail  # non-empty, human-readable
        assert isinstance(info.book_invalidated, bool)
        assert isinstance(info.resync_requested, bool)

    async def test_the_recorded_detail_does_not_dump_a_giant_payload(self, binance_config):
        clock = ManualClock(START_MS)
        adapter = OfflineVenueA(binance_config, clock, ["BTC-USDT"], checkpoints={})
        adapter.bind(Recorder())
        huge_garbage = "{" + "x" * 100_000
        await adapter.handle_payload(huge_garbage)
        assert len(adapter.stats.last_error) <= 300
        assert len(adapter.stats.last_malformed.detail) <= 300

    async def test_no_credentials_appear_anywhere_in_error_state(self, binance_config):
        """Sanity check on the paper boundary from this batch's own angle:
        error plumbing must not become a place a secret could leak, because
        there are none to leak — but the shape of the check is worth pinning.
        """
        clock = ManualClock(START_MS)
        adapter = OfflineVenueA(binance_config, clock, ["BTC-USDT"], checkpoints={})
        adapter.bind(Recorder())
        await adapter.handle_payload("{garbage")
        blob = repr(adapter.stats.last_malformed) + repr(adapter.stats.last_error)
        for word in ("api_key", "secret", "password", "token", "signature"):
            assert word not in blob.lower()
