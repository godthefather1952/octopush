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
    #: Range-sequenced messages that re-covered ground already applied, outside
    #: the one position after a snapshot where the protocol expects it. Lossless
    #: (quantities are absolute) but not normal, so it is counted rather than
    #: ignored.
    overlapping_updates: int = 0
    #: Set when a gap is seen, cleared by the next checkpoint.
    needs_resync: bool = False
    #: True between a snapshot and the first delta applied on top of it.
    #: Range-sequenced feeds relax the continuity rule for exactly that one
    #: update; see :meth:`apply_delta`.
    awaiting_first_delta: bool = False

    # -- mutation ----------------------------------------------------------

    def apply_snapshot(self, snapshot: OrderBookSnapshot) -> None:
        self.bids = {level.price: level.size for level in snapshot.bids if level.size > 0}
        self.asks = {level.price: level.size for level in snapshot.asks if level.size > 0}
        self.sequence = snapshot.sequence
        self.exchange_ts = snapshot.exchange_ts
        self.last_update_ts = snapshot.received_ts
        self.synced = True
        self.needs_resync = False
        self.awaiting_first_delta = True
        self.updates_applied += 1
        self._trim()

    def _desync(self, detail: str) -> BookDesyncError:
        self.sequence_gaps += 1
        self.synced = False
        self.needs_resync = True
        return BookDesyncError(f"{self.venue}:{self.symbol} sequence gap: {detail}")

    def _check_range(self, delta: BookDelta) -> bool:
        """Validate a range-sequenced delta. False means "already covered".

        ``first_sequence..sequence`` is the inclusive span of update ids this
        one message carries, so the question is not "does it start exactly
        where we stopped" but "does it contain the next id we still need".

        Held id ``H``, so the next id needed is ``H + 1``:

        * ``sequence < H + 1``      — the whole span is behind us. Drop it.
        * ``first_sequence > H + 1`` — the span starts past what we need, so
          the ids in between were never delivered. That is a real gap.
        * otherwise the span straddles ``H + 1`` and applying it loses nothing.

        Straddling is the normal shape of the first message after a snapshot
        (Binance's ``U <= lastUpdateId + 1 <= u``), and is a protocol violation
        afterwards. It is tolerated in both positions rather than only the
        first because these deltas carry *absolute* level quantities, not
        increments: re-covering ground already applied rewrites levels to the
        same values it just wrote. Overlap cannot corrupt the book; only a gap
        can, and a gap is still refused. Overlap outside the first position is
        counted so it stays visible instead of merely tolerated.
        """
        assert delta.first_sequence is not None and delta.sequence is not None
        if self.sequence is None:
            return True
        needed = self.sequence + 1
        if delta.sequence < needed:
            return False
        if delta.first_sequence > needed:
            raise self._desync(
                f"have {self.sequence}, next message covers "
                f"{delta.first_sequence}..{delta.sequence}"
            )
        if delta.first_sequence < needed and not self.awaiting_first_delta:
            self.overlapping_updates += 1
        return True

    def _check_point(self, delta: BookDelta) -> bool:
        """Validate a point-sequenced delta. False means "already covered"."""
        if self.sequence is None or delta.sequence is None:
            return True
        if delta.sequence <= self.sequence:
            # Duplicate or out-of-order replay of something already applied.
            return False
        expected = delta.prev_sequence
        if expected is not None and expected != self.sequence:
            raise self._desync(f"have {self.sequence}, update expects {expected}")
        return True

    def apply_delta(self, delta: BookDelta) -> None:
        """Apply an incremental update.

        Raises :class:`BookDesyncError` on a sequence gap; the caller marks the
        book unsynchronised and requests a checkpoint.  Out-of-order or
        duplicate updates (sequence at or below what we hold) are dropped.
        """
        if not self.synced:
            raise BookDesyncError(f"{self.venue}:{self.symbol} has no snapshot yet")
        applicable = (
            self._check_range(delta) if delta.covers_range else self._check_point(delta)
        )
        if not applicable:
            return
        for level in delta.bids:
            self._set(self.bids, level)
        for level in delta.asks:
            self._set(self.asks, level)
        if delta.sequence is not None:
            self.sequence = delta.sequence
        self.exchange_ts = delta.exchange_ts
        self.last_update_ts = delta.received_ts
        self.updates_applied += 1
        self.awaiting_first_delta = False
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
        self.awaiting_first_delta = False

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
