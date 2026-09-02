"""PostgreSQL event store.

The production backend.  The schema is identical in shape to the SQLite one;
``storage/migrations`` holds the DDL, and TimescaleDB can be layered on the
``events`` table later without changing this code — turning it into a
hypertable is a migration, not a rewrite.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterable

from core.events import Event, EventType
from core.models.common import Millis
from storage.base import EventStore, SessionInfo


class PostgresEventStore(EventStore):  # pragma: no cover - requires a server
    def __init__(self, dsn: str, *, min_size: int = 1, max_size: int = 8) -> None:
        self.dsn = dsn
        self._min_size = min_size
        self._max_size = max_size
        self._pool = None

    async def open(self) -> None:
        if self._pool is not None:
            return
        import asyncpg

        self._pool = await asyncpg.create_pool(
            self.dsn, min_size=self._min_size, max_size=self._max_size
        )
        async with self._pool.acquire() as conn:
            await conn.execute(MIGRATION_SQL)

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    def _require(self):
        if self._pool is None:
            raise RuntimeError("event store is not open")
        return self._pool

    async def start_session(
        self, session_id: str, started_at: Millis, label: str = "", config_hash: str = ""
    ) -> None:
        async with self._require().acquire() as conn:
            await conn.execute(
                """
                INSERT INTO sessions (session_id, started_at, label, config_hash)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (session_id) DO UPDATE
                    SET started_at = EXCLUDED.started_at,
                        label = EXCLUDED.label,
                        config_hash = EXCLUDED.config_hash
                """,
                session_id,
                int(started_at),
                label,
                config_hash,
            )

    async def end_session(self, session_id: str, ended_at: Millis) -> None:
        async with self._require().acquire() as conn:
            await conn.execute(
                "UPDATE sessions SET ended_at = $1 WHERE session_id = $2",
                int(ended_at),
                session_id,
            )

    async def append(self, session_id: str, event: Event) -> None:
        await self.append_many(session_id, [event])

    async def append_many(self, session_id: str, events: Iterable[Event]) -> None:
        rows = [
            (
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
            for event in events
        ]
        if not rows:
            return
        async with self._require().acquire() as conn:
            await conn.executemany(
                """
                INSERT INTO events (session_id, event_id, seq, ts_ms, type, source,
                                    schema_name, correlation_id, payload)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb)
                ON CONFLICT (session_id, event_id) DO NOTHING
                """,
                rows,
            )

    async def read(
        self,
        session_id: str,
        *,
        types: Iterable[EventType] | None = None,
        start_ms: Millis | None = None,
        end_ms: Millis | None = None,
    ) -> AsyncIterator[Event]:
        clauses = ["session_id = $1"]
        params: list[object] = [session_id]
        if types is not None:
            params.append([t.value for t in types])
            clauses.append(f"type = ANY(${len(params)})")
        if start_ms is not None:
            params.append(int(start_ms))
            clauses.append(f"ts_ms >= ${len(params)}")
        if end_ms is not None:
            params.append(int(end_ms))
            clauses.append(f"ts_ms <= ${len(params)}")
        sql = (
            "SELECT * FROM events WHERE "
            + " AND ".join(clauses)
            + " ORDER BY ts_ms, COALESCE(seq, 0), event_id"
        )
        async with self._require().acquire() as conn, conn.transaction():
            async for row in conn.cursor(sql, *params):
                payload = row["payload"]
                yield Event(
                    id=row["event_id"],
                    type=EventType(row["type"]),
                    ts_ms=row["ts_ms"],
                    source=row["source"],
                    payload=json.loads(payload) if isinstance(payload, str) else payload,
                    schema_name=row["schema_name"],
                    correlation_id=row["correlation_id"],
                    sequence=row["seq"],
                )

    async def sessions(self) -> list[SessionInfo]:
        async with self._require().acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT s.*, COALESCE(c.n, 0) AS n
                FROM sessions s
                LEFT JOIN (
                    SELECT session_id, COUNT(*) AS n FROM events GROUP BY session_id
                ) c ON c.session_id = s.session_id
                ORDER BY s.started_at
                """
            )
        return [
            SessionInfo(
                session_id=row["session_id"],
                started_at=row["started_at"],
                ended_at=row["ended_at"],
                event_count=row["n"],
                label=row["label"],
                config_hash=row["config_hash"],
            )
            for row in rows
        ]

    async def session(self, session_id: str) -> SessionInfo | None:
        for info in await self.sessions():
            if info.session_id == session_id:
                return info
        return None

    async def count(self, session_id: str) -> int:
        async with self._require().acquire() as conn:
            return await conn.fetchval(
                "SELECT COUNT(*) FROM events WHERE session_id = $1", session_id
            )


MIGRATION_SQL = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id   TEXT PRIMARY KEY,
    started_at   BIGINT NOT NULL,
    ended_at     BIGINT,
    label        TEXT NOT NULL DEFAULT '',
    config_hash  TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS events (
    session_id     TEXT   NOT NULL,
    event_id       TEXT   NOT NULL,
    seq            BIGINT,
    ts_ms          BIGINT NOT NULL,
    type           TEXT   NOT NULL,
    source         TEXT   NOT NULL,
    schema_name    TEXT,
    correlation_id TEXT,
    payload        JSONB  NOT NULL,
    PRIMARY KEY (session_id, event_id)
);

CREATE INDEX IF NOT EXISTS idx_events_order ON events (session_id, ts_ms, seq, event_id);
CREATE INDEX IF NOT EXISTS idx_events_type  ON events (session_id, type, ts_ms);
CREATE INDEX IF NOT EXISTS idx_events_corr  ON events (session_id, correlation_id);
"""
