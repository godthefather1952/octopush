"""Phase 2 Batch 1.2: RedisStreamBus admission order must equal XADD/dispatch
order, mirroring InMemoryEventBus's ordered-commit fix (see
tests/unit/test_bus_ordered_commit.py).

Requires a real Redis reachable at TF_TEST_REDIS_URL (default
redis://localhost:6399/0) -- Redis Stream IDs sort by write order, not by
Event.sequence, so this cannot be proven against a fake.
"""

from __future__ import annotations

import asyncio
import os

import pytest

from core.bus.redis_bus import RedisStreamBus
from core.events import Event, EventType

REDIS_URL = os.environ.get("TF_TEST_REDIS_URL", "redis://localhost:6399/0")
START_MS = 1_788_000_000_000


def _reachable() -> bool:
    import socket
    from urllib.parse import urlparse

    parsed = urlparse(REDIS_URL)
    try:
        with socket.create_connection((parsed.hostname, parsed.port or 6379), timeout=0.5):
            return True
    except OSError:
        return False


pytestmark = pytest.mark.skipif(not _reachable(), reason=f"no Redis at {REDIS_URL}")


def event(tag: str) -> Event:
    return Event(
        type=EventType.SYSTEM_EVENT,
        ts_ms=START_MS,
        source="test",
        schema_name="Test",
        payload={"tag": tag},
    )


async def _yield(n: int = 3) -> None:
    for _ in range(n):
        await asyncio.sleep(0)


@pytest.fixture
async def bus():
    import uuid

    b = RedisStreamBus(
        REDIS_URL,
        group=f"test-{uuid.uuid4().hex[:8]}",
        consumer="c1",
        stream=f"tf:test:{uuid.uuid4().hex[:12]}",
    )
    await b.start()
    yield b
    await b.stop()


class TestRedisOrderedCommit:
    async def test_a_slow_admitted_first_publisher_is_not_overtaken(self, bus):
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
        assert not task1.done()

        task2 = asyncio.create_task(bus.publish(event("two")))
        await _yield()
        # task2's own middleware is not gated, but publish() itself must not
        # return until its admission has resolved -- and it cannot resolve
        # ahead of "one" without violating ordered commit.
        assert not task2.done()

        gate.set()
        await task1
        await task2
        await bus.drain()

        assert delivered == ["one", "two"]

    async def test_b_concurrent_publishers_have_strictly_increasing_stream_ids(self, bus):
        n = 15
        gates = {i: asyncio.Event() for i in range(n)}
        delivered_order: list[int] = []

        async def variable_delay(e: Event) -> None:
            await gates[e.payload["i"]].wait()

        async def probe(e: Event) -> None:
            delivered_order.append(e.payload["i"])

        bus.add_middleware(variable_delay)
        bus.subscribe(probe, name="probe")

        tasks = [
            asyncio.create_task(
                bus.publish(
                    Event(
                        type=EventType.SYSTEM_EVENT,
                        ts_ms=START_MS,
                        source="test",
                        schema_name="Test",
                        payload={"i": i},
                    )
                )
            )
            for i in range(n)
        ]
        await _yield()

        # Resolve out of admission order -- most adversarial to a naive
        # "XADD on middleware completion" implementation.
        for i in reversed(range(n)):
            gates[i].set()
            await asyncio.sleep(0)

        await asyncio.gather(*tasks)
        await bus.drain()

        assert delivered_order == list(range(n)), (
            "dispatch order must match admission order regardless of which "
            "publisher's middleware resolved first"
        )
