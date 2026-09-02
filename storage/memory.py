"""In-memory event store, for tests and short-lived sessions."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterable

from core.events import Event, EventType
from core.models.common import Millis
from storage.base import EventStore, SessionInfo


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
        #: Event ids held per session, so a re-delivery is a no-op exactly as
        #: it is for the durable backends' ON CONFLICT DO NOTHING.
        self._seen: dict[str, set[str]] = {}
        self.opened = False

    async def open(self) -> None:
        self.opened = True

    async def close(self) -> None:
        self.opened = False

    async def start_session(
        self, session_id: str, started_at: Millis, label: str = "", config_hash: str = ""
    ) -> None:
        self._events.setdefault(session_id, [])
        self._sessions[session_id] = SessionInfo(
            session_id=session_id,
            started_at=started_at,
            ended_at=None,
            event_count=0,
            label=label,
            config_hash=config_hash,
        )

    async def end_session(self, session_id: str, ended_at: Millis) -> None:
        info = self._sessions.get(session_id)
        if info is None:
            return
        self._sessions[session_id] = SessionInfo(
            session_id=info.session_id,
            started_at=info.started_at,
            ended_at=ended_at,
            event_count=len(self._events.get(session_id, [])),
            label=info.label,
            config_hash=info.config_hash,
        )

    async def append(self, session_id: str, event: Event) -> None:
        """Store an event, ignoring a re-delivery of one already held.

        Both halves of this mirror the durable backends deliberately. The
        contract suite runs against all three, and a memory store that
        accepted what SQLite and PostgreSQL reject would let a test pass on
        data that cannot survive production — the divergence would surface
        as a storage failure in a live session rather than as a red test.
        """
        _reject_non_finite(event.payload)
        seen = self._seen.setdefault(session_id, set())
        if event.id in seen:
            return
        seen.add(event.id)
        self._events.setdefault(session_id, []).append(event.model_copy(deep=True))

    async def append_many(self, session_id: str, events: Iterable[Event]) -> None:
        for event in events:
            await self.append(session_id, event)

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
