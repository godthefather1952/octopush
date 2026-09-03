"""Venue B adapter — public level-2 feed, no authentication."""

from __future__ import annotations

import json
import logging

from venues.base.adapter import ContinuityUncertain
from venues.base.messages import MalformedVenueMessage
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
        except json.JSONDecodeError as exc:
            self.stats.record_malformed(
                MalformedVenueMessage(str(exc), venue=self.name),
                book_invalidated=False,
                resync_requested=False,
            )
            return
        if message.get("type") == "error":
            # An application-level error from the exchange itself — a bad
            # subscription, most likely. Not a market-data message and not
            # proof the stream is healthy, so it does not call mark_healthy().
            self.stats.errors += 1
            self.stats.last_error = str(message.get("message"))
            return
        try:
            parsed = parser.parse_message(message, self.clock.now_ms())
        except MalformedVenueMessage as exc:
            self._contain(exc)
            return
        if parsed is None:
            return
        self.mark_healthy()
        await self.emit(parsed)

    def _contain(self, exc: MalformedVenueMessage) -> None:
        """TIDAL-M2/M4: malformed L2 content leaves book state uncertain.

        Unlike Binance, there is no per-symbol resync available here — the
        only verified way to obtain a fresh Coinbase snapshot is a full
        reconnect (see the Batch 3 report). A malformed ``snapshot`` or
        ``l2update`` therefore forces one, by raising out of
        ``handle_payload`` into the ordinary reconnect path already tested in
        ``venues/base/ws.py``. A malformed ``match`` (trade print) touches no
        book state and does not warrant tearing down a working connection
        over.
        """
        book_affecting = exc.message_type in ("snapshot", "l2update")
        self.stats.record_malformed(
            exc, book_invalidated=book_affecting, resync_requested=book_affecting
        )
        if book_affecting:
            raise ContinuityUncertain(str(exc)) from exc
