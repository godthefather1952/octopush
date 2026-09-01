"""Venue B adapter — public level-2 feed, no authentication."""

from __future__ import annotations

import json
import logging

from venues.base.ws import WebSocketAdapter
from venues.venue_b import parser

log = logging.getLogger(__name__)


class VenueBAdapter(WebSocketAdapter):
    """Subscribes to level2 and match channels for the configured symbols.

    This feed pushes a full snapshot on subscribe and then incremental
    changes, so the adapter needs no REST checkpoint path.
    """

    async def on_connected(self) -> None:  # pragma: no cover - needs a socket
        await self.send_json(parser.subscribe_message(self.symbols))

    def connect_url(self) -> str:
        return self.config.ws_url or "wss://ws-feed.exchange.coinbase.com"

    async def handle_payload(self, payload: str) -> None:
        try:
            message = json.loads(payload)
        except json.JSONDecodeError:
            self.stats.errors += 1
            return
        if message.get("type") == "error":
            self.stats.errors += 1
            self.stats.last_error = str(message.get("message"))
            return
        parsed = parser.parse_message(message, self.clock.now_ms())
        if parsed is not None:
            await self.emit(parsed)
