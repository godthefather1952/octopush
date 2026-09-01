"""Offline venue adapters backed by the synthetic market.

The default local run and every integration test use these, so the whole
platform can be exercised end to end with no network and no exchange.
"""

from __future__ import annotations

import asyncio
import contextlib

from core.clock import Clock
from core.config import VenueConfig
from simulation.market import SyntheticMarket
from venues.base.adapter import VenueAdapter, VenueCapabilities
from venues.base.messages import VenueStatus, VenueStatusKind


class SimulatedVenueAdapter(VenueAdapter):
    """Passive adapter: the driver pushes this venue's messages into it."""

    capabilities = VenueCapabilities(
        public_market_data=True, order_books=True, trades=True
    )

    def __init__(self, config: VenueConfig, clock: Clock, symbols: list[str]) -> None:
        super().__init__(config, clock, symbols)
        #: Set by failure tests to simulate a dead feed without tearing down
        #: the driver.
        self.suspended = False

    async def run(self) -> None:
        self.stats.connected = True
        self.stats.connects += 1
        await self.emit(
            VenueStatus(
                venue=self.name,
                kind=VenueStatusKind.CONNECTED,
                received_ts=self.clock.now_ms(),
                detail="simulated feed",
            )
        )
        while not self._stopping:
            await self.clock.sleep(1.0)

    async def deliver(self, messages: list) -> None:
        if self.suspended:
            return
        for message in messages:
            await self.emit(message)

    async def simulate_disconnect(self, detail: str = "injected disconnect") -> None:
        self.suspended = True
        self.stats.connected = False
        await self.emit(
            VenueStatus(
                venue=self.name,
                kind=VenueStatusKind.DISCONNECTED,
                received_ts=self.clock.now_ms(),
                detail=detail,
            )
        )

    async def simulate_reconnect(self) -> None:
        self.suspended = False
        self.stats.connected = True
        self.stats.reconnects += 1
        await self.emit(
            VenueStatus(
                venue=self.name,
                kind=VenueStatusKind.CONNECTED,
                received_ts=self.clock.now_ms(),
                detail="reconnected",
            )
        )


class SimulatedMarketDriver:
    """Steps a :class:`SyntheticMarket` and routes messages to adapters."""

    def __init__(
        self,
        market: SyntheticMarket,
        adapters: dict[str, SimulatedVenueAdapter],
        clock: Clock,
    ) -> None:
        self.market = market
        self.adapters = adapters
        self.clock = clock
        self._task: asyncio.Task[None] | None = None
        self._stopping = False

    async def step(self) -> None:
        """Advance the market one step and deliver the resulting messages.

        Messages are stamped from the platform clock, not from the generator's
        internal step counter.  The generator's counter drives the *price
        process*; it is not a clock, and letting it stamp timestamps means
        that as soon as a step takes longer than its nominal interval the
        synthetic time falls behind real time and every book looks stale.
        """
        now = self.clock.now_ms()
        by_venue: dict[str, list] = {name: [] for name in self.adapters}
        for message in self.market.next_step():
            venue = getattr(message, "venue", None)
            if venue not in by_venue:
                continue
            message.exchange_ts = now
            message.received_ts = now
            by_venue[venue].append(message)
        for venue, messages in by_venue.items():
            if messages:
                await self.adapters[venue].deliver(messages)

    async def run(self) -> None:
        interval = self.market.step_ms / 1000.0
        while not self._stopping:
            await self.step()
            await self.clock.sleep(interval)

    async def start(self) -> None:
        self._stopping = False
        self._task = asyncio.create_task(self.run(), name="sim-driver")

    async def stop(self) -> None:
        self._stopping = True
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
