"""Shared fixtures.

Every test runs offline and deterministically: a manual clock, an in-process
bus, an in-memory store, and a seeded synthetic market.
"""

from __future__ import annotations

import logging

import pytest

from apps.orchestrator.wiring import Platform, build_platform
from core.bus import InMemoryEventBus
from core.clock import ManualClock
from core.config import Settings, load_settings, simulated_venues
from core.health import HealthRegistry
from core.models.common import DataQuality, Side
from core.models.market import (
    BookMetrics,
    OrderBookSnapshot,
    PriceLevel,
    VenueMarketState,
)
from simulation.market import DislocationSpec, default_market
from storage import InMemoryEventStore

START_MS = 1_788_000_000_000

logging.getLogger().setLevel(logging.CRITICAL)


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock(START_MS)


@pytest.fixture
def bus() -> InMemoryEventBus:
    return InMemoryEventBus(raise_on_handler_error=True)


@pytest.fixture
def store() -> InMemoryEventStore:
    return InMemoryEventStore()


@pytest.fixture
def health(clock: ManualClock) -> HealthRegistry:
    return HealthRegistry(clock=clock)


@pytest.fixture
def settings() -> Settings:
    base = load_settings()
    return base.model_copy(update={"venues": simulated_venues()})


@pytest.fixture
def platform(
    settings: Settings, clock: ManualClock, bus: InMemoryEventBus, store: InMemoryEventStore
) -> Platform:
    return build_platform(
        settings,
        clock=clock,
        bus=bus,
        store=store,
        market=default_market(start_ms=START_MS),
        raise_on_handler_error=True,
    )


async def run_platform(platform: Platform, ticks: int, *, step_ms: int = 100) -> None:
    """Drive a platform for ``ticks`` synthetic market steps."""
    clock = platform.clock
    await platform.start(record=True)
    for _ in range(ticks):
        clock.advance(step_ms)
        await platform.step_market(1)
        await platform.orchestrator.tick()


def make_book(
    venue: str,
    symbol: str,
    mid: float,
    *,
    spread_bps: float = 2.0,
    levels: int = 8,
    size: float = 0.5,
    tick: float = 0.5,
    ts: int = START_MS,
) -> OrderBookSnapshot:
    """A clean synthetic book, for tests that need an exact shape."""
    half = mid * spread_bps / 20_000
    bids = [
        PriceLevel(price=round(mid - half - i * tick, 6), size=size * (1 + 0.2 * i))
        for i in range(levels)
    ]
    asks = [
        PriceLevel(price=round(mid + half + i * tick, 6), size=size * (1 + 0.2 * i))
        for i in range(levels)
    ]
    return OrderBookSnapshot(
        venue=venue,
        symbol=symbol,
        exchange_ts=ts,
        received_ts=ts,
        sequence=1,
        bids=bids,
        asks=asks,
        is_checkpoint=True,
    )


def venue_state_from_book(
    book: OrderBookSnapshot, *, as_of: int = START_MS, quality: DataQuality = DataQuality.FRESH
) -> VenueMarketState:
    """Build a VenueMarketState directly from a book, for isolated tests."""
    bid, ask = book.best_bid, book.best_ask
    mid = (bid + ask) / 2
    bid_depth = sum(level.notional for level in book.bids)
    ask_depth = sum(level.notional for level in book.asks)
    bid_size = book.bids[0].size
    ask_size = book.asks[0].size
    metrics = BookMetrics(
        best_bid=bid,
        best_ask=ask,
        mid=mid,
        microprice=(bid * ask_size + ask * bid_size) / (bid_size + ask_size),
        spread=ask - bid,
        spread_bps=(ask - bid) / mid * 10_000,
        bid_depth_notional=bid_depth,
        ask_depth_notional=ask_depth,
        bid_depth_by_bps={"10": bid_depth, "1": bid_depth / 4, "5": bid_depth / 2, "25": bid_depth},
        ask_depth_by_bps={"10": ask_depth, "1": ask_depth / 4, "5": ask_depth / 2, "25": ask_depth},
        imbalance=(bid_depth - ask_depth) / (bid_depth + ask_depth),
    )
    return VenueMarketState(
        venue=book.venue,
        symbol=book.symbol,
        metrics=metrics,
        book=book,
        exchange_ts=book.exchange_ts,
        last_update_ts=book.received_ts,
        as_of=as_of,
        quality=quality,
        latency_ms=5.0,
        connected=True,
    )


@pytest.fixture
def dislocated_market():
    """A market with a large, scheduled dislocation on VENUE_B BTC."""

    def _build(magnitude_bps: float = 45.0, start_step: int = 5, duration: int = 200):
        return default_market(
            start_ms=START_MS,
            symbols=["BTC-USD"],
            dislocations=[
                DislocationSpec(
                    start_step=start_step,
                    duration_steps=duration,
                    venue="VENUE_B",
                    symbol="BTC-USD",
                    magnitude_bps=magnitude_bps,
                )
            ],
        )

    return _build


__all__ = ["START_MS", "Side", "make_book", "run_platform", "venue_state_from_book"]
