"""Phase 2 audit reproduction: two more findings, quick to demonstrate.

1. Same event_id, different payload: silently dropped, across all backends,
   with no warning anywhere in the call path.
2. ReplayMode.REALTIME does not pace against wall-clock time at all --
   ``ManualClock.sleep(0)`` is a no-op, so a "REALTIME 1x" replay runs at the
   same speed as FAST.
"""

from __future__ import annotations

import asyncio
import time

from core.clock import ManualClock
from core.events import Event, EventType
from storage.memory import InMemoryEventStore


async def id_collision() -> None:
    store = InMemoryEventStore()
    await store.open()
    sid = "repro-id-collision"
    await store.start_session(sid, started_at=0)

    first = Event(id="bus-fixed-id", type=EventType.SYSTEM_EVENT, ts_ms=100, source="A",
                  payload={"which": "first"})
    second = Event(id="bus-fixed-id", type=EventType.SYSTEM_EVENT, ts_ms=200, source="B",
                   payload={"which": "SECOND -- completely different event"})
    await store.append(sid, first)
    await store.append(sid, second)

    stored = [e async for e in store.read(sid)]
    print("=" * 70)
    print("Reproduction: same event_id, different payload")
    print("=" * 70)
    print(f"events appended: 2, events actually stored: {len(stored)}")
    print(f"stored payload: {stored[0].payload}")
    print("The second event vanished silently -- no exception, no log, no")
    print("count of dropped events anywhere the caller can observe.")


async def realtime_is_a_no_op() -> None:
    clock = ManualClock(0)
    print()
    print("=" * 70)
    print("Reproduction: ReplayMode.REALTIME pacing")
    print("=" * 70)
    wall_start = time.monotonic()
    for _ in range(500):
        # Exactly what ReplaySession.run() does per event in REALTIME mode.
        await clock.sleep(0)
    wall_elapsed = time.monotonic() - wall_start
    print(f"500 calls to clock.sleep(0) (REALTIME's own per-event pacing call) "
          f"took {wall_elapsed:.4f}s wall-clock")
    print("If REALTIME actually paced 1x against wall time and events spanned "
          "e.g. 50 seconds of recorded time, this loop would take ~50s. It "
          "does not -- sleep(0) on ManualClock always takes the immediate "
          "early-return path (deadline <= now) and never advances or waits "
          "on wall-clock time regardless of `speed`.")


async def main() -> None:
    await id_collision()
    await realtime_is_a_no_op()


if __name__ == "__main__":
    asyncio.run(main())
