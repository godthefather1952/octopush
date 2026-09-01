"""Local order book maintenance.

Deterministic, allocation-light code — no AI anywhere near it.  The book is
authoritative only while it is *synchronised*; a sequence gap, a crossed book
or a stale feed takes it out of the usable set instead of quietly degrading
the prices everything else depends on.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from core.models.common import Millis, Side
from core.models.market import OrderBookSnapshot, PriceLevel
from venues.base.messages import BookDelta


class BookDesyncError(RuntimeError):
    """Raised when an update cannot be applied to the current book."""


@dataclass
class LocalOrderBook:
    """One venue's L2 book for one symbol."""

    venue: str
    symbol: str
    max_depth: int = 25
    bids: dict[float, float] = field(default_factory=dict)
    asks: dict[float, float] = field(default_factory=dict)
    sequence: int | None = None
    exchange_ts: Millis | None = None
    last_update_ts: Millis | None = None
    synced: bool = False
    sequence_gaps: int = 0
    updates_applied: int = 0
    #: Set when a gap is seen, cleared by the next checkpoint.
    needs_resync: bool = False

    # -- mutation ----------------------------------------------------------

    def apply_snapshot(self, snapshot: OrderBookSnapshot) -> None:
        self.bids = {level.price: level.size for level in snapshot.bids if level.size > 0}
        self.asks = {level.price: level.size for level in snapshot.asks if level.size > 0}
        self.sequence = snapshot.sequence
        self.exchange_ts = snapshot.exchange_ts
        self.last_update_ts = snapshot.received_ts
        self.synced = True
        self.needs_resync = False
        self.updates_applied += 1
        self._trim()

    def apply_delta(self, delta: BookDelta) -> None:
        """Apply an incremental update.

        Raises :class:`BookDesyncError` on a sequence gap; the caller marks the
        book unsynchronised and requests a checkpoint.  Out-of-order or
        duplicate updates (sequence at or below what we hold) are dropped.
        """
        if not self.synced:
            raise BookDesyncError(f"{self.venue}:{self.symbol} has no snapshot yet")
        if delta.sequence is not None and self.sequence is not None:
            if delta.sequence <= self.sequence:
                # Duplicate or out-of-order replay of something already applied.
                return
            expected = delta.prev_sequence
            if expected is not None and expected != self.sequence:
                self.sequence_gaps += 1
                self.synced = False
                self.needs_resync = True
                raise BookDesyncError(
                    f"{self.venue}:{self.symbol} sequence gap: "
                    f"have {self.sequence}, update expects {expected}"
                )
        for level in delta.bids:
            self._set(self.bids, level)
        for level in delta.asks:
            self._set(self.asks, level)
        if delta.sequence is not None:
            self.sequence = delta.sequence
        self.exchange_ts = delta.exchange_ts
        self.last_update_ts = delta.received_ts
        self.updates_applied += 1
        self._trim()

    @staticmethod
    def _set(side: dict[float, float], level: PriceLevel) -> None:
        if level.size <= 0:
            side.pop(level.price, None)
        else:
            side[level.price] = level.size

    def _trim(self) -> None:
        """Keep only the top ``max_depth`` levels per side."""
        if len(self.bids) > self.max_depth:
            keep = sorted(self.bids, reverse=True)[: self.max_depth]
            self.bids = {p: self.bids[p] for p in keep}
        if len(self.asks) > self.max_depth:
            keep = sorted(self.asks)[: self.max_depth]
            self.asks = {p: self.asks[p] for p in keep}

    def invalidate(self, reason: str = "") -> None:
        self.synced = False
        self.needs_resync = True

    # -- reads -------------------------------------------------------------

    @property
    def best_bid(self) -> float | None:
        return max(self.bids) if self.bids else None

    @property
    def best_ask(self) -> float | None:
        return min(self.asks) if self.asks else None

    @property
    def crossed(self) -> bool:
        bid, ask = self.best_bid, self.best_ask
        return bid is not None and ask is not None and bid >= ask

    @property
    def usable(self) -> bool:
        return self.synced and not self.crossed and bool(self.bids) and bool(self.asks)

    def levels(self, side: Side, depth: int | None = None) -> list[PriceLevel]:
        source = self.bids if side is Side.BUY else self.asks
        prices = sorted(source, reverse=side is Side.BUY)
        if depth is not None:
            prices = prices[:depth]
        return [PriceLevel(price=p, size=source[p]) for p in prices]

    def snapshot(self, now_ms: Millis, *, depth: int | None = None) -> OrderBookSnapshot:
        return OrderBookSnapshot(
            venue=self.venue,
            symbol=self.symbol,
            exchange_ts=self.exchange_ts or now_ms,
            received_ts=self.last_update_ts or now_ms,
            sequence=self.sequence,
            bids=self.levels(Side.BUY, depth or self.max_depth),
            asks=self.levels(Side.SELL, depth or self.max_depth),
            is_checkpoint=True,
        )
