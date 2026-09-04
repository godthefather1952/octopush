"""In-memory event store, for tests and short-lived sessions.

Deliberately enforces exactly what the durable backends enforce — session
lifecycle, event-id collision detection and batch atomicity alike. A memory
store that accepted what SQLite and PostgreSQL reject would let a test pass
on data production cannot store, and the divergence would surface as a
storage failure in a live session rather than as a red test.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterable

from core.events import Event, EventType
from core.models.common import Millis
from storage.base import (
    EventIdCollision,
    EventStore,
    SessionAlreadyExists,
    SessionInfo,
    SessionNotOpen,
    SessionStatus,
    UnknownSession,
    describe_fingerprint_mismatch,
    event_fingerprint,
)


def _reject_non_finite(payload: object) -> None:
    """Refuse what the durable backends would refuse.

    ``allow_nan=False`` is the same guard ``sqlite_store`` and
    ``postgres_store`` apply, and PostgreSQL's JSONB rejects the Infinity
    and NaN tokens outright. Serialising here purely to validate costs a
    little, and buys the guarantee that a payload accepted in a test is a
    payload production can store.
    """
    json.dumps(payload, default=str, allow_nan=False)


class InMemoryEventStore(EventStore):
    def __init__(self) -> None:
        self._events: dict[str, list[Event]] = {}
        self._sessions: dict[str, SessionInfo] = {}
        #: session -> event id -> fingerprint, so a re-delivery is a no-op
        #: and a reused id carrying different content is an error, exactly
        #: as in the durable backends.
        self._fingerprints: dict[str, dict[str, tuple]] = {}
        self.opened = False

    async def open(self) -> None:
        self.opened = True

    async def close(self) -> None:
        self.opened = False

    # -- lifecycle ---------------------------------------------------------

    async def start_session(
        self, session_id: str, started_at: Millis, label: str = "", config_hash: str = ""
    ) -> None:
        if session_id in self._sessions:
            existing = self._sessions[session_id]
            raise SessionAlreadyExists(
                f"session {session_id!r} already exists with status "
                f"{existing.status.value}; a session id is used once and is "
                "never reopened (no resume protocol exists)"
            )
        self._events[session_id] = []
        self._fingerprints[session_id] = {}
        self._sessions[session_id] = SessionInfo(
            session_id=session_id,
            started_at=started_at,
            ended_at=None,
            event_count=0,
            label=label,
            config_hash=config_hash,
            status=SessionStatus.OPEN,
        )

    async def finalize_session(
        self,
        session_id: str,
        ended_at: Millis,
        *,
        status: SessionStatus,
        events_lost: int = 0,
        failure_reason: str = "",
    ) -> None:
        if not status.is_terminal:
            raise ValueError(f"{status.value} is not a terminal status")
        info = self._require_session(session_id)
        if info.status.is_terminal:
            raise SessionNotOpen(
                f"session {session_id!r} is already {info.status.value}; "
                "re-finalising would overwrite an integrity claim silently"
            )
        self._sessions[session_id] = SessionInfo(
            session_id=info.session_id,
            started_at=info.started_at,
            ended_at=ended_at,
            event_count=len(self._events.get(session_id, [])),
            label=info.label,
            config_hash=info.config_hash,
            status=status,
            events_lost=events_lost,
            failure_reason=failure_reason,
        )

    def _require_session(self, session_id: str) -> SessionInfo:
        info = self._sessions.get(session_id)
        if info is None:
            raise UnknownSession(
                f"session {session_id!r} was never started; storage does not "
                "create sessions implicitly"
            )
        return info

    def _require_open(self, session_id: str) -> None:
        info = self._require_session(session_id)
        if info.status.is_terminal:
            raise SessionNotOpen(
                f"session {session_id!r} is {info.status.value}; appending "
                "after finalisation would grow a session already reported as "
                "closed"
            )

    # -- writes ------------------------------------------------------------

    async def append(self, session_id: str, event: Event) -> None:
        await self.append_many(session_id, [event])

    async def append_many(self, session_id: str, events: Iterable[Event]) -> None:
        """Append a batch atomically.

        Everything is validated before anything is stored, so a batch that
        raises leaves the session exactly as it was and an exact retry can
        converge — the same all-or-nothing guarantee the SQL backends get
        from wrapping the batch in one transaction.
        """
        batch = list(events)
        if not batch:
            return
        self._require_open(session_id)

        stored = self._fingerprints[session_id]
        # Staged, not applied: validation must complete for the whole batch
        # before the first event lands.
        pending: dict[str, Event] = {}
        pending_prints: dict[str, tuple] = {}

        for event in batch:
            _reject_non_finite(event.payload)
            fingerprint = event_fingerprint(event)
            previous = stored.get(event.id) or pending_prints.get(event.id)
            if previous is not None:
                if previous != fingerprint:
                    raise EventIdCollision(
                        f"session {session_id!r} already holds a different "
                        f"event under id {event.id!r}: "
                        f"{describe_fingerprint_mismatch(previous, fingerprint)}"
                    )
                # An identical re-delivery: the retry no-op storage exists for.
                continue
            pending_prints[event.id] = fingerprint
            pending[event.id] = event.model_copy(deep=True)

        for event_id, event in pending.items():
            stored[event_id] = pending_prints[event_id]
            self._events[session_id].append(event)

    # -- reads -------------------------------------------------------------

    async def read(
        self,
        session_id: str,
        *,
        types: Iterable[EventType] | None = None,
        start_ms: Millis | None = None,
        end_ms: Millis | None = None,
    ) -> AsyncIterator[Event]:
        wanted = set(types) if types is not None else None
        for event in sorted(self._events.get(session_id, []), key=lambda e: e.sort_key()):
            if wanted is not None and event.type not in wanted:
                continue
            if start_ms is not None and event.ts_ms < start_ms:
                continue
            if end_ms is not None and event.ts_ms > end_ms:
                continue
            yield event

    async def sessions(self) -> list[SessionInfo]:
        return [
            SessionInfo(
                session_id=info.session_id,
                started_at=info.started_at,
                ended_at=info.ended_at,
                event_count=len(self._events.get(info.session_id, [])),
                label=info.label,
                config_hash=info.config_hash,
                status=info.status,
                events_lost=info.events_lost,
                failure_reason=info.failure_reason,
            )
            for info in sorted(self._sessions.values(), key=lambda s: s.started_at)
        ]

    async def session(self, session_id: str) -> SessionInfo | None:
        for info in await self.sessions():
            if info.session_id == session_id:
                return info
        return None

    async def count(self, session_id: str) -> int:
        return len(self._events.get(session_id, []))
