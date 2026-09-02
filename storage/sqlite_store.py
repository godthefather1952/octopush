"""SQLite event store.

The default local backend: durable, replayable and dependency-free, so a
developer can record and replay a session without standing up PostgreSQL.
The schema mirrors the PostgreSQL one, so moving between them is a config
change.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import AsyncIterator, Iterable
from pathlib import Path

from core.events import Event, EventType
from core.models.common import Millis
from storage.base import EventStore, SessionInfo

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id   TEXT PRIMARY KEY,
    started_at   INTEGER NOT NULL,
    ended_at     INTEGER,
    label        TEXT NOT NULL DEFAULT '',
    config_hash  TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS events (
    session_id     TEXT NOT NULL,
    event_id       TEXT NOT NULL,
    seq            INTEGER,
    ts_ms          INTEGER NOT NULL,
    type           TEXT NOT NULL,
    source         TEXT NOT NULL,
    schema_name    TEXT,
    correlation_id TEXT,
    payload        TEXT NOT NULL,
    PRIMARY KEY (session_id, event_id)
);

CREATE INDEX IF NOT EXISTS idx_events_order ON events (session_id, ts_ms, seq, event_id);
CREATE INDEX IF NOT EXISTS idx_events_type  ON events (session_id, type, ts_ms);
CREATE INDEX IF NOT EXISTS idx_events_corr  ON events (session_id, correlation_id);
"""


class SQLiteEventStore(EventStore):
    def __init__(self, path: str) -> None:
        self.path = path
        self._conn: sqlite3.Connection | None = None
        self._lock = asyncio.Lock()

    # -- lifecycle ---------------------------------------------------------

    async def open(self) -> None:
        if self._conn is not None:
            return
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.executescript(SCHEMA)
        conn.commit()
        self._conn = conn

    async def close(self) -> None:
        if self._conn is not None:
            self._conn.commit()
            self._conn.close()
            self._conn = None

    def _require(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("event store is not open")
        return self._conn

    # -- writes ------------------------------------------------------------

    async def start_session(
        self, session_id: str, started_at: Millis, label: str = "", config_hash: str = ""
    ) -> None:
        async with self._lock:
            conn = self._require()
            conn.execute(
                "INSERT OR REPLACE INTO sessions "
                "(session_id, started_at, ended_at, label, config_hash) "
                "VALUES (?, ?, NULL, ?, ?)",
                (session_id, int(started_at), label, config_hash),
            )
            conn.commit()

    async def end_session(self, session_id: str, ended_at: Millis) -> None:
        async with self._lock:
            conn = self._require()
            conn.execute(
                "UPDATE sessions SET ended_at = ? WHERE session_id = ?",
                (int(ended_at), session_id),
            )
            conn.commit()

    @staticmethod
    def _row(session_id: str, event: Event) -> tuple:
        """Serialise one event.

        ``allow_nan=False`` is deliberate: SQLite would happily store the bare
        ``Infinity``/``NaN`` literals as text while PostgreSQL JSONB rejects
        them, so without this the two backends diverge silently. Payloads are
        already sanitised by ``Base.to_json_dict``; this makes any bypass fail
        loudly here instead of at the production backend.
        """
        return (
            session_id,
            event.id,
            None if event.sequence is None else int(event.sequence),
            int(event.ts_ms),
            event.type.value,
            event.source,
            event.schema_name,
            event.correlation_id,
            json.dumps(event.payload, default=str, allow_nan=False),
        )

    async def append(self, session_id: str, event: Event) -> None:
        await self.append_many(session_id, [event])

    async def append_many(self, session_id: str, events: Iterable[Event]) -> None:
        rows = [self._row(session_id, event) for event in events]
        if not rows:
            return
        async with self._lock:
            conn = self._require()
            conn.executemany(
                "INSERT OR IGNORE INTO events "
                "(session_id, event_id, seq, ts_ms, type, source, schema_name, "
                " correlation_id, payload) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
            conn.commit()

    # -- reads -------------------------------------------------------------

    async def read(
        self,
        session_id: str,
        *,
        types: Iterable[EventType] | None = None,
        start_ms: Millis | None = None,
        end_ms: Millis | None = None,
    ) -> AsyncIterator[Event]:
        conn = self._require()
        clauses = ["session_id = ?"]
        params: list[object] = [session_id]
        if types is not None:
            values = [t.value for t in types]
            if not values:
                return
            clauses.append(f"type IN ({','.join('?' * len(values))})")
            params.extend(values)
        if start_ms is not None:
            clauses.append("ts_ms >= ?")
            params.append(int(start_ms))
        if end_ms is not None:
            clauses.append("ts_ms <= ?")
            params.append(int(end_ms))
        sql = (
            "SELECT * FROM events WHERE "
            + " AND ".join(clauses)
            + " ORDER BY ts_ms, COALESCE(seq, 0), event_id"
        )
        for row in conn.execute(sql, params):
            yield Event(
                id=row["event_id"],
                type=EventType(row["type"]),
                ts_ms=row["ts_ms"],
                source=row["source"],
                payload=json.loads(row["payload"]),
                schema_name=row["schema_name"],
                correlation_id=row["correlation_id"],
                sequence=row["seq"],
            )
            # Yield control so a long replay does not starve the loop.
            await asyncio.sleep(0)

    async def sessions(self) -> list[SessionInfo]:
        conn = self._require()
        out: list[SessionInfo] = []
        for row in conn.execute("SELECT * FROM sessions ORDER BY started_at"):
            count = conn.execute(
                "SELECT COUNT(*) FROM events WHERE session_id = ?", (row["session_id"],)
            ).fetchone()[0]
            out.append(
                SessionInfo(
                    session_id=row["session_id"],
                    started_at=row["started_at"],
                    ended_at=row["ended_at"],
                    event_count=count,
                    label=row["label"],
                    config_hash=row["config_hash"],
                )
            )
        return out

    async def session(self, session_id: str) -> SessionInfo | None:
        for info in await self.sessions():
            if info.session_id == session_id:
                return info
        return None

    async def count(self, session_id: str) -> int:
        conn = self._require()
        return conn.execute(
            "SELECT COUNT(*) FROM events WHERE session_id = ?", (session_id,)
        ).fetchone()[0]
