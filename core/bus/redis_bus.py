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
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import logging
from collections.abc import Iterable

from core.bus.base import EventBus, Handler, Middleware, Subscription
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
        for mw in self._middleware:
            await mw(event)
        client = await self._connect()
        # The group must exist before the first publish, otherwise events
        # written ahead of start() are never readable by this consumer.
        await self._ensure_group()
        stream_id = await client.xadd(
            self._stream,
            {"event": json.dumps(event.model_dump(mode="json"))},
            maxlen=self._maxlen,
            approximate=True,
        )
        self._last_published_id = stream_id
        self.published_count += 1

    def subscribe(
        self,
        handler: Handler,
        types: Iterable[EventType] | None = None,
        name: str | None = None,
    ) -> Subscription:
        # Clause 3: valid before or after start(); the consumer reads the
        # subscription list at dispatch time, so this takes effect at once.
        sub = Subscription(
            name=name or getattr(handler, "__qualname__", "handler"),
            types=frozenset(types) if types is not None else None,
            handler=handler,
        )
        self._subs.append(sub)
        return sub

    def unsubscribe(self, subscription: Subscription) -> None:
        if subscription in self._subs:
            self._subs.remove(subscription)

    @property
    def subscriptions(self) -> list[Subscription]:
        return list(self._subs)

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
                except Exception:
                    sub.errors += 1
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
        dispatched to this process's subscribers."""
        if self._last_published_id is None:
            return
        if not self._running:
            # Nothing is consuming; waiting would hang. The contract only
            # promises completion while the bus is running.
            return
        deadline = asyncio.get_running_loop().time() + timeout_s
        while self._id_lt(self._last_dispatched_id, self._last_published_id) or self._inflight:
            if asyncio.get_running_loop().time() > deadline:
                log.warning(
                    "drain timed out",
                    extra={
                        "published": self._last_published_id,
                        "dispatched": self._last_dispatched_id,
                        "inflight": self._inflight,
                    },
                )
                return
            self._progress.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._progress.wait(), timeout=0.05)

    @property
    def queue_depth(self) -> int:
        return self._inflight
