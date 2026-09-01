"""Event store interface.

The store is the system's memory.  Everything that happened — market data,
agent outputs, decisions, orders, fills, failures — goes in, in order, so a
session can be replayed exactly.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass

from core.events import Event, EventType
from core.models.common import Millis


@dataclass(frozen=True)
class SessionInfo:
    session_id: str
    started_at: Millis
    ended_at: Millis | None
    event_count: int
    label: str = ""
    #: Configuration digest, so a replay can refuse to compare sessions that
    #: were produced by materially different configuration.
    config_hash: str = ""


class EventStore(ABC):
    @abstractmethod
    async def open(self) -> None: ...

    @abstractmethod
    async def close(self) -> None: ...

    @abstractmethod
    async def start_session(
        self, session_id: str, started_at: Millis, label: str = "", config_hash: str = ""
    ) -> None: ...

    @abstractmethod
    async def end_session(self, session_id: str, ended_at: Millis) -> None: ...

    @abstractmethod
    async def append(self, session_id: str, event: Event) -> None: ...

    @abstractmethod
    async def append_many(self, session_id: str, events: Iterable[Event]) -> None: ...

    @abstractmethod
    def read(
        self,
        session_id: str,
        *,
        types: Iterable[EventType] | None = None,
        start_ms: Millis | None = None,
        end_ms: Millis | None = None,
    ) -> AsyncIterator[Event]:
        """Yield events in deterministic order: (ts_ms, sequence, id)."""

    @abstractmethod
    async def sessions(self) -> list[SessionInfo]: ...

    @abstractmethod
    async def session(self, session_id: str) -> SessionInfo | None: ...

    @abstractmethod
    async def count(self, session_id: str) -> int: ...
