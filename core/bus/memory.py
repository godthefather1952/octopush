"""In-process event bus.

Deterministic by construction: events are appended to a FIFO and dispatched to
subscribers in registration order, one event at a time.  Events published by a
handler are appended behind the current queue, so a replay of the same input
produces the same interleaving every time.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import logging
from collections import deque
from collections.abc import Iterable

from core.bus.base import EventBus, Handler, Middleware, Subscription
from core.events import Event, EventType

log = logging.getLogger(__name__)


class InMemoryEventBus(EventBus):
    def __init__(self, *, raise_on_handler_error: bool = False) -> None:
        self._queue: deque[Event] = deque()
        self._subs: list[Subscription] = []
        self._middleware: list[Middleware] = []
        self._task: asyncio.Task[None] | None = None
        self._running = False
        self._idle = asyncio.Event()
        self._idle.set()
        #: Set whenever work is queued, so the dispatcher can block instead of
        #: spinning. A busy-wait here starves every other task on the loop —
        #: including the market feed, which then looks stale.
        self._work = asyncio.Event()
        self._seq = itertools.count(1)
        self._raise = raise_on_handler_error
        self.published_count = 0
        self.delivered_count = 0
        self.dropped_count = 0

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run(), name="bus-dispatch")

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _run(self) -> None:
        while self._running:
            if not self._queue:
                self._idle.set()
                self._work.clear()
                # Wake on the next publication; the timeout only bounds how
                # long stop() waits.
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
        self.published_count += 1
        for mw in self._middleware:
            await mw(event)
        self._queue.append(event)
        self._idle.clear()
        self._work.set()

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

    # -- draining ----------------------------------------------------------

    async def _dispatch_one(self) -> None:
        event = self._queue.popleft()
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
                if self._raise:
                    raise

    async def drain(self, max_cycles: int = 100_000) -> None:
        """Dispatch until the queue is empty, including cascaded publications."""
        cycles = 0
        while self._queue:
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
    def subscriptions(self) -> list[Subscription]:
        return list(self._subs)
