"""Shared WebSocket plumbing for public market-data adapters.

Handles the boring but essential parts: connect, subscribe, heartbeat
watchdog, exponential-backoff reconnect, and surfacing every transition as a
:class:`VenueStatus` so TIDAL can degrade the venue rather than quietly serving
stale data.
"""

from __future__ import annotations

import asyncio
import json
import logging
from abc import abstractmethod
from typing import Any

from core.clock import Clock
from core.config import VenueConfig
from venues.base.adapter import (
    ContinuityUncertain,
    ReconnectPolicy,
    VenueAdapter,
    VenueCapabilities,
)
from venues.base.messages import VenueStatus, VenueStatusKind

log = logging.getLogger(__name__)


class WebSocketAdapter(VenueAdapter):
    """Base for adapters that read a public WebSocket stream."""

    capabilities = VenueCapabilities(
        public_market_data=True, order_books=True, trades=True, authenticated=False
    )
    #: Seconds without any message after which the connection is considered dead.
    stale_after_s: float = 20.0

    def __init__(self, config: VenueConfig, clock: Clock, symbols: list[str]) -> None:
        super().__init__(config, clock, symbols)
        self.reconnect = ReconnectPolicy()
        self._ws: Any = None

    # -- to implement per venue -------------------------------------------

    @abstractmethod
    def connect_url(self) -> str: ...

    async def on_connected(self) -> None:
        """Send subscriptions. Default: nothing (URL-encoded subscriptions)."""

    @abstractmethod
    async def handle_payload(self, payload: str) -> None:
        """Parse one raw frame and emit normalised messages."""

    def mark_healthy(self) -> None:
        """Declare the current connection has proven itself.

        Call this from ``handle_payload`` once a message has been parsed into
        a real market-data record — not merely valid JSON, and not a
        heartbeat or other message this venue ignores; see the module
        docstring on why that distinction matters (TIDAL-M5).

        Resetting backoff unconditionally on a successful handshake — the old
        behaviour — could not tell a connection that immediately closes
        afterward from one that runs for an hour: both reset the delay to its
        minimum, so a socket that is accepted and then instantly rejected
        reconnects at roughly 1 Hz forever instead of backing off. Backoff now
        resets only once this connection has actually delivered something.
        """
        self.reconnect.reset()

    # -- driver ------------------------------------------------------------

    async def run(self) -> None:
        while not self._stopping:
            try:
                await self._session()
            except asyncio.CancelledError:
                raise
            except ContinuityUncertain as exc:
                # The error itself was already counted at the point a
                # containment decision chose to force this reconnect
                # (TIDAL-M4); counting it again here would double it for what
                # is really one problem surfacing through one mechanism.
                self.stats.last_error = str(exc)
                log.warning(
                    "venue session reconnecting: continuity uncertain",
                    extra={"venue": self.name, "error": str(exc)},
                )
            except Exception as exc:
                self.stats.errors += 1
                self.stats.last_error = str(exc)
                log.warning(
                    "venue session failed",
                    extra={"venue": self.name, "error": str(exc)},
                )
            finally:
                self.stats.connected = False
                self._ws = None
            if self._stopping:
                break
            await self.emit(
                VenueStatus(
                    venue=self.name,
                    kind=VenueStatusKind.DISCONNECTED,
                    received_ts=self.clock.now_ms(),
                    detail=self.stats.last_error or "session ended",
                )
            )
            delay = self.reconnect.next_delay()
            await self.clock.sleep(delay)

    async def _session(self) -> None:  # pragma: no cover - needs a live socket
        import websockets

        url = self.connect_url()
        # ``max_size`` is passed explicitly because the library default is
        # 1 MiB and a legitimate full level-2 snapshot from a public venue can
        # exceed it -- the connection is then closed with code 1009 before a
        # single book is built, and the adapter reconnects into the same
        # failure forever. The configured bound is finite by construction: see
        # ``VenueConfig.ws_max_message_bytes`` for why this is a transport
        # limit and why it does not relax the local book's storage contract.
        async with websockets.connect(
            url,
            ping_interval=15,
            close_timeout=5,
            max_size=self.config.ws_max_message_bytes,
        ) as ws:
            self._ws = ws
            self.stats.connected = True
            self.stats.connects += 1
            if self.stats.connects > 1:
                self.stats.reconnects += 1
            await self.on_connected()
            await self.emit(
                VenueStatus(
                    venue=self.name,
                    kind=VenueStatusKind.CONNECTED,
                    received_ts=self.clock.now_ms(),
                    detail=url,
                )
            )
            while not self._stopping:
                try:
                    payload = await asyncio.wait_for(ws.recv(), timeout=self.stale_after_s)
                except TimeoutError as exc:
                    raise RuntimeError("feed went silent") from exc
                if isinstance(payload, bytes):
                    payload = payload.decode()
                await self.emit_raw(payload)
                await self.handle_payload(payload)

    async def send_json(self, message: dict[str, Any]) -> None:  # pragma: no cover
        if self._ws is None:
            raise RuntimeError("not connected")
        await self._ws.send(json.dumps(message))
