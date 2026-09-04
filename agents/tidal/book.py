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


#: Bound on ``LocalOrderBook.invalid_reason`` (TIDAL-L2): diagnostic only, so
#: a long formatted detail is truncated rather than let an arbitrarily large
#: string accumulate in book state.
_MAX_INVALID_REASON_LEN = 200


class BookOverflowError(BookDesyncError):
    """Raised when a book's stored level count exceeds its safety bound.

    A subclass of :class:`BookDesyncError` on purpose: TIDAL already treats
    that as "this book needs re-establishing" and requests a resync/reconnect
    through the existing per-venue recovery path (see ``agents/tidal/agent.py:
    on_delta``). Overflow is a different failure than a sequence gap — the
    feed did not skip anything, storage simply grew past what is trusted to
    hold correctly — so it is counted separately (``overflow_count``, not
    ``sequence_gaps``), but recovers the same way: fail the book closed and
    let a fresh snapshot re-establish bounded state, never silently evict
    levels to fit (FULL-BOOK STORAGE BOUND, Batch 5).
    """


@dataclass
class LocalOrderBook:
    """One venue's L2 book for one symbol.

    ``bids``/``asks`` hold the **full** locally known book — every level ever
    inserted and not yet deleted, with no depth limit. ``max_depth`` is a read
    boundary, applied only in :meth:`levels` (and, through it, in
    :meth:`snapshot` and every metric that reads the book). It is not applied
    to storage.

    This used to trim the authoritative dicts to ``max_depth`` after every
    delta. That silently discarded whatever was beyond the configured depth,
    so a level sitting just outside the top-N was gone even though the venue
    never told anyone it was deleted — and if the levels ahead of it later
    vanished (a real, ordinary sequence of trades and cancels), it should have
    resurfaced into the top-N with its real, already-known size. Instead it
    reappeared only once a fresh update happened to touch it again, or not at
    all until the next snapshot (TIDAL-M1). Trimming on write conflated "how
    much of the book do I keep" with "how much of the book do I show", and
    only the second question has a good reason for a fixed answer.
    """

    venue: str
    symbol: str
    max_depth: int = 25
    #: Hard safety bound on distinct levels held per side (storage, not the
    #: read-time ``max_depth`` trim). See :class:`BookOverflowError`. Defaults
    #: to the same conservative value as ``VenueConfig.max_book_levels_per_side``
    #: so a book built directly (tests, or a venue absent from settings) still
    #: fails closed instead of growing without limit.
    max_levels_per_side: int = 10_000
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
    #: Updates carrying no sequence number at all (Coinbase's level2_batch)
    #: whose exchange timestamp was behind what this book already holds, and
    #: were therefore dropped rather than applied. See :meth:`_check_unordered`
    #: — this proves the dropped message was not newer than what we have, and
    #: nothing stronger.
    out_of_order_dropped: int = 0
    #: Times this book's stored level count exceeded ``max_levels_per_side``.
    #: Distinct from ``sequence_gaps``: the feed did not skip anything here,
    #: storage simply grew past the trusted bound. See :class:`BookOverflowError`.
    overflow_count: int = 0
    #: Set when a gap is seen, cleared by the next checkpoint.
    needs_resync: bool = False
    #: Why this book is currently untrusted (TIDAL-L2), bounded and
    #: diagnostic only -- nothing here ever gates trading behavior. ``None``
    #: while the book is synced or has never been invalidated. Set by
    #: :meth:`invalidate`, :meth:`_desync` and :meth:`_check_storage_bound`
    #: alike, so a disconnect, a sequence gap and a storage overflow are
    #: equally visible instead of only the first path anyone happened to add
    #: a message to; cleared the moment a fresh snapshot re-establishes sync.
    invalid_reason: str | None = None
    #: True between a snapshot and the first delta applied on top of it.
    #: Range-sequenced feeds relax the continuity rule for exactly that one
    #: update; see :meth:`apply_delta`.
    awaiting_first_delta: bool = False

    # -- mutation ----------------------------------------------------------

    def apply_snapshot(self, snapshot: OrderBookSnapshot) -> None:
        """Replace the book from a fresh checkpoint.

        Raises :class:`BookOverflowError` if the snapshot exceeds
        ``max_levels_per_side`` on either side -- checked, deliberately,
        *before* anything is copied into ``self.bids``/``self.asks`` (Batch 5
        final pre-commit correction). A delta's possible overshoot is
        naturally tiny (however many levels one venue message carries, at
        most a few thousand in practice), so building the dicts and checking
        their size afterwards (:meth:`_check_storage_bound`, used by
        :meth:`apply_delta`) is cheap either way. A snapshot is different in
        kind: it is a full checkpoint that can legitimately be enormous, and
        an oversized one -- ten, a hundred times the bound -- must never even
        momentarily become resident in this book's own authoritative storage,
        or the safety bound this whole mechanism exists to provide would be
        defeated by the exact case it is meant to catch. The check therefore
        runs against the incoming message's own level counts, which can only
        ever be an upper bound on the eventual distinct-price dict (duplicate
        prices or explicit zero-size deletions can only shrink it further) --
        a conservative, safe pre-flight that never requires building the
        oversized structure to know it is oversized.

        On overflow, this book's prior state is left completely untouched:
        neither the new (oversized) content nor a partial/trimmed version of
        it is stored. That is not the destructive trimming TIDAL-M1 removed
        -- trimming silently keeps *some* levels while hiding that others
        existed; this rejects the *entire* snapshot, loudly, and asks for a
        fresh one instead. The previous authoritative book (whatever it
        was -- absent, or a still-valid earlier snapshot) is exactly as it
        was before this call.
        """
        incoming_bids = len(snapshot.bids)
        incoming_asks = len(snapshot.asks)
        if incoming_bids > self.max_levels_per_side or incoming_asks > self.max_levels_per_side:
            self.overflow_count += 1
            self.synced = False
            self.needs_resync = True
            self.awaiting_first_delta = False
            reason = (
                f"snapshot exceeded max_levels_per_side={self.max_levels_per_side} "
                f"(incoming bids={incoming_bids}, asks={incoming_asks})"
            )
            self.invalid_reason = reason[:_MAX_INVALID_REASON_LEN]
            raise BookOverflowError(
                f"{self.venue}:{self.symbol} {reason} -- rejected before copying into storage"
            )
        self.bids = {level.price: level.size for level in snapshot.bids if level.size > 0}
        self.asks = {level.price: level.size for level in snapshot.asks if level.size > 0}
        self.sequence = snapshot.sequence
        self.exchange_ts = snapshot.exchange_ts
        self.last_update_ts = snapshot.received_ts
        self.synced = True
        self.needs_resync = False
        self.awaiting_first_delta = True
        self.updates_applied += 1
        # A successful checkpoint re-establishes trust from scratch: whatever
        # explained the previous distrust no longer applies (TIDAL-L2).
        self.invalid_reason = None

    def _desync(self, detail: str) -> BookDesyncError:
        self.sequence_gaps += 1
        self.synced = False
        self.needs_resync = True
        reason = f"sequence gap: {detail}"
        self.invalid_reason = reason[:_MAX_INVALID_REASON_LEN]
        return BookDesyncError(f"{self.venue}:{self.symbol} {reason}")

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
        if self.sequence is None:
            return True
        if delta.sequence <= self.sequence:
            # Duplicate or out-of-order replay of something already applied.
            return False
        expected = delta.prev_sequence
        if expected is not None and expected != self.sequence:
            raise self._desync(f"have {self.sequence}, update expects {expected}")
        return True

    def _check_unordered(self, delta: BookDelta) -> bool:
        """Validate a delta that carries no sequence number at all.

        This is Coinbase's actual shape on the public ``level2_batch``
        channel: neither the snapshot nor the incremental updates carry a
        sequence field, so there is no counter whose gap would prove a
        message went missing. See ``docs/`` and the Batch 3 report for the
        full account of what this channel does and does not let a client
        prove; in short, nothing here can detect a dropped update, and this
        method does not pretend otherwise.

        What timestamp ordering *can* prove is narrower and purely local: an
        incoming update whose ``exchange_ts`` is older than the newest one
        already applied to this book is not information we are missing — it
        is information we already have a newer version of. Applying it would
        regress the book to a state that predates data we already hold, which
        is strictly worse than doing nothing. Dropping it is therefore safe
        regardless of *why* it arrived late (batching jitter, network
        reordering, or something worse) — the safety of dropping does not
        depend on diagnosing the cause.

        What this does **not** do is treat an old timestamp as evidence of a
        gap, invalidate the book, or force a resubscribe. Reacting to a
        merely-suspicious signal that way would be inventing a confidence the
        signal cannot support — exactly the kind of fabricated proof of
        continuity this method exists to avoid. A dropped update is silently
        absorbed, not escalated.
        """
        if self.exchange_ts is None or delta.exchange_ts is None:
            return True
        if delta.exchange_ts < self.exchange_ts:
            self.out_of_order_dropped += 1
            return False
        return True

    def apply_delta(self, delta: BookDelta) -> None:
        """Apply an incremental update.

        Raises :class:`BookDesyncError` on a sequence gap, or its subclass
        :class:`BookOverflowError` if applying this update pushes stored
        levels past ``max_levels_per_side``; either way the caller marks the
        book unsynchronised and requests a checkpoint. Out-of-order or
        duplicate updates (sequence at or below what we hold, or — on a feed
        with no sequence numbers at all — older than what we hold) are
        dropped.
        """
        if not self.synced:
            raise BookDesyncError(f"{self.venue}:{self.symbol} has no snapshot yet")
        if delta.covers_range:
            applicable = self._check_range(delta)
        elif delta.sequence is not None:
            applicable = self._check_point(delta)
        else:
            applicable = self._check_unordered(delta)
        if not applicable:
            return
        for level in delta.bids:
            self._set(self.bids, level)
        for level in delta.asks:
            self._set(self.asks, level)
        self._check_storage_bound()
        if delta.sequence is not None:
            self.sequence = delta.sequence
        self.exchange_ts = delta.exchange_ts
        self.last_update_ts = delta.received_ts
        self.updates_applied += 1
        self.awaiting_first_delta = False

    def _check_storage_bound(self) -> None:
        """Fail the book closed if stored levels exceed the safety bound.

        Exactly at the bound is valid (a book that legitimately holds
        ``max_levels_per_side`` distinct prices is not a problem); one level
        past it is not — there is no partial-credit "keep the first N and
        drop the rest" here, because that is exactly the destructive trimming
        TIDAL-M1 removed. The only correct response to "storage exceeded what
        is trusted to hold correctly" is to stop trusting this book and let a
        fresh, bounded snapshot re-establish it (see :class:`BookOverflowError`).

        Used only by :meth:`apply_delta`: it checks the dicts *after*
        applying one delta's levels, which is safe because a single venue
        message's possible overshoot is inherently tiny (however many levels
        one message carries). :meth:`apply_snapshot` cannot use this
        check-after-building approach — a checkpoint can legitimately be
        enormous, so it preflights the incoming message's own level counts
        *before* ever touching ``self.bids``/``self.asks`` (Batch 5 final
        pre-commit correction) rather than building the dicts first. See its
        own docstring.
        """
        over_bids = len(self.bids) > self.max_levels_per_side
        over_asks = len(self.asks) > self.max_levels_per_side
        if not over_bids and not over_asks:
            return
        self.overflow_count += 1
        self.synced = False
        self.needs_resync = True
        self.awaiting_first_delta = False
        reason = (
            f"delta exceeded max_levels_per_side={self.max_levels_per_side} "
            f"(bids={len(self.bids)}, asks={len(self.asks)})"
        )
        self.invalid_reason = reason[:_MAX_INVALID_REASON_LEN]
        raise BookOverflowError(f"{self.venue}:{self.symbol} {reason}")

    @staticmethod
    def _set(side: dict[float, float], level: PriceLevel) -> None:
        if level.size <= 0:
            side.pop(level.price, None)
        else:
            side[level.price] = level.size

    def invalidate(self, reason: str = "") -> None:
        self.synced = False
        self.needs_resync = True
        self.awaiting_first_delta = False
        self.invalid_reason = reason[:_MAX_INVALID_REASON_LEN] if reason else None

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
        """Best-first levels on one side, trimmed to ``depth``.

        ``depth=None`` (the default) trims to ``max_depth`` rather than
        returning the whole book — every existing caller wants a bounded read,
        and "no limit" is not a case anything here needs. The full book is
        still reachable directly via ``self.bids``/``self.asks`` for the rare
        caller that genuinely wants that (none does today).
        """
        source = self.bids if side is Side.BUY else self.asks
        prices = sorted(source, reverse=side is Side.BUY)
        limit = self.max_depth if depth is None else depth
        prices = prices[:limit]
        return [PriceLevel(price=p, size=source[p]) for p in prices]

    def snapshot(self, now_ms: Millis, *, depth: int | None = None) -> OrderBookSnapshot:
        return OrderBookSnapshot(
            venue=self.venue,
            symbol=self.symbol,
            exchange_ts=self.exchange_ts or now_ms,
            received_ts=self.last_update_ts or now_ms,
            sequence=self.sequence,
            bids=self.levels(Side.BUY, depth),
            asks=self.levels(Side.SELL, depth),
            is_checkpoint=True,
        )
