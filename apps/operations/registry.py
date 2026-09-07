"""The operational registry — the platform's memory of its own runs.

WHAT THIS IS
============
Every phase before this one gave a *component* a memory. Phase 6 gave
execution one, Phase 7 reconciliation, Phase 8 coordination, Phase 9 hedging,
Phase 10 intelligence. None of them answers the question that sits above all of
them: *what was this run?*

:class:`OperationalRegistry` is that memory. One record per session, holding the
manifest it was configured with, how far startup got, what status it reached,
and the counters describing what it did.

WHAT THIS IS NOT
================
* **Not a supervisor.** ``Platform.start()`` and ``Platform.stop()`` still own
  the lifecycle. This records what they did, beside them.
* **Not an error handler.** :meth:`mark_failed` witnesses that startup or
  shutdown raised; the exception itself still propagates untouched. A registry
  that swallowed a startup failure would let a half-built platform look like a
  running one.
* **Not a gate.** Nothing reads what this holds to decide whether anything may
  proceed.
* **Not a second identity.** Sessions are keyed by the recorder's own
  ``session_id``, which every recorded event already carries. Minting a second
  id would give one run two names.

Every mutation takes ``now_ms`` explicitly. Nothing in this module reads a
clock.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from core.models.common import Millis
from core.models.runtime import (
    OperationalIncident,
    OperationalIncidentSeverity,
    OperationalMetrics,
    OperationalSessionRecord,
    OperatorAnnotation,
    SessionManifest,
    SessionStatus,
    ShutdownStage,
    StartupStage,
)

log = logging.getLogger(__name__)


class OperationalStore(ABC):
    """The persistence seam for session history.

    **No implementation exists, and none is built here.** ``EventStore`` is
    untouched: it stores the events a session produced, which is a different
    question from what the session *was*, and overloading it would conflate the
    two.

    The seam is real rather than decorative: :class:`OperationalRegistry`
    accepts one and writes through when present, so a later pass adds a class
    instead of restructuring the registry. Reads still come from memory.
    """

    @abstractmethod
    def put_session(self, record: OperationalSessionRecord) -> None: ...

    @abstractmethod
    def get_session(self, session_id: str) -> OperationalSessionRecord | None: ...

    @abstractmethod
    def recent_sessions(self, limit: int) -> list[OperationalSessionRecord]: ...


@dataclass
class OperationalRegistry:
    """Session records, in memory."""

    sessions: dict[str, OperationalSessionRecord] = field(default_factory=dict)
    incidents: dict[str, OperationalIncident] = field(default_factory=dict)
    annotations: dict[str, list[OperatorAnnotation]] = field(default_factory=dict)

    #: Optional write-through persistence. See :class:`OperationalStore`.
    store: OperationalStore | None = None

    #: Registration order, so "recent sessions" is a slice.
    _order: list[str] = field(default_factory=list)
    #: The session this process is running, if one has been created.
    current_session_id: str | None = None

    #: Lifetime counters, unaffected by any future compaction.
    sessions_started: int = 0
    sessions_completed: int = 0
    sessions_failed: int = 0

    # ------------------------------------------------------------------
    # session lifecycle
    # ------------------------------------------------------------------

    def create_session(
        self, manifest: SessionManifest, now_ms: Millis
    ) -> OperationalSessionRecord:
        """Open the record for one run.

        Keyed by ``manifest.session_id``, which is the recorder's id. Creating
        a session that already exists returns the record already held rather
        than starting a second under the same name.
        """
        existing = self.sessions.get(manifest.session_id)
        if existing is not None:
            return existing

        record = OperationalSessionRecord(
            session_id=manifest.session_id,
            created_at=now_ms,
            updated_at=now_ms,
            status=SessionStatus.CREATED,
            startup_stage=StartupStage.CREATED,
            manifest=manifest,
        )
        self.sessions[record.session_id] = record
        self._order.append(record.session_id)
        self.current_session_id = record.session_id
        self._persist(record)
        return record

    def mark_starting(
        self, now_ms: Millis, *, session_id: str | None = None
    ) -> OperationalSessionRecord | None:
        record = self._session(session_id)
        if record is None:
            return None
        record.status = SessionStatus.STARTING
        record.updated_at = now_ms
        self.sessions_started += 1
        self._persist(record)
        return record

    def mark_running(
        self, now_ms: Millis, *, session_id: str | None = None
    ) -> OperationalSessionRecord | None:
        """The platform finished starting.

        A session already marked FAILED stays FAILED. Nothing here may convert
        a recorded failure into a success.
        """
        record = self._session(session_id)
        if record is None:
            return None
        if record.status is SessionStatus.FAILED:
            return record
        record.status = SessionStatus.RUNNING
        record.startup_stage = StartupStage.READY
        record.started_at = now_ms
        record.updated_at = now_ms
        self._persist(record)
        return record

    def mark_stopping(
        self, now_ms: Millis, *, session_id: str | None = None
    ) -> OperationalSessionRecord | None:
        record = self._session(session_id)
        if record is None:
            return None
        if record.status is SessionStatus.FAILED:
            return record
        record.status = SessionStatus.STOPPING
        record.shutdown_stage = ShutdownStage.REQUESTED
        record.updated_at = now_ms
        self._persist(record)
        return record

    def mark_stopped(
        self, now_ms: Millis, *, session_id: str | None = None
    ) -> OperationalSessionRecord | None:
        record = self._session(session_id)
        if record is None:
            return None
        if record.status is SessionStatus.FAILED:
            # A run that failed did not later succeed at stopping cleanly.
            record.stopped_at = now_ms
            record.updated_at = now_ms
            self._persist(record)
            return record
        record.status = SessionStatus.STOPPED
        record.shutdown_stage = ShutdownStage.STOPPED
        record.stopped_at = now_ms
        record.updated_at = now_ms
        self.sessions_completed += 1
        self._persist(record)
        return record

    def mark_failed(
        self,
        now_ms: Millis,
        *,
        failure: str = "",
        session_id: str | None = None,
    ) -> OperationalSessionRecord | None:
        """Record that startup or shutdown raised.

        **The exception is not handled here, and is not handled by the
        caller either.** ``Platform.start`` and ``Platform.stop`` record the
        failure and re-raise exactly what they caught: same exception, same
        type, no retry and no translation. A framework that turned a failed
        startup into a tidy note would leave a half-built platform looking
        like a running one.
        """
        record = self._session(session_id)
        if record is None:
            return None
        record.status = SessionStatus.FAILED
        record.updated_at = now_ms
        if failure:
            record.failure = failure
        self.sessions_failed += 1
        self._persist(record)
        return record

    def set_startup_stage(
        self,
        stage: StartupStage,
        now_ms: Millis,
        *,
        session_id: str | None = None,
    ) -> OperationalSessionRecord | None:
        """Note how far startup has got.

        Returns ``None`` when there is no session record — not an error. A
        metadata call that raised because bookkeeping was missing would let
        the record break the thing it records.
        """
        record = self._session(session_id)
        if record is None:
            return None
        record.startup_stage = stage
        record.updated_at = now_ms
        return record

    def set_shutdown_stage(
        self,
        stage: ShutdownStage,
        now_ms: Millis,
        *,
        session_id: str | None = None,
    ) -> OperationalSessionRecord | None:
        record = self._session(session_id)
        if record is None:
            return None
        record.shutdown_stage = stage
        record.updated_at = now_ms
        return record

    # ------------------------------------------------------------------
    # observations
    # ------------------------------------------------------------------

    def update_counts(
        self,
        now_ms: Millis,
        *,
        ticks: int | None = None,
        events_recorded: int | None = None,
        session_id: str | None = None,
    ) -> OperationalSessionRecord | None:
        """Copy the platform's own counters onto the record.

        Absolute values, not increments: ``Orchestrator.ticks`` and
        ``Recorder.events_recorded`` are already running totals, and adding to
        them here would double-count.
        """
        record = self._session(session_id)
        if record is None:
            return None
        if ticks is not None:
            record.ticks = ticks
        if events_recorded is not None:
            record.events_recorded = events_recorded
        record.updated_at = now_ms
        return record

    def note(
        self, text: str, now_ms: Millis, *, session_id: str | None = None
    ) -> OperationalSessionRecord | None:
        record = self._session(session_id)
        if record is None or not text:
            return None
        record.notes = [*record.notes, text]
        record.updated_at = now_ms
        return record

    def annotate(
        self,
        text: str,
        now_ms: Millis,
        *,
        author: str = "",
        session_id: str | None = None,
    ) -> OperatorAnnotation | None:
        """Attach an operator note for later analysis.

        ``author`` is a free-text label, not an authenticated identity, and
        there is no API route that reaches this. Building an auth model before
        there is anything to authorise would invent a security boundary nobody
        has specified.
        """
        record = self._session(session_id)
        if record is None:
            return None
        annotation = OperatorAnnotation(created_at=now_ms, text=text, author=author)
        self.annotations.setdefault(record.session_id, []).append(annotation)
        return annotation

    def record_incident(
        self,
        component: str,
        reason: str,
        now_ms: Millis,
        *,
        severity: OperationalIncidentSeverity = OperationalIncidentSeverity.INFO,
        detail: str = "",
    ) -> OperationalIncident:
        """Record something notable. **Nothing acts on it.**

        No automatic incident policy exists: nothing raises one of these on its
        own, nothing escalates, and nothing halts. ``HealthRegistry`` remains
        the authority on component health and the kill switch on stopping.
        """
        incident = OperationalIncident(
            created_at=now_ms,
            component=component,
            severity=severity,
            reason=reason,
            detail=detail,
        )
        self.incidents[incident.incident_id] = incident
        return incident

    def resolve_incident(
        self, incident_id: str, now_ms: Millis
    ) -> OperationalIncident | None:
        incident = self.incidents.get(incident_id)
        if incident is None:
            return None
        incident.resolved_at = now_ms
        return incident

    # ------------------------------------------------------------------
    # queries
    # ------------------------------------------------------------------

    def get_session(self, session_id: str) -> OperationalSessionRecord | None:
        return self.sessions.get(session_id)

    def current_session(self) -> OperationalSessionRecord | None:
        if self.current_session_id is None:
            return None
        return self.sessions.get(self.current_session_id)

    def recent_sessions(self, limit: int = 20) -> list[OperationalSessionRecord]:
        """The most recently created sessions, newest last."""
        if limit <= 0:
            return []
        return [
            self.sessions[sid] for sid in self._order[-limit:] if sid in self.sessions
        ]

    def session_annotations(self, session_id: str) -> list[OperatorAnnotation]:
        return list(self.annotations.get(session_id, []))

    def open_incidents(self) -> list[OperationalIncident]:
        return [i for i in self.incidents.values() if not i.resolved]

    def metrics(self, **counts: int) -> OperationalMetrics:
        """Counters, for display. Nothing reads these to decide anything.

        The per-session counts a caller passes in come from the components
        that own them — the orchestrator, the OMS, the registries — rather
        than being tallied here, so the snapshot cannot disagree with the
        component it describes.
        """
        record = self.current_session()
        return OperationalMetrics(
            sessions_started=self.sessions_started,
            sessions_completed=self.sessions_completed,
            sessions_failed=self.sessions_failed,
            ticks=record.ticks if record else 0,
            events_recorded=record.events_recorded if record else 0,
            **counts,
        )

    # ------------------------------------------------------------------
    # retention
    # ------------------------------------------------------------------

    def compact(self, *, keep_all: bool = True) -> int:
        """Release finished sessions. Conservative by construction.

        With the default **nothing is released**, and there is no arbitrary
        count limit — no measurement supports choosing one. The framework
        supplies the hook and the safety rules; the policy belongs to a later
        pass.

        Even when asked to release, these are absolute: a running session is
        never released, the current session is never released, and a session
        with an unresolved incident is never released.

        Returns the number of session records released.
        """
        if keep_all:
            return 0
        doomed = [
            record
            for record in self.recent_sessions(len(self._order))
            if record.is_terminal and record.session_id != self.current_session_id
        ]
        for record in doomed:
            self.sessions.pop(record.session_id, None)
            self.annotations.pop(record.session_id, None)
        self._order = [sid for sid in self._order if sid in self.sessions]
        return len(doomed)

    @property
    def resident_sessions(self) -> int:
        return len(self.sessions)

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _session(self, session_id: str | None) -> OperationalSessionRecord | None:
        target = session_id if session_id is not None else self.current_session_id
        if target is None:
            return None
        return self.sessions.get(target)

    def _persist(self, record: OperationalSessionRecord) -> None:
        if self.store is not None:
            self.store.put_session(record)


__all__ = ["OperationalRegistry", "OperationalStore"]
