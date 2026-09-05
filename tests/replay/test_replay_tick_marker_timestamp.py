"""Phase 2 Batch 1.2 -- second new finding: `Orchestrator.tick()` used to
capture the tick marker's `ts_ms` once, at the very top of the tick, before
`_persist()` awaits real durable I/O. If a market input is published and
dispatched to TIDAL *during* that await -- with a clock-advanced, later
`ts_ms` -- the input becomes part of this tick's `MarketState` (correctly
reflected in the marker's own `processed_input_sequence` watermark) while
the marker's own `ts_ms` stayed the stale, earlier value.

`EventStore.read()` sorts by `Event.sort_key()` == `(ts_ms, sequence, id)` --
timestamp PRIMARY. A marker with a stale `ts_ms` therefore sorts BEFORE an
input it says it already applied, which breaks Batch 1.1's deferred-input
release mechanism (`ReplaySession._pump_one`): a market input is only
released when some marker read AFTER it in stream order has a covering
watermark. A marker sorted BEFORE its own covered input can never do that --
the input just sits in `_pending_inputs` until some later tick's watermark
happens to cover it too, or (at worst) until the stream ends.

Both tests here reproduce this with a real ``Recorder``, a real
``InMemoryEventBus``, and a real ``EventStore`` double whose ``append_many``
can be held open deterministically -- not a fixed sleep or timing guess.
"""

from __future__ import annotations

import asyncio

from apps.orchestrator.wiring import build_platform
from core.bus import InMemoryEventBus
from core.clock import ManualClock
from core.config import simulated_venues
from core.events import Event, EventType
from replay.engine import ReplaySession, config_digest
from storage.memory import InMemoryEventStore
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


class BlockableStore(InMemoryEventStore):
    """A real, working EventStore whose very next ``append_many`` call can be
    held open -- simulating durable I/O that has started but not returned,
    without any fixed sleep or timing guess.

    Only the FIRST call after ``block_next()`` actually waits; every later
    call (in particular one for a different, smaller, unrelated batch)
    passes straight through -- which is exactly what lets a fast, unrelated
    flush proceed while an earlier, larger one is still stuck in I/O.
    """

    def __init__(self) -> None:
        super().__init__()
        self._gate: asyncio.Event | None = None

    def block_next(self) -> asyncio.Event:
        gate = asyncio.Event()
        self._gate = gate
        return gate

    async def append_many(self, session_id, events):
        gate, self._gate = self._gate, None
        if gate is not None:
            await gate.wait()
        await super().append_many(session_id, events)


async def _pump(n: int = 5) -> None:
    for _ in range(n):
        await asyncio.sleep(0)


async def _force_a_slow_second_tick_persist(platform, store: BlockableStore) -> asyncio.Event:
    """Get ``platform`` into the exact P2-Section-6 shape: tick 1 done, its
    events sitting unflushed in the recorder's buffer, and the NEXT
    ``_persist()`` call (tick 2's) forced to actually flush -- and held open
    on a real ``append_many`` call.
    """
    # No auto-flush on size or age until we force one explicitly.
    platform.recorder.buffer_size = 1_000_000
    platform.recorder.flush_interval_ms = 1_000_000

    await platform.start(record=True, feeds=False)
    await platform.bus.start()

    await platform.bus.publish(_snapshot_event(50_000.0, platform.clock.now_ms()))
    await platform.bus.wait_idle()
    await platform.orchestrator.tick()
    await platform.bus.wait_idle()

    # From here, ANY non-empty buffer is due -- the very next _persist()
    # call (tick 2's, flushing what tick 1 buffered) will actually flush.
    platform.recorder.flush_interval_ms = 0
    return store.block_next()


class TestTickMarkerTimestampStaleness:
    async def test_a_marker_ts_ms_can_precede_an_input_its_own_watermark_covers(
        self, settings
    ):
        clock = ManualClock(START_MS)
        bus = InMemoryEventBus(raise_on_handler_error=True)
        store = BlockableStore()
        platform = build_platform(
            settings.model_copy(update={"venues": simulated_venues()}),
            clock=clock,
            bus=bus,
            store=store,
            raise_on_handler_error=True,
        )
        gate = await _force_a_slow_second_tick_persist(platform, store)

        tick_task = asyncio.create_task(platform.orchestrator.tick())
        await _pump()
        assert not tick_task.done(), "tick 2 must actually be stuck inside _persist()"

        # While tick 2 is stuck -- after capturing its stale top-of-tick
        # `now` but before _observe() -- advance the clock and let a
        # materially different input arrive and be fully applied to TIDAL.
        clock.advance(2)
        moved = _snapshot_event(55_000.0, clock.now_ms())
        await bus.publish(moved)
        await bus.wait_idle()

        gate.set()
        await tick_task
        await platform.recorder.flush()

        events = [e async for e in store.read(platform.session_id)]
        markers = [e for e in events if e.type is EventType.ORCHESTRATOR_TICK]
        assert len(markers) == 2
        second_marker = markers[1]
        watermark = second_marker.payload["processed_input_sequence"]

        assert moved.sequence is not None and second_marker.sequence is not None
        # The watermark says tick 2 DID see `moved`...
        assert watermark is not None and watermark >= moved.sequence
        # ...so the marker must not claim to be earlier than an input it
        # says it already applied. This is the fix under test: current
        # (pre-fix) code reuses the stale top-of-tick `now` here instead of
        # a timestamp captured after _observe().
        assert second_marker.ts_ms >= moved.ts_ms, (
            "the tick marker's ts_ms must not precede an input its own "
            "watermark says it already applied"
        )
        moved_index = next(i for i, e in enumerate(events) if e.id == moved.id)
        marker_index = events.index(second_marker)
        assert marker_index > moved_index, (
            "EventStore read order (ts_ms, sequence, id) put the marker "
            "before the input its own watermark says it covers"
        )

        await platform.stop()

    async def test_b_replay_reproduces_the_original_ticks_market_state_despite_storage_order(
        self, settings
    ):
        """The consequence: replay must reconstruct tick 2's MarketState
        exactly as the original run built it -- including `moved` -- even
        though (pre-fix) the marker that should release it would sort
        before it in storage.

        Batch 1.1's deferred-input mechanism releases a pending input only
        when some marker read AFTER it has a covering watermark; a marker
        sorted BEFORE its own covered input can never do that, so
        (pre-fix) replay's tick 2 would hold `moved` back until some later
        tick or the end of the stream instead of tick 2, silently
        diverging from the original run. This test must fail on that
        pre-fix behaviour and pass once the marker's ts_ms is captured
        fresh.
        """
        clock = ManualClock(START_MS)
        bus = InMemoryEventBus(raise_on_handler_error=True)
        store = BlockableStore()
        recorded = build_platform(
            settings.model_copy(update={"venues": simulated_venues()}),
            clock=clock,
            bus=bus,
            store=store,
            raise_on_handler_error=True,
        )
        gate = await _force_a_slow_second_tick_persist(recorded, store)

        tick_task = asyncio.create_task(recorded.orchestrator.tick())
        await _pump()

        clock.advance(2)
        moved = _snapshot_event(55_000.0, clock.now_ms())
        await bus.publish(moved)
        await bus.wait_idle()

        gate.set()
        await tick_task
        original_second_tick_mid = recorded.state.market.venues["VENUE_A:BTC-USD"].metrics.mid
        assert original_second_tick_mid == 55_000.0, "sanity: the original tick did see `moved`"

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
        ticks_fired = 0
        replayed_second_tick_mid = None
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

        assert ticks_fired == 2
        assert replayed_second_tick_mid == original_second_tick_mid, (
            "replay's second tick must see exactly what the original "
            "second tick saw, regardless of the storage order of the "
            "marker and the input it covers"
        )

        await replayed.stop()
