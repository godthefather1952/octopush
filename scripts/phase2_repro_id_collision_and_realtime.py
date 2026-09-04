"""Phase 2 findings P2-6 (closed) and P2-3 (still open).

1. **P2-6 -- CLOSED by Batch 2.** Reusing an event id for a *different*
   event used to be silently dropped, across all backends, with no warning
   anywhere in the call path. It is now an ``EventIdCollision``; only a
   byte-identical re-delivery is still the intended idempotent no-op.
2. **P2-3 -- STILL OPEN.** ``ReplayMode.REALTIME`` does not pace against
   wall-clock time at all: ``ManualClock.sleep(0)`` is a no-op, so a
   "REALTIME 1x" replay runs at the same speed as FAST. Batch 2 deliberately
   did not remediate this; the reproduction below still stands.

    python -m scripts.phase2_repro_id_collision_and_realtime
"""

from __future__ import annotations

import asyncio
import time

from core.clock import ManualClock
from core.events import Event, EventType
from storage.base import EventIdCollision
from storage.memory import InMemoryEventStore


async def id_collision() -> bool:
    store = InMemoryEventStore()
    await store.open()
    sid = "repro-id-collision"
    await store.start_session(sid, started_at=0)

    first = Event(id="bus-fixed-id", type=EventType.SYSTEM_EVENT, ts_ms=100, source="A",
                  payload={"which": "first"})
    second = Event(id="bus-fixed-id", type=EventType.SYSTEM_EVENT, ts_ms=200, source="B",
                   payload={"which": "SECOND -- completely different event"})
    await store.append(sid, first)

    print("=" * 70)
    print("P2-6 verification: same event_id, different payload")
    print("=" * 70)
    refused = False
    try:
        await store.append(sid, second)
    except EventIdCollision as exc:
        refused = True
        print(f"second append refused: {type(exc).__name__}")
        print(f"  {exc}")
    else:
        print("second append ACCEPTED: DEFECT NOT CLOSED")

    # ...while an exact re-delivery of the FIRST event is still a no-op, which
    # is what makes retry-after-unknown-commit-outcome safe.
    await store.append(sid, first)
    stored = [e async for e in store.read(sid)]
    print(f"events stored after 1 collision + 1 exact retry: {len(stored)}")
    print(f"stored payload: {stored[0].payload}")
    idempotent = len(stored) == 1 and stored[0].payload == {"which": "first"}
    print(
        "A reused id carrying different content is now an error the caller "
        "must handle; an identical re-delivery still converges on one copy."
    )
    return refused and idempotent


async def realtime_is_a_no_op() -> None:
    clock = ManualClock(0)
    print()
    print("=" * 70)
    print("P2-3 reproduction (STILL OPEN): ReplayMode.REALTIME pacing")
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


async def main() -> int:
    closed = await id_collision()
    await realtime_is_a_no_op()
    return 0 if closed else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
