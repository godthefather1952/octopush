"""Shared EventBus conformance suite.

Every clause of the contract in :mod:`core.bus.base`, asserted identically
against every implementation. This suite exists because the audit found the
two buses passing their own tests while disagreeing on ordering, drain and
subscription lifecycle — an interface is not valid because two classes share
method names.

Redis tests are skipped when no server is reachable, and the skip is loud:
``pytest -m redis`` with ``TF_TEST_REDIS_URL`` set runs them.
"""

from __future__ import annotations

import asyncio
import os
import resource
import time

import pytest

from core.bus import InMemoryEventBus
from core.bus.redis_bus import RedisStreamBus
from core.events import Event, EventType

REDIS_URL = os.environ.get("TF_TEST_REDIS_URL", "redis://localhost:6399/0")


def _redis_available() -> bool:
    import socket
    from urllib.parse import urlparse

    parsed = urlparse(REDIS_URL)
    try:
        with socket.create_connection((parsed.hostname or "localhost", parsed.port or 6379), 0.5):
            return True
    except OSError:
        return False


REDIS_UP = _redis_available()

_counter = 0


def _unique(prefix: str) -> str:
    global _counter
    _counter += 1
    return f"{prefix}-{os.getpid()}-{_counter}"


@pytest.fixture(params=["memory", "redis"])
async def bus(request):
    """Every test in this module runs against both implementations."""
    if request.param == "memory":
        made = InMemoryEventBus()
    else:
        if not REDIS_UP:
            pytest.skip(
                f"no Redis at {REDIS_URL} — run `redis-server --port 6399` "
                "or set TF_TEST_REDIS_URL"
            )
        name = _unique("tfcontract")
        made = RedisStreamBus(
            REDIS_URL, group=name, consumer="c1", stream=f"tf:test:{name}", block_ms=20
        )
    try:
        yield made
    finally:
        await made.stop()


def ev(source: str, kind: EventType = EventType.SYSTEM_EVENT, ts: int = 1) -> Event:
    return Event(type=kind, ts_ms=ts, source=source)


async def settle(bus, predicate, timeout: float = 4.0) -> bool:
    """Wait for a condition, bounded. Used only where the contract does not
    promise synchronous completion."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return predicate()


# --------------------------------------------------------------------------
# Clause 1 — total publication order across all event types
# --------------------------------------------------------------------------


class TestClause1PublicationOrder:
    async def test_single_type_order_is_preserved(self, bus):
        seen: list[str] = []

        async def handler(event):
            seen.append(event.source)

        bus.subscribe(handler, name="probe")
        await bus.start()
        for source in ("1", "2", "3", "4"):
            await bus.publish(ev(source))
        await bus.drain()
        assert seen == ["1", "2", "3", "4"]

    async def test_order_is_total_across_event_types(self, bus):
        """The regression for the measured 1,2,3,4 -> 3,1,2,4 defect."""
        seen: list[str] = []

        async def handler(event):
            seen.append(event.source)

        bus.subscribe(
            handler,
            types=[
                EventType.MARKET_STATE,
                EventType.AGENT_OPINION,
                EventType.RISK_PASS,
                EventType.OPPORTUNITY_DETECTED,
            ],
            name="probe",
        )
        await bus.start()
        sequence = [
            (EventType.MARKET_STATE, "1"),
            (EventType.AGENT_OPINION, "2"),
            (EventType.MARKET_STATE, "3"),
            (EventType.RISK_PASS, "4"),
            (EventType.OPPORTUNITY_DETECTED, "5"),
        ]
        for kind, source in sequence:
            await bus.publish(ev(source, kind))
        await bus.drain()
        assert seen == ["1", "2", "3", "4", "5"]

    async def test_interleaved_types_reach_separate_subscribers_in_order(self, bus):
        a_seen: list[str] = []
        b_seen: list[str] = []

        async def a(event):
            a_seen.append(event.source)

        async def b(event):
            b_seen.append(event.source)

        bus.subscribe(a, types=[EventType.MARKET_STATE], name="a")
        bus.subscribe(b, types=[EventType.AGENT_OPINION], name="b")
        await bus.start()
        for i in range(6):
            kind = EventType.MARKET_STATE if i % 2 == 0 else EventType.AGENT_OPINION
            await bus.publish(ev(str(i), kind))
        await bus.drain()
        assert a_seen == ["0", "2", "4"]
        assert b_seen == ["1", "3", "5"]


# --------------------------------------------------------------------------
# Clause 2 — at-least-once delivery, handlers must be idempotent
# --------------------------------------------------------------------------


class TestClause2Delivery:
    async def test_every_published_event_is_delivered_at_least_once(self, bus):
        seen: list[str] = []

        async def handler(event):
            seen.append(event.id)

        bus.subscribe(handler, name="probe")
        await bus.start()
        published = [ev(str(i)) for i in range(25)]
        for event in published:
            await bus.publish(event)
        await bus.drain()
        assert set(e.id for e in published) <= set(seen)

    async def test_duplicate_publication_reaches_the_handler_twice(self, bus):
        """At-least-once: the bus does not de-duplicate. Handlers must."""
        seen: list[str] = []

        async def handler(event):
            seen.append(event.id)

        bus.subscribe(handler, name="probe")
        await bus.start()
        event = ev("dup")
        await bus.publish(event)
        await bus.publish(event.model_copy())
        await bus.drain()
        assert len(seen) == 2

    async def test_a_handler_can_deduplicate_on_event_id(self, bus):
        applied: set[str] = set()
        calls = []

        async def idempotent(event):
            calls.append(event.id)
            if event.id in applied:
                return
            applied.add(event.id)

        bus.subscribe(idempotent, name="probe")
        await bus.start()
        event = ev("dup")
        await bus.publish(event)
        await bus.publish(event.model_copy())
        await bus.drain()
        assert len(calls) == 2 and len(applied) == 1


# --------------------------------------------------------------------------
# Clause 3 — subscription lifecycle
# --------------------------------------------------------------------------


class TestClause3SubscriptionLifecycle:
    async def test_subscribe_before_start_receives_events(self, bus):
        seen: list[str] = []

        async def handler(event):
            seen.append(event.source)

        bus.subscribe(handler, name="early")
        await bus.start()
        await bus.publish(ev("x"))
        await bus.drain()
        assert seen == ["x"]

    async def test_subscribe_after_start_receives_subsequent_events(self, bus):
        """The regression for 'late subscriber never delivered' on Redis."""
        await bus.start()
        seen: list[str] = []

        async def handler(event):
            seen.append(event.source)

        bus.subscribe(handler, name="late")
        await bus.publish(ev("after"))
        await bus.drain()
        assert seen == ["after"]

    async def test_late_subscriber_does_not_receive_history(self, bus):
        await bus.start()
        await bus.publish(ev("before"))
        await bus.drain()

        seen: list[str] = []

        async def handler(event):
            seen.append(event.source)

        bus.subscribe(handler, name="late")
        await bus.publish(ev("after"))
        await bus.drain()
        assert seen == ["after"]

    async def test_unsubscribe_stops_delivery(self, bus):
        seen: list[str] = []

        async def handler(event):
            seen.append(event.source)

        sub = bus.subscribe(handler, name="probe")
        await bus.start()
        await bus.publish(ev("one"))
        await bus.drain()
        bus.unsubscribe(sub)
        await bus.publish(ev("two"))
        await bus.drain()
        assert seen == ["one"]

    async def test_multiple_subscribers_each_receive_every_matching_event(self, bus):
        a, b, c = [], [], []

        async def ha(e):
            a.append(e.source)

        async def hb(e):
            b.append(e.source)

        async def hc(e):
            c.append(e.source)

        for handler, name in ((ha, "a"), (hb, "b"), (hc, "c")):
            bus.subscribe(handler, name=name)
        await bus.start()
        for i in range(4):
            await bus.publish(ev(str(i)))
        await bus.drain()
        assert a == b == c == ["0", "1", "2", "3"]

    async def test_type_filter_excludes_other_types(self, bus):
        seen: list[EventType] = []

        async def handler(event):
            seen.append(event.type)

        bus.subscribe(handler, types=[EventType.PAPER_FILL], name="probe")
        await bus.start()
        await bus.publish(ev("a", EventType.PAPER_FILL))
        await bus.publish(ev("b", EventType.SYSTEM_EVENT))
        await bus.drain()
        assert seen == [EventType.PAPER_FILL]


# --------------------------------------------------------------------------
# Clause 4 — drain() is a local completion barrier
# --------------------------------------------------------------------------


class TestClause4Drain:
    async def test_drain_guarantees_handlers_have_run(self, bus):
        """The regression for 'publish + drain, handler saw nothing' on Redis."""
        seen: list[str] = []

        async def handler(event):
            seen.append(event.source)

        bus.subscribe(handler, types=[EventType.AGENT_OPINION], name="probe")
        await bus.start()
        await bus.publish(ev("opinion", EventType.AGENT_OPINION))
        await bus.drain()
        assert seen == ["opinion"], "drain() returned before the handler ran"

    async def test_drain_covers_a_burst(self, bus):
        seen: list[str] = []

        async def handler(event):
            seen.append(event.source)

        bus.subscribe(handler, name="probe")
        await bus.start()
        for i in range(50):
            await bus.publish(ev(str(i)))
        await bus.drain()
        assert len(seen) == 50

    async def test_drain_on_an_empty_bus_returns(self, bus):
        await bus.start()
        await bus.drain()

    async def test_drain_waits_for_a_slow_handler(self, bus):
        finished = []

        async def slow(event):
            await asyncio.sleep(0.05)
            finished.append(event.source)

        bus.subscribe(slow, name="slow")
        await bus.start()
        await bus.publish(ev("slow-one"))
        await bus.drain()
        assert finished == ["slow-one"]


# --------------------------------------------------------------------------
# Clause 5 — subscriber failure isolation
# --------------------------------------------------------------------------


class TestClause5FailureIsolation:
    async def test_a_raising_handler_does_not_block_others(self, bus):
        survived: list[str] = []

        async def broken(event):
            raise RuntimeError("handler exploded")

        async def fine(event):
            survived.append(event.source)

        broken_sub = bus.subscribe(broken, name="broken")
        bus.subscribe(fine, name="fine")
        await bus.start()
        await bus.publish(ev("x"))
        await bus.drain()
        assert survived == ["x"]
        assert broken_sub.errors == 1

    async def test_the_bus_keeps_running_after_a_handler_failure(self, bus):
        survived: list[str] = []

        async def broken(event):
            raise RuntimeError("nope")

        async def fine(event):
            survived.append(event.source)

        bus.subscribe(broken, name="broken")
        bus.subscribe(fine, name="fine")
        await bus.start()
        for i in range(5):
            await bus.publish(ev(str(i)))
        await bus.drain()
        assert survived == ["0", "1", "2", "3", "4"]


# --------------------------------------------------------------------------
# Clause 6 — event identity
# --------------------------------------------------------------------------


class TestClause6Identity:
    async def test_event_ids_are_present_and_unique(self, bus):
        seen: list[str] = []

        async def handler(event):
            seen.append(event.id)

        bus.subscribe(handler, name="probe")
        await bus.start()
        for i in range(30):
            await bus.publish(ev(str(i)))
        await bus.drain()
        assert all(seen)
        assert len(set(seen)) == 30

    async def test_sequence_is_assigned_and_strictly_increasing(self, bus):
        events = [ev(str(i)) for i in range(10)]
        await bus.start()
        for event in events:
            await bus.publish(event)
        sequences = [e.sequence for e in events]
        assert all(s is not None for s in sequences)
        assert sequences == sorted(sequences)
        assert len(set(sequences)) == len(sequences)

    async def test_a_preassigned_sequence_is_preserved(self, bus):
        event = ev("x")
        event.sequence = 999
        await bus.start()
        await bus.publish(event)
        assert event.sequence == 999


# --------------------------------------------------------------------------
# Clause 7 — shutdown discards undispatched events
# --------------------------------------------------------------------------


class TestClause7Shutdown:
    async def test_drain_before_stop_delivers_everything(self, bus):
        seen: list[str] = []

        async def handler(event):
            seen.append(event.source)

        bus.subscribe(handler, name="probe")
        await bus.start()
        for i in range(20):
            await bus.publish(ev(str(i)))
        await bus.drain()
        await bus.stop()
        assert len(seen) == 20

    async def test_stop_is_idempotent(self, bus):
        await bus.start()
        await bus.stop()
        await bus.stop()


# --------------------------------------------------------------------------
# Clause 8 — observability of queue depth; idle behaviour
# --------------------------------------------------------------------------


class TestClause8Observability:
    async def test_queue_depth_is_exposed(self, bus):
        await bus.start()
        assert bus.queue_depth >= 0
        await bus.drain()
        assert bus.queue_depth == 0

    async def test_subscriptions_are_exposed(self, bus):
        async def handler(event):
            return None

        bus.subscribe(handler, name="named")
        assert any(s.name == "named" for s in bus.subscriptions)

    async def test_idle_bus_does_not_burn_cpu(self, bus):
        """Regression for the busy-spin defect: the old dispatcher held a core
        at 100% while idle, starving the market feed.

        Asserts a generous ceiling (25%) rather than the measured ~0.7%, so
        normal CI contention cannot cause a spurious failure while a true spin
        loop (which pins ~100%) still fails.
        """
        await bus.start()
        await asyncio.sleep(0.2)

        def cpu() -> float:
            usage = resource.getrusage(resource.RUSAGE_SELF)
            return usage.ru_utime + usage.ru_stime

        before, wall_before = cpu(), time.monotonic()
        await asyncio.sleep(1.5)
        used, wall = cpu() - before, time.monotonic() - wall_before
        utilisation = used / wall
        assert utilisation < 0.25, f"idle bus used {utilisation:.1%} CPU — busy-spin regression"

    async def test_an_idle_bus_still_wakes_promptly(self, bus):
        """The other half of the idle guarantee: blocking must not cost latency."""
        seen = asyncio.Event()

        async def handler(event):
            seen.set()

        bus.subscribe(handler, name="probe")
        await bus.start()
        await asyncio.sleep(0.3)
        started = time.monotonic()
        await bus.publish(ev("wake"))
        assert await settle(bus, lambda: seen.is_set(), timeout=2.0)
        assert time.monotonic() - started < 1.0
