"""Phase 11 platform lifecycle invariants."""

import pytest

from apps.orchestrator.wiring import build_platform
from core.bus import InMemoryEventBus
from core.models.runtime import SessionStatus, ShutdownStage, StartupStage
from simulation.market import default_market
from tests.conftest import START_MS


class _StartupBoom(RuntimeError):
    pass


class _FailingStartBus(InMemoryEventBus):
    def __init__(self, error: BaseException) -> None:
        super().__init__(raise_on_handler_error=True)
        self.error = error

    async def start(self) -> None:
        raise self.error


async def test_bus_start_failure_is_recorded_and_exception_identity_survives(
    settings, clock, store
) -> None:
    error = _StartupBoom("bus failed")
    bus = _FailingStartBus(error)
    platform = build_platform(
        settings,
        clock=clock,
        bus=bus,
        store=store,
        market=default_market(start_ms=START_MS),
    )

    with pytest.raises(_StartupBoom) as caught:
        await platform.start_bus(record=False)

    assert caught.value is error
    record = platform.current_session()
    assert record is not None
    assert record.status is SessionStatus.FAILED
    assert record.startup_stage is StartupStage.BUS
    assert platform.operations.sessions_started == 1
    assert platform.operations.sessions_failed == 1


async def test_recorder_start_failure_is_recorded_at_storage_stage(
    platform, monkeypatch
) -> None:
    error = _StartupBoom("recorder failed")

    async def fail_start() -> None:
        raise error

    monkeypatch.setattr(platform.recorder, "start", fail_start)

    try:
        with pytest.raises(_StartupBoom) as caught:
            await platform.start(record=True)
        assert caught.value is error

        record = platform.current_session()
        assert record is not None
        assert record.status is SessionStatus.FAILED
        assert record.startup_stage is StartupStage.STORAGE
        assert platform.operations.sessions_started == 1
        assert platform.operations.sessions_failed == 1
    finally:
        await platform.bus.stop()


async def test_clean_stop_and_repeated_stop_are_idempotent(platform) -> None:
    await platform.start(record=False)
    record = platform.current_session()
    assert record is not None
    assert record.status is SessionStatus.RUNNING
    assert record.startup_stage is StartupStage.READY

    await platform.stop()

    assert record.status is SessionStatus.STOPPED
    assert record.shutdown_stage is ShutdownStage.STOPPED
    assert platform.operations.sessions_completed == 1

    # Cleanup may be requested more than once by callers/finalizers.
    await platform.stop()
    assert record.status is SessionStatus.STOPPED
    assert platform.operations.sessions_completed == 1
