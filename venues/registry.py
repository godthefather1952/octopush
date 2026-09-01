"""Adapter registry.

Adding a venue means adding an entry here and a config block — no other part
of the system changes.  This is what keeps the "2 -> 3 -> 5 -> 8 venues"
expansion path from being a redesign.
"""

from __future__ import annotations

from collections.abc import Callable

from core.clock import Clock
from core.config import VenueConfig
from venues.base.adapter import VenueAdapter
from venues.simulated import SimulatedVenueAdapter
from venues.venue_a.adapter import VenueAAdapter
from venues.venue_b.adapter import VenueBAdapter

AdapterFactory = Callable[[VenueConfig, Clock, list[str]], VenueAdapter]

ADAPTERS: dict[str, AdapterFactory] = {
    "simulated": SimulatedVenueAdapter,
    "binance_public": VenueAAdapter,
    "coinbase_public": VenueBAdapter,
}


def build_adapter(config: VenueConfig, clock: Clock, symbols: list[str]) -> VenueAdapter:
    try:
        factory = ADAPTERS[config.adapter]
    except KeyError as exc:
        raise ValueError(f"unknown venue adapter: {config.adapter}") from exc
    adapter = factory(config, clock, symbols)
    if adapter.capabilities.order_submission or adapter.capabilities.authenticated:
        # Structural guard: this build has no live-trading path, and an adapter
        # that claims one must never be constructed.
        raise RuntimeError(
            f"venue adapter {config.adapter} declares trading capability; "
            "this build is public-market-data only"
        )
    return adapter
