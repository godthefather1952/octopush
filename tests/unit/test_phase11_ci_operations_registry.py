"""Phase 11 operational registry invariants."""

from apps.operations.registry import OperationalRegistry
from core.models.runtime import (
    OperationalIncidentSeverity,
    SessionManifest,
    SessionStatus,
)


def _manifest(session_id: str, now: int = 1) -> SessionManifest:
    return SessionManifest(session_id=session_id, created_at=now)


def test_lifecycle_counters_count_transitions_not_method_calls() -> None:
    registry = OperationalRegistry()
    registry.create_session(_manifest("s1"), 1)

    registry.mark_starting(2)
    registry.mark_starting(3)
    registry.mark_running(4)
    registry.mark_stopping(5)
    registry.mark_stopping(6)
    registry.mark_stopped(7)
    registry.mark_stopped(8)

    record = registry.get_session("s1")
    assert record is not None
    assert record.status is SessionStatus.STOPPED
    assert registry.sessions_started == 1
    assert registry.sessions_completed == 1
    assert registry.sessions_failed == 0

    # A late/repeated failure report cannot rewrite a completed run.
    registry.mark_failed(9, failure="late")
    assert record.status is SessionStatus.STOPPED
    assert registry.sessions_failed == 0


def test_failed_session_is_counted_once() -> None:
    registry = OperationalRegistry()
    registry.create_session(_manifest("s1"), 1)
    registry.mark_starting(2)

    registry.mark_failed(3, failure="boom")
    registry.mark_failed(4, failure="boom again")

    record = registry.get_session("s1")
    assert record is not None
    assert record.status is SessionStatus.FAILED
    assert registry.sessions_failed == 1
    assert record.failure == "boom"


def test_incidents_are_session_scoped_and_block_compaction() -> None:
    registry = OperationalRegistry()

    registry.create_session(_manifest("s1"), 1)
    registry.mark_starting(2)
    registry.mark_running(3)
    registry.mark_stopping(4)
    registry.mark_stopped(5)

    registry.create_session(_manifest("s2", 10), 10)
    registry.mark_starting(11)
    registry.mark_running(12)

    incident = registry.record_incident(
        "RECORDER",
        "TEST",
        13,
        severity=OperationalIncidentSeverity.WARNING,
        session_id="s1",
    )

    assert incident.session_id == "s1"
    assert registry.open_incidents(session_id="s1") == [incident]
    assert registry.open_incidents(session_id="s2") == []

    # s1 is terminal and no longer current, but the open incident retains it.
    assert registry.compact(keep_all=False) == 0
    assert registry.get_session("s1") is not None

    registry.resolve_incident(incident.incident_id, 14)
    assert registry.compact(keep_all=False) == 1
    assert registry.get_session("s1") is None
    assert registry.get_session("s2") is not None
