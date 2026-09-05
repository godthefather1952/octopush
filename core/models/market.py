"""Normalised market-data schemas.

Everything a venue adapter emits is expressed with these types; no
exchange-specific field name or symbol format survives past the adapter.
"""

from __future__ import annotations

import itertools
import math
from collections.abc import Iterable

from pydantic import Field, field_validator

from core.models.common import Base, DataQuality, Envelope, Millis, Side


class PriceLevel(Base):
    #: ``allow_inf_nan=False`` closes TIDAL-M3: without it, ``gt=0``/``ge=0``
    #: reject NaN and -inf (Python's ordering comparisons are false against
    #: NaN, and -inf fails gt/ge 0) but let +inf through, since +inf > 0 and
    #: +inf >= 0 are both true. An infinite price or size would make a level's
    #: notional infinite and corrupt every sum built from it downstream —
    #: exactly the "rely on downstream math to fail" this forbids.
    price: float = Field(gt=0, allow_inf_nan=False)
    size: float = Field(ge=0, allow_inf_nan=False)

    @property
    def notional(self) -> float:
        return self.price * self.size


# There is deliberately no `BookSide` model. It duplicated depth arithmetic
# that TIDAL already performs on its own `LocalOrderBook` (see
# agents/tidal/metrics.py), and nothing outside this module ever constructed
# one. Two implementations of the same calculation is one more than can be
# kept correct.


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
    price: float = Field(gt=0, allow_inf_nan=False)
    size: float = Field(gt=0, allow_inf_nan=False)
    #: Side of the aggressor.
    aggressor: Side
    trade_id: str | None = None

    @property
    def notional(self) -> float:
        return self.price * self.size


#: Distances from mid, in bps, at which resting depth is measured.
#:
#: This is the *contract* between the producer of :class:`BookMetrics` (TIDAL)
#: and every consumer of its depth buckets. It lives here, beside the model
#: that carries the buckets, rather than inside the producing agent, so a
#: consumer can state which bucket it needs — and a configuration can be
#: validated against the set — without importing an agent.
#:
#: Depth is measured at these distances and nowhere else. A consumer asking
#: for 10.001 bps is asking for a measurement that was never taken, and the
#: only honest answers are "reject the request" or "exclude the contributor" —
#: never "here is the whole book instead", which silently answers a completely
#: different question with a number one to two orders of magnitude larger.
DEPTH_BUCKETS_BPS: tuple[float, ...] = (1.0, 5.0, 10.0, 25.0)


def depth_bucket_key(window_bps: float) -> str:
    """The :class:`BookMetrics` dict key for a depth bucket.

    One formatting rule, shared by the producer and every consumer, so the two
    cannot drift into writing ``"10"`` and reading ``"10.0"``.
    """
    return f"{window_bps:g}"


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

    def depth_within(self, window_bps: float) -> tuple[float, float] | None:
        """``(bid, ask)`` notional resting within ``window_bps`` of mid.

        ``None`` when that bucket was not measured on this snapshot — either
        the distance is not one of :data:`DEPTH_BUCKETS_BPS`, or the book was
        too poor for metrics to be computed at all. The caller must treat that
        as *no evidence at this distance* and exclude the venue. There is
        deliberately no fallback to :attr:`bid_depth_notional` /
        :attr:`ask_depth_notional`: whole-book depth answers a different
        question, and substituting it silently makes a venue that measured
        nothing near the touch look like the deepest contributor in the market.
        """
        key = depth_bucket_key(window_bps)
        bid = self.bid_depth_by_bps.get(key)
        ask = self.ask_depth_by_bps.get(key)
        if bid is None or ask is None:
            return None
        return bid, ask


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
    #: Smoothed, non-negative economic latency: max(0, received_ts -
    #: exchange_ts). Floored so a downstream cost model always gets a usable
    #: number; see ``clock_skew_ms`` for what the floor would otherwise hide.
    latency_ms: float | None = None
    #: Unfloored received_ts - exchange_ts for the most recent update, so a
    #: negative value (exchange timestamp ahead of local receipt) stays
    #: visible instead of silently reading as zero latency (TIDAL-H3).
    #: ``None`` before any message has been recorded.
    clock_skew_ms: float | None = None
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

    def source_data_timestamp_for(
        self, legs: Iterable[tuple[str, str]]
    ) -> Millis | None:
        """Oldest exchange observation among the venues actually behind ``legs``.

        A record derived from several legs (a cross-venue opportunity, the
        intent built from it, an exit or a hedge) is only as fresh as its
        stalest leg: a two-leg trade whose buy side is 100ms old and whose
        sell side is 1,900ms old cannot honestly be called 100ms old, and a
        fresh venue must never mask a stale one (TIDAL-H4). Using this
        symbol's *overall* newest timestamp, or an unrelated symbol's, both
        launder that staleness away.

        Each pair is ``(venue, symbol)``. A leg whose venue state is missing
        or carries no exchange observation makes the whole result unknown
        (``None``) rather than silently skipped, so a caller that treats
        ``None`` as fail-closed — as :func:`risk.limits.gate_data_age` does —
        blocks rather than averaging over a hole in the data.

        Uses ``exchange_ts`` — the exchange's own observation time, not local
        receipt time — because that is what the corrected freshness model
        (TIDAL-H3) treats as authoritative for how old a market observation
        actually is.
        """
        timestamps: list[Millis] = []
        for venue, symbol in legs:
            state = self.venue_state(venue, symbol)
            if state is None or state.exchange_ts is None:
                return None
            timestamps.append(state.exchange_ts)
        return min(timestamps) if timestamps else None


# There is deliberately no `MarketSnapshot` bundle. The specification named
# one, but the recording path never used it: books and trades are published
# individually onto the bus and read back in order by the replay engine, so a
# second bundled representation of the same data would be a parallel format
# that nothing writes and nothing validates.


def safe_bps(numerator: float, reference: float) -> float | None:
    """``numerator / reference`` in basis points, guarding against zero."""
    if reference is None or reference == 0 or not math.isfinite(reference):
        return None
    value = numerator / reference * 10_000
    return value if math.isfinite(value) else None
