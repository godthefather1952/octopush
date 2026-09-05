"""Phase 2 Batch 1.1: replay must observe exactly the market-input state a
tick's ORIGINAL snapshot actually applied -- not merely every input
published before the tick's boundary marker.

Batch 1's ``ORCHESTRATOR_TICK`` marker fixed replay's TICK CADENCE (one tick
per original tick, not one per market event) by recording the marker's own
position in the bus's publication-sequence order. That is not the same fact
as "which inputs had TIDAL actually applied when the tick's ``MarketState``
was built": under an asynchronous bus (``core/bus/memory.py``'s own
background dispatcher, ``bus.start()``, used by every live/production run --
``apps/orchestrator/__main__.py:64``), a market input can be published (and
so recorded, with a sequence number) well before a later tick's marker while
its DISPATCH to TIDAL's own handler is still pending. Publication order is
not delivery order.

This is reproduced with a real ``InMemoryEventBus`` background dispatcher
and a subscriber registered ahead of TIDAL that can hold one specific
input's dispatch open indefinitely -- not the artificial
publish-then-immediately-drain pattern every other replay test uses, which
makes the two orders coincide by construction and so cannot exercise this at
all.
"""

from __future__ import annotations

import asyncio

from apps.orchestrator.wiring import build_platform
from core.bus import InMemoryEventBus
from core.clock import ManualClock
from core.config import simulated_venues
from core.events import Event, EventType
from replay.engine import ReplaySession, config_digest
from storage import InMemoryEventStore
from tests.conftest import START_MS, make_book


def _snapshot_event(mid: float, ts: int) -> Event:
    book = make_book("VENUE_A", "BTC-USD", mid, ts=ts)
    return Event(
        type=EventType.BOOK_SNAPSHOT,
        ts_ms=ts,
        source="VENUE_A",
        schema_name="OrderBookSnapshot",
        payload=book.to_json_dict(),
    )


class _Gate:
    """Holds BOOK_SNAPSHOT dispatch open for every subscriber after it (in
    particular TIDAL) once armed, until explicitly released. Registered on
    the bus before ``build_platform()`` so it dispatches ahead of TIDAL's
    own subscription (subscriber registration order).
    """

    def __init__(self) -> None:
        self.armed = False
        self._release = asyncio.Event()
        self.blocked_ids: list[str] = []

    async def __call__(self, event: Event) -> None:
        if event.type is EventType.BOOK_SNAPSHOT and self.armed:
            self.blocked_ids.append(event.id)
            await self._release.wait()

    def release(self) -> None:
        self._release.set()


async def _pump(n: int = 5) -> None:
    """Yield to the event loop so the background dispatcher can advance."""
    for _ in range(n):
        await asyncio.sleep(0)


class TestPublicationVsDeliveryRace:
    async def test_tick_watermark_excludes_an_input_still_blocked_mid_dispatch(
        self, settings
    ):
        bus = InMemoryEventBus(raise_on_handler_error=True)
        clock = ManualClock(START_MS)
        store = InMemoryEventStore()
        gate = _Gate()
        bus.subscribe(gate, types=[EventType.BOOK_SNAPSHOT], name="gate")

        platform = build_platform(
            settings.model_copy(update={"venues": simulated_venues()}),
            clock=clock,
            bus=bus,
            store=store,
            raise_on_handler_error=True,
        )
        await platform.start(record=True, feeds=False)
        await bus.start()

        # An initial valid book, allowed through normally (gate not armed).
        await bus.publish(_snapshot_event(50_000.0, clock.now_ms()))
        await bus.wait_idle()
        await platform.orchestrator.tick()

        # Arm the gate, then publish a second snapshot that materially moves
        # the market. It receives a sequence number and is durably queued
        # (and will be recorded), but its dispatch to TIDAL is held open.
        gate.armed = True
        moved = _snapshot_event(55_000.0, clock.now_ms())
        await bus.publish(moved)
        await _pump()
        assert gate.blocked_ids == [moved.id], "the gate must actually be holding it"

        # The next tick can proceed through _observe() -- TIDAL's book read
        # is a direct, synchronous read of its own state, not gated by bus
        # dispatch -- but will then block, inside its own bus.drain() call,
        # on the very same stuck dispatcher. The watermark is captured
        # (inside publish_state(), before that drain call) regardless.
        tick_task = asyncio.create_task(platform.orchestrator.tick())
        await _pump()
        assert not tick_task.done(), "the tick must actually be stuck on the gate too"

        gate.release()
        await tick_task

        await platform.recorder.flush()
        events = [e async for e in store.read(platform.session_id)]
        markers = [e for e in events if e.type is EventType.ORCHESTRATOR_TICK]
        assert len(markers) == 2
        second_marker = markers[1]
        watermark = second_marker.payload["processed_input_sequence"]

        assert moved.sequence is not None and second_marker.sequence is not None
        # Published (and recorded) BEFORE the marker in stream position...
        assert moved.sequence < second_marker.sequence
        # ...but the tick's own watermark proves it had NOT actually been
        # applied when the snapshot was taken. Publication order alone
        # would have wrongly said otherwise -- this is the bug.
        assert watermark < moved.sequence

        # Nothing is lost: a further tick observes it normally.
        await platform.orchestrator.tick()
        third_state = platform.state.market.venues["VENUE_A:BTC-USD"]
        assert third_state.metrics.mid == 55_000.0

        await platform.stop()

    async def test_replay_reproduces_the_deferred_tick_exactly(self, settings):
        """The full round-trip: replay this exact race and confirm the
        replayed second tick's MarketState matches the ORIGINAL second
        tick's -- excluding the still-blocked input -- not the input's
        eventual, later-visible state.
        """
        bus = InMemoryEventBus(raise_on_handler_error=True)
        clock = ManualClock(START_MS)
        store = InMemoryEventStore()
        gate = _Gate()
        bus.subscribe(gate, types=[EventType.BOOK_SNAPSHOT], name="gate")

        recorded = build_platform(
            settings.model_copy(update={"venues": simulated_venues()}),
            clock=clock,
            bus=bus,
            store=store,
            raise_on_handler_error=True,
        )
        await recorded.start(record=True, feeds=False)
        await bus.start()

        await bus.publish(_snapshot_event(50_000.0, clock.now_ms()))
        await bus.wait_idle()
        await recorded.orchestrator.tick()

        gate.armed = True
        moved = _snapshot_event(55_000.0, clock.now_ms())
        await bus.publish(moved)
        await _pump()

        tick_task = asyncio.create_task(recorded.orchestrator.tick())
        await _pump()
        gate.release()
        await tick_task

        original_second_tick_mid = recorded.state.market.venues["VENUE_A:BTC-USD"].metrics.mid

        await recorded.recorder.flush()
        session_id = recorded.session_id
        await recorded.stop()

        replay_clock = ManualClock(START_MS)
        replay_bus = InMemoryEventBus(raise_on_handler_error=True)
        replay_settings = settings.model_copy(update={"venues": simulated_venues()})
        replayed = build_platform(
            replay_settings,
            clock=replay_clock,
            bus=replay_bus,
            store=InMemoryEventStore(),
            raise_on_handler_error=True,
        )
        await replayed.start(record=False, feeds=False)
        session = ReplaySession(
            store=store,
            bus=replay_bus,
            clock=replay_clock,
            session_id=session_id,
            current_config_hash=config_digest(replay_settings.model_dump()),
        )
        assert session
        ticks_fired = 0
        with session:
            await session.open()
            assert session.stats.timeline_fidelity is None
            while True:
                event = await session.step()
                if event is None:
                    break
                if event.type is EventType.ORCHESTRATOR_TICK:
                    await replayed.orchestrator.tick()
                    ticks_fired += 1
                    if ticks_fired == 2:
                        replayed_second_tick_mid = replayed.state.market.venues[
                            "VENUE_A:BTC-USD"
                        ].metrics.mid

        assert replayed_second_tick_mid == original_second_tick_mid
        assert replayed_second_tick_mid != 55_000.0, (
            "the replayed second tick must NOT see the still-blocked snapshot, "
            "exactly like the original tick it reproduces"
        )
