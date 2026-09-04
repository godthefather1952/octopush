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


def _status_of(raw: str | None) -> SessionStatus:
    """Interpret a stored status column; NULL means a pre-Batch-2 row."""
    if raw is None:
        return SessionStatus.LEGACY_UNVERIFIED
    try:
        return SessionStatus(raw)
    except ValueError:
        return SessionStatus.LEGACY_UNVERIFIED


def _row_fingerprint(row) -> tuple:
    """The stored identity of an event row, matching ``event_fingerprint``."""
    payload = row["payload"]
    if isinstance(payload, str):
        payload = json.loads(payload)
    return (
        row["event_id"],
        None if row["seq"] is None else int(row["seq"]),
        int(row["ts_ms"]),
        row["type"],
        row["source"],
        row["schema_name"],
        int(row["schema_version"] or 1),
        row["correlation_id"],
        row["causation_id"],
        json.dumps(payload, sort_keys=True, default=str, allow_nan=False),
    )


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
            # Same reason as the SQLite store: CREATE TABLE IF NOT EXISTS is a
            # no-op against a table that already exists, so a database created
            # by an older build would keep its original columns and every
            # insert would fail on the unknown ones. IF NOT EXISTS on the
            # column makes this idempotent and safe to run on every open.
            await conn.execute(ADD_COLUMNS_SQL)

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
        # No ON CONFLICT DO UPDATE: it used to rewrite an existing session's
        # metadata in place (P2-2), producing a third, different answer from
        # the same call the other two backends already disagreed about.
        # Imported here rather than at module scope for the same reason
        # asyncpg itself is: this module must import without the driver
        # installed, so a SQLite-only deployment needs no PostgreSQL client.
        from asyncpg.exceptions import UniqueViolationError

        async with self._require().acquire() as conn, conn.transaction():
            existing = await conn.fetchrow(
                "SELECT status FROM sessions WHERE session_id = $1 FOR UPDATE",
                session_id,
            )
            if existing is not None:
                raise SessionAlreadyExists(
                    f"session {session_id!r} already exists with status "
                    f"{_status_of(existing['status']).value}; a session id is "
                    "used once and is never reopened (no resume protocol "
                    "exists)"
                )
            try:
                await conn.execute(
                    """
                    INSERT INTO sessions (session_id, started_at, label, config_hash,
                                          status, events_lost, failure_reason)
                    VALUES ($1, $2, $3, $4, $5, 0, '')
                    """,
                    session_id,
                    int(started_at),
                    label,
                    config_hash,
                    SessionStatus.OPEN.value,
                )
            except UniqueViolationError as exc:
                # FOR UPDATE locks rows that exist; it cannot lock a row that
                # does not. Two processes starting the same id concurrently
                # therefore race to the primary key, and the loser must see
                # the same refusal as any other duplicate start.
                raise SessionAlreadyExists(
                    f"session {session_id!r} already exists (created "
                    "concurrently); a session id is used once and is never "
                    "reopened"
                ) from exc

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
        async with self._require().acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                "SELECT status FROM sessions WHERE session_id = $1 FOR UPDATE",
                session_id,
            )
            if row is None:
                raise UnknownSession(
                    f"session {session_id!r} was never started; storage does "
                    "not create sessions implicitly"
                )
            current = _status_of(row["status"])
            if current.is_terminal:
                raise SessionNotOpen(
                    f"session {session_id!r} is already {current.value}; "
                    "re-finalising would overwrite an integrity claim silently"
                )
            await conn.execute(
                "UPDATE sessions SET ended_at = $1, status = $2, "
                "events_lost = $3, failure_reason = $4 WHERE session_id = $5",
                int(ended_at),
                status.value,
                int(events_lost),
                failure_reason,
                session_id,
            )

    async def append(self, session_id: str, event: Event) -> None:
        await self.append_many(session_id, [event])

    @staticmethod
    def _row(session_id: str, event: Event) -> tuple:
        return (
            session_id,
            event.id,
            None if event.sequence is None else int(event.sequence),
            int(event.ts_ms),
            event.type.value,
            event.source,
            event.schema_name,
            int(event.schema_version),
            event.correlation_id,
            event.causation_id,
            json.dumps(event.payload, default=str, allow_nan=False),
        )

    async def append_many(self, session_id: str, events: Iterable[Event]) -> None:
        """Append a batch atomically.

        Wrapped in ONE explicit transaction on purpose (Phase 2 Batch 2).
        asyncpg's ``executemany`` is not documented to give the caller a
        single all-or-nothing statement, and the recorder's retry safety
        cannot rest on driver folklore: with an explicit transaction, a
        raise leaves storage untouched and an exact retry converges on the
        intended history.
        """
        batch = list(events)
        if not batch:
            return

        incoming: dict[str, Event] = {}
        prints: dict[str, tuple] = {}
        for event in batch:
            fingerprint = event_fingerprint(event)
            previous = prints.get(event.id)
            if previous is not None:
                if previous != fingerprint:
                    raise EventIdCollision(
                        f"batch for session {session_id!r} carries two "
                        f"different events under id {event.id!r}: "
                        f"{describe_fingerprint_mismatch(previous, fingerprint)}"
                    )
                continue
            prints[event.id] = fingerprint
            incoming[event.id] = event

        async with self._require().acquire() as conn, conn.transaction():
            status = await conn.fetchrow(
                "SELECT status FROM sessions WHERE session_id = $1", session_id
            )
            if status is None:
                raise UnknownSession(
                    f"session {session_id!r} was never started; storage does "
                    "not create sessions implicitly"
                )
            current = _status_of(status["status"])
            if current.is_terminal:
                raise SessionNotOpen(
                    f"session {session_id!r} is {current.value}; appending "
                    "after finalisation would grow a session already reported "
                    "as closed"
                )

            existing = await conn.fetch(
                "SELECT * FROM events WHERE session_id = $1 AND event_id = ANY($2)",
                session_id,
                list(incoming),
            )
            for row in existing:
                stored = _row_fingerprint(row)
                incoming_print = prints[row["event_id"]]
                if stored != incoming_print:
                    raise EventIdCollision(
                        f"session {session_id!r} already holds a different "
                        f"event under id {row['event_id']!r}: "
                        + describe_fingerprint_mismatch(stored, incoming_print)
                    )
                incoming.pop(row["event_id"], None)

            rows = [self._row(session_id, e) for e in incoming.values()]
            if rows:
                await conn.executemany(
                    """
                    INSERT INTO events (session_id, event_id, seq, ts_ms, type,
                                        source, schema_name, schema_version,
                                        correlation_id, causation_id, payload)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11::jsonb)
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
                    # A row written before the column existed reports NULL. It was
                    # written by the schema that predates versioning, which is
                    # version 1 — not today's version, which would claim the old
                    # row had a shape it never had.
                    schema_version=row["schema_version"] or 1,
                    correlation_id=row["correlation_id"],
                    causation_id=row["causation_id"],
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
                status=_status_of(row["status"]),
                events_lost=row["events_lost"] or 0,
                failure_reason=row["failure_reason"] or "",
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


ADD_COLUMNS_SQL = """
ALTER TABLE events   ADD COLUMN IF NOT EXISTS schema_version INTEGER;
ALTER TABLE events   ADD COLUMN IF NOT EXISTS causation_id   TEXT;
-- Phase 2 Batch 2. status stays NULLABLE with NO backfill: an existing row
-- reads back as LEGACY_UNVERIFIED. `UPDATE sessions SET status='COMPLETE'`
-- would certify history this build has no evidence for -- the writer that
-- created those rows could discard a failed batch and still end the session.
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS status         TEXT;
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS events_lost    BIGINT NOT NULL DEFAULT 0;
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS failure_reason TEXT NOT NULL DEFAULT '';
"""

MIGRATION_SQL = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id     TEXT PRIMARY KEY,
    started_at     BIGINT NOT NULL,
    ended_at       BIGINT,
    label          TEXT NOT NULL DEFAULT '',
    config_hash    TEXT NOT NULL DEFAULT '',
    status         TEXT,
    events_lost    BIGINT NOT NULL DEFAULT 0,
    failure_reason TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS events (
    session_id     TEXT   NOT NULL,
    event_id       TEXT   NOT NULL,
    seq            BIGINT,
    ts_ms          BIGINT NOT NULL,
    type           TEXT   NOT NULL,
    source         TEXT   NOT NULL,
    schema_name    TEXT,
    schema_version INTEGER NOT NULL DEFAULT 1,
    correlation_id TEXT,
    causation_id   TEXT,
    payload        JSONB  NOT NULL,
    PRIMARY KEY (session_id, event_id)
);

CREATE INDEX IF NOT EXISTS idx_events_order ON events (session_id, ts_ms, seq, event_id);
CREATE INDEX IF NOT EXISTS idx_events_type  ON events (session_id, type, ts_ms);
CREATE INDEX IF NOT EXISTS idx_events_corr  ON events (session_id, correlation_id);
"""
