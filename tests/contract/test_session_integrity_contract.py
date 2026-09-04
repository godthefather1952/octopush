"""Phase 2 Batch 2: one session-integrity contract, identical on every backend.

The claim this batch has to earn is:

    "A session reported as COMPLETE contains every event the recorder
    accepted for that session, and no event was silently replaced, dropped,
    mutated, mixed with another run, or appended after finalization."

Each part of that sentence is a section below, run against the in-memory,
SQLite and (when one is reachable) real PostgreSQL stores — because the
defect this replaces was precisely that the three backends answered the same
call three different ways (P2-2).
"""

from __future__ import annotations

import json
import os

import pytest

from core.events import Event, EventType
from storage import InMemoryEventStore, SQLiteEventStore
from storage.base import (
    EventIdCollision,
    EventStore,
    SessionAlreadyExists,
    SessionNotOpen,
    SessionStatus,
    UnknownSession,
)

POSTGRES_DSN = os.environ.get("TF_TEST_POSTGRES_DSN", "")
START_MS = 1_700_000_000_000


def postgres_available() -> bool:
    if not POSTGRES_DSN:
        return False
    try:
        import asyncpg  # noqa: F401
    except ImportError:
        return False
    return True


@pytest.fixture(params=["memory", "sqlite", "postgres"])
async def store(request, tmp_path):
    kind = request.param
    if kind == "memory":
        opened: EventStore = InMemoryEventStore()
    elif kind == "sqlite":
        opened = SQLiteEventStore(str(tmp_path / "events.db"))
    else:
        if not postgres_available():
            pytest.skip("no PostgreSQL server configured")
        from storage.postgres_store import PostgresEventStore

        opened = PostgresEventStore(POSTGRES_DSN)

    await opened.open()
    if kind == "postgres":
        async with opened._require().acquire() as conn:
            await conn.execute("TRUNCATE events, sessions")
    try:
        yield opened
    finally:
        await opened.close()


def event(**overrides) -> Event:
    fields: dict = {
        "id": "evt-1",
        "type": EventType.MARKET_UPDATE,
        "ts_ms": START_MS,
        "source": "TIDAL",
        "schema_name": "Probe",
        "schema_version": 1,
        "sequence": 1,
        "correlation_id": "corr-1",
        "causation_id": "cause-1",
        "payload": {"value": 1},
    }
    fields.update(overrides)
    return Event(**fields)


# ==========================================================================
# Section 2 — the session state machine
# ==========================================================================


class TestSessionStateMachine:
    """The eleven required transitions, identical on every backend."""

    async def test_1_a_new_id_starts_open(self, store):
        await store.start_session("s1", START_MS)
        info = await store.session("s1")
        assert info.status is SessionStatus.OPEN
        assert info.ended_at is None
        assert info.events_lost == 0

    async def test_2_starting_an_open_session_again_fails(self, store):
        await store.start_session("s1", START_MS)
        with pytest.raises(SessionAlreadyExists):
            await store.start_session("s1", START_MS + 1)

    async def test_3_starting_a_complete_session_again_fails(self, store):
        await store.start_session("s1", START_MS)
        await store.finalize_session(
            "s1", START_MS + 1, status=SessionStatus.COMPLETE
        )
        with pytest.raises(SessionAlreadyExists):
            await store.start_session("s1", START_MS + 2)

    async def test_4_starting_an_incomplete_session_again_fails(self, store):
        await store.start_session("s1", START_MS)
        await store.finalize_session(
            "s1",
            START_MS + 1,
            status=SessionStatus.INCOMPLETE,
            events_lost=3,
            failure_reason="storage gone",
        )
        with pytest.raises(SessionAlreadyExists):
            await store.start_session("s1", START_MS + 2)

    async def test_5_starting_a_legacy_session_again_fails(self, tmp_path):
        """LEGACY_UNVERIFIED is a real status, not a gap to be filled in."""
        store = await _legacy_sqlite(tmp_path)
        try:
            assert (await store.session("legacy")).status is (
                SessionStatus.LEGACY_UNVERIFIED
            )
            with pytest.raises(SessionAlreadyExists):
                await store.start_session("legacy", START_MS)
        finally:
            await store.close()

    async def test_6_appending_to_an_unknown_session_fails(self, store):
        with pytest.raises(UnknownSession):
            await store.append("never-started", event())
        with pytest.raises(UnknownSession):
            await store.append_many("never-started", [event()])

    async def test_7_appending_to_an_open_session_is_allowed(self, store):
        await store.start_session("s1", START_MS)
        await store.append("s1", event())
        assert await store.count("s1") == 1

    async def test_8_appending_to_a_complete_session_fails(self, store):
        await store.start_session("s1", START_MS)
        await store.append("s1", event())
        await store.finalize_session(
            "s1", START_MS + 1, status=SessionStatus.COMPLETE
        )
        with pytest.raises(SessionNotOpen):
            await store.append("s1", event(id="evt-2"))
        assert await store.count("s1") == 1

    async def test_9_appending_to_an_incomplete_session_fails(self, store):
        await store.start_session("s1", START_MS)
        await store.finalize_session(
            "s1", START_MS + 1, status=SessionStatus.INCOMPLETE, events_lost=1
        )
        with pytest.raises(SessionNotOpen):
            await store.append("s1", event())
        assert await store.count("s1") == 0

    async def test_10_finalizing_an_unknown_session_fails(self, store):
        with pytest.raises(UnknownSession):
            await store.finalize_session(
                "never-started", START_MS, status=SessionStatus.COMPLETE
            )

    async def test_11_finalizing_a_terminal_session_again_is_refused(self, store):
        """Rejection, not a silent metadata rewrite -- and identically on
        every backend, which is the whole point of P2-2."""
        await store.start_session("s1", START_MS)
        await store.finalize_session(
            "s1", START_MS + 1, status=SessionStatus.COMPLETE
        )
        before = await store.session("s1")
        with pytest.raises(SessionNotOpen):
            await store.finalize_session(
                "s1", START_MS + 99, status=SessionStatus.INCOMPLETE, events_lost=5
            )
        assert await store.session("s1") == before

    async def test_a_non_terminal_finalize_status_is_rejected(self, store):
        await store.start_session("s1", START_MS)
        with pytest.raises(ValueError):
            await store.finalize_session(
                "s1", START_MS + 1, status=SessionStatus.OPEN
            )

    async def test_incomplete_records_why_and_how_much(self, store):
        await store.start_session("s1", START_MS)
        await store.finalize_session(
            "s1",
            START_MS + 1,
            status=SessionStatus.INCOMPLETE,
            events_lost=17,
            failure_reason="database unreachable at shutdown",
        )
        info = await store.session("s1")
        assert info.status is SessionStatus.INCOMPLETE
        assert info.events_lost == 17
        assert info.failure_reason == "database unreachable at shutdown"
        assert not info.is_verified_complete


# ==========================================================================
# Section 3 — P2-2: a refused duplicate start changes NOTHING
# ==========================================================================


class TestDuplicateStartIsInert:
    @pytest.mark.parametrize(
        "status",
        [SessionStatus.OPEN, SessionStatus.COMPLETE, SessionStatus.INCOMPLETE],
    )
    async def test_every_field_survives_a_refused_duplicate(self, store, status):
        await store.start_session("s1", START_MS, label="first", config_hash="hash-a")
        await store.append("s1", event())
        if status is not SessionStatus.OPEN:
            await store.finalize_session(
                "s1",
                START_MS + 500,
                status=status,
                events_lost=4 if status is SessionStatus.INCOMPLETE else 0,
                failure_reason="why" if status is SessionStatus.INCOMPLETE else "",
            )
        before = await store.session("s1")
        before_events = [e.id async for e in store.read("s1")]

        with pytest.raises(SessionAlreadyExists):
            await store.start_session(
                "s1", START_MS + 9_999, label="second", config_hash="hash-b"
            )

        after = await store.session("s1")
        assert after == before, "a refused start must not touch any field"
        assert [e.id async for e in store.read("s1")] == before_events

    async def test_a_concurrent_duplicate_raises_the_same_error(self, tmp_path):
        """The SQL backends check-then-insert; the primary key is the backstop.

        Reading the row first serialises callers inside ONE process only. A
        second process starting the same id wins the race at the primary key
        instead, and that must reach the caller as ``SessionAlreadyExists``
        like every other duplicate start -- not as a raw driver error nobody
        is catching.
        """
        import sqlite3

        store = SQLiteEventStore(str(tmp_path / "race.db"))
        await store.open()
        try:
            real = store._require()

            class LosesTheRace:
                """The connection, except that the INSERT loses to another
                process that committed the same id a moment earlier."""

                def __getattr__(self, name):
                    return getattr(real, name)

                def execute(self, sql, *args, **kwargs):
                    if sql.lstrip().upper().startswith("INSERT INTO SESSIONS"):
                        raise sqlite3.IntegrityError(
                            "UNIQUE constraint failed: sessions.session_id"
                        )
                    return real.execute(sql, *args, **kwargs)

            store._conn = LosesTheRace()  # type: ignore[assignment]
            with pytest.raises(SessionAlreadyExists):
                await store.start_session("s1", START_MS)
            store._conn = real
        finally:
            await store.close()

    async def test_two_real_connections_racing_produce_exactly_one_session(self):
        """The same race against PostgreSQL, genuinely concurrent.

        Two stores with independent pools start the same id at once. Whether
        the loser is stopped by the SELECT or by the primary key depends on
        timing; the contract does not. Exactly one call succeeds, the other
        raises ``SessionAlreadyExists``, and one session exists afterwards.
        """
        if not postgres_available():
            pytest.skip("no PostgreSQL server configured")
        import asyncio

        from storage.postgres_store import PostgresEventStore

        a = PostgresEventStore(POSTGRES_DSN)
        b = PostgresEventStore(POSTGRES_DSN)
        await a.open()
        await b.open()
        try:
            async with a._require().acquire() as conn:
                await conn.execute("TRUNCATE events, sessions")

            results = await asyncio.gather(
                a.start_session("race", START_MS, label="a"),
                b.start_session("race", START_MS, label="b"),
                return_exceptions=True,
            )
            refusals = [r for r in results if isinstance(r, SessionAlreadyExists)]
            successes = [r for r in results if r is None]
            other = [
                r
                for r in results
                if r is not None and not isinstance(r, SessionAlreadyExists)
            ]
            assert not other, f"an unexpected error escaped: {other}"
            assert len(successes) == 1
            assert len(refusals) == 1

            info = await a.session("race")
            assert info is not None
            assert info.status is SessionStatus.OPEN
            assert info.label in {"a", "b"}
        finally:
            await a.close()
            await b.close()


# ==========================================================================
# Sections 13/14 — event-id collision vs true idempotency
# ==========================================================================


class TestEventIdCollisionMatrix:
    """Same id + same event is a retry. Same id + anything else is a lie."""

    async def _seed(self, store) -> None:
        await store.start_session("s1", START_MS)
        await store.append("s1", event())

    async def test_identical_event_is_an_idempotent_no_op(self, store):
        await self._seed(store)
        await store.append("s1", event())
        await store.append_many("s1", [event(), event()])
        assert await store.count("s1") == 1

    async def test_identical_payload_built_in_a_different_key_order(self, store):
        """Canonical comparison: dict order is not part of an event's identity."""
        await store.start_session("s1", START_MS)
        await store.append("s1", event(payload={"a": 1, "b": 2}))
        await store.append("s1", event(payload={"b": 2, "a": 1}))
        assert await store.count("s1") == 1

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("payload", {"value": 2}),
            ("ts_ms", START_MS + 1),
            ("sequence", 99),
            ("type", EventType.SYSTEM_EVENT),
            ("source", "OTHER"),
            ("schema_version", 2),
            ("correlation_id", "corr-2"),
            ("causation_id", "cause-2"),
            ("schema_name", "Different"),
        ],
    )
    async def test_a_different_event_under_the_same_id_is_refused(
        self, store, field, value
    ):
        await self._seed(store)
        with pytest.raises(EventIdCollision):
            await store.append("s1", event(**{field: value}))
        # The stored event is the original, untouched.
        stored = [e async for e in store.read("s1")]
        assert len(stored) == 1
        assert stored[0].id == "evt-1"
        assert stored[0].payload == {"value": 1}

    async def test_the_same_id_in_a_different_session_is_fine(self, store):
        await store.start_session("a", START_MS)
        await store.start_session("b", START_MS)
        await store.append("a", event())
        await store.append("b", event(payload={"value": 2}))
        assert await store.count("a") == 1
        assert await store.count("b") == 1

    async def test_a_collision_inside_one_batch_is_refused(self, store):
        await store.start_session("s1", START_MS)
        with pytest.raises(EventIdCollision):
            await store.append_many("s1", [event(), event(payload={"value": 2})])
        assert await store.count("s1") == 0

    async def test_a_duplicate_inside_one_batch_collapses(self, store):
        await store.start_session("s1", START_MS)
        await store.append_many("s1", [event(), event(), event(id="evt-2")])
        assert await store.count("s1") == 2


# ==========================================================================
# Section 15 — batch atomicity
# ==========================================================================


class TestBatchAtomicity:
    """Either the whole intended batch lands, or an exact retry converges."""

    async def test_a_collision_mid_batch_persists_nothing(self, store):
        await store.start_session("s1", START_MS)
        await store.append("s1", event(id="seed", sequence=0))
        good = [event(id=f"e{i}", sequence=i) for i in range(1, 5)]
        poisoned = [*good[:2], event(id="seed", payload={"value": 999}), *good[2:]]

        with pytest.raises(EventIdCollision):
            await store.append_many("s1", poisoned)

        assert await store.count("s1") == 1, (
            "a rejected batch must leave storage exactly as it was, so the "
            "recorder's retry converges instead of layering a half batch"
        )

    async def test_a_malformed_event_mid_batch_persists_nothing(self, store):
        await store.start_session("s1", START_MS)
        batch = [
            event(id="e1", sequence=1),
            event(id="e2", sequence=2, payload={"bad": float("inf")}),
            event(id="e3", sequence=3),
        ]
        with pytest.raises(ValueError):
            await store.append_many("s1", batch)
        assert await store.count("s1") == 0

    async def test_an_exact_retry_after_a_rejected_batch_converges(self, store):
        await store.start_session("s1", START_MS)
        batch = [event(id=f"e{i}", sequence=i) for i in range(1, 4)]
        poisoned = [*batch, event(id="e1", payload={"value": 999})]

        with pytest.raises(EventIdCollision):
            await store.append_many("s1", poisoned)
        assert await store.count("s1") == 0

        await store.append_many("s1", batch)
        assert await store.count("s1") == 3
        stored = [e.id async for e in store.read("s1")]
        assert sorted(stored) == ["e1", "e2", "e3"]

    async def test_an_unknown_commit_outcome_retry_does_not_duplicate(self, store):
        """Section 8: the database committed, the caller never heard so.

        The recorder cannot tell this apart from a real failure, so it
        retries the identical batch. Storage must converge on exactly one
        copy of each event.
        """
        await store.start_session("s1", START_MS)
        batch = [event(id=f"e{i}", sequence=i) for i in range(1, 6)]

        await store.append_many("s1", batch)  # commit the caller never learns of
        await store.append_many("s1", batch)  # the blind retry
        await store.append_many("s1", batch)  # and another, for good measure

        assert await store.count("s1") == 5
        stored = [e.id async for e in store.read("s1")]
        assert sorted(stored) == ["e1", "e2", "e3", "e4", "e5"]

    async def test_a_partially_overlapping_retry_converges(self, store):
        """The realistic retry: some of the batch was already committed."""
        await store.start_session("s1", START_MS)
        first = [event(id=f"e{i}", sequence=i) for i in range(1, 4)]
        await store.append_many("s1", first)

        extended = [*first, event(id="e4", sequence=4), event(id="e5", sequence=5)]
        await store.append_many("s1", extended)

        assert await store.count("s1") == 5


# ==========================================================================
# Section 6 — migration must not certify what it cannot verify
# ==========================================================================


OLD_SESSIONS_SCHEMA = """
CREATE TABLE sessions (
    session_id   TEXT PRIMARY KEY,
    started_at   INTEGER NOT NULL,
    ended_at     INTEGER,
    label        TEXT NOT NULL DEFAULT '',
    config_hash  TEXT NOT NULL DEFAULT ''
);
CREATE TABLE events (
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
"""


async def _legacy_sqlite(tmp_path) -> SQLiteEventStore:
    """A database written by a pre-Batch-2 build, with an ENDED session."""
    import sqlite3

    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    conn.executescript(OLD_SESSIONS_SCHEMA)
    conn.execute(
        "INSERT INTO sessions (session_id, started_at, ended_at, label) "
        "VALUES ('legacy', 1000, 2000, 'old run')"
    )
    conn.execute(
        "INSERT INTO events (session_id, event_id, seq, ts_ms, type, source, "
        "schema_name, correlation_id, payload) VALUES "
        "('legacy', 'old-1', 1, 1000, 'MARKET_UPDATE', 'TIDAL', 'Probe', NULL, ?)",
        (json.dumps({"value": 1}),),
    )
    conn.commit()
    conn.close()
    store = SQLiteEventStore(str(path))
    await store.open()
    return store


class TestLegacyMigration:
    async def test_an_ended_legacy_session_is_not_certified_complete(self, tmp_path):
        """Section 5, the crux.

        The old recorder could fail a write, discard the batch, count the
        loss in RAM only, and still end the session. ``ended_at`` therefore
        proves nothing, and backfilling these rows to COMPLETE would certify
        history nobody verified.
        """
        store = await _legacy_sqlite(tmp_path)
        try:
            info = await store.session("legacy")
            assert info.ended_at == 2000, "the row really does look finished"
            assert info.status is SessionStatus.LEGACY_UNVERIFIED
            assert not info.is_verified_complete
        finally:
            await store.close()

    async def test_legacy_events_remain_readable(self, tmp_path):
        store = await _legacy_sqlite(tmp_path)
        try:
            events = [e async for e in store.read("legacy")]
            assert [e.id for e in events] == ["old-1"]
            assert events[0].payload == {"value": 1}
        finally:
            await store.close()

    async def test_the_migration_is_idempotent(self, tmp_path):
        store = await _legacy_sqlite(tmp_path)
        await store.close()
        for _ in range(3):
            again = SQLiteEventStore(store.path)
            await again.open()
            assert await again.count("legacy") == 1
            assert (await again.session("legacy")).status is (
                SessionStatus.LEGACY_UNVERIFIED
            )
            await again.close()

    async def test_a_fresh_database_gets_the_current_schema(self, tmp_path):
        store = SQLiteEventStore(str(tmp_path / "fresh.db"))
        await store.open()
        try:
            await store.start_session("s1", START_MS)
            assert (await store.session("s1")).status is SessionStatus.OPEN
            await store.append("s1", event())
            await store.finalize_session(
                "s1", START_MS + 1, status=SessionStatus.COMPLETE
            )
            assert (await store.session("s1")).status is SessionStatus.COMPLETE
        finally:
            await store.close()
