"""Phase 2 Batch 2 Section 19: storage failure must still stop trading.

Batch 2 changed what a failed flush *means* — it is now a retained batch
rather than destroyed history — so it would be easy to accidentally soften
the safety path that turns "we cannot record what we are doing" into "then
stop doing it". This pins that path end to end:

    recorder failure -> recorder unhealthy -> storage_ok False
      -> STORAGE_FAILURE confirmation -> new trading halted

and pins the two directions that must NOT change: a transient error that
recovers may restore health, and a kill switch that has already engaged is
never cleared automatically by that recovery.
"""

from __future__ import annotations

import pytest

from core.events import Event, EventType
from core.models.ops import HealthStatus
from storage import InMemoryEventStore
from tests.conftest import START_MS


def make_event(i: int) -> Event:
    return Event(
        type=EventType.SYSTEM_EVENT,
        ts_ms=START_MS + i,
        source="TEST",
        schema_name="Probe",
        payload={"i": i},
        sequence=i,
    )


class BrokenStore(InMemoryEventStore):
    def __init__(self) -> None:
        super().__init__()
        self.fail = False

    async def append_many(self, session_id, events):
        if self.fail:
            raise RuntimeError("storage unavailable")
        await super().append_many(session_id, events)


@pytest.fixture
async def wired(platform):
    """A running platform whose recorder writes to a breakable store."""
    store = BrokenStore()
    platform.recorder.store = store
    # Large buffer + large interval so nothing flushes implicitly: each test
    # drives exactly the number of flush ATTEMPTS it means to, instead of
    # having every record past the buffer size trigger another retry.
    platform.recorder.buffer_size = 1_000_000
    platform.recorder.flush_interval_ms = 1_000_000
    await platform.start(record=True, feeds=False)
    return platform, store


async def _tick(platform, times: int = 1) -> None:
    for _ in range(times):
        platform.clock.advance(100)
        await platform.step_market(1)
        await platform.orchestrator.tick()


class TestStorageFailureStopsTrading:
    async def test_a_one_flush_failure_that_recovers_restores_health(self, wired):
        platform, store = wired
        recorder = platform.recorder

        store.fail = True
        for i in range(5):
            await recorder.record(make_event(i))
        await recorder.flush()
        assert recorder.healthy is False
        assert recorder.consecutive_failures == 1
        assert recorder.unpersisted == 5
        assert recorder.events_lost == 0, "a retryable failure is not loss"

        store.fail = False
        await recorder.flush()

        assert recorder.healthy is True
        assert recorder.consecutive_failures == 0
        assert recorder.unpersisted == 0
        assert recorder.events_lost == 0

    async def test_b_failures_below_the_threshold_do_not_halt_trading(self, wired):
        platform, store = wired
        orchestrator = platform.orchestrator
        recorder = platform.recorder

        store.fail = True
        await recorder.record(make_event(0))
        for _ in range(orchestrator.storage_failure_threshold - 1):
            await recorder.flush()

        assert 0 < recorder.consecutive_failures < orchestrator.storage_failure_threshold
        orchestrator._recorder_heartbeat()
        assert platform.health.status_of("RECORDER") is HealthStatus.DEGRADED
        assert orchestrator._storage_ok() is True, (
            "a couple of transient errors are not an outage"
        )

    async def test_c_sustained_failure_crosses_the_threshold_and_halts(self, wired):
        platform, store = wired
        orchestrator = platform.orchestrator
        recorder = platform.recorder

        store.fail = True
        for i in range(5):
            await recorder.record(make_event(i))
        for _ in range(orchestrator.storage_failure_threshold + 1):
            await recorder.flush()

        assert recorder.consecutive_failures >= orchestrator.storage_failure_threshold
        orchestrator._recorder_heartbeat()
        assert platform.health.status_of("RECORDER") is HealthStatus.OFFLINE
        assert orchestrator._storage_ok() is False

        # ...and that flows through to the kill switch stopping new trades.
        await _tick(platform, 3)
        assert not platform.kill_switch.state.trading_allowed
        assert "STORAGE_FAILURE" in platform.kill_switch.state.triggered_by

        # Nothing was destroyed to get here.
        assert recorder.events_lost == 0
        assert recorder.unpersisted > 0

    async def test_d_recovery_does_not_clear_an_engaged_kill_switch(self, wired):
        platform, store = wired
        orchestrator = platform.orchestrator
        recorder = platform.recorder

        store.fail = True
        for i in range(5):
            await recorder.record(make_event(i))
        for _ in range(orchestrator.storage_failure_threshold + 1):
            await recorder.flush()
        await _tick(platform, 3)
        assert platform.kill_switch.state.engaged

        # Storage comes back and the recorder recovers completely...
        store.fail = False
        await recorder.flush()
        assert recorder.healthy is True
        assert recorder.unpersisted == 0
        await _tick(platform, 3)

        # ...but the kill switch stays engaged. Clearing is manual, always.
        assert platform.kill_switch.state.engaged
        assert "STORAGE_FAILURE" in platform.kill_switch.state.triggered_by

    async def test_the_recorded_history_survives_the_whole_outage(self, wired):
        """The point of retaining batches: nothing is lost by the outage."""
        platform, store = wired
        recorder = platform.recorder

        store.fail = True
        for i in range(25):
            await recorder.record(make_event(i))
        await recorder.flush()
        assert recorder.unpersisted >= 25

        store.fail = False
        await recorder.flush()

        stored = [e async for e in store.read(recorder.session_id)]
        recorded_probe = sorted(
            e.payload["i"] for e in stored if e.schema_name == "Probe"
        )
        assert recorded_probe == list(range(25))
        assert recorder.events_lost == 0
