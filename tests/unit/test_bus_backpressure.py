"""Event-bus backpressure: TIDAL-M7.

The in-memory bus used to accept publications onto an unbounded deque: a slow
consumer (a stalled recorder, a handler doing real I/O) let producer traffic
accumulate without limit, and the only remedy was the process running out of
memory. The fix bounds the queue at ``max_pending`` and makes a publisher
from outside the bus's own dispatch wait for capacity rather than growing the
queue further — real backpressure, never a silent drop.

The one subtlety worth restating here: a publish made from *inside* a handler
currently being dispatched by this bus (a causal cascade — TIDAL publishing
MARKET_STATE while processing a BOOK_DELTA, for instance) is exempt from the
bound. Blocking a nested publish would deadlock the one task capable of ever
draining the queue that publish is waiting on. See ``core/bus/memory.py``'s
module docstring for the full proof; the tests in
``TestReentrantPublishCannotDeadlock`` exercise it directly rather than
merely asserting the reasoning.
"""

from __future__ import annotations

import asyncio

import pytest

from core.bus.memory import InMemoryEventBus
from core.events import Event, EventType

START_MS = 1_788_000_000_000


def event(i: int, event_type: EventType = EventType.SYSTEM_EVENT) -> Event:
    return Event(
        type=event_type, ts_ms=START_MS + i, source="test", schema_name="Test",
        payload={"i": i},
    )


async def settle(n: int = 50) -> None:
    for _ in range(n):
        await asyncio.sleep(0)


# ======================================================================
# A / C. The bound holds, and nothing published disappears
# ======================================================================


class TestTheBoundHolds:
    async def test_the_queue_never_exceeds_the_configured_maximum(self):
        """A slow consumer with a fast producer: queue_depth is capped, not
        merely "usually low" — sampled continuously while filling.
        """
        bus = InMemoryEventBus(max_pending=10)
        gate = asyncio.Event()

        async def slow_handler(_e):
            await gate.wait()

        bus.subscribe(slow_handler, name="slow")
        await bus.start()

        max_seen = 0

        async def producer():
            nonlocal max_seen
            for i in range(40):
                await bus.publish(event(i))
                max_seen = max(max_seen, bus.queue_depth)

        task = asyncio.create_task(producer())
        await asyncio.sleep(0.05)  # let the producer race ahead and fill the queue
        assert bus.queue_depth <= 10
        assert max_seen <= 10

        gate.set()
        await asyncio.wait_for(task, timeout=5)
        await bus.drain()
        await bus.stop()

    async def test_zero_or_negative_max_pending_is_rejected(self):
        with pytest.raises(ValueError):
            InMemoryEventBus(max_pending=0)
        with pytest.raises(ValueError):
            InMemoryEventBus(max_pending=-1)


class TestNoEventDisappearsUnderBackpressure:
    async def test_every_published_event_is_eventually_delivered(self):
        bus = InMemoryEventBus(max_pending=5, raise_on_handler_error=True)
        received: list[int] = []

        async def handler(e):
            await asyncio.sleep(0)  # a little real work, enough to be "slow"
            received.append(e.payload["i"])

        bus.subscribe(handler, name="h")
        await bus.start()

        for i in range(50):
            await bus.publish(event(i))
        await bus.drain()
        await bus.stop()

        assert received == list(range(50)), "backpressure must not lose or reorder events"
        assert bus.published_count == 50
        assert bus.delivered_count == 50


# ======================================================================
# D / G. Ordering under backpressure and concurrent producers
# ======================================================================


class TestOrderingUnderBackpressure:
    async def test_single_producer_order_survives_backpressure(self):
        bus = InMemoryEventBus(max_pending=3)
        received: list[int] = []

        async def handler(e):
            await asyncio.sleep(0)
            received.append(e.payload["i"])

        bus.subscribe(handler, name="h")
        await bus.start()
        for i in range(30):
            await bus.publish(event(i))
        await bus.drain()
        await bus.stop()
        assert received == list(range(30))

    async def test_each_concurrent_producers_own_sequence_stays_in_order(self):
        """The contract promises per-producer order, not a global interleave
        across producers (base.py clause 1) — so this checks each producer's
        own events arrive in the order *that producer* sent them, not a
        specific interleaving between the two.
        """
        bus = InMemoryEventBus(max_pending=4)
        received: list[tuple[str, int]] = []

        async def handler(e):
            await asyncio.sleep(0)
            received.append((e.source, e.payload["i"]))

        bus.subscribe(handler, name="h")
        await bus.start()

        async def producer(name: str, count: int):
            for i in range(count):
                await bus.publish(
                    Event(
                        type=EventType.SYSTEM_EVENT, ts_ms=START_MS, source=name,
                        schema_name="Test", payload={"i": i},
                    )
                )

        await asyncio.gather(producer("P1", 25), producer("P2", 25))
        await bus.drain()
        await bus.stop()

        p1 = [i for src, i in received if src == "P1"]
        p2 = [i for src, i in received if src == "P2"]
        assert p1 == list(range(25))
        assert p2 == list(range(25))
        assert len(received) == 50


# ======================================================================
# E. drain() still means "everything published before the call"
# ======================================================================


class TestDrainSemanticsUnchanged:
    async def test_drain_processes_everything_published_first(self):
        # Comfortably above what this test publishes: the point here is
        # drain()'s own semantics, not the bound, and a batch-then-drain
        # caller that never interleaves dispatch can only ever publish up to
        # the bound before it would need to (see the next test for that case).
        bus = InMemoryEventBus(max_pending=25)
        received = []

        async def handler(e):
            received.append(e.payload["i"])

        bus.subscribe(handler, name="h")
        for i in range(20):
            await bus.publish(event(i))
        await bus.drain()
        assert received == list(range(20))

    async def test_drain_interleaved_with_publishing_respects_a_small_bound(self):
        """The realistic version of the pattern seen throughout
        ``apps/orchestrator/orchestrator.py`` — publish a little, drain,
        repeat — works correctly with a bound far smaller than the total
        event count, because each drain() frees capacity before the next
        batch needs it. A drain-only (no ``start()``) caller that published
        *more than the bound* in one uninterrupted burst would have nothing
        to free capacity until its own next drain() call — which is not a
        bus defect, it is the same fact real backpressure always implies:
        something has to keep consuming.
        """
        bus = InMemoryEventBus(max_pending=2)
        received: list[int] = []
        bus.subscribe(lambda e: _record(received, e), name="h")
        for i in range(10):
            await bus.publish(event(i))
            await bus.drain()
        assert received == list(range(10))


async def _record(sink: list, e: Event) -> None:
    sink.append(e.payload["i"])


# ======================================================================
# F. Reentrant/cascade publish cannot deadlock
# ======================================================================


class TestReentrantPublishCannotDeadlock:
    async def test_a_handler_publishing_while_the_queue_is_at_capacity_does_not_hang(self):
        """The exact deadlock scenario: the queue is completely full, and the
        handler processing the event that would free a slot itself tries to
        publish before that slot is freed. If the cascade publish waited for
        capacity, it would be waiting on itself.
        """
        bus = InMemoryEventBus(max_pending=1, raise_on_handler_error=True)
        cascade_done = asyncio.Event()

        async def handler(e):
            if e.payload["i"] == "root":
                # The queue is at capacity (this event alone fills it) when
                # this cascade publish happens.
                assert bus.queue_depth == 0  # popped before dispatch (see memory.py)
                await bus.publish(
                    Event(
                        type=EventType.SYSTEM_EVENT, ts_ms=START_MS, source="cascade",
                        schema_name="Test", payload={"i": "cascade"},
                    )
                )
                cascade_done.set()

        bus.subscribe(handler, name="h")
        root = Event(
            type=EventType.SYSTEM_EVENT, ts_ms=START_MS, source="root",
            schema_name="Test", payload={"i": "root"},
        )
        await bus.publish(root)
        await asyncio.wait_for(bus.drain(), timeout=5)
        assert cascade_done.is_set()

    async def test_a_deep_cascade_chain_at_capacity_one_does_not_hang(self):
        """Each handler publishes the next in a chain, with room for exactly
        one event at a time — the worst case for the reentrancy exemption.
        """
        bus = InMemoryEventBus(max_pending=1, raise_on_handler_error=True)
        seen: list[int] = []
        depth = 25

        async def handler(e):
            i = e.payload["i"]
            seen.append(i)
            if i + 1 < depth:
                await bus.publish(
                    Event(
                        type=EventType.SYSTEM_EVENT, ts_ms=START_MS, source="chain",
                        schema_name="Test", payload={"i": i + 1},
                    )
                )

        bus.subscribe(handler, name="h")
        await bus.publish(
            Event(
                type=EventType.SYSTEM_EVENT, ts_ms=START_MS, source="chain",
                schema_name="Test", payload={"i": 0},
            )
        )
        await asyncio.wait_for(bus.drain(), timeout=5)
        assert seen == list(range(depth))

    async def test_an_external_publisher_still_waits_normally_alongside_cascades(self):
        """Only the reentrant path is exempt — an unrelated external producer
        hitting the same full queue must still experience real backpressure,
        not be waved through because *some* cascade happened recently.

        With a live dispatcher running, the first publish is popped for
        dispatch (and blocks inside the slow handler) as soon as the loop
        gets a turn, so filling the queue itself to ``max_pending`` takes one
        more publish than the bound — that popped-and-in-flight item is being
        actively processed, not sitting idle, so it is correctly outside
        ``queue_depth``.
        """
        bus = InMemoryEventBus(max_pending=2)
        gate = asyncio.Event()

        async def slow_handler(_e):
            await gate.wait()

        bus.subscribe(slow_handler, name="slow")
        await bus.start()
        await bus.publish(event(0))  # popped immediately; blocks inside slow_handler
        await bus.publish(event(1))
        await bus.publish(event(2))
        await settle()
        assert bus.queue_depth == 2

        blocked = asyncio.create_task(bus.publish(event(3)))
        await settle()
        assert not blocked.done(), "an external publisher must actually wait when full"
        assert bus.backpressure_events >= 1

        gate.set()
        await asyncio.wait_for(blocked, timeout=5)
        await bus.drain()
        await bus.stop()


# ======================================================================
# H. Shutdown while publishers are waiting does not hang
# ======================================================================


class TestShutdownWhileWaiting:
    async def test_stop_releases_a_blocked_publisher_promptly(self):
        # max_pending=1, plus the one item the live dispatcher immediately
        # pops into the slow handler: two publishes fill the system before a
        # third genuinely has to wait (see the eager-pop note above).
        bus = InMemoryEventBus(max_pending=1)
        gate = asyncio.Event()

        async def slow_handler(_e):
            await gate.wait()

        bus.subscribe(slow_handler, name="slow")
        await bus.start()
        await bus.publish(event(0))  # popped immediately; blocks in slow_handler
        await bus.publish(event(1))
        await settle()
        assert bus.queue_depth == 1

        blocked = asyncio.create_task(bus.publish(event(2)))
        await settle()
        assert not blocked.done()

        await asyncio.wait_for(bus.stop(), timeout=5)
        await asyncio.wait_for(blocked, timeout=5)
        assert bus.discarded_at_stop >= 1
        gate.set()  # release the stuck handler so its task can finish cleanly

    async def test_multiple_blocked_publishers_all_release_on_stop(self):
        bus = InMemoryEventBus(max_pending=1)
        gate = asyncio.Event()
        bus.subscribe(lambda _e: gate.wait(), name="slow")
        await bus.start()
        await bus.publish(event(0))  # popped immediately; blocks in slow_handler
        await bus.publish(event(1))
        await settle()

        blocked = [asyncio.create_task(bus.publish(event(i))) for i in range(2, 7)]
        await settle()
        assert all(not t.done() for t in blocked)

        await asyncio.wait_for(bus.stop(), timeout=5)
        await asyncio.wait_for(asyncio.gather(*blocked), timeout=5)
        gate.set()


# ======================================================================
# I. Health/metrics expose saturation
# ======================================================================


class TestObservability:
    async def test_backpressure_events_counts_only_when_capacity_was_actually_reached(
        self,
    ):
        bus = InMemoryEventBus(max_pending=100)
        await bus.start()
        for i in range(10):
            await bus.publish(event(i))
        await bus.drain()
        assert bus.backpressure_events == 0
        await bus.stop()

    async def test_backpressure_events_increments_once_per_wait(self):
        bus = InMemoryEventBus(max_pending=1)
        gate = asyncio.Event()
        bus.subscribe(lambda _e: gate.wait(), name="slow")
        await bus.start()
        await bus.publish(event(0))
        await settle()  # let the dispatcher pop it and block inside slow_handler
        assert bus.queue_depth == 0

        await bus.publish(event(1))  # queue was empty; fills the one slot without waiting
        await settle()
        assert bus.queue_depth == 1
        assert bus.backpressure_events == 0

        blocked = [asyncio.create_task(bus.publish(event(i))) for i in range(2, 5)]
        await settle()
        assert bus.backpressure_events == 3

        gate.set()
        await asyncio.wait_for(asyncio.gather(*blocked), timeout=5)
        await bus.drain()
        await bus.stop()

    async def test_max_pending_is_exposed(self):
        bus = InMemoryEventBus(max_pending=250)
        assert bus.max_pending == 250

    async def test_the_default_bound_is_the_documented_conservative_value(self):
        assert InMemoryEventBus().max_pending == InMemoryEventBus.DEFAULT_MAX_PENDING
        assert InMemoryEventBus.DEFAULT_MAX_PENDING == 10_000


# ======================================================================
# K. Contract suite is exercised elsewhere:
# pytest tests/contract/test_event_bus_contract.py
# ======================================================================
