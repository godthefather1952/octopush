"""Phase 2 Batch 1.2 Section 11: re-affirm original-vs-replay economic
equivalence under the exact conditions Batch 1.2 fixed -- concurrent
publishers whose middleware (the real Recorder) finishes out of admission
order, a stale-marker-timestamp race, and multiple venues publishing at
once. None of these tests serialize every market publication with
``bus.drain()`` the way ``tests/conftest.run_platform`` (used by the
800-tick Batch-1 equivalence test) does -- that pattern makes publication
order and dispatch order coincide by construction, which is exactly the
condition under which the P2-13 race (and the marker-timestamp race) cannot
occur at all.
"""

from __future__ import annotations

import asyncio

import pytest

from apps.orchestrator.wiring import build_platform
from core.bus import InMemoryEventBus
from core.clock import ManualClock
from core.config import simulated_venues
from core.events import Event, EventType
from replay.engine import ReplaySession, config_digest
from storage.memory import InMemoryEventStore
from tests.conftest import START_MS, make_book
from tests.replay.test_replay import fresh_platform, replay_equivalence_summary
from tests.replay.test_replay_tick_marker_timestamp import BlockableStore


async def _replay(settings, store, session_id: str):
    clock = ManualClock(START_MS)
    bus = InMemoryEventBus(raise_on_handler_error=True)
    # The exact settings the ORIGINAL run recorded its digest from, so the
    # replay is comparing like with like.
    replay_settings = settings.model_copy(update={"venues": simulated_venues()})
    platform = build_platform(
        replay_settings,
        clock=clock,
        bus=bus,
        store=InMemoryEventStore(),
        raise_on_handler_error=True,
    )
    await platform.start(record=False, feeds=False)
    session = ReplaySession(
        store=store,
        bus=bus,
        clock=clock,
        session_id=session_id,
        # The recorded session carries this digest, so supplying it is what
        # lets the replay claim configuration fidelity rather than merely
        # never having checked (Phase 2 finalization, P2-9).
        current_config_hash=config_digest(replay_settings.model_dump()),
    )
    with session:
        await session.open()
        while True:
            event = await session.step()
            if event is None:
                break
            if event.type is EventType.ORCHESTRATOR_TICK:
                await platform.orchestrator.tick()
    return platform, session


async def run_concurrent_session(recorded, ticks: int):
    """Drive ``recorded`` for ``ticks`` under genuinely concurrent scheduling.

    A real background bus dispatcher, an independently-scheduled
    feed-publication task and an independently-scheduled orchestrator-tick
    task, with neither task draining the bus to synchronize with the other.
    """
    await recorded.bus.start()

    # A bounded producer/consumer handoff, NOT bus.drain(): tick_loop may run
    # at most one tick ahead of what feed_loop has fed it, but the two are
    # otherwise free to interleave however asyncio happens to schedule them
    # -- unlike ManualClock.sleep()-based pacing (which risks the two loops'
    # waiter deadlines drifting out of lockstep and deadlocking once one loop
    # finishes advancing the clock before the other's last wait can ever be
    # released), this handoff cannot deadlock: feed_loop always eventually
    # unblocks tick_loop's wait.
    progress = asyncio.Condition()
    feed_done = 0
    tick_done = 0

    async def feed_loop() -> None:
        nonlocal feed_done
        for _ in range(ticks):
            recorded.clock.advance(100)
            # No bus.drain() here: publication is left to the real background
            # dispatcher, running independently of both this loop and
            # tick_loop below. This is also what lets the clock advance
            # *during* a tick, which is precisely the condition P2-14's
            # canonical tick time exists to survive.
            await recorded.sim_driver.step()
            async with progress:
                feed_done += 1
                progress.notify_all()
            await asyncio.sleep(0)

    async def tick_loop() -> None:
        nonlocal tick_done
        for _ in range(ticks):
            async with progress:
                # feed_done/tick_done are read live (nonlocal, mutated
                # elsewhere in this same coroutine) on every predicate check
                # -- the closure deliberately does NOT snapshot them, unlike
                # the stale-loop-variable case B023 flags.
                await progress.wait_for(lambda: feed_done > tick_done)  # noqa: B023
            await recorded.orchestrator.tick()
            async with progress:
                tick_done += 1
            await asyncio.sleep(0)

    await asyncio.gather(feed_loop(), tick_loop())
    await recorded.bus.drain()

    # Settle any order still SUBMITTING when the concurrent section ended --
    # comparing state while paper-execution latency is still in flight would
    # compare two runs at different, non-reproducible phases of that
    # in-flight work, which is a test-harness concern, not a replay-fidelity
    # one.
    for _ in range(60):
        if not any(o.status.value == "SUBMITTING" for o in recorded.oms.orders.values()):
            break
        recorded.clock.advance(100)
        await recorded.sim_driver.step()
        await recorded.bus.drain()
        await recorded.orchestrator.tick()

    await recorded.recorder.flush()


class TestConcurrentPublisherAndYieldingRecorderEquivalence:
    """Section 11(A), and the mission's "at minimum one" requirement: a real
    background bus dispatcher, an independently-scheduled feed-publication
    task, an independently-scheduled orchestrator-tick task, and a real
    Recorder -- with neither task draining the bus to synchronize with the
    other.

    Parametrised over run lengths deliberately, including every length that
    reproduced P2-14 (40, 55, 70 -- see
    ``tests/unit/test_execution_time_fidelity.py`` for the root cause). An
    exact-replay test may not pick a scenario merely because that scenario
    happens to avoid a known deterministic divergence, so the lengths that
    exposed the bug stay in the suite permanently.
    """

    @pytest.mark.parametrize("ticks", [40, 50, 55, 70])
    async def test_original_and_replay_agree_under_independent_concurrent_scheduling(
        self, settings, ticks
    ):
        recorded = fresh_platform(settings)
        await recorded.start(record=True, feeds=False)
        await run_concurrent_session(recorded, ticks)

        total_ticks = recorded.orchestrator.ticks
        session_id = recorded.session_id
        await recorded.stop()

        replayed, session = await _replay(settings, recorded.store, session_id)

        assert session.stats.timeline_fidelity is None
        assert session.stats.ticks_read == total_ticks

        original = replay_equivalence_summary(recorded)
        reproduced = replay_equivalence_summary(replayed)
        assert original["fills"], "the run must actually trade for this to prove anything"
        assert reproduced == original


def _snapshot_event(venue: str, mid: float, ts: int) -> Event:
    book = make_book(venue, "BTC-USD", mid, ts=ts)
    return Event(
        type=EventType.BOOK_SNAPSHOT,
        ts_ms=ts,
        source=venue,
        schema_name="OrderBookSnapshot",
        payload=book.to_json_dict(),
    )


class TestTimestampOrderInversionEquivalence:
    """Section 11(B): the Section 6/7 root-cause race, driven through the
    full pipeline for a run that produces real trades, checked against the
    FULL economic equivalence summary rather than only MarketState mids
    (see ``tests/replay/test_replay_tick_marker_timestamp.py`` for the
    narrower, root-cause-focused version of this reproduction).

    Built on the same 50-tick, two-venue default scenario as
    ``TestConcurrentPublisherAndYieldingRecorderEquivalence`` above, with the
    marker-timestamp race injected around its midpoint.
    """

    async def test_original_and_replay_agree_despite_a_stale_marker_timestamp_race(
        self, settings
    ):
        store = BlockableStore()
        recorded = fresh_platform(settings, store=store)
        recorded.recorder.buffer_size = 1_000_000
        recorded.recorder.flush_interval_ms = 1_000_000
        await recorded.start(record=True, feeds=False)
        await recorded.bus.start()

        ticks = 50
        race_at = 25

        for i in range(ticks):
            recorded.clock.advance(100)
            await recorded.sim_driver.step()

            if i != race_at:
                await recorded.bus.drain()
                await recorded.orchestrator.tick()
                continue

            # Force the race at the midpoint: the NEXT _persist() flush
            # (everything buffered so far) is held open in real durable
            # I/O while a fresh, later-timestamped input is published and
            # applied to TIDAL before this tick's _observe() runs.
            await recorded.bus.drain()
            recorded.recorder.flush_interval_ms = 0
            gate = store.block_next()
            tick_task = asyncio.create_task(recorded.orchestrator.tick())
            for _ in range(5):
                await asyncio.sleep(0)
            recorded.clock.advance(2)
            moved = _snapshot_event("VENUE_A", 50_500.0, recorded.clock.now_ms())
            await recorded.bus.publish(moved)
            await recorded.bus.wait_idle()
            gate.set()
            await tick_task
            recorded.recorder.flush_interval_ms = 1_000_000

        for _ in range(60):
            if not any(o.status.value == "SUBMITTING" for o in recorded.oms.orders.values()):
                break
            recorded.clock.advance(100)
            await recorded.sim_driver.step()
            await recorded.bus.drain()
            await recorded.orchestrator.tick()

        await recorded.recorder.flush()
        assert recorded.account.fill_log, (
            "the scenario must actually produce trades for this to prove anything"
        )
        total_ticks = recorded.orchestrator.ticks
        session_id = recorded.session_id
        await recorded.stop()

        replayed, session = await _replay(settings, store, session_id)
        assert session.stats.timeline_fidelity is None
        assert session.stats.ticks_read == total_ticks

        original = replay_equivalence_summary(recorded)
        reproduced = replay_equivalence_summary(replayed)
        assert reproduced == original


class TestMultiVenueConcurrentPublishersEquivalence:
    """Section 11(C): two venues' publishers admitted concurrently, with
    their real Recorder middleware resolving in the OPPOSITE order from
    admission -- the same adversarial shape as the bus-ordering tests, this
    time exercised through the real platform/TIDAL/replay path rather than
    a bare bus.
    """

    async def test_original_and_replay_agree_under_concurrent_multi_venue_publication(
        self, settings
    ):
        clock = ManualClock(START_MS)
        bus = InMemoryEventBus(raise_on_handler_error=True)
        store = InMemoryEventStore()
        recorded = build_platform(
            settings.model_copy(update={"venues": simulated_venues()}),
            clock=clock,
            bus=bus,
            store=store,
            raise_on_handler_error=True,
        )
        await recorded.start(record=True, feeds=False)
        await bus.start()

        gate_a = asyncio.Event()
        gate_b = asyncio.Event()

        async def stagger(event: Event) -> None:
            if event.source == "VENUE_A":
                await gate_a.wait()
            elif event.source == "VENUE_B":
                await gate_b.wait()

        bus.add_middleware(stagger)

        a = _snapshot_event("VENUE_A", 50_000.0, clock.now_ms())
        b = _snapshot_event("VENUE_B", 50_010.0, clock.now_ms())
        task_a = asyncio.create_task(bus.publish(a))
        task_b = asyncio.create_task(bus.publish(b))
        await asyncio.sleep(0)

        # B (admitted second) resolves its middleware first -- the
        # adversarial ordering the P2-13 fix must still get right with two
        # different venues racing, not only one.
        gate_b.set()
        await asyncio.sleep(0)
        gate_a.set()
        await asyncio.gather(task_a, task_b)
        await bus.drain()
        await recorded.orchestrator.tick()

        await recorded.recorder.flush()
        assert a.sequence is not None and b.sequence is not None
        assert a.sequence < b.sequence, "sanity: A was admitted first"

        session_id = recorded.session_id
        await recorded.stop()

        replayed, session = await _replay(settings, store, session_id)
        assert session.stats.timeline_fidelity is None

        original_market = recorded.state.market
        replayed_market = replayed.state.market
        for key in ("VENUE_A:BTC-USD", "VENUE_B:BTC-USD"):
            assert (
                replayed_market.venues[key].metrics.mid
                == original_market.venues[key].metrics.mid
            )
