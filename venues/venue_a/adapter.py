"""Venue A adapter — public combined stream, no authentication."""

from __future__ import annotations

import json
import logging

from venues.base.symbols import denormalize
from venues.base.ws import WebSocketAdapter
from venues.venue_a import parser

log = logging.getLogger(__name__)


class VenueAAdapter(WebSocketAdapter):
    """Reads depth and trade streams for the configured symbols.

    Depth updates arrive as deltas keyed by update id.  TIDAL applies them
    against the sequence it holds and requests a REST checkpoint when the ids
    do not line up, so a gap can never be silently absorbed.
    """

    def connect_url(self) -> str:
        streams = "/".join(parser.stream_names(self.symbols))
        base = (self.config.ws_url or "wss://stream.binance.com:9443/stream").rstrip("/")
        return f"{base}?streams={streams}"

    async def handle_payload(self, payload: str) -> None:
        try:
            message = json.loads(payload)
        except json.JSONDecodeError:
            self.stats.errors += 1
            return
        parsed = parser.parse_message(message, self.clock.now_ms())
        if parsed is not None:
            await self.emit(parsed)

    async def fetch_checkpoint(self, symbol: str):  # pragma: no cover - network
        """Fetch a REST depth snapshot to resynchronise a gapped book.

        Read-only public endpoint; no credentials are sent.
        """
        import httpx

        venue_symbol = denormalize(symbol, parser.SYMBOL_STYLE)
        base = (self.config.rest_url or "https://api.binance.com").rstrip("/")
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"{base}/api/v3/depth",
                params={"symbol": venue_symbol, "limit": self.config.book_depth * 2},
            )
            resp.raise_for_status()
            return parser.parse_depth_snapshot(resp.json(), symbol, self.clock.now_ms())
