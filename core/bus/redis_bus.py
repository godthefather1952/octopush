"""Redis Streams event bus.

The initial transport.  The interface is deliberately narrow so it can be
swapped for NATS/Kafka/RabbitMQ later without touching any component.

Each :class:`~core.events.EventType` maps to one stream, ``tf:<TYPE>``.  A
consumer group per subscriber name gives at-least-once delivery; handlers must
therefore be idempotent, which the domain code already requires (duplicate
fills, duplicate events).
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

STREAM_PREFIX = "tf"
MAXLEN = 100_000


def stream_name(event_type: EventType) -> str:
    return f"{STREAM_PREFIX}:{event_type.value}"


class RedisStreamBus(EventBus):
    def __init__(
        self,
        url: str,
        *,
        group: str = "trading-floor",
        consumer: str = "default",
        block_ms: int = 200,
        batch: int = 64,
    ) -> None:
        self._url = url
        self._group = group
        self._consumer = consumer
        self._block_ms = block_ms
        self._batch = batch
        self._subs: list[Subscription] = []
        self._middleware: list[Middleware] = []
        self._tasks: list[asyncio.Task[None]] = []
        self._client = None  # type: ignore[var-annotated]
        self._running = False
        self._seq = itertools.count(1)
        self._inflight = 0

    async def _connect(self):  # pragma: no cover - requires a server
        if self._client is None:
            from redis import asyncio as aioredis

            self._client = aioredis.from_url(self._url, decode_responses=True)
        return self._client

    async def start(self) -> None:  # pragma: no cover - requires a server
        client = await self._connect()
        self._running = True
        wanted: set[EventType] = set()
        for sub in self._subs:
            wanted |= set(sub.types) if sub.types else set(EventType)
        for event_type in sorted(wanted, key=lambda t: t.value):
            name = stream_name(event_type)
            try:
                await client.xgroup_create(name, self._group, id="$", mkstream=True)
            except Exception as exc:  # BUSYGROUP means it already exists
                if "BUSYGROUP" not in str(exc):
                    raise
            self._tasks.append(
                asyncio.create_task(self._consume(name), name=f"redis-{name}")
            )

    async def stop(self) -> None:  # pragma: no cover - requires a server
        self._running = False
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks.clear()
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def add_middleware(self, middleware: Middleware) -> None:
        self._middleware.append(middleware)

    async def publish(self, event: Event) -> None:  # pragma: no cover
        if event.sequence is None:
            event.sequence = next(self._seq)
        for mw in self._middleware:
            await mw(event)
        client = await self._connect()
        await client.xadd(
            stream_name(event.type),
            {"event": json.dumps(event.model_dump(mode="json"))},
            maxlen=MAXLEN,
            approximate=True,
        )

    def subscribe(
        self,
        handler: Handler,
        types: Iterable[EventType] | None = None,
        name: str | None = None,
    ) -> Subscription:
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

    async def _consume(self, stream: str) -> None:  # pragma: no cover
        client = await self._connect()
        while self._running:
            try:
                resp = await client.xreadgroup(
                    self._group,
                    self._consumer,
                    {stream: ">"},
                    count=self._batch,
                    block=self._block_ms,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("xreadgroup failed on %s", stream)
                await asyncio.sleep(1.0)
                continue
            if not resp:
                continue
            for _, messages in resp:
                for message_id, fields in messages:
                    self._inflight += 1
                    try:
                        event = Event.model_validate_json(fields["event"])
                        for sub in list(self._subs):
                            if sub.wants(event):
                                await sub.handler(event)
                                sub.delivered += 1
                    except Exception:
                        log.exception("failed handling %s", message_id)
                    finally:
                        self._inflight -= 1
                        await client.xack(stream, self._group, message_id)

    async def drain(self) -> None:  # pragma: no cover
        while self._inflight:
            await asyncio.sleep(0.01)

    @property
    def queue_depth(self) -> int:  # pragma: no cover
        return self._inflight
