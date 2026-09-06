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

Every implementation also records each delivery attempt — one outcome per
matching handler per event, success or failure — in a bounded rolling window,
exposed as ``recent_error_rate`` (clause 9).

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

8. BACKPRESSURE — BOUNDED, NEVER SILENT LOSS
---------------------------------------------
A consumer that cannot keep up must not grow this process's memory without
limit, and an event accepted for publication must not silently vanish because
of that. Both implementations satisfy this, by different means suited to
their own architecture — this clause does not mandate one mechanism:

* :class:`~core.bus.memory.InMemoryEventBus` has a hard, finite queue ceiling
  of ``max_pending + cascade_reserve`` events (defaults 10,000 + 1,000, ~58
  MiB + ~6 MiB resident at full per the Batch 5 ``tracemalloc`` measurement;
  see the class docstring). A publish made from outside the bus's own
  dispatch blocks until capacity frees up when ``max_pending`` is reached —
  real backpressure, not a drop — which is provably deadlock-free because
  such a publisher always runs on a different task from the one dispatching
  (see the module docstring for the proof). A publish made from *inside* a
  handler the bus is currently dispatching — the causal cascades this
  platform's pipeline depends on — cannot wait the same way (that would
  deadlock the one task capable of ever freeing capacity), so it draws
  instead against the smaller ``cascade_reserve`` layered on top; once even
  that is exhausted it fails closed with :class:`~core.bus.memory.CascadeCapacityExceeded`
  rather than blocking or growing further, counted in ``cascade_overflow``.
  The only loss under normal operation is at shutdown: a blocked external
  publisher wakes and abandons rather than hangs, counted in
  ``discarded_at_stop`` alongside clause 7's existing count — and, since
  recording happens only once capacity is secured (the "acceptance point"; see
  the class docstring), an abandoned or refused publish is never durably
  recorded either, so live and replay never disagree about which events were
  actually accepted. ``backpressure_events`` exposes how often the primary
  bound was reached at all.

* :class:`~core.bus.redis_bus.RedisStreamBus` bounds the underlying stream
  itself (``maxlen``, approximately trimmed by Redis); a slow consumer leaves
  events durably queued in Redis, not accumulating in this process's memory,
  and very old, undelivered entries age out of the stream by Redis's own
  trimming rather than this code choosing to drop anything.

``queue_depth`` is exported by both so the condition is observable either way.

9. HEALTH IS MEASURED OVER A RECENT WINDOW, NOT OVER ALL TIME
--------------------------------------------------------------
``recent_error_rate`` is the share of the most recent N delivery attempts that
raised, where N is fixed at construction. It is a *rolling* measurement: a
process that has been healthy for a million deliveries and is failing every
delivery right now reports 1.0, not a number diluted towards zero by its own
history. RUNE's ``MAX_ERROR_RATE`` gate reads it, and that gate exists to stop
a platform that is failing *now*, which a lifetime ratio cannot express (P5-8).

Every implementation records one outcome per matching handler per event, at
dispatch time, into a :class:`DeliveryOutcomeWindow` — bounded, deterministic
and clock-free, so a replay measures the same health as the run it replays.
Where a bus re-raises a handler failure to its caller, the outcome is recorded
before the re-raise: a failure that propagates is still a failure that
happened.

``Subscription.delivered`` and ``Subscription.errors`` remain lifetime
counters. They are diagnostics — which handler is failing, and how much — and
must not be used to derive a health rate, because the arithmetic that does so
cannot distinguish "healthy now" from "healthy for long enough".
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections import deque
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field

from core.events import Event, EventType

Handler = Callable[[Event], Awaitable[None]]
Middleware = Callable[[Event], Awaitable[None]]

#: Deliveries retained by the rolling health window when no size is given.
#: ``RiskLimits.error_rate_window_deliveries`` carries the same default and is
#: pinned equal to this by the audit suite, so the composition root and a bus
#: built by hand agree without one importing the other.
DEFAULT_DELIVERY_WINDOW = 200


class DeliveryOutcomeWindow:
    """The outcomes of the most recent ``maxlen`` delivery attempts.

    One entry per matching handler per event: ``record_success`` or
    ``record_error``, never both for the same attempt. ``error_rate`` is the
    share of the retained entries that failed, and an empty window reports
    ``0.0`` — no evidence is not evidence of failure, and the gate that reads
    this has a separate UNKNOWN path for genuinely missing inputs.

    The window is bounded (a fixed-length ``deque``, so memory does not grow
    with uptime), deterministic (the same sequence of calls always yields the
    same rate) and takes no reading of time — eviction is by count, not by age.
    That is what makes it usable inside replay: two runs over the same event
    sequence measure the same health, whatever wall time either took.
    """

    __slots__ = ("_outcomes", "_errors")

    def __init__(self, maxlen: int = DEFAULT_DELIVERY_WINDOW) -> None:
        if maxlen <= 0:
            raise ValueError(f"delivery window must retain at least one outcome, got {maxlen}")
        self._outcomes: deque[bool] = deque(maxlen=maxlen)
        self._errors = 0

    @property
    def maxlen(self) -> int:
        """How many outcomes are retained before the oldest is evicted."""
        return self._outcomes.maxlen or 0

    def __len__(self) -> int:
        return len(self._outcomes)

    def record_success(self) -> None:
        self._append(False)

    def record_error(self) -> None:
        self._append(True)

    def _append(self, failed: bool) -> None:
        if len(self._outcomes) == self._outcomes.maxlen and self._outcomes[0]:
            self._errors -= 1
        self._outcomes.append(failed)
        if failed:
            self._errors += 1

    @property
    def error_rate(self) -> float:
        """Share of the retained attempts that raised. ``0.0`` when empty."""
        total = len(self._outcomes)
        return self._errors / total if total else 0.0


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
        """Publish one event. Assigns ``sequence`` if unset. May await
        capacity under backpressure rather than block on a *consumer*
        directly, and never blocks forever (clause 8)."""

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

    @property
    @abstractmethod
    def recent_error_rate(self) -> float:
        """Share of the most recent delivery attempts that raised (clause 9).

        Declared abstract deliberately. An implementation that cannot answer
        this cannot be used, rather than being silently substituted or quietly
        measured by lifetime totals: a health input that degrades to a
        different definition without saying so is worse than one that is
        missing, because the risk gate reading it cannot tell the difference.
        """

    def add_middleware(self, middleware: Middleware) -> None:
        """Register a hook invoked for every published event, before delivery.

        Middleware runs on the *publish* path, so it observes every event even
        if delivery is later abandoned by ``stop()``. Used by the recorder.
        """
        raise NotImplementedError
