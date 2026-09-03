"""Binance depth synchronisation: the snapshot/delta handshake.

A Binance diff-depth stream is not self-starting. It delivers updates from
whenever you happened to connect, with no base state, so a consumer that
applies them to an empty book is applying deltas to nothing. The documented
procedure is a handshake between the stream and a REST checkpoint:

    1. open the stream and buffer what arrives
    2. GET /api/v3/depth, note ``lastUpdateId`` (L)
    3. drop buffered messages entirely at or below L
    4. the first message to keep must satisfy ``U <= L + 1 <= u``
    5. apply the snapshot, then replay the kept messages in order

Step 4 is the part worth stating plainly: the snapshot and the stream are
independent, so the snapshot can land *behind* the buffer (its ids already
passed — retry with a newer one) or *ahead* of it (fine, the overlap is
discarded). Only the straddling case proves the two are contiguous, and
without that proof the book is built on a hole.

This module owns that handshake and nothing else. It performs no I/O itself:
the snapshot fetch arrives as an injected coroutine, which is what lets the
whole protocol be tested against synthetic sequences with no network and no
clock assumptions.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from core.clock import Clock
from core.models.common import StrEnum
from core.models.market import OrderBookSnapshot
from venues.base.adapter import ReconnectPolicy
from venues.base.messages import BookDelta

log = logging.getLogger(__name__)

#: Fetches a REST depth checkpoint for one canonical symbol.
FetchSnapshot = Callable[[str], Awaitable[OrderBookSnapshot]]
#: Publishes a normalised message onward (the adapter's ``emit``).
Emit = Callable[[OrderBookSnapshot | BookDelta], Awaitable[None]]


class SyncState(StrEnum):
    #: No snapshot, and none being fetched.
    IDLE = "IDLE"
    #: A fetch is in flight; deltas are being buffered, not emitted.
    SYNCING = "SYNCING"
    #: Synchronised; deltas pass straight through.
    LIVE = "LIVE"
    #: Retries exhausted. Stays unusable until the feed reconnects.
    FAILED = "FAILED"


class DepthSynchronizer:
    """Drives one symbol's book from unsynchronised to live and back.

    One instance per symbol. Nothing here is shared between symbols, which is
    what makes a BTC failure structurally incapable of touching ETH.
    """

    def __init__(
        self,
        symbol: str,
        fetch_snapshot: FetchSnapshot,
        emit: Emit,
        clock: Clock,
        *,
        max_buffer: int = 2_000,
        max_attempts: int = 8,
        min_interval_s: float = 5.0,
        backoff: ReconnectPolicy | None = None,
    ) -> None:
        self.symbol = symbol
        self._fetch = fetch_snapshot
        self._emit = emit
        self.clock = clock
        #: Hard cap on buffered deltas. Overflow abandons the attempt rather
        #: than dropping from the middle: a buffer with a hole in it cannot be
        #: replayed, and pretending otherwise is the corruption this exists to
        #: prevent.
        self.max_buffer = max_buffer
        self.max_attempts = max_attempts
        #: Floor on the interval between REST checkpoints for this symbol, so
        #: a book that desyncs on every message still fetches at a walking
        #: pace.
        self.min_interval_s = min_interval_s
        self.backoff = backoff or ReconnectPolicy(initial_s=1.0, factor=2.0, max_s=30.0)

        self.state = SyncState.IDLE
        self._buffer: list[BookDelta] = []
        self._task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self._last_fetch_ms: int | None = None

        # Observability. Every one of these answers a question an operator
        # actually asks when a book will not come up.
        self.attempts = 0
        self.resyncs = 0
        self.snapshots_applied = 0
        self.buffered_replayed = 0
        self.buffered_discarded = 0
        self.buffer_overflows = 0
        self.stale_snapshots = 0
        self.fetch_failures = 0
        self.last_error: str | None = None

    # -- inbound -----------------------------------------------------------

    async def on_delta(self, delta: BookDelta) -> None:
        """Handle one streamed depth update."""
        start = False
        async with self._lock:
            if self.state is SyncState.LIVE:
                emit = True
            elif self.state is SyncState.FAILED:
                # Nothing to do until the feed reconnects and resets us. The
                # delta is dropped deliberately: emitting it would only make
                # TIDAL raise a desync it can do nothing about.
                return
            else:
                self._buffer_delta(delta)
                emit = False
                start = self.state is SyncState.IDLE
                if start:
                    self.state = SyncState.SYNCING
        if emit:
            await self._emit(delta)
        elif start:
            self._spawn_fetch()

    async def request_resync(self, reason: str = "") -> None:
        """Re-establish this book from a fresh checkpoint.

        Idempotent while a fetch is already in flight, which is what makes a
        burst of desync reports cost one REST call rather than one each.
        """
        async with self._lock:
            if self.state is SyncState.SYNCING:
                return
            self.resyncs += 1
            self.state = SyncState.SYNCING
            self.backoff.reset()
            self.attempts = 0
            log.info(
                "resynchronising book",
                extra={"symbol": self.symbol, "reason": reason},
            )
        self._spawn_fetch()

    def reset(self) -> None:
        """Drop all state. Called when the socket reconnects.

        A new connection means a new stream position, so the buffer and any
        in-flight attempt describe a stream that no longer exists.
        """
        if self._task is not None and not self._task.done():
            self._task.cancel()
        self._task = None
        self._buffer.clear()
        self.backoff.reset()
        self.attempts = 0
        self.state = SyncState.IDLE

    # -- internals ---------------------------------------------------------

    def _buffer_delta(self, delta: BookDelta) -> None:
        if len(self._buffer) >= self.max_buffer:
            # The oldest buffered update is the one the snapshot has to join
            # onto. Losing it means we can no longer prove contiguity, so the
            # honest move is to throw the attempt away and start again with a
            # fresh snapshot, not to keep a buffer we know has a hole in it.
            self.buffer_overflows += 1
            self.buffered_discarded += len(self._buffer)
            self._buffer.clear()
            log.warning(
                "depth buffer overflowed while syncing; restarting handshake",
                extra={"symbol": self.symbol, "max_buffer": self.max_buffer},
            )
        self._buffer.append(delta)

    def _spawn_fetch(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(
            self._synchronise(), name=f"binance-sync-{self.symbol}"
        )

    async def _synchronise(self) -> None:
        """Fetch checkpoints until one joins onto the buffer, or give up."""
        while self.attempts < self.max_attempts:
            self.attempts += 1
            await self._respect_min_interval()
            try:
                snapshot = await self._fetch(self.symbol)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.fetch_failures += 1
                self.last_error = f"{type(exc).__name__}: {exc}"
                log.warning(
                    "depth checkpoint fetch failed",
                    extra={"symbol": self.symbol, "error": str(exc)},
                )
                await self.clock.sleep(self.backoff.next_delay())
                continue

            if await self._try_activate(snapshot):
                return
            # The snapshot was older than the buffer we already hold: its ids
            # are behind the earliest message we kept, so there is a hole
            # between them. A newer checkpoint closes it.
            self.stale_snapshots += 1
            self.last_error = "checkpoint older than buffered updates"
            await self.clock.sleep(self.backoff.next_delay())

        async with self._lock:
            self.state = SyncState.FAILED
            self._buffer.clear()
        log.error(
            "gave up synchronising book",
            extra={"symbol": self.symbol, "attempts": self.attempts},
        )

    async def _respect_min_interval(self) -> None:
        now = self.clock.now_ms()
        if self._last_fetch_ms is not None:
            elapsed_s = (now - self._last_fetch_ms) / 1000.0
            if elapsed_s < self.min_interval_s:
                await self.clock.sleep(self.min_interval_s - elapsed_s)
        self._last_fetch_ms = self.clock.now_ms()

    async def _try_activate(self, snapshot: OrderBookSnapshot) -> bool:
        """Reconcile ``snapshot`` against the buffer and go live if they join.

        Returns False when the snapshot is too old to be joined onto what we
        buffered, which is a retry, not a failure.
        """
        async with self._lock:
            last_id = snapshot.sequence
            if last_id is None:
                # No checkpoint id means no way to prove contiguity. Treat the
                # snapshot as the whole truth and start clean from it.
                self._buffer.clear()
                replay: list[BookDelta] = []
            else:
                kept = [d for d in self._buffer if (d.sequence or 0) > last_id]
                self.buffered_discarded += len(self._buffer) - len(kept)
                if kept and not self._joins(kept[0], last_id):
                    # kept[0] starts past last_id + 1: the updates in between
                    # were never seen. Keep buffering and fetch again.
                    return False
                replay = kept
                self._buffer = []

            self.snapshots_applied += 1
            self.buffered_replayed += len(replay)
            self.backoff.reset()
            self.last_error = None

        # The state stays SYNCING across these emits, deliberately. Emitting
        # yields, and a delta arriving in that window must still be buffered —
        # if it went straight out it would overtake the snapshot it belongs
        # after. LIVE is set only once the buffer has drained, in
        # _drain_late_arrivals.
        await self._emit(snapshot)
        for delta in replay:
            await self._emit(delta)
        await self._drain_late_arrivals()
        return True

    @staticmethod
    def _joins(delta: BookDelta, last_id: int) -> bool:
        """Binance's rule: ``U <= lastUpdateId + 1 <= u``."""
        first = delta.first_sequence
        final = delta.sequence
        if first is None or final is None:
            return True
        return first <= last_id + 1 <= final

    async def _drain_late_arrivals(self) -> None:
        """Emit what buffered during the flush, then open the direct path.

        Going LIVE is the last step rather than the first: the switch is only
        safe once there is nothing left in front of the deltas that would take
        it.
        """
        while True:
            async with self._lock:
                if not self._buffer:
                    self.state = SyncState.LIVE
                    return
                pending, self._buffer = self._buffer, []
            for delta in pending:
                await self._emit(delta)


__all__ = ["DepthSynchronizer", "SyncState"]
