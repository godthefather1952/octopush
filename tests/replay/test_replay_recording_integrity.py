"""Phase 2 Batch 2 Section 12: exact replay requires a verified recording.

Every other check in the replay engine asks whether the recorded events are
replayed faithfully. This asks the prior question: are they all of them?

A session is only exact-replay safe when the recorder confirmed every event
it accepted and said so durably. An OPEN session is missing whatever the
recorder still held; an INCOMPLETE one is missing history it already knows
about; a LEGACY_UNVERIFIED one was written by a build that could lose a batch
and end the session anyway. None of those may be presented as an exact
reproduction, and none may be silently upgraded by replaying them.
"""

from __future__ import annotations

import pytest

from core.bus import InMemoryEventBus
from core.clock import ManualClock
from core.events import Event, EventType
from replay.engine import (
    UNVERIFIED_CONFIGURATION,
    UNVERIFIED_RECORDING_INTEGRITY,
    FidelityDimension,
    IncompleteSessionError,
    ReplaySession,
    UnverifiedLegacySessionError,
    UnverifiedRecordingError,
)
from storage import InMemoryEventStore, SQLiteEventStore
from storage.base import SessionStatus

START_MS = 1_788_000_000_000
SESSION_ID = "s1"
#: Hand-built sessions carry a digest of their own and the replay is given
#: the matching one, so these tests exercise recording integrity rather
#: than tripping over an unrelated, unverified configuration dimension.
CONFIG_HASH = "test-config"


def _input(seq: int, ts_ms: int) -> Event:
    return Event(
        type=EventType.BOOK_SNAPSHOT,
        ts_ms=ts_ms,
        sequence=seq,
        source="VENUE_A",
        schema_name="Test",
        payload={},
    )


def _marker(seq: int, ts_ms: int, tick: int, watermark: int) -> Event:
    return Event(
        type=EventType.ORCHESTRATOR_TICK,
        ts_ms=ts_ms,
        sequence=seq,
        source="ORCHESTRATOR",
        schema_name="OrchestratorTick",
        payload={
            "tick": tick,
            "warmed_up": True,
            "processed_input_sequence": watermark,
        },
    )


EVENTS = [
    _input(1, START_MS),
    _marker(2, START_MS + 1, 1, 1),
    _input(3, START_MS + 2),
    _marker(4, START_MS + 3, 2, 3),
]


async def _store_with(status: SessionStatus | None, **finalize) -> InMemoryEventStore:
    """A well-formed recording, finalised into the requested status.

    ``None`` leaves the session OPEN, i.e. never finalised at all.
    """
    store = InMemoryEventStore()
    await store.open()
    await store.start_session(SESSION_ID, START_MS, config_hash=CONFIG_HASH)
    await store.append_many(SESSION_ID, EVENTS)
    if status is not None:
        await store.finalize_session(
            SESSION_ID, START_MS + 10, status=status, **finalize
        )
    return store


def _session(store, **kwargs) -> ReplaySession:
    return ReplaySession(
        store=store,
        bus=InMemoryEventBus(raise_on_handler_error=True),
        clock=ManualClock(START_MS),
        session_id=SESSION_ID,
        current_config_hash=CONFIG_HASH,
        **kwargs,
    )


class TestOnlyCompleteReplaysByDefault:
    async def test_a_complete_session_replays_as_verified(self):
        store = await _store_with(SessionStatus.COMPLETE)
        session = _session(store)
        with session:
            await session.open()
            assert session.stats.timeline_fidelity is None
            stats = await session.run()
        assert stats.ticks_read == 2

    async def test_an_open_session_is_refused(self):
        store = await _store_with(None)
        session = _session(store)
        with pytest.raises(IncompleteSessionError) as excinfo:
            await session.open()
        assert "OPEN" in str(excinfo.value)

    async def test_an_incomplete_session_is_refused_and_says_what_was_lost(self):
        store = await _store_with(
            SessionStatus.INCOMPLETE,
            events_lost=12,
            failure_reason="storage unreachable at shutdown",
        )
        session = _session(store)
        with pytest.raises(IncompleteSessionError) as excinfo:
            await session.open()
        message = str(excinfo.value)
        assert "INCOMPLETE" in message
        assert "12" in message
        assert "storage unreachable at shutdown" in message

    async def test_a_legacy_session_is_refused_with_its_own_error(self, tmp_path):
        store = await _legacy_store(tmp_path)
        try:
            session = _session(store)
            with pytest.raises(UnverifiedLegacySessionError):
                await session.open()
        finally:
            await store.close()


class TestTheOverrideIsVisible:
    """Replaying anyway is allowed -- silently calling it exact is not."""

    @pytest.mark.parametrize(
        ("status", "kwargs"),
        [
            (None, {}),
            (SessionStatus.INCOMPLETE, {"events_lost": 3}),
        ],
    )
    async def test_the_override_reports_unverified_integrity(self, status, kwargs):
        store = await _store_with(status, **kwargs)
        session = _session(store, allow_incomplete_session=True)
        with session:
            await session.open()
            assert session.stats.timeline_fidelity == UNVERIFIED_RECORDING_INTEGRITY
            stats = await session.run()
        assert stats.ticks_read == 2
        assert stats.timeline_fidelity == UNVERIFIED_RECORDING_INTEGRITY, (
            "a run over unverified history must never report itself as exact"
        )

    async def test_a_legacy_session_replays_under_the_override(self, tmp_path):
        """A pre-Batch-2 database predates config digests too, so BOTH
        dimensions are unverified -- which is precisely the case a single
        fidelity string could not express."""
        store = await _legacy_store(tmp_path)
        try:
            session = _session(
                store, allow_incomplete_session=True, allow_config_mismatch=True
            )
            with session:
                await session.open()
                fidelity = session.stats.fidelity
                assert (
                    fidelity.issues[FidelityDimension.RECORDING_INTEGRITY]
                    == UNVERIFIED_RECORDING_INTEGRITY
                )
                assert (
                    fidelity.issues[FidelityDimension.CONFIGURATION]
                    == UNVERIFIED_CONFIGURATION
                )
                assert not fidelity.is_exact
        finally:
            await store.close()

    async def test_the_override_does_not_upgrade_the_stored_session(self):
        """Replaying an unverified session must not certify it afterwards."""
        store = await _store_with(SessionStatus.INCOMPLETE, events_lost=1)
        session = _session(store, allow_incomplete_session=True)
        with session:
            await session.open()
            await session.run()
        info = await store.session(SESSION_ID)
        assert info.status is SessionStatus.INCOMPLETE


class TestTheOverrideIsReachableFromTheCli:
    """A refusal nobody can answer is a bug, not a safeguard.

    The engine's error messages tell the operator to accept the risk
    explicitly. That instruction is only true if the command-line tool can
    actually express it, and only if the flag's ``dest`` still matches the
    attribute ``replay()`` reads.
    """

    def test_the_flag_exists_and_defaults_to_refusing(self):
        from replay.__main__ import build_parser

        args = build_parser().parse_args(["--session", "s1"])
        assert args.allow_incomplete_session is False

    def test_the_flag_sets_the_attribute_replay_reads(self):
        from replay.__main__ import build_parser

        args = build_parser().parse_args(
            ["--session", "s1", "--allow-incomplete-session"]
        )
        assert args.allow_incomplete_session is True

    def test_the_cli_catches_every_integrity_refusal(self):
        """Both refusals must reach the ``except`` clause the CLI installs."""
        assert issubclass(IncompleteSessionError, UnverifiedRecordingError)
        assert issubclass(UnverifiedLegacySessionError, UnverifiedRecordingError)


class TestCrashAndRestart:
    """Section 20, through the store rather than by killing a process."""

    async def test_an_open_session_stays_open_across_a_reopen(self, tmp_path):
        path = str(tmp_path / "crash.db")
        store = SQLiteEventStore(path)
        await store.open()
        await store.start_session(SESSION_ID, START_MS)
        await store.append_many(SESSION_ID, EVENTS)
        await store.close()  # process dies here; nothing finalised

        reopened = SQLiteEventStore(path)
        await reopened.open()
        try:
            info = await reopened.session(SESSION_ID)
            assert info.status is SessionStatus.OPEN
            assert not info.is_verified_complete
        finally:
            await reopened.close()

    async def test_a_new_recorder_cannot_reuse_the_crashed_session_id(self, tmp_path):
        from storage import Recorder, SessionAlreadyExists

        path = str(tmp_path / "crash.db")
        store = SQLiteEventStore(path)
        await store.open()
        await store.start_session(SESSION_ID, START_MS)
        await store.close()

        reopened = SQLiteEventStore(path)
        recorder = Recorder(
            store=reopened, clock=ManualClock(START_MS), session_id=SESSION_ID
        )
        with pytest.raises(SessionAlreadyExists):
            await recorder.start()
        await reopened.close()

    async def test_a_new_session_id_does_not_mix_with_the_crashed_one(self, tmp_path):
        from storage import Recorder

        path = str(tmp_path / "crash.db")
        store = SQLiteEventStore(path)
        await store.open()
        await store.start_session(SESSION_ID, START_MS)
        await store.append_many(SESSION_ID, EVENTS)
        await store.close()

        reopened = SQLiteEventStore(path)
        recorder = Recorder(
            store=reopened, clock=ManualClock(START_MS), session_id="fresh-run"
        )
        await recorder.start()
        await recorder.record(_input(9, START_MS + 100))
        await recorder.stop()

        again = SQLiteEventStore(path)
        await again.open()
        try:
            assert await again.count(SESSION_ID) == len(EVENTS)
            assert await again.count("fresh-run") == 1
            assert (await again.session(SESSION_ID)).status is SessionStatus.OPEN
            assert (await again.session("fresh-run")).status is (
                SessionStatus.COMPLETE
            )
        finally:
            await again.close()


async def _legacy_store(tmp_path) -> SQLiteEventStore:
    """A pre-Batch-2 database holding a well-formed, ENDED session."""
    import json
    import sqlite3

    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
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
    )
    conn.execute(
        "INSERT INTO sessions (session_id, started_at, ended_at) VALUES (?, ?, ?)",
        (SESSION_ID, START_MS, START_MS + 10),
    )
    for e in EVENTS:
        conn.execute(
            "INSERT INTO events (session_id, event_id, seq, ts_ms, type, source, "
            "schema_name, correlation_id, payload) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                SESSION_ID,
                e.id,
                e.sequence,
                e.ts_ms,
                e.type.value,
                e.source,
                e.schema_name,
                None,
                json.dumps(e.payload),
            ),
        )
    conn.commit()
    conn.close()
    store = SQLiteEventStore(str(path))
    await store.open()
    return store
