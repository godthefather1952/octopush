"""Phase 2 Batch 1.2 Section 9: after the ordered-commit fix (P2-13,
``core/bus/memory.py`` / ``core/bus/redis_bus.py``), prove
``Tidal.processed_input_sequence`` really is a gap-free prefix watermark:
"every MARKET_INPUT event with sequence <= N has been applied to TIDAL" --
not merely "the highest sequence TIDAL happened to see so far", which is
only the same claim if the bus can never let a higher-sequence event reach
TIDAL while a lower-sequence one is still outstanding.

The proof: admit many publishers concurrently with deliberately reversed
middleware completion order (the exact P2-13 adversarial shape), and record
``processed_input_sequence`` on every dispatch. Ordered commit
(admission order == dispatch order == sequence order, for a fresh sequence
on one bus instance) plus the bus's own strictly-serial dispatch loop
(one queued item fully handled before the next is popped) together force
TIDAL's own watermark to advance by exactly one, in order, on every single
dispatched market input -- so at any point in time it names the exact count
of inputs applied, with no possible gap.

A mutation test (reverting the ordered-commit fix in ``core/bus/memory.py``)
is intentionally not duplicated here -- ``tests/unit/test_bus_ordered_commit.py``
already proves the bus-level property this test's proof rests on; this file
proves the one additional link (TIDAL's own watermark tracks it exactly).
"""

from __future__ import annotations

import asyncio

from agents.tidal import Tidal
from core.bus import InMemoryEventBus
from core.events import Event, EventType
from core.health import HealthRegistry
from tests.conftest import START_MS, make_book


def _snapshot(i: int) -> Event:
    book = make_book("VENUE_A", "BTC-USD", 50_000.0 + i, ts=START_MS + i)
    return Event(
        type=EventType.BOOK_SNAPSHOT,
        ts_ms=START_MS + i,
        source="VENUE_A",
        schema_name="OrderBookSnapshot",
        payload=book.to_json_dict(),
    )


async def _yield(n: int = 3) -> None:
    for _ in range(n):
        await asyncio.sleep(0)


class TestWatermarkIsARealPrefix:
    async def test_processed_input_sequence_advances_with_no_gaps_under_concurrent_admission(
        self, settings, clock
    ):
        bus = InMemoryEventBus(raise_on_handler_error=True)
        health = HealthRegistry(clock=clock)
        tidal = Tidal(bus, clock, settings, health)
        tidal.subscribe()

        # Registered AFTER tidal's own subscription, so by the time this
        # runs for a given event, tidal.on_event has already returned for
        # that same event -- the bus awaits each subscriber for one queued
        # item fully before moving to the next.
        watermark_at_dispatch: list[int] = []

        async def probe(event: Event) -> None:
            watermark_at_dispatch.append(tidal.processed_input_sequence)

        bus.subscribe(probe, types=[EventType.BOOK_SNAPSHOT], name="probe-after-tidal")

        n = 25
        gates = {i: asyncio.Event() for i in range(n)}

        async def variable_delay(event: Event) -> None:
            i = round(event.payload["received_ts"] - START_MS)
            await gates[i].wait()

        bus.add_middleware(variable_delay)

        tasks = [asyncio.create_task(bus.publish(_snapshot(i))) for i in range(n)]
        await _yield()

        # Reverse completion order: the last-admitted publisher's middleware
        # resolves first -- most adversarial to anything that lets dispatch
        # order track middleware-completion order instead of admission order.
        for i in reversed(range(n)):
            gates[i].set()
            await asyncio.sleep(0)

        await asyncio.gather(*tasks)
        await bus.drain()

        assert watermark_at_dispatch == list(range(1, n + 1)), (
            "processed_input_sequence must advance by exactly one, in "
            "order, on every dispatch -- any repeat or skip here is a gap "
            "in the prefix the watermark claims to represent"
        )
        assert tidal.processed_input_sequence == n
