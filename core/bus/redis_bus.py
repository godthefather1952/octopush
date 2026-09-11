"""Redis Streams event bus.

Conforms to the contract in :mod:`core.bus.base`. The shared conformance suite
runs against this implementation and the in-memory one identically.

**One stream, not one per type.** An earlier design gave each ``EventType`` its
own stream with its own consumer task, which meant cross-type publication order
was lost — an audit measured events published 1,2,3,4 arriving 3,1,2,4. Redis
Streams guarantee total order *within* a stream, so the fix is to use exactly
one: ``tf:events``. Subscriptions are local filters applied by the single
consumer, which is also what makes late subscription work (clause 3) and gives
subscriber-failure isolation identical to the in-memory bus (clause 5).

``drain()`` is implementable here precisely because there is one ordered log:
the bus records the stream id of its last publication and waits until the
consumer has dispatched past it. That is local completion (clause 4), which is
what the contract promises — not a distributed barrier.

ORDERED COMMIT (Phase 2 Batch 1.2 -- P2-13 parity)
====================================================
``Event.sequence`` used to be assigned before middleware ran, and ``XADD``
(the actual commit to the ordered stream) happened after -- with no
serialization between concurrent ``publish()`` calls at all. Two publishers
racing (A admitted first, given the lower ``Event.sequence``, but slow
through middleware; B admitted second, but fast) could XADD in either order:
B's entry could land in the stream, and so be dispatched, before A's. Redis
Stream IDs sort by write order, not by ``Event.sequence`` -- they do not
repair this.

The fix mirrors ``InMemoryEventBus``'s: a purely internal
``_admission_seq``, assigned to every ``publish()`` call at admission time
(before middleware), distinct from ``Event.sequence`` for the same reason it
is there -- a caller can pre-set ``Event.sequence`` from a different bus
instance's history. Once a publisher's middleware resolves (success or
abort), its event is placed in ``_ready`` keyed by admission id, and
``_flush_ready_locked`` commits the longest already-resolved contiguous
prefix via real ``XADD`` calls, in admission order, while holding
``_commit_cond``. Every ``publish()`` call then waits (on the same
condition) until its OWN admission id has been committed or aborted --
whether that happens inside its own flush call or a later caller's cascading
one -- so a return from ``publish()`` continues to mean "this event's
position in the stream, if any, is now fixed," which ``drain()`` depends on.

Holding the lock across the ``XADD`` call itself, rather than only around
bookkeeping, is deliberate here, not a shortcut: writes to one Redis stream
already have exactly one true order, so committing two never-interleaved
prefixes concurrently cannot preserve that order across them without
re-adding the same race one level up. This is the minimal serialization a
correctly-ordered single log requires, not the arbitrary "hold the lock
around all of middleware" shortcut this design deliberately avoids
(middleware itself still runs fully unlocked, exactly as before).
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import logging
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

#: The single ordered log carrying every event type.
STREAM = "tf:events"
MAXLEN = 100_000


class RedisStreamBus(EventBus):
    def __init__(
        self,
        url: str,
        *,
        group: str = "trading-floor",
        consumer: str = "default",
        block_ms: int = 50,
        batch: int = 256,
        stream: str = STREAM,
        maxlen: int = MAXLEN,
        error_rate_window: int = DEFAULT_DELIVERY_WINDOW,
    ) -> None:
        self._url = url
        self._group = group
        self._consumer = consumer
        self._block_ms = block_ms
        self._batch = batch
        self._stream = stream
        self._maxlen = maxlen
        self._subs: list[Subscription] = []
        self._middleware: list[Middleware] = []
        self._task: asyncio.Task[None] | None = None
        self._client = None  # type: ignore[var-annotated]
        self._running = False
        self._seq = itertools.count(1)
        self._inflight = 0
        #: Stream id of the most recent publication by this instance, and the
        #: most recent id this instance has finished dispatching. drain()
        #: compares them.
        self._last_published_id: str | None = None
        self._last_dispatched_id: str | None = None
        self._progress = asyncio.Event()
        self.published_count = 0
        self.delivered_count = 0
        #: Clause 9. Same semantics as the in-memory bus: one entry per
        #: handler invocation, written at dispatch time, bounded and
        #: clock-free. Health must not be measured differently just because
        #: the transport underneath changed.
        self._outcomes = DeliveryOutcomeWindow(error_rate_window)
        #: Internal admission-order counter -- see "ORDERED COMMIT" above.
        #: Deliberately distinct from ``_seq``/``Event.sequence``.
        self._admission_seq = itertools.count(1)
        #: The next admission id whose resolution (committed or aborted) is
        #: still needed before anything later can be XADDed.
        self._next_commit = 1
        #: Admission id -> event, for a middleware that finished but is
        #: still waiting for every earlier admission id to resolve first.
        self._ready: dict[int, Event] = {}
        #: Admission ids whose middleware raised or was cancelled -- skipped
        #: (not XADDed) when their turn in commit order arrives.
        self._aborted: set[int] = set()
        #: Admitted but not yet committed (mid-middleware, or resolved but
        #: waiting in ``_ready``/``_aborted`` for its commit turn). Nonzero
        #: means drain() must wait even if ``_last_published_id`` does not
        #: yet reflect this event at all.
        self._pending_admissions = 0
        self._commit_cond = asyncio.Condition()

    # -- connection --------------------------------------------------------

    async def _connect(self):
        if self._client is None:
            from redis import asyncio as aioredis

            self._client = aioredis.from_url(self._url, decode_responses=True)
        return self._client

    async def _ensure_group(self) -> None:
        client = await self._connect()
        try:
            await client.xgroup_create(self._stream, self._group, id="$", mkstream=True)
        except Exception as exc:  # BUSYGROUP means it already exists
            if "BUSYGROUP" not in str(exc):
                raise

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        if self._running:
            return
        await self._ensure_group()
        self._running = True
        self._task = asyncio.create_task(self._consume(), name="redis-bus")

    async def stop(self) -> None:
        # Clause 7: undispatched events are discarded, not delivered.
        self._running = False
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # -- pub/sub -----------------------------------------------------------

    def add_middleware(self, middleware: Middleware) -> None:
        self._middleware.append(middleware)

    async def publish(self, event: Event) -> None:
        if event.sequence is None:
            event.sequence = next(self._seq)

        async with self._commit_cond:
            admission_id = next(self._admission_seq)
            self._pending_admissions += 1

        exc: BaseException | None = None
        try:
            for mw in self._middleware:
                await mw(event)
        except BaseException as caught:
            exc = caught

        async with self._commit_cond:
            if exc is None:
                self._ready[admission_id] = event
            else:
                self._aborted.add(admission_id)
            await self._flush_ready_locked()
            self._commit_cond.notify_all()
            # Wait until OUR OWN admission id has resolved -- it may already
            # have, just above, if it was our turn; otherwise a later
            # caller's cascading flush will eventually reach it (see
            # "ORDERED COMMIT" in the module docstring).
            await self._commit_cond.wait_for(lambda: admission_id < self._next_commit)

        if exc is not None:
            raise exc

    async def _flush_ready_locked(self) -> None:
        """Commit (XADD) the longest already-resolved contiguous prefix, in
        admission order. Must be called with ``self._commit_cond`` held;
        performs real network I/O while holding it deliberately -- see
        "ORDERED COMMIT" in the module docstring for why an out-of-lock
        commit cannot preserve stream order across concurrent publishers.

        An item is popped from ``_ready`` only once its own XADD succeeds:
        if this specific call is cancelled mid-XADD, the item stays in
        ``_ready`` for whichever future caller next reaches this lock to
        retry it, rather than being lost or permanently blocking everyone
        behind it.
        """
        while True:
            if self._next_commit in self._aborted:
                self._aborted.discard(self._next_commit)
                self._next_commit += 1
                self._pending_admissions -= 1
                continue
            if self._next_commit in self._ready:
                event = self._ready[self._next_commit]
                client = await self._connect()
                # The group must exist before the first publish, otherwise
                # events written ahead of start() are never readable by
                # this consumer.
                await self._ensure_group()
                stream_id = await client.xadd(
                    self._stream,
                    {"event": json.dumps(event.model_dump(mode="json"))},
                    maxlen=self._maxlen,
                    approximate=True,
                )
                del self._ready[self._next_commit]
                self._last_published_id = stream_id
                self.published_count += 1
                self._next_commit += 1
                self._pending_admissions -= 1
                continue
            break

    def subscribe(
        self,
        handler: Handler,
        types: Iterable[EventType] | None = None,
        name: str | None = None,
    ) -> Subscription:
        # Clause 3: valid before or after start(); the consumer reads the
        # subscription list at dispatch time, so this takes effect at once.
        sub = Subscription(
            name=name or str(getattr(handler, "__qualname__", "handler")),
            types=frozenset(types) if types is not None else None,
            handler=handler,
            health_relevant=health_relevant,
        )
        self._subs.append(sub)
        return sub

    def unsubscribe(self, subscription: Subscription) -> None:
        if subscription in self._subs:
            self._subs.remove(subscription)

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

    # -- consumption -------------------------------------------------------

    async def _consume(self) -> None:
        client = await self._connect()
        while self._running:
            try:
                resp = await client.xreadgroup(
                    self._group,
                    self._consumer,
                    {self._stream: ">"},
                    count=self._batch,
                    block=self._block_ms,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("xreadgroup failed on %s", self._stream)
                # Backoff without the Clock: this is transport recovery, not
                # domain logic, and must make progress under a manual clock.
                await asyncio.sleep(0.5)
                continue
            if not resp:
                # Nothing pending: we are caught up with everything written so
                # far, so drain() can be satisfied.
                self._last_dispatched_id = self._last_published_id
                self._progress.set()
                continue
            for _, messages in resp:
                for message_id, fields in messages:
                    await self._dispatch(client, message_id, fields)
            self._progress.set()

    async def _dispatch(self, client, message_id: str, fields: dict) -> None:
        self._inflight += 1
        try:
            event = Event.model_validate_json(fields["event"])
        except Exception:
            log.exception("undecodable event %s; acking to avoid a poison loop", message_id)
            self._inflight -= 1
            await client.xack(self._stream, self._group, message_id)
            self._last_dispatched_id = message_id
            return
        try:
            # Clause 1: one ordered log, dispatched in order to every matching
            # subscription. Clause 5: a failing handler is isolated.
            for sub in list(self._subs):
                if not sub.wants(event):
                    continue
                try:
                    await sub.handler(event)
                    sub.delivered += 1
                    self.delivered_count += 1
                    if sub.health_relevant:
                        self._outcomes.record_success()
                except Exception:
                    sub.errors += 1
                    if sub.health_relevant:
                        self._outcomes.record_error()
                    log.exception("handler %s failed on %s", sub.name, event.type)
        finally:
            self._inflight -= 1
            self._last_dispatched_id = message_id
            await client.xack(self._stream, self._group, message_id)

    # -- draining ----------------------------------------------------------

    @staticmethod
    def _id_lt(a: str | None, b: str | None) -> bool:
        """Redis stream ids sort as (millis, seq) integer pairs, not strings."""
        if a is None:
            return b is not None
        if b is None:
            return False

        def parse(v: str) -> tuple[int, int]:
            ms, _, seq = v.partition("-")
            return (int(ms), int(seq or 0))

        return parse(a) < parse(b)

    async def drain(self, timeout_s: float = 5.0) -> None:
        """Clause 4: wait until everything this instance published has been
        dispatched to this process's subscribers.

        ``_pending_admissions`` (admitted, not yet committed -- mid-
        middleware, or resolved but waiting in ``_ready`` for its commit
        turn; see "ORDERED COMMIT" in the module docstring) must reach zero
        too: a concurrent ``publish()`` can still be in flight even before
        ``_last_published_id`` reflects it at all, and draining before that
        settles would let this method return while an admitted event is
        still guaranteed to appear on the stream shortly after.
        """
        if self._last_published_id is None and self._pending_admissions == 0:
            return
        if not self._running:
            # Nothing is consuming; waiting would hang. The contract only
            # promises completion while the bus is running.
            return
        deadline = asyncio.get_running_loop().time() + timeout_s
        while (
            self._pending_admissions > 0
            or self._id_lt(self._last_dispatched_id, self._last_published_id)
            or self._inflight
        ):
            if asyncio.get_running_loop().time() > deadline:
                log.warning(
                    "drain timed out",
                    extra={
                        "published": self._last_published_id,
                        "dispatched": self._last_dispatched_id,
                        "inflight": self._inflight,
                        "pending_admissions": self._pending_admissions,
                    },
                )
                return
            self._progress.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._progress.wait(), timeout=0.05)

    @property
    def queue_depth(self) -> int:
        return self._inflight
