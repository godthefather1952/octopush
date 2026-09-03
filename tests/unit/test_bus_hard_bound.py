"""Batch 5 pre-commit correction: a true hard bound, and a single publish
acceptance point shared by recording and enqueueing.

Two gaps in the original TIDAL-M7 fix:

1. Reentrant/cascade publishes were exempt from ``max_pending`` entirely, so
   there was no mathematically finite ceiling on queue size — only an
   argument that cascades are usually small. A recursive or accidental
   cascade publish loop had nothing capping it.

2. Middleware (the recorder) ran unconditionally before the capacity check,
   so an event blocked on capacity and then abandoned at shutdown could
   already be durably recorded despite never being enqueued or dispatched —
   a live/replay divergence: replay would see and process an event the
   original run never actually accepted.

This file proves both are closed: the queue has one finite ceiling
(``max_pending + cascade_reserve``) that no publish, however cascaded, can
exceed without an explicit, counted failure; and recording only ever happens
at the same moment enqueueing does, never before it is certain.
"""

from __future__ import annotations

import asyncio
import contextlib

import pytest

from core.bus.memory import CascadeCapacityExceeded, InMemoryEventBus
from core.clock import ManualClock
from core.events import Event, EventType
from storage.memory import InMemoryEventStore
from storage.recorder import Recorder

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
# Issue 1 — a true, finite hard bound on cascades
# ======================================================================


class TestTheHardCeilingCannotBeExceeded:
    async def test_an_infinitely_recursive_cascade_is_stopped_not_unbounded(self):
        """The worst case for a cascade bound: a handler that, unconditionally
        and forever, *fans out* two new events every time it runs -- pure
        exponential-shaped growth with no built-in stopping condition.
        Without a real ceiling this grows without limit. With one, growth
        must be capped and the excess must fail explicitly and be counted,
        never silently dropped, never a hang.

        Driven with a live background dispatcher for a bounded wall-clock
        window rather than ``drain()``: this handler never stops cascading on
        its own, so the only way this test can pass is the ceiling actually
        holding, not the cascade terminating by itself.
        """
        bus = InMemoryEventBus(max_pending=5, cascade_reserve=10, raise_on_handler_error=False)
        max_queue_seen = 0

        async def infinite_cascade_handler(e):
            nonlocal max_queue_seen
            max_queue_seen = max(max_queue_seen, bus.queue_depth)
            # A real suspension point: without one, this handler's cascade
            # publishes never actually return control to the event loop (a
            # coroutine that never awaits something pending does not yield),
            # so the test's own wall-clock timers would never get a turn to
            # fire and this would hang the process rather than time out.
            await asyncio.sleep(0)
            i = e.payload["i"]
            await bus.publish(event(2 * i + 1))
            await bus.publish(event(2 * i + 2))

        bus.subscribe(infinite_cascade_handler, name="infinite")
        await bus.start()
        await bus.publish(event(0))

        await asyncio.wait_for(asyncio.sleep(0.2), timeout=5)
        await asyncio.wait_for(bus.stop(), timeout=5)

        assert max_queue_seen <= bus.hard_ceiling == 15
        assert bus.cascade_overflow >= 1, "sustained fan-out must have hit the ceiling"
        # Reaching this line at all, under the 5s timeouts above, is itself
        # part of the proof: no deadlock, no runaway growth.

    async def test_the_ceiling_is_exactly_max_pending_plus_cascade_reserve(self):
        bus = InMemoryEventBus(max_pending=100, cascade_reserve=7)
        assert bus.hard_ceiling == 107
        assert bus.max_pending == 100
        assert bus.cascade_reserve == 7

    async def test_zero_or_negative_cascade_reserve_is_rejected(self):
        with pytest.raises(ValueError):
            InMemoryEventBus(cascade_reserve=0)
        with pytest.raises(ValueError):
            InMemoryEventBus(cascade_reserve=-1)

    async def test_the_default_cascade_reserve_is_documented_and_finite(self):
        bus = InMemoryEventBus()
        assert bus.cascade_reserve == InMemoryEventBus.DEFAULT_CASCADE_RESERVE == 1_000
        assert bus.hard_ceiling == bus.max_pending + bus.cascade_reserve


class TestCascadeOverflowDoesNotDeadlockOrDropSilently:
    async def test_overflow_raises_and_is_caught_by_failure_isolation(self):
        """A cascade that overflows must not hang the dispatcher, and the
        event that triggered it must still be delivered to every OTHER
        subscription (clause 5) even though the cascade publish itself
        failed.
        """
        bus = InMemoryEventBus(max_pending=1, cascade_reserve=1, raise_on_handler_error=False)
        other_sub_saw: list[int] = []

        async def cascading_handler(e):
            if e.payload["i"] == 0:
                # Fill the hard ceiling (2) with two more cascade publishes,
                # then a third must overflow.
                await bus.publish(event(1))
                await bus.publish(event(2))
                await bus.publish(event(3))  # must overflow: queue already at ceiling

        async def other_handler(e):
            other_sub_saw.append(e.payload["i"])

        bus.subscribe(cascading_handler, name="cascader")
        bus.subscribe(other_handler, name="other")

        await asyncio.wait_for(bus.publish(event(0)), timeout=5)
        await asyncio.wait_for(bus.drain(), timeout=5)

        assert 0 in other_sub_saw, "the triggering event must still be delivered elsewhere"
        assert bus.cascade_overflow >= 1
        # find the cascader subscription and confirm its failure was counted
        cascader_sub = next(s for s in bus.subscriptions if s.name == "cascader")
        assert cascader_sub.errors >= 1

    async def test_raise_on_handler_error_surfaces_the_specific_exception_type(self):
        bus = InMemoryEventBus(max_pending=1, cascade_reserve=1, raise_on_handler_error=True)

        async def cascading_handler(e):
            if e.payload["i"] == 0:
                await bus.publish(event(1))
                await bus.publish(event(2))
                with pytest.raises(CascadeCapacityExceeded):
                    await bus.publish(event(3))

        bus.subscribe(cascading_handler, name="cascader")
        await asyncio.wait_for(bus.publish(event(0)), timeout=5)
        await asyncio.wait_for(bus.drain(), timeout=5)


# ======================================================================
# Issue 2 — acceptance point: recording and enqueueing agree
# ======================================================================


class RecordingSpy:
    def __init__(self) -> None:
        self.seen: list[int] = []

    async def __call__(self, e: Event) -> None:
        self.seen.append(e.payload["i"])


class TestBlockedPublisherStoppedBeforeAcceptanceIsNeverRecorded:
    async def test_a_publish_abandoned_at_stop_never_reaches_middleware(self):
        bus = InMemoryEventBus(max_pending=1)
        spy = RecordingSpy()
        bus.add_middleware(spy)
        gate = asyncio.Event()

        async def slow_handler(_e):
            await gate.wait()

        bus.subscribe(slow_handler, name="slow")
        await bus.start()
        await bus.publish(event(0))  # popped immediately; blocks in slow_handler
        await bus.publish(event(1))  # fills the one queue slot
        await settle()
        assert bus.queue_depth == 1
        assert spy.seen == [0, 1], "sanity: both accepted events were recorded"

        blocked = asyncio.create_task(bus.publish(event(2)))
        await settle()
        assert not blocked.done(), "must actually be waiting for capacity"

        await asyncio.wait_for(bus.stop(), timeout=5)
        await asyncio.wait_for(blocked, timeout=5)
        gate.set()

        assert 2 not in spy.seen, (
            "an event abandoned before acceptance must never reach the recorder"
        )
        assert bus.discarded_at_stop >= 1


class TestAcceptedEventsAgreeOnRecordingAndDispatch:
    async def test_every_recorded_event_is_also_delivered(self):
        bus = InMemoryEventBus(max_pending=3, raise_on_handler_error=True)
        spy = RecordingSpy()
        bus.add_middleware(spy)
        delivered: list[int] = []

        async def handler(e):
            await asyncio.sleep(0)
            delivered.append(e.payload["i"])

        bus.subscribe(handler, name="h")
        await bus.start()
        for i in range(20):
            await bus.publish(event(i))
        await bus.drain()
        await bus.stop()

        assert spy.seen == list(range(20))
        assert delivered == list(range(20))
        assert spy.seen == delivered, "recording and dispatch must agree exactly"


class TestNoDuplicateRecordingFromCapacityRetries:
    async def test_a_publish_that_waits_multiple_times_is_recorded_exactly_once(self):
        bus = InMemoryEventBus(max_pending=1)
        spy = RecordingSpy()
        bus.add_middleware(spy)
        gate = asyncio.Event()

        async def slow_handler(_e):
            await gate.wait()

        bus.subscribe(slow_handler, name="slow")
        await bus.start()
        await bus.publish(event(0))  # popped immediately; blocks in slow_handler
        await settle()
        assert bus.queue_depth == 0
        await bus.publish(event(1))  # queue was empty; fills the one slot without waiting
        await settle()
        assert bus.queue_depth == 1

        # This third publish call must actually wait, get woken by
        # notify_all() on every subsequent dispatch (there are none, since
        # the handler is stuck) and by stop()/other churn -- across all of
        # that, record() must run at most once for this one publish call
        # once it finally succeeds or is abandoned.
        blocked = asyncio.create_task(bus.publish(event(2)))
        await settle()
        assert not blocked.done(), "must actually be waiting for capacity"

        gate.set()
        await asyncio.wait_for(blocked, timeout=5)
        await bus.drain()
        await bus.stop()

        assert spy.seen.count(2) == 1, "one publish call must record exactly once"


class TestReplaySeesExactlyWhatWasAccepted:
    async def test_the_durable_record_matches_the_accepted_set_not_the_attempted_set(self):
        clock = ManualClock(START_MS)
        store = InMemoryEventStore()
        await store.open()
        recorder = Recorder(store=store, clock=clock, session_id="sess-hard-bound")
        await recorder.start()

        bus = InMemoryEventBus(max_pending=1)
        recorder.attach(bus)
        gate = asyncio.Event()

        async def slow_handler(_e):
            await gate.wait()

        bus.subscribe(slow_handler, name="slow")
        await bus.start()
        await bus.publish(event(0))  # accepted: popped immediately
        await bus.publish(event(1))  # accepted: fills the one slot
        await settle()

        blocked = asyncio.create_task(bus.publish(event(2)))  # will be abandoned
        await settle()
        await asyncio.wait_for(bus.stop(), timeout=5)
        await asyncio.wait_for(blocked, timeout=5)
        gate.set()

        await recorder.stop()
        stored = [e async for e in store.read("sess-hard-bound")]
        stored_payload_is = sorted(e.payload["i"] for e in stored if e.type == EventType.SYSTEM_EVENT)

        assert stored_payload_is == [0, 1], (
            "the durable record must contain exactly the accepted events -- "
            "not the one that was published-but-abandoned at stop"
        )
        assert bus.published_count == 2
        assert bus.discarded_at_stop >= 1


class TestReentrantAcceptanceSemanticsMatchExternalPublishes:
    async def test_a_successful_cascade_publish_is_recorded_exactly_once(self):
        bus = InMemoryEventBus(max_pending=10, cascade_reserve=10, raise_on_handler_error=True)
        spy = RecordingSpy()
        bus.add_middleware(spy)

        async def cascading_handler(e):
            if e.payload["i"] == 0:
                await bus.publish(event(1))

        bus.subscribe(cascading_handler, name="cascader")
        await bus.publish(event(0))
        await bus.drain()

        assert spy.seen == [0, 1]
        assert spy.seen.count(1) == 1

    async def test_a_rejected_cascade_publish_is_never_recorded(self):
        bus = InMemoryEventBus(max_pending=1, cascade_reserve=1, raise_on_handler_error=False)
        spy = RecordingSpy()
        bus.add_middleware(spy)

        async def cascading_handler(e):
            if e.payload["i"] == 0:
                await bus.publish(event(1))
                await bus.publish(event(2))
                await bus.publish(event(3))  # must overflow and never be recorded

        bus.subscribe(cascading_handler, name="cascader")
        await bus.publish(event(0))
        await bus.drain()

        assert 3 not in spy.seen, "a publish refused by the cascade ceiling must never be recorded"
        assert bus.cascade_overflow >= 1


# ======================================================================
# Final pre-commit correction: concurrent admission across async middleware
# ======================================================================


class TestConcurrentAdmissionCannotJointlyExceedCapacity:
    async def test_concurrent_publishers_racing_through_slow_middleware_stay_bounded(self):
        """The exact race: middleware is async and can suspend, so a publish
        that has passed the capacity check but not yet finished middleware is
        vulnerable to *other* publishers checking capacity in the meantime.
        If admission only checked ``queue_depth``, N publishers could all see
        "room available" concurrently and all proceed, jointly exceeding
        ``max_pending``. This drives ten concurrent publishers against a
        bound of three, with middleware that deliberately suspends until
        released, and samples ``admitted_total`` from inside middleware --
        the one place the race, if it existed, would show up.
        """
        bus = InMemoryEventBus(max_pending=3, cascade_reserve=3)
        max_seen_admitted = 0
        entered_middleware = 0
        gate = asyncio.Event()

        async def slow_middleware(_e):
            nonlocal max_seen_admitted, entered_middleware
            entered_middleware += 1
            max_seen_admitted = max(max_seen_admitted, bus.admitted_total)
            await gate.wait()

        bus.add_middleware(slow_middleware)

        async def fast_handler(_e):
            return None

        bus.subscribe(fast_handler, name="fast")
        await bus.start()

        tasks = [asyncio.create_task(bus.publish(event(i))) for i in range(10)]
        await settle()

        assert entered_middleware <= 3, (
            "no more than max_pending publishers may ever be concurrently "
            "past admission at once"
        )
        assert max_seen_admitted <= 3
        assert bus.admitted_total <= 3
        assert not all(t.done() for t in tasks), "the rest must still be waiting for capacity"

        gate.set()
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=5)
        await bus.drain()
        await bus.stop()

        assert bus.published_count == 10
        assert bus.admitted_total == 0

    async def test_the_ceiling_formula_covers_queued_plus_reserved_for_cascades_too(self):
        """Same race, but for the cascade/reentrant path: a handler
        publishing several cascade events in a row, with slow middleware,
        must never let ``admitted_total`` exceed ``hard_ceiling`` even though
        each cascade publish enqueues "immediately" from its own caller's
        point of view (no waiting) -- immediately must still mean "after
        correctly accounting for reservations", not "without checking them".
        """
        bus = InMemoryEventBus(max_pending=2, cascade_reserve=2, raise_on_handler_error=False)
        max_seen_admitted = 0

        async def slow_middleware(_e):
            nonlocal max_seen_admitted
            max_seen_admitted = max(max_seen_admitted, bus.admitted_total)
            await asyncio.sleep(0)

        bus.add_middleware(slow_middleware)

        async def cascading_handler(e):
            if e.payload["i"] == 0:
                for i in range(1, 6):
                    with contextlib.suppress(CascadeCapacityExceeded):
                        await bus.publish(event(i))

        bus.subscribe(cascading_handler, name="cascader")
        await bus.publish(event(0))
        await bus.drain()

        assert max_seen_admitted <= bus.hard_ceiling == 4


class TestMiddlewareFailureReleasesTheReservation:
    async def test_a_raising_middleware_frees_capacity_for_the_next_waiter(self):
        bus = InMemoryEventBus(max_pending=1)
        should_fail = {"value": True}

        async def flaky_middleware(_e):
            if should_fail["value"]:
                raise RuntimeError("boom")

        bus.add_middleware(flaky_middleware)

        with pytest.raises(RuntimeError):
            await bus.publish(event(0))

        assert bus.reservation_released == 1
        assert bus.admitted_total == 0
        assert bus.queue_depth == 0
        assert bus.published_count == 0

        should_fail["value"] = False
        await bus.publish(event(1))  # must not be blocked by a leaked reservation
        assert bus.queue_depth == 1
        assert bus.published_count == 1

    async def test_a_failed_publish_is_never_recorded_by_a_later_successful_one(self):
        bus = InMemoryEventBus(max_pending=5)
        recorded: list[int] = []

        async def flaky_then_fine(e):
            if e.payload["i"] == 0:
                raise RuntimeError("boom")
            recorded.append(e.payload["i"])

        bus.add_middleware(flaky_then_fine)

        with pytest.raises(RuntimeError):
            await bus.publish(event(0))
        await bus.publish(event(1))

        assert recorded == [1]
        assert bus.queue_depth == 1

    async def test_a_cancelled_publish_releases_its_reservation(self):
        bus = InMemoryEventBus(max_pending=1)
        entered = asyncio.Event()

        async def hanging_middleware(_e):
            entered.set()
            await asyncio.Event().wait()  # never completes on its own

        bus.add_middleware(hanging_middleware)

        task = asyncio.create_task(bus.publish(event(0)))
        await entered.wait()
        assert bus.admitted_total == 1

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert bus.admitted_total == 0, "a cancelled publish must not leak its reservation"
        assert bus.reservation_released == 1
