"""Phase 11 readiness must report facts coherently and gate nothing."""

from tests.conftest import run_platform


def test_readiness_before_startup_is_explicitly_not_ready(platform) -> None:
    readiness = platform.operational_readiness(platform.clock.now_ms())

    assert readiness.ready is False
    assert readiness.bus_ready is False
    assert readiness.feed_ready is False
    assert "BUS_NOT_STARTED" in readiness.reason_codes
    assert "FEED_NOT_READY" in readiness.reason_codes
    assert "NO_MARKET_STATE" in readiness.reason_codes


async def test_intentionally_unrecorded_replay_style_session_is_not_a_storage_fault(
    platform,
) -> None:
    await platform.start(record=False, feeds=False)
    try:
        readiness = platform.operational_readiness(platform.clock.now_ms())

        # Recording/feed tasks were intentionally not requested. Their
        # requirements are satisfied rather than misreported as failures.
        assert readiness.storage_ready is True
        assert readiness.recording_ready is True
        assert readiness.feed_ready is True
        assert "NOT_RECORDING" not in readiness.reason_codes
        assert "STORAGE_UNHEALTHY" not in readiness.reason_codes
        # No market has been replayed yet, so the platform is still not ready.
        assert readiness.ready is False
        assert "NO_MARKET_STATE" in readiness.reason_codes
    finally:
        await platform.stop()


async def test_recording_session_cannot_report_ready_with_unhealthy_storage(
    platform,
) -> None:
    await run_platform(platform, 20)
    try:
        platform.recorder.healthy = False
        readiness = platform.operational_readiness(platform.clock.now_ms())

        assert readiness.storage_ready is False
        assert readiness.ready is False
        assert "STORAGE_UNHEALTHY" in readiness.reason_codes
    finally:
        await platform.stop()


async def test_lumen_unavailability_never_becomes_a_required_readiness_failure(
    platform,
) -> None:
    await run_platform(platform, 20)
    try:
        readiness = platform.operational_readiness(platform.clock.now_ms())

        # The shipped null provider is intentionally unavailable.
        assert readiness.intelligence_available is False
        assert not any(
            "LUMEN" in reason or "INTELLIGENCE" in reason
            for reason in readiness.reason_codes
        )
    finally:
        await platform.stop()


async def test_kill_switch_is_reflected_without_making_readiness_a_gate(platform) -> None:
    await run_platform(platform, 20)
    try:
        state = await platform.kill_switch.engage("PHASE11_TEST", "readiness test")
        platform.state.kill_switch = state

        readiness = platform.operational_readiness(platform.clock.now_ms())
        assert readiness.kill_switch_clear is False
        assert readiness.ready is False
        assert "KILL_SWITCH_ENGAGED" in readiness.reason_codes
    finally:
        await platform.stop()
