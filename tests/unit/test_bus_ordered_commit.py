"""Phase 2 Batch 1.2: EventBus admission order must equal commit/dispatch
order, even when middleware (the real ``Recorder``, doing real durable I/O)
takes different amounts of time for different publishers.

Before this fix, ``InMemoryEventBus.publish()`` released its capacity lock
between reserving a slot and running middleware, then re-acquired it only to
append to ``_queue`` once middleware finished. Two concurrent publishers
could therefore commit in whichever order their middleware happened to
finish in, not the order they were admitted (and so not the order their own
``Event.sequence`` numbers reflect): a publisher admitted first but slow
through middleware could be overtaken by one admitted second but fast.

This is exactly the property Batch 1.1's replay watermark
(``Tidal.processed_input_sequence`` / the ``ORCHESTRATOR_TICK`` marker's
``processed_input_sequence``) depends on being true: it is a scalar,
prefix-style watermark ("every input with sequence <= N was applied"), and a
scalar prefix watermark is only mathematically honest if the bus never lets
a higher-sequence event commit while a lower-sequence one is still pending.
"""

from __future__ import annotations

import asyncio

import pytest

from core.bus.memory import InMemoryEventBus
from core.events import Event, EventType
from storage.memory import InMemoryEventStore
from storage.recorder import Recorder

START_MS = 1_788_000_000_000


def event(tag: str, i: int = 0) -> Event:
    return Event(
        type=EventType.SYSTEM_EVENT,
        ts_ms=START_MS + i,
        source="test",
        schema_name="Test",
        payload={"tag": tag},
    )


class GatedStore(InMemoryEventStore):
    """A real, working EventStore whose ``append_many`` can hold open for
    specific events -- simulating durable I/O that has started but not yet
    returned, without any fixed sleep or timing guess.
    """

    def __init__(self, gate_tags: set[str]) -> None:
        super().__init__()
        self.gate_tags = gate_tags
        self._gates: dict[str, asyncio.Event] = {}
        self.append_batches: list[list[str]] = []

    def _gate_for(self, tag: str) -> asyncio.Event:
        return self._gates.setdefault(tag, asyncio.Event())

    async def append_many(self, session_id, events):
        events = list(events)
        tags = [e.payload.get("tag") for e in events]
        self.append_batches.append(tags)
        for e in events:
            tag = e.payload.get("tag")
            if tag in self.gate_tags:
                await self._gate_for(tag).wait()
        await super().append_many(session_id, events)

    def release(self, tag: str) -> None:
        self._gate_for(tag).set()


async def _yield(n: int = 3) -> None:
    for _ in range(n):
        await asyncio.sleep(0)


class TestP2_13RealRecorderReproduction:
    async def test_a_slow_recorder_flush_cannot_let_a_later_publish_overtake_it(self, clock):
        """The exact P2-13 scenario: A's Recorder-triggered flush is stuck
        inside real durable I/O; B is published concurrently and its own
        flush completes fast. B must not be dispatched, or committed to the
        queue, before A -- regardless of which middleware call finished
        first.
        """
        store = GatedStore(gate_tags={"A"})
        bus = InMemoryEventBus(raise_on_handler_error=True)
        recorder = Recorder(store=store, clock=clock, buffer_size=1)
        await recorder.start()
        recorder.attach(bus)

        delivered: list[str] = []

        async def probe(e: Event) -> None:
            delivered.append(e.payload["tag"])

        bus.subscribe(probe, name="probe")

        event_a = event("A")
        event_b = event("B")

        task_a = asyncio.create_task(bus.publish(event_a))
        await _yield()  # let A's flush start and block inside append_many
        assert not task_a.done()

        task_b = asyncio.create_task(bus.publish(event_b))
        await task_b  # B's own flush is not gated -- its publish() call returns
        await _yield()

        # B's publish() call completing does NOT mean B reached the queue:
        # it must still be held behind A's unresolved admission.
        assert bus.queue_depth == 0, "neither A nor B may be in the dispatch queue yet"
        assert bus.admitted_total == 2, "both are admitted; neither has committed"
        assert delivered == []

        store.release("A")
        await task_a
        await bus.drain()

        assert delivered == ["A", "B"], (
            "B must never be dispatched before A, regardless of which "
            "middleware call (real Recorder flush I/O) finished first"
        )
        assert event_a.sequence is not None and event_b.sequence is not None
        assert event_a.sequence < event_b.sequence
        # The durable record agrees with dispatch order too (clause F).
        # Flushed explicitly: since Batch 2 the recorder holds accepted
        # events until storage confirms them and will not start a second
        # flush over a batch already in flight, so B is still pending here
        # rather than lost.
        await recorder.flush()
        assert store.append_batches == [["A"], ["B"]]


class TestConcurrentPublisherOrdering:
    async def test_a_two_publishers_one_blocked_dispatch_is_still_in_order(self, clock):
        bus = InMemoryEventBus(raise_on_handler_error=True)
        gate = asyncio.Event()
        delivered: list[str] = []

        async def slow_for_one(e: Event) -> None:
            if e.payload["tag"] == "one":
                await gate.wait()

        async def probe(e: Event) -> None:
            delivered.append(e.payload["tag"])

        bus.add_middleware(slow_for_one)
        bus.subscribe(probe, name="probe")

        task1 = asyncio.create_task(bus.publish(event("one")))
        await _yield()
        task2 = asyncio.create_task(bus.publish(event("two")))
        await task2
        await _yield()

        assert bus.queue_depth == 0
        gate.set()
        await task1
        await bus.drain()
        assert delivered == ["one", "two"]

    async def test_b_many_concurrent_publishers_deliver_in_strictly_increasing_sequence(
        self, clock
    ):
        bus = InMemoryEventBus(raise_on_handler_error=True)
        gates = {i: asyncio.Event() for i in range(20)}
        delivered_sequences: list[int] = []

        async def variable_delay(e: Event) -> None:
            await gates[e.payload["i"]].wait()

        async def probe(e: Event) -> None:
            delivered_sequences.append(e.sequence)

        bus.add_middleware(variable_delay)
        bus.subscribe(probe, name="probe")

        tasks = [
            asyncio.create_task(
                bus.publish(
                    Event(
                        type=EventType.SYSTEM_EVENT,
                        ts_ms=START_MS + i,
                        source="test",
                        schema_name="Test",
                        payload={"i": i},
                    )
                )
            )
            for i in range(20)
        ]
        await _yield()

        # Release the gates in REVERSE order -- the last-admitted publisher's
        # middleware finishes first, most adversarial to a naive "append on
        # middleware completion" implementation.
        for i in reversed(range(20)):
            gates[i].set()
            await asyncio.sleep(0)

        await asyncio.gather(*tasks)
        await bus.drain()

        assert delivered_sequences == sorted(delivered_sequences)
        assert len(delivered_sequences) == 20

    async def test_c_a_cancelled_earlier_publisher_does_not_block_later_ones(self, clock):
        bus = InMemoryEventBus(raise_on_handler_error=True)
        gate = asyncio.Event()
        delivered: list[str] = []

        async def slow_for_one(e: Event) -> None:
            if e.payload["tag"] == "one":
                await gate.wait()

        async def probe(e: Event) -> None:
            delivered.append(e.payload["tag"])

        bus.add_middleware(slow_for_one)
        bus.subscribe(probe, name="probe")

        task1 = asyncio.create_task(bus.publish(event("one")))
        await _yield()
        task2 = asyncio.create_task(bus.publish(event("two")))
        await task2

        task1.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task1

        await bus.drain()
        assert delivered == ["two"], "cancelling the earlier admission must not strand the later one"
        assert bus.reservation_released == 1

    async def test_d_a_middleware_exception_does_not_block_later_publishers(self, clock):
        bus = InMemoryEventBus(raise_on_handler_error=False)
        gate = asyncio.Event()
        delivered: list[str] = []

        async def fails_for_one(e: Event) -> None:
            if e.payload["tag"] == "one":
                await gate.wait()
                raise RuntimeError("boom")

        async def probe(e: Event) -> None:
            delivered.append(e.payload["tag"])

        bus.add_middleware(fails_for_one)
        bus.subscribe(probe, name="probe")

        task1 = asyncio.create_task(bus.publish(event("one")))
        await _yield()
        task2 = asyncio.create_task(bus.publish(event("two")))
        await task2

        gate.set()
        with pytest.raises(RuntimeError):
            await task1

        await bus.drain()
        assert delivered == ["two"]
        assert bus.reservation_released == 1

    async def test_e_hard_ceiling_stays_intact_across_admission_middleware_and_commit(
        self, clock
    ):
        bus = InMemoryEventBus(
            raise_on_handler_error=True, max_pending=5, cascade_reserve=2
        )
        gate = asyncio.Event()

        async def slow_for_first(e: Event) -> None:
            if e.payload["tag"] == "first":
                await gate.wait()

        bus.add_middleware(slow_for_first)

        first_task = asyncio.create_task(bus.publish(event("first")))
        await _yield()

        # Fill exactly to the external (max_pending) ceiling with the first
        # admission still unresolved and uncommitted.
        fillers = [asyncio.create_task(bus.publish(event(f"f{i}"))) for i in range(4)]
        await asyncio.gather(*fillers)
        assert bus.admitted_total == 5

        # A 6th external publish must now wait for capacity -- exactly as
        # before, unaffected by the ordered-commit buffering.
        sixth = asyncio.create_task(bus.publish(event("sixth")))
        await _yield()
        assert not sixth.done()
        assert bus.admitted_total <= bus.max_pending

        gate.set()
        await first_task
        # Committing "first" (and, cascading, the four fillers that were
        # already ready behind it) moves them from _reserved into _queue --
        # admitted_total is unchanged until something actually DISPATCHES
        # (queue emptying is what frees capacity, not merely committing).
        assert not sixth.done()
        await bus.drain()
        await sixth
        assert bus.admitted_total <= bus.hard_ceiling

    async def test_f_recorder_persistence_order_matches_dispatch_order(self, clock):
        """Restates the P2-13 reproduction as an explicit parity check
        between what got durably recorded and what subscribers saw.
        """
        store = GatedStore(gate_tags={"slow"})
        bus = InMemoryEventBus(raise_on_handler_error=True)
        recorder = Recorder(store=store, clock=clock, buffer_size=1)
        await recorder.start()
        recorder.attach(bus)

        delivered: list[str] = []
        bus.subscribe(lambda e: delivered.append(e.payload["tag"]) or _noop(), name="probe")

        slow_task = asyncio.create_task(bus.publish(event("slow")))
        await _yield()
        fast_task = asyncio.create_task(bus.publish(event("fast")))
        await fast_task
        await _yield()

        store.release("slow")
        await slow_task
        await bus.drain()
        await recorder.flush()

        recorded_order = [tag for batch in store.append_batches for tag in batch]
        assert recorded_order == ["slow", "fast"]
        assert delivered == recorded_order


async def _noop() -> None:
    return None
