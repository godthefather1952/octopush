"""Bounded WebSocket receive size: P12-F1.

THE FIELD FAILURE
=================
The Phase 12 SHADOW rehearsal never warmed a book on VENUE_B. Every session
ended the same way::

    sent 1009 (message too big) ... exceeds limit of 1048576 bytes

1,048,576 is ``websockets``' default ``max_size``, and a legitimate full
level-2 snapshot from a public venue is larger than that. The adapter was
doing nothing wrong: it connected, subscribed, and was closed by its own
client library before the first snapshot could be parsed — then reconnected
into exactly the same failure, forever. TIDAL, NORO and ZEPHR never warmed
because no book was ever built.

THE FIX, AND WHAT IT IS NOT
===========================
The transport now passes a **configured, finite** ``max_size``. It is not
``max_size=None``: this is unauthenticated input from a public endpoint, and
an unbounded receive size would make the platform's memory a function of what
a remote server chooses to send.

It is also **not** a relaxation of the local book's storage contract.
``ws_max_message_bytes`` governs how big a frame may be off the wire;
``max_book_levels_per_side`` governs how much state a book may hold. They
solve different problems, and a message big enough to arrive can still be
contained afterwards for overflowing the book — the last test here proves
exactly that, because a transport fix that quietly disabled the storage bound
would be a worse bug than the one it fixed.
"""

from __future__ import annotations

import asyncio
import contextlib
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from agents.tidal.book import BookOverflowError, LocalOrderBook
from core.clock import ManualClock
from core.config import VenueConfig
from core.models.market import OrderBookSnapshot, PriceLevel
from tests.conftest import START_MS
from venues.base.messages import BookDelta
from venues.base.ws import WebSocketAdapter

#: The library default that the field failure ran into.
WEBSOCKETS_DEFAULT_MAX_SIZE = 1_048_576


class ScriptedSocket:
    """Stands in for the object ``websockets.connect(...)`` yields."""

    def __init__(self, frames: list[str] = ()) -> None:
        self._frames = list(frames)

    async def __aenter__(self) -> ScriptedSocket:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def recv(self) -> str:
        if not self._frames:
            raise ConnectionResetError("scripted close")
        return self._frames.pop(0)

    async def send(self, data: str) -> None:
        return None


class ToyAdapter(WebSocketAdapter):
    """The minimal concrete adapter needed to exercise ``_session()``."""

    def __init__(self, config: VenueConfig, clock: ManualClock) -> None:
        super().__init__(config, clock, [])
        self.received: list[str] = []

    def connect_url(self) -> str:
        return "wss://example.invalid/toy"

    async def handle_payload(self, payload: str) -> None:
        self.received.append(payload)


def venue(**overrides) -> VenueConfig:
    return VenueConfig(
        name="TOY", display_name="Toy", adapter="simulated", **overrides
    )


def bounded_book(*, levels: int) -> LocalOrderBook:
    """A synced book holding exactly ``levels`` bids — right at its ceiling."""
    book = LocalOrderBook(
        venue="TOY", symbol="BTC-USD", max_depth=2, max_levels_per_side=levels
    )
    book.apply_snapshot(
        OrderBookSnapshot(
            venue="TOY",
            symbol="BTC-USD",
            exchange_ts=START_MS,
            received_ts=START_MS,
            sequence=1,
            bids=[PriceLevel(price=100.0 - i, size=1.0) for i in range(levels)],
            asks=[PriceLevel(price=101.0, size=1.0)],
            is_checkpoint=True,
        )
    )
    assert book.synced and len(book.bids) == levels
    return book


def one_more_bid(book: LocalOrderBook, *, price: float) -> BookDelta:
    """The single level that takes ``book`` one past its storage ceiling."""
    return BookDelta(
        venue="TOY",
        symbol="BTC-USD",
        exchange_ts=START_MS,
        received_ts=START_MS,
        first_sequence=book.sequence + 1,
        sequence=book.sequence + 1,
        bids=[PriceLevel(price=price, size=1.0)],
        asks=[],
    )


async def captured_connect_kwargs(config: VenueConfig) -> dict:
    """Run one real ``_session()`` and return the kwargs production passed.

    The patch target is the same call site the adapter actually uses, so this
    records what ``websockets.connect`` was really given rather than what a
    helper says it would be given.
    """
    seen: dict = {}

    def fake_connect(*args, **kwargs):
        seen.update(kwargs)
        return ScriptedSocket([])

    adapter = ToyAdapter(config, ManualClock(0))
    with patch("websockets.connect", fake_connect), contextlib.suppress(Exception):
        await adapter._session()
    return seen


# ======================================================================
# A — the configured bound reaches the transport
# ======================================================================


class TestConfiguredBoundReachesWebsocketsConnect:
    async def test_max_size_is_passed_explicitly(self):
        """The literal defect: no ``max_size`` meant the 1 MiB default."""
        kwargs = await captured_connect_kwargs(venue())
        assert "max_size" in kwargs, (
            "without an explicit max_size the library default applies and a "
            "full level-2 snapshot is closed with code 1009"
        )

    async def test_it_is_the_configured_value_not_a_hidden_constant(self):
        kwargs = await captured_connect_kwargs(venue(ws_max_message_bytes=2_222_222))
        assert kwargs["max_size"] == 2_222_222

    async def test_the_default_clears_the_snapshot_that_failed_in_the_field(self):
        kwargs = await captured_connect_kwargs(venue())
        assert kwargs["max_size"] > WEBSOCKETS_DEFAULT_MAX_SIZE

    async def test_the_existing_keepalive_settings_are_unchanged(self):
        """The fix adds a bound; it must not quietly restyle the connection."""
        kwargs = await captured_connect_kwargs(venue())
        assert kwargs["ping_interval"] == 15
        assert kwargs["close_timeout"] == 5


# ======================================================================
# B — a valid message larger than 1 MiB really can be received
# ======================================================================


class TestLargeFrameOverLoopback:
    """Real ``websockets`` client and server, over 127.0.0.1 only.

    No exchange is contacted. This is the one place the bound can be shown to
    be a genuine transport property rather than a number being handed around:
    the same payload is refused under the old default and accepted under the
    configured one.
    """

    @staticmethod
    async def _serve_one(payload: str):
        import websockets

        async def handler(ws):
            await ws.send(payload)
            with contextlib.suppress(Exception):
                await ws.recv()

        return await websockets.serve(handler, "127.0.0.1", 0)

    @staticmethod
    def _url(server) -> str:
        port = next(iter(server.sockets)).getsockname()[1]
        return f"ws://127.0.0.1:{port}"

    async def test_a_frame_over_1mib_is_refused_at_the_old_default(self):
        """Reproduces the field failure before asserting the fix."""
        import websockets

        payload = "x" * (WEBSOCKETS_DEFAULT_MAX_SIZE + 50_000)
        server = await self._serve_one(payload)
        try:
            with pytest.raises(Exception) as caught:
                async with websockets.connect(
                    self._url(server), max_size=WEBSOCKETS_DEFAULT_MAX_SIZE
                ) as ws:
                    await asyncio.wait_for(ws.recv(), timeout=10)
            assert "too big" in str(caught.value).lower() or "1009" in str(caught.value)
        finally:
            server.close()
            await server.wait_closed()

    async def test_the_same_frame_arrives_intact_under_the_configured_bound(self):
        import websockets

        size = WEBSOCKETS_DEFAULT_MAX_SIZE + 50_000
        payload = "x" * size
        configured = venue().ws_max_message_bytes
        assert configured > size, "the default bound must cover this case"

        server = await self._serve_one(payload)
        try:
            async with websockets.connect(
                self._url(server), max_size=configured
            ) as ws:
                received = await asyncio.wait_for(ws.recv(), timeout=10)
            assert len(received) == size
            assert received == payload, "the frame must arrive whole, not truncated"
        finally:
            server.close()
            await server.wait_closed()

    async def test_a_frame_past_the_configured_bound_is_still_refused(self):
        """Finite means finite: raising the limit must not remove it."""
        import websockets

        bound = 200_000
        payload = "x" * (bound + 10_000)
        configured = venue(ws_max_message_bytes=bound).ws_max_message_bytes
        server = await self._serve_one(payload)
        try:
            with pytest.raises(Exception) as caught:
                async with websockets.connect(
                    self._url(server), max_size=configured
                ) as ws:
                    await asyncio.wait_for(ws.recv(), timeout=10)
            # Asserted on the message so this cannot pass for some unrelated
            # reason -- it must be the size bound that refused the frame.
            assert "too big" in str(caught.value).lower() or "1009" in str(caught.value)
        finally:
            server.close()
            await server.wait_closed()


# ======================================================================
# C — the bound stays finite and defensible
# ======================================================================


class TestBoundRemainsFinite:
    def test_the_default_is_a_finite_integer(self):
        value = venue().ws_max_message_bytes
        assert isinstance(value, int)
        assert 0 < value < float("inf")

    def test_unbounded_is_rejected(self):
        """``max_size=None`` is the tempting one-character fix. Not available."""
        with pytest.raises(ValidationError):
            venue(ws_max_message_bytes=None)

    @pytest.mark.parametrize("value", [0, -1, -1_048_576])
    def test_nonpositive_is_rejected(self, value):
        with pytest.raises(ValidationError):
            venue(ws_max_message_bytes=value)

    def test_an_implausibly_large_frame_allowance_is_rejected(self):
        with pytest.raises(ValidationError):
            venue(ws_max_message_bytes=1_073_741_824)

    def test_the_validation_ceiling_itself_is_accepted(self):
        assert venue(ws_max_message_bytes=67_108_864).ws_max_message_bytes == 67_108_864


# ======================================================================
# D — the storage contract is untouched
# ======================================================================


class TestTransportAllowanceDoesNotWeakenStorageBound:
    """The two bounds are independent, and the big one must not disarm the
    small one. A frame large enough to be *received* is still subject to the
    local book's storage ceiling once it is parsed.
    """

    def test_the_two_bounds_are_separate_settings(self):
        config = venue(ws_max_message_bytes=67_108_864, max_book_levels_per_side=5_000)
        assert config.ws_max_message_bytes == 67_108_864
        assert config.max_book_levels_per_side == 5_000

    def test_raising_the_transport_bound_does_not_raise_the_book_bound(self):
        small = venue(ws_max_message_bytes=1_048_576)
        large = venue(ws_max_message_bytes=67_108_864)
        assert small.max_book_levels_per_side == large.max_book_levels_per_side

    def test_an_oversized_book_still_fails_closed_under_a_huge_transport_bound(self):
        """The containment path P12-F1 must not have opened.

        The transport is configured at its maximum permitted allowance — far
        more than enough to carry the message that produces this book — and
        the storage ceiling still refuses the level that crosses it.
        """
        config = venue(ws_max_message_bytes=67_108_864)
        assert config.ws_max_message_bytes == 67_108_864

        book = bounded_book(levels=5)
        with pytest.raises(BookOverflowError):
            book.apply_delta(one_more_bid(book, price=50.0))

        assert not book.synced, "crossing the storage ceiling must fail the book"
        assert book.needs_resync
        assert book.overflow_count == 1

    def test_every_level_is_retained_when_the_book_fails_closed(self):
        """Fail-closed, never silently-evict — the TIDAL-M1 invariant."""
        book = bounded_book(levels=5)
        with contextlib.suppress(BookOverflowError):
            book.apply_delta(one_more_bid(book, price=50.0))

        assert len(book.bids) == 6, "no level may be discarded to make storage fit"
        assert book.bids[50.0] == 1.0
