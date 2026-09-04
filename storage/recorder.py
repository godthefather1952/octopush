"""Event recorder.

Attaches to the bus as middleware and writes every published event to the
store.  Buffered, because a synchronous write per event would put storage
latency on the trading path; flushed on a size or age trigger and on shutdown.

WHAT "RECORDED" MEANS (Phase 2 Batch 2)
=======================================
Three rules, each of which the previous implementation broke:

**A transient write failure is not event loss (P2-7).** The old flush took
the buffer, cleared it, attempted the write, and on failure counted the batch
as ``events_lost`` — the events themselves were already gone. A database that
was briefly unreachable therefore destroyed history permanently. Now events
stay in ``_pending`` until storage *confirms* them; a failure leaves them
queued for the next attempt, marks the recorder unhealthy, and counts a
failure, not a loss. ``events_lost`` means "accepted and then permanently
abandoned", and nothing else.

**Retrying is safe even when the outcome was never learned.** If the database
committed a batch and the connection died before the caller heard so, the
recorder retries the identical batch. The stores treat a byte-identical
re-delivery as a no-op and a *different* event under a known id as an
``EventIdCollision``, so the retry converges on exactly one copy of each
event rather than duplicating or silently replacing anything.

**What is recorded is what was accepted (P2-12).** ``record()`` stores a deep
snapshot. The bus hands the same ``Event`` instance to every subscriber after
this middleware returns, and an ``Event`` is mutable: without the copy, a
later subscriber touching ``payload`` would rewrite history that had already
been accepted, and the recording would describe something that never
happened.

Retaining failed batches introduces the opposite danger — an outage that
grows memory without bound — so acceptance is bounded by
``max_pending_events``. When the bound is reached the recorder *fails closed*:
``record()`` raises, which the bus turns into a refused publish (the event is
never admitted and never dispatched), storage health goes bad, and the kill
switch stops trading. Nothing already accepted is ever dropped, trimmed or
overwritten to make room.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from core.bus import EventBus
from core.clock import Clock
from core.events import Event
from core.models.common import Millis, new_id
from storage.base import EventStore, SessionStatus

log = logging.getLogger(__name__)

SERVICE = "RECORDER"


class RecorderAtCapacity(RuntimeError):
    """Raised by ``record()`` when pending history has hit its hard bound.

    Deliberately an exception rather than a drop: the bus aborts the publish
    that hit it, so the event is never admitted and never dispatched, and no
    accepted history is discarded to make room. Trading stops because storage
    health degrades, which is the intended outcome of "we can no longer prove
    what happened".
    """


@dataclass
class Recorder:
    store: EventStore
    clock: Clock
    session_id: str = field(default_factory=lambda: new_id("session"))
    buffer_size: int = 200
    flush_interval_ms: int = 1_000
    label: str = ""
    config_hash: str = ""
    #: Hard ceiling on accepted-but-unconfirmed events. Reached only during a
    #: storage outage; see the module docstring for why this fails closed
    #: instead of trimming. At ~6 KB per realistic event this bounds the
    #: recorder at roughly 300 MiB.
    max_pending_events: int = 50_000
    #: Accepted events awaiting durable confirmation. Snapshots, never the
    #: caller's live objects.
    _pending: list[Event] = field(default_factory=list)
    _last_flush: Millis = 0
    #: True while a flush is awaiting storage. Pending events are no longer
    #: removed before the write, so without this a second flush starting
    #: mid-write would re-send the in-flight batch alongside the newer
    #: events -- doubling the work and, against a slow store, blocking the
    #: newer publish behind the older one's I/O.
    _flushing: bool = False
    _started: bool = False
    _finalized: bool = False
    events_recorded: int = 0
    failures: int = 0
    healthy: bool = True
    #: Consecutive failed flushes. One transient error is not an outage; a
    #: run of them means the audit trail is at risk.
    consecutive_failures: int = 0
    #: Events accepted and then PERMANENTLY abandoned — only ever set when a
    #: controlled shutdown gives up on pending history. A failed flush that
    #: is later retried successfully never contributes here.
    events_lost: int = 0
    #: Publishes refused because pending history was at its hard bound.
    rejected_at_capacity: int = 0

    async def start(self) -> None:
        await self.store.open()
        await self.store.start_session(
            self.session_id, self.clock.now_ms(), self.label, self.config_hash
        )
        self._started = True
        self._last_flush = self.clock.now_ms()

    async def stop(self) -> None:
        """Finalise the session, telling the truth about what it contains.

        A session becomes COMPLETE only when every accepted event has been
        durably confirmed. If pending history remains after a final flush
        attempt, the session is finalised INCOMPLETE and those events are
        counted as lost, because this shutdown is where they stop being
        retryable.

        If even that write fails, the session is left OPEN. An OPEN session
        is honestly "we do not know"; a COMPLETE one would be a lie, and
        those are the only two outcomes available once storage is gone.
        """
        if self._finalized:
            # Already finalised: Platform.stop() and an explicit
            # recorder.stop() can both run. Re-finalising would be refused by
            # the store anyway; closing twice is harmless.
            await self.store.close()
            return

        try:
            await self.flush()
        except Exception:  # pragma: no cover - flush already swallows
            log.exception("final flush raised")

        pending = len(self._pending)
        if pending:
            self.events_lost += pending
            self._pending.clear()
            status = SessionStatus.INCOMPLETE
            reason = (
                f"{pending} accepted events were never durably confirmed "
                f"({self.consecutive_failures} consecutive flush failures)"
            )
        else:
            status = SessionStatus.COMPLETE
            reason = ""

        try:
            await self.store.finalize_session(
                self.session_id,
                self.clock.now_ms(),
                status=status,
                events_lost=self.events_lost,
                failure_reason=reason,
            )
            self._finalized = True
        except Exception as exc:
            # Case (E): everything may be durably present, but we could not
            # record the claim. Leaving the session OPEN is the honest
            # outcome -- replay refuses it rather than trusting it.
            self.healthy = False
            log.error(
                "could not finalize session; leaving it OPEN",
                extra={"session_id": self.session_id, "error": str(exc)},
            )
        finally:
            await self.store.close()

    def attach(self, bus: EventBus) -> None:
        bus.add_middleware(self.record)

    @property
    def unpersisted(self) -> int:
        """Events accepted but not yet durably confirmed. The exposure window."""
        return len(self._pending)

    @property
    def at_capacity(self) -> bool:
        return len(self._pending) >= self.max_pending_events

    def flush_is_due(self) -> bool:
        return bool(self._pending) and (
            len(self._pending) >= self.buffer_size
            or self.clock.now_ms() - self._last_flush >= self.flush_interval_ms
        )

    async def flush_if_due(self) -> None:
        """Flush on the age trigger without needing a new event to arrive.

        The age check used to live only in ``record()``, so it fired only
        when traffic did. A quiet period — a halted strategy, an out-of-hours
        session, a kill switch that stopped new trades — left the last events
        before the pause sitting in memory indefinitely, and those are
        precisely the events that explain why the platform went quiet.

        Driven from the orchestrator's tick rather than a background task:
        a task sleeping on real time cannot be stepped by ManualClock, and
        every determinism guarantee here depends on the clock being the only
        source of time.
        """
        if self.flush_is_due():
            await self.flush()

    async def record(self, event: Event) -> None:
        """Accept an event for recording, as bus middleware.

        Stores a deep snapshot: the bus hands the same mutable instance to
        every later subscriber, and history must describe the event as it
        was accepted (P2-12).
        """
        if self.at_capacity:
            self.rejected_at_capacity += 1
            self.healthy = False
            raise RecorderAtCapacity(
                f"recorder holds {len(self._pending)} unconfirmed events "
                f"(max_pending_events={self.max_pending_events}); refusing "
                "further history rather than discarding any of it"
            )
        self._pending.append(event.model_copy(deep=True))
        if self.flush_is_due():
            await self.flush()

    async def flush(self) -> None:
        """Attempt to confirm every pending event durably.

        The pending list is NOT cleared before the write. On failure the
        events remain queued for the next attempt — a transient outage costs
        latency and health, never history (P2-7).
        """
        if self._flushing:
            # Another attempt is already in flight over these same events.
            # Whatever arrived since will be covered by the next flush.
            return
        if not self._pending:
            self._last_flush = self.clock.now_ms()
            return
        # A stable snapshot of what this attempt covers: record() may append
        # more while the write is in flight, and those are not part of this
        # attempt's confirmation.
        batch = list(self._pending)
        self._flushing = True
        try:
            await self.store.append_many(self.session_id, batch)
        except Exception as exc:
            self.failures += 1
            self.consecutive_failures += 1
            self.healthy = False
            # Deliberately NOT events_lost: these are still held, and the
            # next flush retries them. The identical retry is safe because
            # the stores treat a byte-identical re-delivery as a no-op.
            log.error(
                "failed to persist events; retaining them for retry",
                extra={
                    "count": len(batch),
                    "pending": len(self._pending),
                    "error": str(exc),
                },
            )
        else:
            del self._pending[: len(batch)]
            self.events_recorded += len(batch)
            self.healthy = True
            self.consecutive_failures = 0
        finally:
            self._flushing = False
            self._last_flush = self.clock.now_ms()
