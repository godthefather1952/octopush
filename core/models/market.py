"""Normalised market-data schemas.

Everything a venue adapter emits is expressed with these types; no
exchange-specific field name or symbol format survives past the adapter.
"""

from __future__ import annotations

import itertools
import math

from pydantic import Field, field_validator

from core.models.common import Base, DataQuality, Envelope, Millis, Side


class PriceLevel(Base):
    price: float = Field(gt=0)
    size: float = Field(ge=0)

    @property
    def notional(self) -> float:
        return self.price * self.size


class BookSide(Base):
    """One side of an order book, sorted best-price-first."""

    side: Side
    levels: list[PriceLevel] = Field(default_factory=list)

    @property
    def best(self) -> PriceLevel | None:
        return self.levels[0] if self.levels else None

    def depth_notional(self, max_levels: int | None = None) -> float:
        levels = self.levels if max_levels is None else self.levels[:max_levels]
        return sum(level.notional for level in levels)

    def depth_within_bps(self, reference: float, bps: float) -> float:
        """Notional resting within ``bps`` of ``reference``."""
        if reference <= 0:
            return 0.0
        limit = reference * (1 + bps / 10_000 * (1 if self.side is Side.SELL else -1))
        total = 0.0
        for level in self.levels:
            if self.side is Side.SELL and level.price > limit:
                break
            if self.side is Side.BUY and level.price < limit:
                break
            total += level.notional
        return total


class OrderBookSnapshot(Base):
    """A point-in-time L2 book for one symbol on one venue."""

    venue: str
    symbol: str
    #: Exchange-provided timestamp of the underlying data.
    exchange_ts: Millis
    #: When this snapshot was constructed locally.
    received_ts: Millis
    sequence: int | None = None
    bids: list[PriceLevel] = Field(default_factory=list)
    asks: list[PriceLevel] = Field(default_factory=list)
    #: True when this snapshot was produced by a full refresh rather than by
    #: applying an incremental update.
    is_checkpoint: bool = False

    @field_validator("bids")
    @classmethod
    def _bids_descending(cls, v: list[PriceLevel]) -> list[PriceLevel]:
        for a, b in itertools.pairwise(v):
            if b.price >= a.price:
                raise ValueError("bids must be strictly descending in price")
        return v

    @field_validator("asks")
    @classmethod
    def _asks_ascending(cls, v: list[PriceLevel]) -> list[PriceLevel]:
        for a, b in itertools.pairwise(v):
            if b.price <= a.price:
                raise ValueError("asks must be strictly ascending in price")
        return v

    @property
    def best_bid(self) -> float | None:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> float | None:
        return self.asks[0].price if self.asks else None

    @property
    def bid_side(self) -> BookSide:
        return BookSide(side=Side.BUY, levels=self.bids)

    @property
    def ask_side(self) -> BookSide:
        return BookSide(side=Side.SELL, levels=self.asks)

    def side(self, side: Side) -> list[PriceLevel]:
        return self.bids if side is Side.BUY else self.asks

    @property
    def crossed(self) -> bool:
        bid, ask = self.best_bid, self.best_ask
        return bid is not None and ask is not None and bid >= ask


class TradeEvent(Base):
    """A public trade print."""

    venue: str
    symbol: str
    exchange_ts: Millis
    received_ts: Millis
    price: float = Field(gt=0)
    size: float = Field(gt=0)
    #: Side of the aggressor.
    aggressor: Side
    trade_id: str | None = None

    @property
    def notional(self) -> float:
        return self.price * self.size


class BookMetrics(Base):
    """Microstructure statistics derived from a single venue's book."""

    best_bid: float | None = None
    best_ask: float | None = None
    mid: float | None = None
    microprice: float | None = None
    spread: float | None = None
    spread_bps: float | None = None
    bid_depth_notional: float = 0.0
    ask_depth_notional: float = 0.0
    #: Notional resting within N bps of mid, keyed by the bps distance.
    bid_depth_by_bps: dict[str, float] = Field(default_factory=dict)
    ask_depth_by_bps: dict[str, float] = Field(default_factory=dict)
    #: (bid_depth - ask_depth) / (bid_depth + ask_depth) in [-1, 1].
    imbalance: float = 0.0
    #: Realised volatility of the mid over the rolling window, in bps.
    short_vol_bps: float = 0.0
    buy_volume: float = 0.0
    sell_volume: float = 0.0

    @property
    def trade_flow_imbalance(self) -> float:
        total = self.buy_volume + self.sell_volume
        if total <= 0:
            return 0.0
        return (self.buy_volume - self.sell_volume) / total


class VenueMarketState(Base):
    """Everything TIDAL knows about one symbol on one venue."""

    venue: str
    symbol: str
    metrics: BookMetrics
    #: Top-of-book ladder, carried on the bus so that ZEPHR and the paper
    #: executor can price against real levels without reaching into TIDAL.
    book: OrderBookSnapshot | None = None
    #: Newest exchange timestamp incorporated.
    exchange_ts: Millis | None = None
    #: Local time at which that data arrived.
    last_update_ts: Millis | None = None
    #: Local time at which the state was assembled.
    as_of: Millis
    quality: DataQuality = DataQuality.UNAVAILABLE
    #: Observed transport latency: received_ts - exchange_ts.
    latency_ms: float | None = None
    connected: bool = False
    sequence_gaps: int = 0
    reconnects: int = 0

    @property
    def age_ms(self) -> int | None:
        if self.last_update_ts is None:
            return None
        return self.as_of - self.last_update_ts


class ConsolidatedView(Base):
    """Cross-venue view of one symbol."""

    symbol: str
    as_of: Millis
    #: Liquidity-weighted reference price across usable venues.
    reference_price: float | None = None
    #: Best bid across venues and where it is.
    best_bid: float | None = None
    best_bid_venue: str | None = None
    best_ask: float | None = None
    best_ask_venue: str | None = None
    #: best_bid - best_ask across venues; positive means a crossed market.
    cross_venue_spread_bps: float | None = None
    #: Largest absolute mid deviation from the reference, in bps.
    max_deviation_bps: float | None = None
    max_deviation_venue: str | None = None
    quality: DataQuality = DataQuality.UNAVAILABLE
    usable_venues: list[str] = Field(default_factory=list)


class MarketState(Envelope):
    """TIDAL's published view of the whole market."""

    venues: dict[str, VenueMarketState] = Field(default_factory=dict)
    consolidated: dict[str, ConsolidatedView] = Field(default_factory=dict)

    def venue_state(self, venue: str, symbol: str) -> VenueMarketState | None:
        return self.venues.get(f"{venue}:{symbol}")

    def states_for(self, symbol: str) -> list[VenueMarketState]:
        return [s for s in self.venues.values() if s.symbol == symbol]


class MarketSnapshot(Envelope):
    """A recorded, replayable bundle of raw normalised market data."""

    books: list[OrderBookSnapshot] = Field(default_factory=list)
    trades: list[TradeEvent] = Field(default_factory=list)


def safe_bps(numerator: float, reference: float) -> float | None:
    """``numerator / reference`` in basis points, guarding against zero."""
    if reference is None or reference == 0 or not math.isfinite(reference):
        return None
    value = numerator / reference * 10_000
    return value if math.isfinite(value) else None
