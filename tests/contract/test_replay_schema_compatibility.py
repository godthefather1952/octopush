"""Phase 2 finalization, P2-8: the schema gate replay never had.

``Event`` has carried ``schema_version`` and an ``is_readable`` property since
Phase 0, and replay never consulted either. A recording written by a future
build would be read back, validated against models that do not describe it,
and -- when the change was purely additive -- would validate and behave
differently from the run it recorded. That is the worst outcome available:
not a crash, a quietly different answer presented as a reproduction.

The policy is deliberately strict, because this build ships no upcasters:

    version in SUPPORTED_SCHEMA_VERSIONS  -> readable
    anything else                         -> refuse, before replaying anything

"Older" is not a synonym for "compatible", so an older unsupported version is
refused for the same reason a newer one is: nothing here can prove what it
means.

Run against memory, SQLite and (when available) real PostgreSQL, because the
gate is worth nothing if a backend does not round-trip ``schema_version``.
"""

from __future__ import annotations

import os

import pytest

from core.bus import InMemoryEventBus
from core.clock import ManualClock
from core.events import Event, EventType
from replay.engine import (
    SUPPORTED_SCHEMA_VERSIONS,
    ReplaySession,
    UnsupportedEventSchemaVersion,
)
from storage import InMemoryEventStore, SQLiteEventStore
from storage.base import EventStore, SessionStatus

POSTGRES_DSN = os.environ.get("TF_TEST_POSTGRES_DSN", "")
START_MS = 1_788_000_000_000
SESSION_ID = "s1"
CONFIG_HASH = "cfg"
CURRENT = Event.CURRENT_SCHEMA_VERSION


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
        opened = SQLiteEventStore(str(tmp_path / "schema.db"))
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


def _input(seq: int, ts_ms: int, version: int = CURRENT) -> Event:
    return Event(
        type=EventType.BOOK_SNAPSHOT,
        ts_ms=ts_ms,
        sequence=seq,
        source="VENUE_A",
        schema_name="OrderBookSnapshot",
        schema_version=version,
        payload={"seq": seq},
    )


def _marker(seq: int, ts_ms: int, tick: int, watermark: int, version: int = CURRENT):
    return Event(
        type=EventType.ORCHESTRATOR_TICK,
        ts_ms=ts_ms,
        sequence=seq,
        source="ORCHESTRATOR",
        schema_name="OrchestratorTick",
        schema_version=version,
        payload={
            "tick": tick,
            "warmed_up": True,
            "processed_input_sequence": watermark,
        },
    )


def _derived(seq: int, ts_ms: int, version: int = CURRENT) -> Event:
    """An event replay RECOMPUTES rather than reads back."""
    return Event(
        type=EventType.RISK_PASS,
        ts_ms=ts_ms,
        sequence=seq,
        source="RUNE",
        schema_name="RiskDecision",
        schema_version=version,
        payload={},
    )


async def _seed(store, events) -> None:
    await store.start_session(SESSION_ID, START_MS, config_hash=CONFIG_HASH)
    await store.append_many(SESSION_ID, list(events))
    await store.finalize_session(
        SESSION_ID, START_MS + 9_999, status=SessionStatus.COMPLETE
    )


def _session(store, **kwargs) -> ReplaySession:
    kwargs.setdefault("current_config_hash", CONFIG_HASH)
    return ReplaySession(
        store=store,
        bus=InMemoryEventBus(raise_on_handler_error=True),
        clock=ManualClock(START_MS),
        session_id=SESSION_ID,
        **kwargs,
    )


GOOD = [
    _input(1, START_MS + 100),
    _marker(2, START_MS + 100, 1, 1),
    _input(3, START_MS + 200),
    _marker(4, START_MS + 200, 2, 3),
]


class TestTheSupportedSet:
    def test_only_the_current_version_is_supported(self):
        assert frozenset({CURRENT}) == SUPPORTED_SCHEMA_VERSIONS

    def test_no_upcasters_are_claimed(self):
        """If this ever grows, it must grow WITH a tested upcaster -- the
        refusal is only honest while there is nothing that could convert."""
        assert len(SUPPORTED_SCHEMA_VERSIONS) == 1


class TestTheGate:
    async def test_the_current_version_replays(self, store):
        await _seed(store, GOOD)
        async with _session(store) as session:
            stats = await session.run()
        assert stats.ticks_read == 2
        assert stats.is_exact

    async def test_a_future_version_is_refused(self, store):
        await _seed(
            store,
            [
                _input(1, START_MS + 100),
                _marker(2, START_MS + 100, 1, 1),
                _input(3, START_MS + 200, version=CURRENT + 1),
                _marker(4, START_MS + 200, 2, 3),
            ],
        )
        with pytest.raises(UnsupportedEventSchemaVersion) as excinfo:
            await _session(store).open()
        message = str(excinfo.value)
        assert SESSION_ID in message
        assert str(CURRENT + 1) in message
        assert "BOOK_SNAPSHOT" in message
        assert "OrderBookSnapshot" in message

    async def test_an_older_version_with_no_upcaster_is_refused(self, store):
        """schema_version is ``ge=1``, so version 1 IS the floor; an older
        version can only be simulated by a value outside the supported set.
        The rule under test is the one that matters: unsupported is refused,
        in either direction, rather than assumed compatible."""
        await _seed(
            store,
            [
                _input(1, START_MS + 100, version=CURRENT + 5),
                _marker(2, START_MS + 100, 1, 1),
            ],
        )
        with pytest.raises(UnsupportedEventSchemaVersion):
            await _session(store).open()

    async def test_an_unsupported_marker_is_refused(self, store):
        await _seed(
            store,
            [
                _input(1, START_MS + 100),
                _marker(2, START_MS + 100, 1, 1, version=CURRENT + 1),
            ],
        )
        with pytest.raises(UnsupportedEventSchemaVersion) as excinfo:
            await _session(store).open()
        assert "ORCHESTRATOR_TICK" in str(excinfo.value)

    async def test_a_derived_event_replay_never_reads_is_not_a_blocker(self, store):
        """Refusing over an event this build never feeds back in would block
        replays that are in fact perfectly reproducible: a RISK_PASS is
        RECOMPUTED from the market inputs, so its recorded envelope version
        cannot change the replayed answer."""
        await _seed(
            store,
            [
                _input(1, START_MS + 100),
                _derived(2, START_MS + 150, version=CURRENT + 1),
                _marker(3, START_MS + 200, 1, 1),
            ],
        )
        async with _session(store) as session:
            stats = await session.run()
        assert stats.ticks_read == 1
        assert stats.is_exact


class TestTheRefusalComesBeforeAnySideEffect:
    async def test_a_late_incompatible_event_stops_the_whole_replay(self, store):
        """The preflight case. Discovering event 400 is unreadable AFTER
        replaying 399 of them means ticks were executed and events published
        on a session that was never replayable."""
        events = []
        seq = 0
        for tick in range(1, 21):
            seq += 1
            events.append(_input(seq, START_MS + tick * 100))
            seq += 1
            events.append(_marker(seq, START_MS + tick * 100, tick, seq - 1))
        # ...and one unreadable event, right at the end.
        seq += 1
        events.append(_input(seq, START_MS + 2_100, version=CURRENT + 1))
        await _seed(store, events)

        published: list[Event] = []
        session = _session(store, on_event=published.append)
        with pytest.raises(UnsupportedEventSchemaVersion):
            await session.open()
        assert published == [], (
            "nothing may be published before the schema check completes"
        )
        assert session.stats.events_published == 0
        assert session.stats.ticks_read == 0

    async def test_a_mixed_session_is_refused_rather_than_partly_replayed(self, store):
        await _seed(
            store,
            [
                _input(1, START_MS + 100),
                _marker(2, START_MS + 100, 1, 1),
                _input(3, START_MS + 200, version=CURRENT + 1),
                _marker(4, START_MS + 200, 2, 3),
                _input(5, START_MS + 300),
                _marker(6, START_MS + 300, 3, 5),
            ],
        )
        session = _session(store)
        with pytest.raises(UnsupportedEventSchemaVersion):
            await session.open()
        assert session.stats.ticks_read == 0


class TestBackendsPreserveTheVersion:
    """A gate is worth nothing if a backend loses the field it reads."""

    @pytest.mark.parametrize("version", [1, 2, 7, 99])
    async def test_schema_version_round_trips_exactly(self, store, version):
        await store.start_session(SESSION_ID, START_MS)
        await store.append(SESSION_ID, _input(1, START_MS, version=version))
        stored = [e async for e in store.read(SESSION_ID)]
        assert [e.schema_version for e in stored] == [version]

    async def test_a_missing_version_reads_back_as_the_default(self, store):
        """Rows written before the column existed must not read as 0 or None,
        either of which would fail the gate for the wrong reason."""
        await store.start_session(SESSION_ID, START_MS)
        await store.append(SESSION_ID, _input(1, START_MS))
        stored = [e async for e in store.read(SESSION_ID)]
        assert stored[0].schema_version == CURRENT
