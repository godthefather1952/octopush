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

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id     TEXT PRIMARY KEY,
    started_at     INTEGER NOT NULL,
    ended_at       INTEGER,
    label          TEXT NOT NULL DEFAULT '',
    config_hash    TEXT NOT NULL DEFAULT '',
    -- NULL means "written before this column existed": such a row is read
    -- back as LEGACY_UNVERIFIED, never backfilled to COMPLETE. The build
    -- that wrote it could discard a failed batch and still end the session,
    -- so its ended_at proves nothing about completeness.
    status         TEXT,
    events_lost    INTEGER NOT NULL DEFAULT 0,
    failure_reason TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS events (
    session_id     TEXT NOT NULL,
    event_id       TEXT NOT NULL,
    seq            INTEGER,
    ts_ms          INTEGER NOT NULL,
    type           TEXT NOT NULL,
    source         TEXT NOT NULL,
    schema_name    TEXT,
    schema_version INTEGER NOT NULL DEFAULT 1,
    correlation_id TEXT,
    causation_id   TEXT,
    payload        TEXT NOT NULL,
    PRIMARY KEY (session_id, event_id)
);

CREATE INDEX IF NOT EXISTS idx_events_order ON events (session_id, ts_ms, seq, event_id);
CREATE INDEX IF NOT EXISTS idx_events_type  ON events (session_id, type, ts_ms);
CREATE INDEX IF NOT EXISTS idx_events_corr  ON events (session_id, correlation_id);
"""


#: Columns added after the initial schema, with the DDL to add them.
#: ``CREATE TABLE IF NOT EXISTS`` does nothing to a table that already exists,
#: so without this a database created by an older build keeps its original
#: columns and every insert fails on the unknown ones — which the recorder
#: catches by design, so the loss shows up as a climbing ``events_lost``
#: rather than as a crash.
_ADDED_COLUMNS: tuple[tuple[str, str, str], ...] = (
    # (table, column, DDL)
    ("events", "schema_version", "ALTER TABLE events ADD COLUMN schema_version INTEGER"),
    ("events", "causation_id", "ALTER TABLE events ADD COLUMN causation_id TEXT"),
    # Phase 2 Batch 2. Deliberately nullable with NO backfill: an existing
    # row gets status NULL and is therefore reported LEGACY_UNVERIFIED.
    # `UPDATE sessions SET status='COMPLETE'` would certify history this
    # build has no evidence for.
    ("sessions", "status", "ALTER TABLE sessions ADD COLUMN status TEXT"),
    (
        "sessions",
        "events_lost",
        "ALTER TABLE sessions ADD COLUMN events_lost INTEGER NOT NULL DEFAULT 0",
    ),
    (
        "sessions",
        "failure_reason",
        "ALTER TABLE sessions ADD COLUMN failure_reason TEXT NOT NULL DEFAULT ''",
    ),
)


def _add_missing_columns(conn: sqlite3.Connection) -> None:
    """Bring an existing database up to the current schema.

    Only ever adds nullable columns, so it cannot fail on existing rows and
    needs no backfill: a row written before the column existed reads back as
    NULL, which is the truthful answer — that event genuinely had no
    causation recorded.
    """
    for table, column, ddl in _ADDED_COLUMNS:
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            conn.execute(ddl)


def _status_of(raw: str | None) -> SessionStatus:
    """Interpret a stored status column.

    NULL means the row predates the column. It is LEGACY_UNVERIFIED, never
    COMPLETE: the build that wrote it could lose a batch and still end the
    session, so its ``ended_at`` carries no completeness claim at all.
    """
    if raw is None:
        return SessionStatus.LEGACY_UNVERIFIED
    try:
        return SessionStatus(raw)
    except ValueError:
        return SessionStatus.LEGACY_UNVERIFIED


def _row_fingerprint(row) -> tuple:
    """The stored identity of an event row, matching ``event_fingerprint``."""
    payload = json.loads(row["payload"])
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
        _add_missing_columns(conn)
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
            # A plain INSERT, never INSERT OR REPLACE: replacing would clear
            # ended_at and status on an existing session, quietly reopening
            # finished history (P2-2).
            existing = conn.execute(
                "SELECT status FROM sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
            if existing is not None:
                raise SessionAlreadyExists(
                    f"session {session_id!r} already exists with status "
                    f"{_status_of(existing['status']).value}; a session id is "
                    "used once and is never reopened (no resume protocol "
                    "exists)"
                )
            try:
                conn.execute(
                    "INSERT INTO sessions "
                    "(session_id, started_at, ended_at, label, config_hash, "
                    " status, events_lost, failure_reason) "
                    "VALUES (?, ?, NULL, ?, ?, ?, 0, '')",
                    (
                        session_id,
                        int(started_at),
                        label,
                        config_hash,
                        SessionStatus.OPEN.value,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                # The SELECT above is serialised only against this process's
                # own callers. Another process writing the same database wins
                # the race at the primary key instead, and that must surface
                # as the same refusal rather than as a raw driver error.
                raise SessionAlreadyExists(
                    f"session {session_id!r} already exists (created "
                    "concurrently); a session id is used once and is never "
                    "reopened"
                ) from exc
            conn.commit()

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
        async with self._lock:
            conn = self._require()
            row = conn.execute(
                "SELECT status FROM sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
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
            conn.execute(
                "UPDATE sessions SET ended_at = ?, status = ?, events_lost = ?, "
                "failure_reason = ? WHERE session_id = ?",
                (
                    int(ended_at),
                    status.value,
                    int(events_lost),
                    failure_reason,
                    session_id,
                ),
            )
            conn.commit()

    def _status_row(self, session_id: str):
        conn = self._require()
        return conn.execute(
            "SELECT status FROM sessions WHERE session_id = ?", (session_id,)
        ).fetchone()

    def _require_open_locked(self, session_id: str) -> None:
        row = self._status_row(session_id)
        if row is None:
            raise UnknownSession(
                f"session {session_id!r} was never started; storage does not "
                "create sessions implicitly"
            )
        current = _status_of(row["status"])
        if current.is_terminal:
            raise SessionNotOpen(
                f"session {session_id!r} is {current.value}; appending after "
                "finalisation would grow a session already reported as closed"
            )

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
            int(event.schema_version),
            event.correlation_id,
            event.causation_id,
            json.dumps(event.payload, default=str, allow_nan=False),
        )

    async def append(self, session_id: str, event: Event) -> None:
        await self.append_many(session_id, [event])

    async def append_many(self, session_id: str, events: Iterable[Event]) -> None:
        """Append a batch atomically.

        The whole batch runs inside ONE explicit transaction, and every
        collision check happens inside it too. That is deliberate rather
        than inherited from driver defaults (Phase 2 Batch 2): sqlite3's
        implicit transaction handling around ``executemany`` is a property
        of the connection's isolation level, not a guarantee the caller can
        reason about. With an explicit BEGIN, a raise leaves storage exactly
        as it was, so the recorder's retry converges on the intended history
        instead of layering a half-written batch under it.
        """
        batch = list(events)
        if not batch:
            return
        async with self._lock:
            conn = self._require()
            self._require_open_locked(session_id)

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

            # Compare against what is already stored, in the same transaction
            # that will do the writing.
            conn.execute("BEGIN IMMEDIATE")
            try:
                ids = list(incoming)
                to_write: list[tuple] = []
                for start in range(0, len(ids), 500):
                    chunk = ids[start : start + 500]
                    placeholders = ",".join("?" * len(chunk))
                    existing = conn.execute(
                        f"SELECT * FROM events WHERE session_id = ? "
                        f"AND event_id IN ({placeholders})",
                        [session_id, *chunk],
                    ).fetchall()
                    for row in existing:
                        stored = _row_fingerprint(row)
                        incoming_print = prints[row["event_id"]]
                        if stored != incoming_print:
                            raise EventIdCollision(
                                f"session {session_id!r} already holds a "
                                f"different event under id "
                                f"{row['event_id']!r}: "
                                + describe_fingerprint_mismatch(
                                    stored, incoming_print
                                )
                            )
                        # Identical re-delivery: the retry no-op.
                        incoming.pop(row["event_id"], None)

                to_write = [self._row(session_id, e) for e in incoming.values()]
                if to_write:
                    conn.executemany(
                        "INSERT INTO events "
                        "(session_id, event_id, seq, ts_ms, type, source, "
                        " schema_name, schema_version, correlation_id, "
                        " causation_id, payload) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        to_write,
                    )
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

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
                # A row written before the column existed reports NULL. It was
                # written by the schema that predates versioning, which is
                # version 1 — not today's version, which would claim the old
                # row had a shape it never had.
                schema_version=row["schema_version"] or 1,
                correlation_id=row["correlation_id"],
                causation_id=row["causation_id"],
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
                    status=_status_of(row["status"]),
                    events_lost=row["events_lost"] or 0,
                    failure_reason=row["failure_reason"] or "",
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
