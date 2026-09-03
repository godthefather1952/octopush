"""Venue A adapter — public combined stream, no authentication."""

from __future__ import annotations

import json
import logging

from core.clock import Clock
from core.config import VenueConfig
from core.models.market import OrderBookSnapshot
from venues.base.messages import BookDelta, MalformedVenueMessage
from venues.base.symbols import denormalize
from venues.base.ws import WebSocketAdapter
from venues.venue_a import parser
from venues.venue_a.sync import DepthSynchronizer

log = logging.getLogger(__name__)


class VenueAAdapter(WebSocketAdapter):
    """Reads depth and trade streams for the configured symbols.

    The depth stream is a diff stream with no base state, so it is not usable
    on its own: this adapter runs the documented snapshot handshake per symbol
    (see :mod:`venues.venue_a.sync`) and only emits depth once the REST
    checkpoint and the streamed updates have been proven contiguous. Until
    then the symbol emits nothing, and TIDAL reports it UNAVAILABLE — which is
    the intended answer to "we do not yet know what this book looks like".

    Recovery works the same way. TIDAL detects a gap and publishes a resync
    request; the request reaches :meth:`request_resync` through the wiring, and
    a fresh checkpoint re-enters the normal event flow. Every endpoint touched
    here is public and unauthenticated.
    """

    def __init__(self, config: VenueConfig, clock: Clock, symbols: list[str]) -> None:
        super().__init__(config, clock, symbols)
        self._sync: dict[str, DepthSynchronizer] = {
            symbol: DepthSynchronizer(
                symbol,
                self.fetch_checkpoint,
                self._emit_book_message,
                clock,
                max_buffer=config.depth_sync_max_buffer,
                max_attempts=config.depth_sync_max_attempts,
                min_interval_s=config.depth_sync_min_interval_s,
            )
            for symbol in self.symbols
        }

    def connect_url(self) -> str:
        streams = "/".join(parser.stream_names(self.symbols))
        base = (self.config.ws_url or "wss://stream.binance.com:9443/stream").rstrip("/")
        return f"{base}?streams={streams}"

    async def on_connected(self) -> None:
        """A new socket is a new stream position, so every book restarts.

        Carrying a book across a reconnect would mean assuming the updates
        missed while disconnected did not matter. They might have been the
        only updates that did.
        """
        for synchronizer in self._sync.values():
            synchronizer.reset()

    async def _emit_book_message(self, message: OrderBookSnapshot | BookDelta) -> None:
        await self.emit(message)

    async def handle_payload(self, payload: str) -> None:
        try:
            message = json.loads(payload)
        except json.JSONDecodeError as exc:
            self.stats.record_malformed(
                MalformedVenueMessage(str(exc), venue=self.name),
                book_invalidated=False,
                resync_requested=False,
            )
            return
        try:
            parsed = parser.parse_message(message, self.clock.now_ms())
        except MalformedVenueMessage as exc:
            await self._contain(exc)
            return
        if parsed is None:
            return
        # Proof of life for backoff purposes (TIDAL-M5): a message this venue
        # actually cares about, successfully parsed — not merely valid JSON,
        # and (since unknown types return None above, before this line) not a
        # message type this venue ignores either.
        self.mark_healthy()
        if isinstance(parsed, BookDelta):
            synchronizer = self._sync.get(parsed.symbol)
            if synchronizer is None:
                # A symbol we never subscribed to. Nothing downstream is
                # expecting it and it has no snapshot, so it is not ours.
                return
            await synchronizer.on_delta(parsed)
            return
        await self.emit(parsed)

    async def _contain(self, exc: MalformedVenueMessage) -> None:
        """TIDAL-M4: isolate one malformed message without dropping the venue.

        A malformed ``depthUpdate`` may have hidden a real book change that
        this symbol's book will now never see — its continuity is uncertain
        in exactly the way a sequence gap's is, so it gets the same recovery:
        a resync request through the existing synchronizer, not a socket
        reset. A malformed trade print, or an envelope too broken to even
        name a message type, carries no book information either way, so
        nothing beyond counting the error is warranted.
        """
        resync_requested = False
        if exc.message_type == "depthUpdate" and exc.symbol is not None:
            synchronizer = self._sync.get(exc.symbol)
            if synchronizer is not None:
                resync_requested = True
                await synchronizer.request_resync(f"malformed depth message: {exc}")
        self.stats.record_malformed(
            exc, book_invalidated=False, resync_requested=resync_requested
        )

    async def request_resync(self, symbol: str, reason: str = "") -> None:
        synchronizer = self._sync.get(symbol)
        if synchronizer is None:
            return
        self.stats.sequence_gaps += 1
        await synchronizer.request_resync(reason)

    @property
    def sync_stats(self) -> dict[str, DepthSynchronizer]:
        """Per-symbol handshake state, for health reporting and tests."""
        return dict(self._sync)

    async def fetch_checkpoint(self, symbol: str) -> OrderBookSnapshot:
        """Fetch a REST depth snapshot to synchronise or resynchronise a book.

        Read-only public endpoint; no credentials are sent.
        """
        import httpx

        venue_symbol = denormalize(symbol, parser.SYMBOL_STYLE)
        base = (self.config.rest_url or "https://api.binance.com").rstrip("/")
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"{base}/api/v3/depth",
                params={"symbol": venue_symbol, "limit": self._checkpoint_limit()},
            )
            resp.raise_for_status()
            return parser.parse_depth_snapshot(resp.json(), symbol, self.clock.now_ms())

    def _checkpoint_limit(self) -> int:
        """Depth limit accepted by the public endpoint.

        ``/api/v3/depth`` only takes certain limits; anything else is rejected
        outright, so the configured level count is rounded up to the next one
        it accepts rather than sent verbatim.
        """
        wanted = self.config.book_depth_levels * 2
        for allowed in (5, 10, 20, 50, 100, 500, 1000, 5000):
            if wanted <= allowed:
                return allowed
        return 5000


__all__ = ["VenueAAdapter"]
