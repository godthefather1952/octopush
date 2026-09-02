"""Event bus interface and its contract.

THE CONTRACT
============

Every implementation of :class:`EventBus` must satisfy all of the following.
The shared conformance suite in ``tests/contract/test_event_bus_contract.py``
is parameterised over every implementation and asserts each clause. An
implementation that passes its own tests but not the conformance suite is not
a valid ``EventBus``.

The contract was written after an audit found the in-memory and Redis
implementations disagreeing on ordering, drain and subscription lifecycle,
with the orchestrator silently depending on the in-memory behaviour.

1. PUBLICATION ORDER — TOTAL, ACROSS ALL EVENT TYPES
----------------------------------------------------
Given ``publish(A)``, ``publish(B)``, ``publish(C)`` from a single producer,
every subscriber observes A then B then C, regardless of their event types.
Ordering is *not* per-topic; interleaved types keep their publication order.

This is a strong guarantee and it is deliberate: the orchestrator's pipeline
(opportunity -> opinions -> consensus -> intent -> plan -> fill) is a causal
chain, and reordering it changes decisions. Implementations achieve this with
a single ordered log, not one stream per type.

Ordering is guaranteed for a single producer. Two processes publishing
concurrently interleave in whatever order the transport serialises them; the
per-producer order of each is preserved.

2. DELIVERY — AT-LEAST-ONCE
---------------------------
A published event is delivered to each matching subscription one or more
times. Duplicates are possible after a consumer restart or a redelivery, so
**every handler must be idempotent**. The domain already requires this
(duplicate fills, duplicate events); the bus does not add exactly-once on top.

Handlers that must not act twice should de-duplicate on ``Event.id``, which is
unique per published event (clause 6).

3. SUBSCRIPTION LIFECYCLE — LATE SUBSCRIPTION ALLOWED, NO HISTORY
-----------------------------------------------------------------
``subscribe()`` may be called before or after ``start()``, and takes effect
immediately for events published from that point on. Subscribers never
receive events published before they subscribed — there is no replay of
history through the bus. (Historical replay is the event store's job, via
``replay.ReplaySession``.)

Startup order therefore does not matter for correctness, only for how much of
the early stream a late subscriber misses.

4. drain() — LOCAL COMPLETION, NOT DISTRIBUTED COMPLETION
----------------------------------------------------------
``await bus.drain()`` guarantees: *every event published through this bus
instance before the call has been dispatched to every matching subscription
in this process, and those handlers have returned.*

It does NOT guarantee anything about other processes. There is no distributed
barrier here and none is faked. On a multi-process deployment, ``drain()``
tells you only that your own process has caught up.

Callers must not use ``drain()`` to mean "the agents have answered". Use
:class:`core.bus.barrier.ResponseBarrier` for that — it waits for named
responses with a deadline and reports which ones are missing, which works
identically whether the responders are in-process or remote.

5. SUBSCRIBER FAILURE ISOLATION
--------------------------------
A handler that raises does not prevent delivery to other subscriptions, does
not stop the dispatcher, and does not lose the event for anyone else. The
failure is counted on the subscription (``Subscription.errors``) and logged.

6. EVENT IDENTITY
-----------------
``Event.id`` is unique per published event and never empty. ``Event.sequence``
is assigned by the bus at publish time, is strictly increasing per bus
instance, and combined with ``ts_ms`` gives the deterministic replay ordering
key (``Event.sort_key``).

7. SHUTDOWN — QUEUED EVENTS ARE DISCARDED, NOT DELIVERED
---------------------------------------------------------
``stop()`` stops dispatch. Events already published but not yet dispatched are
NOT delivered to subscribers. They have already passed through middleware, so
the recorder has persisted them; only in-process delivery is abandoned.

Callers wanting delivery before shutdown must ``await drain()`` first. This is
explicit rather than best-effort so that shutdown is bounded.

8. BACKPRESSURE — NONE
----------------------
``publish()`` never blocks on a slow consumer and the queue is unbounded. A
consumer that cannot keep up grows memory without limit. This is a known,
accepted limitation of the current design, recorded here rather than left to
be discovered: the platform's own load is bounded by its tick rate, and
``queue_depth`` is exported so the condition is observable.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field

from core.events import Event, EventType

Handler = Callable[[Event], Awaitable[None]]
Middleware = Callable[[Event], Awaitable[None]]


@dataclass
class Subscription:
    name: str
    types: frozenset[EventType] | None
    handler: Handler
    #: Events this subscription has seen; exposed for health/metrics.
    delivered: int = 0
    errors: int = 0
    #: Ids already delivered, so a duplicate publication is a no-op for
    #: handlers that opt into de-duplication.
    _seen: set[str] = field(default_factory=set, repr=False)

    def wants(self, event: Event) -> bool:
        return self.types is None or event.type in self.types


class EventBus(ABC):
    """Publish/subscribe transport. See the module docstring for the contract."""

    @abstractmethod
    async def publish(self, event: Event) -> None:
        """Publish one event. Assigns ``sequence`` if unset. Never blocks on
        consumers (clause 8)."""

    @abstractmethod
    def subscribe(
        self,
        handler: Handler,
        types: Iterable[EventType] | None = None,
        name: str | None = None,
    ) -> Subscription:
        """Register a handler. Valid before or after ``start()`` (clause 3)."""

    @abstractmethod
    def unsubscribe(self, subscription: Subscription) -> None: ...

    @abstractmethod
    async def start(self) -> None: ...

    @abstractmethod
    async def stop(self) -> None:
        """Stop dispatch. Undispatched events are discarded (clause 7)."""

    @abstractmethod
    async def drain(self) -> None:
        """Wait until everything published by this instance has been dispatched
        to this process's subscribers (clause 4). Not a distributed barrier."""

    @property
    @abstractmethod
    def queue_depth(self) -> int:
        """Events accepted but not yet dispatched in this process."""

    @property
    @abstractmethod
    def subscriptions(self) -> list[Subscription]:
        """Live subscriptions, for health and metrics."""

    def add_middleware(self, middleware: Middleware) -> None:
        """Register a hook invoked for every published event, before delivery.

        Middleware runs on the *publish* path, so it observes every event even
        if delivery is later abandoned by ``stop()``. Used by the recorder.
        """
        raise NotImplementedError
