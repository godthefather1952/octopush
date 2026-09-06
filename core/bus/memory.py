"""In-process event bus.

Deterministic by construction: events are appended to a FIFO and dispatched to
subscribers in registration order, one event at a time.  Events published by a
handler are appended behind the current queue, so a replay of the same input
produces the same interleaving every time.

BACKPRESSURE (TIDAL-M7)
=======================
The queue has a **hard, finite ceiling**: ``max_pending + cascade_reserve``.
Nothing — external traffic or a cascade of any depth — can ever push the
queue past that number. There are two ways of approaching it, because the two
kinds of publisher cannot be treated the same way without deadlocking:

* An **external publisher** (a venue adapter, the orchestrator's tick, a
  test) is not itself running inside this bus's own dispatch. It awaits
  capacity when the queue reaches ``max_pending``, rather than growing the
  queue further or dropping the event. That wait is safe because such a
  publisher runs on a *different* task from whichever task is popping the
  queue: the dispatcher makes independent progress and eventually frees room,
  waking the waiter. No event is ever silently evicted or dropped for this
  reason.

* A publish made from *inside* a handler currently being dispatched by this
  same bus — the causal cascades this platform relies on (a book delta
  causing TIDAL to publish MARKET_STATE, an opportunity causing an intent,
  and so on) — **cannot** wait for capacity the way an external publisher
  does: the single dispatch loop (whether driven by ``_run()`` or
  synchronously by ``drain()``) is the *only* thing that can ever shrink the
  queue, so a nested publish blocking on it would be waiting on the very call
  stack that is the only path to shrinking it — a guaranteed deadlock, on the
  one task there is no other task available to break.

  Earlier in Batch 5 this was resolved by exempting cascades from the bound
  entirely — correct about deadlock-freedom, but it meant ``max_pending`` was
  not actually a hard ceiling: an accidental or future recursive publish path
  could grow the queue without limit. The corrected design instead gives
  cascades a small, separate, **also-finite** allowance —
  ``cascade_reserve`` — layered on top of ``max_pending``. A cascade publish
  enqueues immediately as long as the queue is below the combined ceiling.
  Once the reserve itself is exhausted, the cascade publish does not wait
  (that would still deadlock) and does not silently vanish either: it raises
  :class:`CascadeCapacityExceeded`, which propagates out of the publishing
  handler into the bus's existing per-subscriber failure isolation (clause
  5) — every *other* subscription still receives the event that triggered
  the cascade, and the failure is counted twice, generically
  (``Subscription.errors``) and specifically (``cascade_overflow``), so it is
  observable, never a silent loss. This makes ``max_pending +
  cascade_reserve`` a true mathematical ceiling on queue size: no sequence of
  publishes, however deeply or accidentally recursive, can exceed it.

The one place capacity is abandoned rather than waited for is shutdown: if
``stop()`` runs while an external publisher is blocked on capacity, that
publisher wakes immediately, contributes to ``discarded_at_stop`` (clause 7's
existing counter — this is the same category of loss, just caught one step
earlier), and returns without enqueueing or being recorded (see "ACCEPTANCE
POINT" below). Waiting through a shutdown would itself hang forever, which is
worse.

ACCEPTANCE POINT (TIDAL-M7 pre-commit correction)
==================================================
Recording (the ``Recorder`` attaches as bus middleware, see
``storage/recorder.py``) and enqueueing must never disagree about which
events actually entered this bus. An earlier version of this file ran
middleware unconditionally before the capacity check, so an event could be
durably recorded and then never enqueued at all (discarded while its
publisher was still blocked on capacity at shutdown) — live and replay would
then disagree about whether that event ever happened.

The fix defines one explicit acceptance point: an event is ACCEPTED the
moment a capacity slot is *reserved* — immediately if the queue has room,
after a successful capacity wait (not stopped), or under the cascade reserve
for a reentrant publish. That reservation happens synchronously, inside the
same locked section as the check that authorised it (see "CONCURRENT
ADMISSION" below) — a publish that never gets this far (rejected at
admission, or abandoned at shutdown) never reaches middleware and is
therefore never recorded.

Middleware then runs with the lock released (it can be slow — real I/O for
the recorder — and must not stall every other publisher's admission decision
while it runs); the reservation already accounts for the event's capacity
regardless of how long that takes. If middleware raises, or the publishing
task is cancelled, before the event is appended, the reservation is released
(``reservation_released``) and capacity frees for the next waiter — the
event never reaches ``_queue`` and is never dispatched. What this bus
guarantees is that an event which never reaches *any* middleware is never
recorded; it does not retroactively undo a side effect an individual
middleware already committed to its own state before a *later* middleware
(if more than one is attached) raised — that is that middleware's own
responsibility. The one middleware this codebase attaches
(``Recorder.record``) never raises in normal operation (storage failures are
caught internally and tracked via ``Recorder.failures``, not propagated), so
this is a defensive guarantee for cancellation, not a routine path.

CONCURRENT ADMISSION (TIDAL-M7 final pre-commit correction)
=============================================================
The capacity check above and the increment of ``_reserved`` happen inside one
``async with self._capacity`` block, with no ``await`` of anything else
between them (``self._capacity.wait()`` is the only await inside that block,
and it re-acquires the lock — and re-enters the ``while`` check — before
returning). A version that checked capacity, then ran middleware, and only
*then* incremented an accepted count would let arbitrarily many concurrent
publishers each observe "room available" before any of them had actually
consumed it — each publish() call is a distinct task, and ``await mw(event)``
is a suspension point every other task can run through in the meantime.
Reserving synchronously, as part of the same critical section as the check,
closes that race regardless of how many publishers race concurrently: the
true bound enforced everywhere is ``len(self._queue) + self._reserved``, not
``len(self._queue)`` alone, so a reservation "holds the publisher's place in
line" for the entire duration middleware takes to run.

ORDERED COMMIT (Phase 2 Batch 1.2 -- P2-13)
============================================
Reserving a capacity slot atomically (above) makes admission race-free, but
it does not by itself make *commit order* (append-to-``_queue`` order) match
*admission order*. Middleware runs with the capacity lock released — it must,
since it can be slow real I/O and must not stall every other publisher's
admission decision — and two concurrent publishers' middleware calls can
finish in either order. Before this section existed, whichever publisher's
middleware happened to finish first was appended first, regardless of which
was admitted first: publisher A (admitted first, assigned the lower
``Event.sequence``) could still be slower through middleware than publisher
B (admitted second), so B's event could reach ``_queue`` — and so be
dispatched to TIDAL — before A's, even though A logically came first. Any
watermark or ordering claim built on ``Event.sequence`` being the true
delivery order (replay's own tick-visibility cut, most directly) is false
under that behaviour.

The fix is a second, purely internal counter — ``_admission_seq`` —
assigned to every ``publish()`` call at the exact moment it is admitted
(inside the same locked section as the reservation, so its assignment order
is itself race-free). It is deliberately NOT ``Event.sequence``: a caller
(replay, most notably) can pre-set ``Event.sequence`` to a value carried over
from a completely different bus instance's history, so ``Event.sequence``
cannot be trusted to reflect *this* instance's own admission order. Once an
event's middleware completes (or aborts — raises, or its task is cancelled),
it is placed in ``_ready`` keyed by its own admission id, and
``_flush_ready_locked`` appends the longest *contiguous prefix* of admission
ids, starting from ``_next_commit``, that has already resolved — abort or
success — releasing each one's reservation as it commits. A later-finishing
publisher's event physically cannot overtake an earlier one still in
middleware: it is held in ``_ready`` until every earlier admission id has
resolved, then flushed in the same call that finally unblocks the logjam
(whether that is its own completion or an earlier admission's).

This guarantees, for every event that reaches this bus instance's ``_queue``
at all: admission order == queue order == dispatch order. A permanently
stuck earlier publisher (middleware that never resolves at all — never
returns, never raises, never gets cancelled) still blocks every later one
from committing; nothing can fix that without abandoning the ordering
guarantee entirely, and it is no worse than today's behaviour, in which that
same stuck publisher already blocks callers waiting on ``drain()``/
``wait_idle()`` from ever correctly observing quiescence. See "IDLE
CORRECTNESS" below for the related fix to ``drain()``/``wait_idle()`` needed
once ``_reserved`` can be nonzero for a *committed* (not just mid-middleware)
duration.

IDLE CORRECTNESS (Phase 2 Batch 1.2)
=====================================
``_run()``'s and ``drain()``'s "is there still work outstanding" check used
to test ``self._queue`` alone. That was already an approximation before this
section existed: a publisher that had reserved capacity but not yet finished
middleware left ``_queue`` empty while genuinely more work was still coming,
so ``wait_idle()`` (hence ``drain()`` under a running background dispatcher)
could return while a publish was still in flight and about to append. Now
that ordered commit can hold a *finished* middleware's event in ``_ready``
behind an earlier, still-unresolved admission, that same gap would silently
widen. The fix: "idle" means ``not self._queue and self._reserved == 0`` —
nothing queued and nothing admitted-but-uncommitted, whether mid-middleware
or waiting in ``_ready`` for its commit turn — and every place that changes
either quantity notifies ``self._capacity`` so waiters (``drain()`` without a
background dispatcher now waits on the condition variable rather than
busy-looping) observe the change immediately rather than on a polling delay.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import logging
from collections import deque
from collections.abc import Iterable

from core.bus.base import (
    DEFAULT_DELIVERY_WINDOW,
    DeliveryOutcomeWindow,
    EventBus,
    Handler,
    Middleware,
    Subscription,
)
from core.events import Event, EventType

log = logging.getLogger(__name__)


class CascadeCapacityExceeded(RuntimeError):
    """Raised when a reentrant (cascade) publish would exceed the hard queue
    ceiling (``max_pending + cascade_reserve``).

    A cascade publish cannot block for capacity — see the module docstring —
    so once even the reserved cascade allowance is exhausted, refusing the
    publish outright is the only safe response left. This propagates out of
    the handler that attempted the cascade publish and is caught by the
    bus's existing per-subscriber failure isolation (clause 5): the event
    that triggered the cascade is still delivered to every other
    subscription, and this failure is counted both generically
    (``Subscription.errors``) and specifically (``cascade_overflow``).
    """


class InMemoryEventBus(EventBus):
    #: ~10,000 events is roughly 100-150 seconds of headroom at the platform's
    #: measured live rate (tens of book deltas/second per symbol across two
    #: venues, per the Phase 1 audit) — enough to absorb a stalled recorder or
    #: a GC pause without real backpressure ever engaging, while a genuinely
    #: stuck consumer still applies backpressure well before memory becomes a
    #: concern. Measured via ``tracemalloc`` at ~6 KB per realistic
    #: ``BookDelta`` event (10 levels/side, as actually constructed by a venue
    #: adapter), so the bound represents ~58 MiB resident at full — see the
    #: Batch 5 report for the measurement.
    DEFAULT_MAX_PENDING = 10_000
    #: Extra, also-finite room reserved exclusively for cascade/reentrant
    #: publishes on top of ``max_pending``. Cascade fan-out per external event
    #: is architecturally small (a handful of derived events, not a
    #: proportional echo of raw feed volume — TIDAL publishing MARKET_STATE
    #: off a book delta, an opportunity producing an intent, and so on), so
    #: this can be — and is — much smaller than ``max_pending`` while still
    #: comfortably covering normal fan-out. At ~6 KB/event (see
    #: ``DEFAULT_MAX_PENDING``'s docstring) this adds ~6 MiB worst case. It
    #: exists purely as a hard ceiling for the pathological case (an
    #: accidental or future infinite-recursion publish path) — normal
    #: operation should never come close to exhausting it.
    DEFAULT_CASCADE_RESERVE = 1_000

    def __init__(
        self,
        *,
        raise_on_handler_error: bool = False,
        max_pending: int | None = None,
        cascade_reserve: int | None = None,
        error_rate_window: int = DEFAULT_DELIVERY_WINDOW,
    ) -> None:
        self._queue: deque[Event] = deque()
        self._max_pending = self.DEFAULT_MAX_PENDING if max_pending is None else max_pending
        if self._max_pending <= 0:
            raise ValueError(f"max_pending must be positive, got {self._max_pending}")
        self._cascade_reserve = (
            self.DEFAULT_CASCADE_RESERVE if cascade_reserve is None else cascade_reserve
        )
        if self._cascade_reserve <= 0:
            raise ValueError(f"cascade_reserve must be positive, got {self._cascade_reserve}")
        #: The one true, finite ceiling on queue size — see the module
        #: docstring. Nothing, external or cascaded, can ever push
        #: ``len(self._queue)`` past this.
        self._hard_ceiling = self._max_pending + self._cascade_reserve
        self._subs: list[Subscription] = []
        self._middleware: list[Middleware] = []
        self._task: asyncio.Task[None] | None = None
        self._running = False
        #: True once stop() has run. Deliberately distinct from ``_running``:
        #: a bus that is never start()-ed (the synchronous publish-then-drain
        #: pattern used throughout this test suite) has ``_running == False``
        #: from the moment it is constructed, and treating that as "stopped"
        #: would make every capacity wait on such a bus discard immediately
        #: instead of legitimately waiting for a drain() to free room.
        self._stopped = False
        self._idle = asyncio.Event()
        self._idle.set()
        #: Set whenever work is queued, so the dispatcher can block instead of
        #: spinning. A busy-wait here starves every other task on the loop —
        #: including the market feed, which then looks stale.
        self._work = asyncio.Event()
        #: Guards ``_queue`` capacity waits. Notified whenever the queue
        #: shrinks (a dispatch completes) or the bus stops.
        self._capacity = asyncio.Condition()
        #: Tasks currently executing a handler dispatched by this bus. A
        #: publish from one of these tasks is the reentrant/cascade case
        #: described in the module docstring and bypasses the capacity wait.
        self._dispatching_tasks: set[asyncio.Task] = set()
        self._seq = itertools.count(1)
        self._raise = raise_on_handler_error
        self.published_count = 0
        self.delivered_count = 0
        #: Clause 9. One entry per handler invocation, written at dispatch
        #: time. ``delivered_count`` and the per-subscription counters below
        #: stay lifetime totals for diagnostics; this is the health input.
        self._outcomes = DeliveryOutcomeWindow(error_rate_window)
        #: Publishes abandoned by stop() while blocked awaiting capacity —
        #: never enqueued at all, but the same category of loss as clause 7's
        #: existing discard-at-stop, so counted alongside it.
        self.discarded_at_stop = 0
        #: Number of times a publish had to wait for capacity at all (whether
        #: it went on to succeed or was abandoned at stop). Zero in normal
        #: operation; a nonzero, growing count is the health-visible signal
        #: that a consumer cannot keep up.
        self.backpressure_events = 0
        #: Times a cascade/reentrant publish was refused because the hard
        #: ceiling (``max_pending + cascade_reserve``) was reached. Zero in
        #: normal operation; nonzero means a handler's cascade fan-out (or an
        #: accidental recursive publish loop) is pushing on the one place
        #: this bus refuses to grow further rather than block or drop.
        self.cascade_overflow = 0
        #: Publishes ADMITTED (a capacity slot reserved, under ``_capacity``'s
        #: lock) but not yet appended to ``_queue`` -- currently running
        #: through middleware. Every capacity decision checks
        #: ``len(self._queue) + self._reserved``, not ``len(self._queue)``
        #: alone (Batch 5 final pre-commit correction): middleware runs with
        #: the lock released (it can be slow, and must not block every other
        #: publisher's admission check while it runs), so without counting
        #: reservations, multiple concurrent publishers could each observe
        #: "room available" before any of them had appended anything, and
        #: jointly admit more than ``max_pending``/``hard_ceiling`` events.
        #: Reserving synchronously, atomically with the check, closes that
        #: race regardless of how many publishers race concurrently.
        self._reserved = 0
        #: Times an admitted (reserved) publish never made it into the queue
        #: because its middleware raised or its task was cancelled. The
        #: reservation is released and this is counted so the condition is
        #: observable rather than a silent capacity leak.
        self.reservation_released = 0
        #: Internal admission-order counter -- see "ORDERED COMMIT" above.
        #: Deliberately distinct from ``_seq``/``Event.sequence``: assigned
        #: fresh to every publish() call on THIS bus instance, regardless of
        #: whether the event's own ``Event.sequence`` was pre-set by a
        #: caller (replay) carrying it over from a different bus instance's
        #: history.
        self._admission_seq = itertools.count(1)
        #: The next admission id whose resolution (success or abort) is
        #: still needed before anything can be appended to ``_queue``.
        self._next_commit = 1
        #: Admission id -> event, for a middleware that finished but is
        #: still waiting for every earlier admission id to resolve first.
        self._ready: dict[int, Event] = {}
        #: Admission ids whose middleware raised or was cancelled -- skipped
        #: (not appended) when their turn in commit order arrives.
        self._aborted: set[int] = set()

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run(), name="bus-dispatch")

    async def stop(self) -> None:
        # Clause 7: undispatched events are discarded, not delivered. Recorded
        # so the condition is observable rather than silent.
        self.discarded_at_stop += len(self._queue)
        self._running = False
        self._stopped = True
        # Wake anyone blocked awaiting capacity: with _stopped now True they
        # will abandon their publish (counted above them, in publish()) rather
        # than wait through a shutdown that will never free room on their
        # behalf.
        async with self._capacity:
            self._capacity.notify_all()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _run(self) -> None:
        while self._running:
            if not self._queue:
                # "Idle" means nothing queued AND nothing admitted-but-not-
                # yet-committed (see "IDLE CORRECTNESS" in the module
                # docstring) -- a publish still mid-middleware, or held in
                # `_ready` waiting for an earlier admission's commit turn,
                # both still represent work that is genuinely coming.
                if self._reserved == 0:
                    self._idle.set()
                self._work.clear()
                # Wake on the next publication (or the next commit, if
                # something is only waiting on `_reserved`); the timeout
                # only bounds how long stop() waits and how promptly a
                # reserved-but-not-yet-queued commit is re-checked.
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._work.wait(), timeout=0.05)
                continue
            await self._dispatch_one()

    # -- pub/sub -----------------------------------------------------------

    def add_middleware(self, middleware: Middleware) -> None:
        self._middleware.append(middleware)

    async def publish(self, event: Event) -> None:
        if event.sequence is None:
            event.sequence = next(self._seq)

        # A publish made from inside a handler this bus is currently
        # dispatching is the causal-cascade case (see module docstring): it
        # cannot wait for capacity the way an external publisher does
        # (waiting here could only ever be satisfied by this same call stack
        # continuing, which is exactly what waiting would block), so it is
        # checked against the hard ceiling instead and fails closed rather
        # than blocking or growing the queue without limit.
        reentrant = asyncio.current_task() in self._dispatching_tasks

        # ADMISSION: the check and the reservation happen inside the same
        # ``async with self._capacity`` critical section, with no ``await``
        # of anything else in between (only ``self._capacity.wait()``, which
        # re-acquires the lock before returning and is re-checked in the
        # ``while``). That is what makes this race-free under concurrent
        # publishers: two publish() calls can never both observe "room
        # available" and both proceed, because the observation and the
        # reservation that immediately follows it are one atomic step from
        # every other publisher's point of view.
        async with self._capacity:
            if reentrant:
                if len(self._queue) + self._reserved >= self._hard_ceiling:
                    self.cascade_overflow += 1
                    raise CascadeCapacityExceeded(
                        f"cascade publish refused: admitted total at hard ceiling "
                        f"{self._hard_ceiling} (max_pending={self._max_pending} + "
                        f"cascade_reserve={self._cascade_reserve})"
                    )
            else:
                if len(self._queue) + self._reserved >= self._max_pending:
                    self.backpressure_events += 1
                while not self._stopped and len(self._queue) + self._reserved >= self._max_pending:
                    await self._capacity.wait()
                if self._stopped:
                    # stop() ran while we waited: this publish never gets to
                    # exist in the queue, which is the same category of loss
                    # clause 7 already names for events that did make it in.
                    # It must not be recorded either -- see "ACCEPTANCE
                    # POINT" in the module docstring -- so this returns
                    # before middleware ever runs, and no reservation is made.
                    self.discarded_at_stop += 1
                    return
            # ACCEPTANCE POINT: capacity is secured -- immediately, after a
            # successful wait, or under the cascade reserve. From here the
            # event counts against every other publisher's admission check
            # even though it has not been appended to ``_queue`` yet.
            self._reserved += 1
            self._idle.clear()
            # Admission id: this bus instance's own record of the order in
            # which publish() calls were admitted -- see "ORDERED COMMIT" in
            # the module docstring. Assigned in the same locked section as
            # the reservation, so its order is race-free the same way
            # admission itself is.
            admission_id = next(self._admission_seq)

        # Middleware runs with the lock released: it can be slow (real I/O,
        # in the recorder's case) and must not stall every other publisher's
        # admission decision while it does. The reservation above already
        # accounts for this event, so that staying unlocked here cannot
        # reopen the concurrent-admission race. Because commit is ordered by
        # admission id (below), a slower-middleware publisher admitted
        # earlier still cannot be overtaken by a faster one admitted later.
        try:
            for mw in self._middleware:
                await mw(event)
        except BaseException:
            # The event never reaches the queue. Its reservation is released
            # as part of the ordered commit (below), not immediately: a
            # later admission id may already be sitting in ``_ready`` behind
            # this one, and skipping this slot is what lets it proceed.
            # Whether an individual middleware's own side effect (e.g. a
            # recorder having already buffered the event before a *later*
            # middleware raised) is itself undone is that middleware's own
            # responsibility -- this bus guarantees only that an event which
            # never reaches ANY middleware (rejected or abandoned at
            # admission) is never recorded. The one middleware this codebase
            # actually attaches (``Recorder.record``) never raises in normal
            # operation; this path exists chiefly for task cancellation.
            async with self._capacity:
                self._aborted.add(admission_id)
                self.reservation_released += 1
                appended = self._flush_ready_locked()
                self._capacity.notify_all()
            if appended:
                self._work.set()
            raise

        async with self._capacity:
            self._ready[admission_id] = event
            appended = self._flush_ready_locked()
            self._capacity.notify_all()
        if appended:
            self._work.set()

    def _flush_ready_locked(self) -> bool:
        """Append the longest already-resolved prefix, in admission order.

        Must be called with ``self._capacity`` held. Releases each
        committed admission id's reservation as it resolves -- whether by
        being appended (success) or skipped (abort) -- so a permanently
        stuck earlier admission is the only thing that can leave later,
        already-``_ready`` events waiting forever; anything that eventually
        resolves (success, exception, or cancellation) unblocks every
        admission id behind it. Returns whether anything was appended (the
        caller sets ``self._work`` only then, outside the lock).
        """
        appended = False
        while True:
            if self._next_commit in self._aborted:
                self._aborted.discard(self._next_commit)
                self._next_commit += 1
                self._reserved -= 1
                continue
            if self._next_commit in self._ready:
                self._queue.append(self._ready.pop(self._next_commit))
                self._next_commit += 1
                self._reserved -= 1
                self.published_count += 1
                appended = True
                continue
            break
        return appended

    def subscribe(
        self,
        handler: Handler,
        types: Iterable[EventType] | None = None,
        name: str | None = None,
    ) -> Subscription:
        sub = Subscription(
            name=name or str(getattr(handler, "__qualname__", "handler")),
            types=frozenset(types) if types is not None else None,
            handler=handler,
        )
        self._subs.append(sub)
        return sub

    def unsubscribe(self, subscription: Subscription) -> None:
        if subscription in self._subs:
            self._subs.remove(subscription)

    # -- draining ----------------------------------------------------------

    async def _dispatch_one(self) -> None:
        event = self._queue.popleft()
        # Freed a slot: wake anyone waiting for capacity immediately, rather
        # than after the handler(s) below finish running.
        async with self._capacity:
            self._capacity.notify_all()

        task = asyncio.current_task()
        if task is not None:
            self._dispatching_tasks.add(task)
        try:
            for sub in list(self._subs):
                if not sub.wants(event):
                    continue
                try:
                    await sub.handler(event)
                    sub.delivered += 1
                    self.delivered_count += 1
                    self._outcomes.record_success()
                except Exception:
                    sub.errors += 1
                    # Recorded BEFORE the optional re-raise: with
                    # ``raise_on_handler_error`` set, a failure that propagates
                    # is still a failure that happened, and a window that
                    # forgot it would report the healthier of the two possible
                    # answers exactly when the bus is configured to be strict.
                    self._outcomes.record_error()
                    log.exception("handler %s failed on %s", sub.name, event.type)
                    if self._raise:
                        raise
        finally:
            if task is not None:
                self._dispatching_tasks.discard(task)

    async def drain(self, max_cycles: int = 100_000) -> None:
        """Dispatch until the queue is empty, including cascaded publications.

        When a background dispatcher is running (``start()`` was called),
        this must NOT call ``_dispatch_one()`` itself: that would race the two
        loops over the same queue, and — the bug this guards against — let
        this method return the instant the queue merely *looks* empty, while
        ``_run()`` is still awaiting a handler for the very last item it just
        popped. A ``stop()`` called right after such a premature return
        cancels that in-flight dispatch, silently losing the event with no
        exception and no counter incremented — a pre-existing race, confirmed
        to reproduce identically before TIDAL-M7 touched this file.

        ``_idle`` is the correct signal instead: ``publish()`` clears it the
        moment anything is admitted (not merely enqueued -- see "IDLE
        CORRECTNESS" in the module docstring), and only ``_run()``'s own loop
        sets it again, and only once both the queue and ``_reserved`` are
        empty *after* a dispatch (including the handler) has fully
        completed. Waiting on it here means drain() cannot return while a
        dispatch, or a concurrent publish still admitted-but-uncommitted, is
        still in flight.

        Without a background dispatcher, this loop drives dispatch itself,
        but must still wait -- on the same condition variable ``publish()``
        notifies, never a fixed sleep -- whenever the queue is momentarily
        empty but ``_reserved`` is not: that means a concurrent publish() is
        still mid-middleware or waiting in ``_ready`` for its commit turn
        (ordered commit, see the module docstring), and will append more
        shortly.
        """
        if self._task is not None:
            await self.wait_idle()
            return
        cycles = 0
        while True:
            async with self._capacity:
                await self._capacity.wait_for(lambda: bool(self._queue) or self._reserved == 0)
                should_dispatch = bool(self._queue)
            if not should_dispatch:
                break
            await self._dispatch_one()
            cycles += 1
            if cycles > max_cycles:
                raise RuntimeError("event cascade did not terminate")
        self._idle.set()

    async def wait_idle(self) -> None:
        await self._idle.wait()

    @property
    def queue_depth(self) -> int:
        return len(self._queue)

    @property
    def reserved_pending(self) -> int:
        """Admitted publishes not yet appended to the queue (mid-middleware).

        Counts against capacity exactly like a queued event does (Batch 5
        final pre-commit correction): ``queue_depth + reserved_pending`` is
        what every admission check actually bounds, never ``queue_depth``
        alone.
        """
        return self._reserved

    @property
    def admitted_total(self) -> int:
        """``queue_depth + reserved_pending`` — the true, currently-admitted
        total this bus's capacity checks bound."""
        return len(self._queue) + self._reserved

    @property
    def max_pending(self) -> int:
        """The configured external-publisher capacity bound (TIDAL-M7)."""
        return self._max_pending

    @property
    def cascade_reserve(self) -> int:
        """Extra room reserved for cascade/reentrant publishes (TIDAL-M7)."""
        return self._cascade_reserve

    @property
    def hard_ceiling(self) -> int:
        """The true, finite maximum admitted total: ``max_pending + cascade_reserve``.

        Bounds ``admitted_total`` (queued + reserved-but-not-yet-queued), not
        merely ``queue_depth`` — see the module docstring, "CONCURRENT
        ADMISSION".
        """
        return self._hard_ceiling

    @property
    def subscriptions(self) -> list[Subscription]:
        return list(self._subs)

    @property
    def recent_error_rate(self) -> float:
        """Clause 9: failures among the most recent delivery attempts."""
        return self._outcomes.error_rate

    @property
    def error_rate_window(self) -> int:
        """How many delivery attempts ``recent_error_rate`` measures over."""
        return self._outcomes.maxlen
