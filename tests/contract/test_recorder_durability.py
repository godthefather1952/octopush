"""The audit trail must not sit in memory indefinitely — P0-M.

The recorder buffers so that storage latency stays off the trading path, and
flushes on a size or an age trigger. The age trigger was evaluated only
inside ``record()`` — that is, only when a *new event arrived*. In a quiet
period there is no new event, so buffered events were never flushed by time
at all: they waited for the next event or for shutdown, and a process that
died in between lost them silently.

Quiet periods are not exotic. A halted strategy, an out-of-hours session, a
kill switch that has stopped new trades: all of them leave the last events
before the pause unpersisted, and those are exactly the events explaining
why the platform went quiet.
"""

from __future__ import annotations

import pytest

from core.clock import ManualClock
from core.events import Event, EventType
from storage import InMemoryEventStore, Recorder

START_MS = 1_700_000_000_000


def make_event(i: int) -> Event:
    return Event(
        type=EventType.SYSTEM_EVENT,
        ts_ms=START_MS + i,
        source="TEST",
        schema_name="Probe",
        payload={"i": i},
        sequence=i,
    )


@pytest.fixture
async def recorder():
    clock = ManualClock(start_ms=START_MS)
    store = InMemoryEventStore()
    rec = Recorder(store=store, clock=clock, buffer_size=200, flush_interval_ms=1_000)
    await rec.start()
    yield rec, store, clock


class TestTheAgeTriggerIsReallyTimeBased:
    async def test_a_quiet_period_still_flushes(self, recorder):
        """The regression: no new events, so nothing ever triggered a flush."""
        rec, store, clock = recorder
        await rec.record(make_event(1))
        assert await store.count(rec.session_id) == 0, "buffered, as intended"

        clock.advance(5_000)  # long past flush_interval_ms
        await rec.flush_if_due()

        assert await store.count(rec.session_id) == 1, (
            "an event sat unpersisted for 5s of wall time with no traffic"
        )

    async def test_nothing_is_flushed_before_the_interval_elapses(self, recorder):
        """Buffering is the point; this must not become a write per event."""
        rec, store, clock = recorder
        await rec.record(make_event(1))
        clock.advance(999)
        await rec.flush_if_due()
        assert await store.count(rec.session_id) == 0

    async def test_an_empty_buffer_does_not_touch_the_store(self, recorder):
        rec, store, clock = recorder
        clock.advance(10_000)
        await rec.flush_if_due()
        assert await store.count(rec.session_id) == 0

    async def test_repeated_due_flushes_do_not_duplicate(self, recorder):
        rec, store, clock = recorder
        await rec.record(make_event(1))
        clock.advance(2_000)
        await rec.flush_if_due()
        await rec.flush_if_due()
        await rec.flush_if_due()
        assert await store.count(rec.session_id) == 1


class TestTheSizeTriggerStillWorks:
    async def test_a_full_buffer_flushes_without_waiting(self, recorder):
        rec, store, _ = recorder
        for i in range(200):
            await rec.record(make_event(i))
        assert await store.count(rec.session_id) == 200

    async def test_the_window_is_bounded_by_the_buffer_size(self, recorder):
        """However busy it gets, only buffer_size events are ever at risk."""
        rec, _, _ = recorder
        for i in range(1_000):
            await rec.record(make_event(i))
        assert rec.unpersisted <= rec.buffer_size


class TestShutdownIsStillTheBackstop:
    async def test_stop_flushes_what_is_left(self, recorder):
        rec, store, _ = recorder
        await rec.record(make_event(1))
        await rec.stop()
        assert await store.count(rec.session_id) == 1


class TestTheOrchestratorDrivesIt:
    async def test_a_tick_flushes_a_due_buffer(self):
        """Wiring check: the trigger is useless if nobody calls it.

        Driven from the tick rather than from a background task on purpose —
        a task sleeping on real time cannot be stepped by ManualClock, and
        every determinism guarantee in this codebase depends on the clock
        being the only source of time.
        """
        from apps.orchestrator.wiring import build_platform
        from core.config import load_settings

        clock = ManualClock(start_ms=START_MS)
        platform = build_platform(load_settings(), clock=clock, session_label="flush")
        await platform.start(record=True, feeds=False)
        try:
            await platform.orchestrator.tick()
            before = platform.recorder.events_recorded
            clock.advance(platform.recorder.flush_interval_ms * 2)
            await platform.orchestrator.tick()
            assert platform.recorder.events_recorded > before
        finally:
            await platform.stop()


class TestAFailedFlushIsNotLoss:
    """P2-7, inverted from what this suite used to assert.

    It previously required a failed flush to report the batch as LOST --
    because the recorder really did throw it away. That is the defect: a
    database that was briefly unreachable destroyed history permanently. A
    failed write is now a retained batch, a failure count and bad health;
    loss is reserved for history that is genuinely abandoned.
    """

    async def test_a_failed_flush_retains_the_batch_and_reports_no_loss(
        self, recorder
    ):
        rec, store, _ = recorder

        async def explode(*args, **kwargs):
            raise RuntimeError("disk gone")

        store.append_many = explode
        for i in range(200):
            await rec.record(make_event(i))

        assert rec.events_lost == 0, "a transient write failure is not loss"
        assert rec.unpersisted == 200, "the batch must still be held for retry"
        assert rec.consecutive_failures == 1
        assert rec.healthy is False

    async def test_unpersisted_never_counts_events_already_written(self, recorder):
        rec, _, _ = recorder
        for i in range(200):
            await rec.record(make_event(i))
        assert rec.unpersisted == 0
        await rec.record(make_event(999))
        assert rec.unpersisted == 1
