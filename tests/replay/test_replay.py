"""Replay: recording, deterministic ordering, and reproducibility.

The claim replay has to earn is that running the same recorded market through
the same code twice produces the same answer — and that changing the code is
therefore the only thing that can change the answer.
"""

from __future__ import annotations

import pytest

from apps.orchestrator.wiring import build_platform
from core.bus import InMemoryEventBus
from core.clock import ManualClock
from core.config import simulated_venues
from core.events import MARKET_INPUT_TYPES, Event, EventType
from replay import ReplaySession, collect, config_digest
from simulation.market import default_market
from storage import InMemoryEventStore, SQLiteEventStore
from storage.recorder import Recorder
from tests.conftest import START_MS, run_platform


def summary(platform) -> dict:
    account = platform.account.snapshot()
    return {
        "opportunities": len(platform.state.opportunities),
        "orders": len(platform.oms.orders),
        "fills": len(platform.account.fill_log),
        "cash": round(account.cash, 9),
        "realized_pnl": round(account.realized_pnl, 9),
        "fees": round(account.fees_paid, 9),
        "positions": {
            key: round(position.quantity, 12)
            for key, position in sorted(account.positions.items())
        },
    }


def fresh_platform(settings, *, seed_start: int = START_MS, store=None):
    clock = ManualClock(seed_start)
    return build_platform(
        settings.model_copy(update={"venues": simulated_venues()}),
        clock=clock,
        bus=InMemoryEventBus(raise_on_handler_error=True),
        store=store or InMemoryEventStore(),
        market=default_market(start_ms=seed_start),
        raise_on_handler_error=True,
    )


class TestRecording:
    async def test_a_session_records_market_inputs(self, settings):
        platform = fresh_platform(settings)
        await run_platform(platform, 40)
        await platform.recorder.flush()
        events = await collect(platform.store, platform.session_id)
        assert events
        types = {e.type for e in events}
        assert types & MARKET_INPUT_TYPES
        assert EventType.MARKET_STATE in types

    async def test_every_event_is_ordered_deterministically(self, settings):
        platform = fresh_platform(settings)
        await run_platform(platform, 30)
        await platform.recorder.flush()
        events = await collect(platform.store, platform.session_id)
        keys = [e.sort_key() for e in events]
        assert keys == sorted(keys)

    async def test_sequence_breaks_ties_within_a_millisecond(self, settings):
        platform = fresh_platform(settings)
        await run_platform(platform, 20)
        await platform.recorder.flush()
        events = await collect(platform.store, platform.session_id)
        same_ms = [e for e in events if e.ts_ms == events[0].ts_ms]
        sequences = [e.sequence for e in same_ms]
        assert len(set(sequences)) == len(sequences)

    async def test_session_metadata_records_the_config_digest(self, settings):
        platform = fresh_platform(settings)
        await run_platform(platform, 5)
        await platform.recorder.stop()
        info = await platform.store.session(platform.session_id)
        assert info is not None
        assert info.config_hash == config_digest(platform.settings.model_dump(mode="json"))
        assert info.ended_at is not None

    async def test_sqlite_round_trips_events(self, settings, tmp_path):
        store = SQLiteEventStore(str(tmp_path / "events.db"))
        platform = fresh_platform(settings, store=store)
        await run_platform(platform, 25)
        await platform.recorder.flush()
        events = await collect(store, platform.session_id)
        assert len(events) > 10
        # Reopening reads the same data back from disk.
        await store.close()
        reopened = SQLiteEventStore(str(tmp_path / "events.db"))
        again = await collect(reopened, platform.session_id)
        assert [e.id for e in again] == [e.id for e in events]
        await reopened.close()

    async def test_recorder_survives_a_failing_store(self, clock):
        class BrokenStore(InMemoryEventStore):
            async def append_many(self, session_id, events):
                raise RuntimeError("disk on fire")

        recorder = Recorder(store=BrokenStore(), clock=clock, buffer_size=1)
        await recorder.start()
        await recorder.record(
            Event(type=EventType.SYSTEM_EVENT, ts_ms=clock.now_ms(), source="test")
        )
        # The platform keeps running, but the failure is visible.
        assert recorder.failures == 1
        assert recorder.healthy is False


class TestDeterminism:
    async def test_two_identical_live_runs_agree(self, settings):
        a = fresh_platform(settings)
        b = fresh_platform(settings)
        await run_platform(a, 120)
        await run_platform(b, 120)
        assert summary(a) == summary(b)

    async def test_a_different_seed_produces_a_different_market(self, settings):
        a = fresh_platform(settings)
        clock = ManualClock(START_MS)
        b = build_platform(
            settings.model_copy(update={"venues": simulated_venues()}),
            clock=clock,
            bus=InMemoryEventBus(raise_on_handler_error=True),
            store=InMemoryEventStore(),
            market=default_market(seed=999, start_ms=START_MS),
            raise_on_handler_error=True,
        )
        await run_platform(a, 120)
        await run_platform(b, 120)
        assert summary(a) != summary(b)


class TestReplaySession:
    async def _record(self, settings, ticks: int = 150):
        platform = fresh_platform(settings)
        await run_platform(platform, ticks)
        await platform.recorder.flush()
        return platform

    async def _replay(self, settings, store, session_id: str):
        clock = ManualClock(START_MS)
        bus = InMemoryEventBus(raise_on_handler_error=True)
        platform = build_platform(
            settings.model_copy(update={"venues": simulated_venues()}),
            clock=clock,
            bus=bus,
            store=InMemoryEventStore(),
            raise_on_handler_error=True,
        )
        # Feeds stay off: the recording is the market.
        await platform.start(record=False, feeds=False)
        session = ReplaySession(store=store, bus=bus, clock=clock, session_id=session_id)
        await session.open()
        while True:
            event = await session.step()
            if event is None:
                break
            await platform.orchestrator.tick()
        return platform, session

    async def test_replay_reproduces_market_state(self, settings):
        recorded = await self._record(settings, ticks=100)
        replayed, session = await self._replay(settings, recorded.store, recorded.session_id)
        assert session.stats.events_published > 0
        # The replayed books reach the same prices as the recorded ones.
        for key, original in recorded.state.market.venues.items():
            reproduced = replayed.state.market.venues.get(key)
            assert reproduced is not None, key
            assert reproduced.metrics.best_bid == pytest.approx(original.metrics.best_bid)
            assert reproduced.metrics.best_ask == pytest.approx(original.metrics.best_ask)

    async def test_replay_is_repeatable(self, settings):
        recorded = await self._record(settings, ticks=120)
        first, _ = await self._replay(settings, recorded.store, recorded.session_id)
        second, _ = await self._replay(settings, recorded.store, recorded.session_id)
        assert summary(first) == summary(second)

    async def test_replay_clock_never_runs_backwards(self, settings):
        recorded = await self._record(settings, ticks=60)
        clock = ManualClock(START_MS)
        bus = InMemoryEventBus()
        session = ReplaySession(
            store=recorded.store, bus=bus, clock=clock, session_id=recorded.session_id
        )
        await session.open()
        seen: list[int] = []
        while True:
            event = await session.step()
            if event is None:
                break
            seen.append(clock.now_ms())
        assert seen == sorted(seen)

    async def test_only_market_inputs_are_replayed(self, settings):
        recorded = await self._record(settings, ticks=40)
        clock = ManualClock(START_MS)
        bus = InMemoryEventBus()
        published: list[Event] = []
        bus.subscribe(lambda e: published.append(e) or _noop(), name="probe")
        session = ReplaySession(
            store=recorded.store, bus=bus, clock=clock, session_id=recorded.session_id
        )
        await session.run()
        # Derived state is recomputed, never replayed back at the platform.
        assert published
        assert all(e.type in MARKET_INPUT_TYPES for e in published)

    async def test_step_mode_advances_exactly_one_event(self, settings):
        recorded = await self._record(settings, ticks=30)
        clock = ManualClock(START_MS)
        session = ReplaySession(
            store=recorded.store,
            bus=InMemoryEventBus(),
            clock=clock,
            session_id=recorded.session_id,
        )
        await session.open()
        assert await session.step() is not None
        assert session.stats.events_published == 1

    async def test_replay_finishes_and_reports_its_span(self, settings):
        recorded = await self._record(settings, ticks=50)
        clock = ManualClock(START_MS)
        session = ReplaySession(
            store=recorded.store,
            bus=InMemoryEventBus(),
            clock=clock,
            session_id=recorded.session_id,
        )
        stats = await session.run()
        assert session.finished
        assert stats.span_ms > 0
        assert stats.events_published == stats.events_read


async def _noop() -> None:
    return None
