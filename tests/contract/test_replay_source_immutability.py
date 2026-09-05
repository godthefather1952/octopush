"""Phase 2 finalization, §40: replaying a session never changes it.

A recording is evidence. Replay reads it, reconstructs a fresh platform from
it, and -- with ``--record-output`` -- writes a NEW session beside it. What it
must never do is write back into the source, and that has to be proved rather
than assumed, because several things in this design write to storage while a
replay runs: the output recorder, the replay platform's own event stream, and
(on a durable backend) both of those sharing one database with the source.

A count is not a proof on a durable backend. An event could be replaced in
place, a timestamp rewritten, a session's status or label overwritten, and the
count would not move. So every case below fingerprints the source completely
-- every event id, sequence, timestamp, type, source, schema version and
payload digest, plus all session metadata -- before and after.
"""

from __future__ import annotations

import hashlib
import json
import os

import pytest

from apps.orchestrator.wiring import build_platform
from core.bus import InMemoryEventBus
from core.clock import ManualClock
from core.config import simulated_venues
from core.events import EventType
from replay.engine import ReplayMode, ReplaySession, config_digest
from simulation.market import default_market
from storage import InMemoryEventStore, SQLiteEventStore
from storage.base import EventStore, SessionStatus
from tests.conftest import START_MS
from tests.replay.test_replay import fresh_platform

POSTGRES_DSN = os.environ.get("TF_TEST_POSTGRES_DSN", "")
TICKS = 30


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
        opened = SQLiteEventStore(str(tmp_path / "immutable.db"))
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


async def fingerprint(store, session_id: str) -> dict:
    """Everything about a session a replay must leave untouched."""
    events = [e async for e in store.read(session_id)]
    info = await store.session(session_id)
    return {
        "metadata": None
        if info is None
        else (
            info.session_id,
            info.started_at,
            info.ended_at,
            info.status.value,
            info.event_count,
            info.config_hash,
            info.label,
            info.events_lost,
            info.failure_reason,
        ),
        "events": [
            (
                e.id,
                e.sequence,
                e.ts_ms,
                e.type.value,
                e.source,
                e.schema_name,
                e.schema_version,
                e.correlation_id,
                e.causation_id,
                hashlib.sha256(
                    json.dumps(e.payload, sort_keys=True, default=str).encode()
                ).hexdigest(),
            )
            for e in events
        ],
    }


async def record(settings, store):
    platform = fresh_platform(settings, store=store)
    await platform.start(record=True)
    for _ in range(TICKS):
        platform.clock.advance(100)
        await platform.step_market(1)
        await platform.orchestrator.tick()
    await platform.bus.drain()
    session_id = platform.session_id
    # stop() finalises the session AND closes the store; reopen it, since the
    # fixture hands the same object to the replay and to the fingerprinting.
    await platform.recorder.stop()
    await store.open()
    return session_id


async def replay_once(settings, store, session_id, *, output_store=None, **kwargs):
    """Replay ``session_id``, optionally recording output into ``output_store``.

    ``output_store`` deliberately may BE the source store: on a durable
    backend that is the default arrangement, and it is precisely the case
    where a scoping mistake would write derived events into the source.
    """
    limit = kwargs.pop("_max_items", None)
    replay_settings = settings.model_copy(update={"venues": simulated_venues()})
    clock = ManualClock(START_MS)
    bus = InMemoryEventBus(raise_on_handler_error=True)
    platform = build_platform(
        replay_settings,
        clock=clock,
        bus=bus,
        store=output_store or InMemoryEventStore(),
        market=default_market(start_ms=START_MS),
        raise_on_handler_error=True,
        session_label=f"replay-of-{session_id}",
    )
    kwargs.setdefault(
        "current_config_hash", config_digest(replay_settings.model_dump())
    )
    session = ReplaySession(
        store=store,
        bus=bus,
        clock=clock,
        session_id=session_id,
        **kwargs,
    )
    async with session:
        await platform.start(record=output_store is not None, feeds=False)
        items = 0
        while limit is None or items < limit:
            event = await session.step()
            if event is None:
                break
            items += 1
            if event.type is EventType.ORCHESTRATOR_TICK:
                await platform.orchestrator.tick()
    output_session = platform.session_id
    await platform.stop()
    # platform.stop() closes whatever store it was recording into -- which on
    # a durable backend is the SOURCE database, shared by design.
    await store.open()
    return output_session


@pytest.fixture
def settings(settings):
    """Narrows the shared fixture to the deterministic offline venue set."""
    return settings.model_copy(update={"venues": simulated_venues()})


class TestTheSourceSurvivesEveryKindOfReplay:
    async def test_a_normal_replay_changes_nothing(self, settings, store):
        session_id = await record(settings, store)
        before = await fingerprint(store, session_id)
        await replay_once(settings, store, session_id)
        assert await fingerprint(store, session_id) == before

    async def test_a_counterfactual_replay_changes_nothing(self, settings, store):
        session_id = await record(settings, store)
        before = await fingerprint(store, session_id)
        await replay_once(
            settings,
            store,
            session_id,
            current_config_hash="cfg-different",
            allow_config_mismatch=True,
        )
        assert await fingerprint(store, session_id) == before

    async def test_a_realtime_replay_changes_nothing(self, settings, store):
        async def instant(seconds: float) -> None:
            return None

        session_id = await record(settings, store)
        before = await fingerprint(store, session_id)
        await replay_once(
            settings,
            store,
            session_id,
            mode=ReplayMode.REALTIME,
            speed=1.0,
            host_sleep=instant,
        )
        assert await fingerprint(store, session_id) == before

    async def test_an_early_stop_changes_nothing(self, settings, store):
        session_id = await record(settings, store)
        before = await fingerprint(store, session_id)
        await replay_once(settings, store, session_id, _max_items=5)
        assert await fingerprint(store, session_id) == before

    async def test_a_replay_that_raises_midway_changes_nothing(
        self, settings, store
    ):
        session_id = await record(settings, store)
        before = await fingerprint(store, session_id)

        class Boom(RuntimeError):
            pass

        clock = ManualClock(START_MS)
        session = ReplaySession(
            store=store,
            bus=InMemoryEventBus(raise_on_handler_error=True),
            clock=clock,
            session_id=session_id,
            current_config_hash=config_digest(
                settings.model_copy(
                    update={"venues": simulated_venues()}
                ).model_dump()
            ),
        )
        with pytest.raises(Boom):
            async with session:
                await session.step()
                raise Boom("something downstream failed")

        assert await fingerprint(store, session_id) == before


class TestRecordingOutputIntoTheSameDatabase:
    """The arrangement the CLI actually uses on a durable backend."""

    async def test_the_output_lands_beside_the_source_not_inside_it(
        self, settings, store
    ):
        session_id = await record(settings, store)
        before = await fingerprint(store, session_id)

        output_id = await replay_once(
            settings, store, session_id, output_store=store
        )

        assert output_id != session_id
        assert await fingerprint(store, session_id) == before, (
            "the replay wrote a new session into the SAME database and the "
            "source is byte-identical"
        )

        output = await store.session(output_id)
        assert output is not None
        assert output.status is SessionStatus.COMPLETE
        assert output.event_count > 0
        assert output.label == f"replay-of-{session_id}"

    async def test_the_source_read_stays_scoped_to_the_source(
        self, settings, store
    ):
        """Both sessions live in one table; the read must be scoped to one.

        Note what is NOT asserted: that the two sessions hold disjoint event
        ids. They deliberately overlap. A replayed market input is republished
        with its RECORDED id preserved (see ``_apply_and_queue``), so that two
        replays of one session can be diffed entity by entity -- and the
        output recorder then stores it under that same id. Storage keys on
        (session_id, event_id), so this is two rows, not one. The scoping
        property is that reading one session never yields the other's rows.
        """
        session_id = await record(settings, store)
        source_ids = [e.id async for e in store.read(session_id)]
        source_count = await store.count(session_id)

        output_id = await replay_once(
            settings, store, session_id, output_store=store
        )
        output_ids = [e.id async for e in store.read(output_id)]

        assert output_ids, "the output session actually recorded something"
        assert [e.id async for e in store.read(session_id)] == source_ids, (
            "reading the source must yield exactly the source's rows"
        )
        assert await store.count(session_id) == source_count
        assert set(output_ids) - set(source_ids), (
            "...and the output holds events of its own -- the derived ones "
            "the replay recomputed -- so it is genuinely a separate session"
        )

    async def test_replaying_twice_into_one_database_is_stable(
        self, settings, store
    ):
        """The source must be identical after the second replay too -- and the
        second replay must not have read the first one's output."""
        session_id = await record(settings, store)
        before = await fingerprint(store, session_id)

        first = await replay_once(settings, store, session_id, output_store=store)
        second = await replay_once(settings, store, session_id, output_store=store)

        assert first != second
        assert await fingerprint(store, session_id) == before
        assert await store.count(first) == await store.count(second), (
            "the second replay saw exactly what the first did -- if it had "
            "read the first one's output, it would have seen more"
        )
