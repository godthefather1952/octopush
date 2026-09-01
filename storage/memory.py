"""In-memory event store, for tests and short-lived sessions."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable

from core.events import Event, EventType
from core.models.common import Millis
from storage.base import EventStore, SessionInfo


class InMemoryEventStore(EventStore):
    def __init__(self) -> None:
        self._events: dict[str, list[Event]] = {}
        self._sessions: dict[str, SessionInfo] = {}
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
