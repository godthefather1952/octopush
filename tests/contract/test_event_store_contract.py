"""One conformance suite every EventStore backend must satisfy — P0-H6, P0-M.

The audit found the PostgreSQL store completely untested: it carried a
``# pragma: no cover - requires a server`` and no test had ever connected to
a server. It is also the only backend whose column types, JSONB validation
and connection pooling can reject data the other two accept, so "the SQLite
tests pass" said nothing about it.

The contract below is written once and run against every backend, so a
divergence between them is a test failure rather than a production surprise.
``postgres`` is skipped — loudly, never silently passed — when no server is
reachable at ``TF_TEST_POSTGRES_DSN``.
"""

from __future__ import annotations

import json
import math
import os

import pytest

from core.events import Event, EventType
from storage import InMemoryEventStore, SQLiteEventStore
from storage.base import EventStore

POSTGRES_DSN = os.environ.get("TF_TEST_POSTGRES_DSN", "")


def postgres_available() -> bool:
    if not POSTGRES_DSN:
        return False
    try:
        import asyncpg  # noqa: F401
    except ImportError:
        return False
    return True


postgres_only = pytest.mark.skipif(
    not postgres_available(),
    reason="set TF_TEST_POSTGRES_DSN to a reachable server to run the PostgreSQL suite",
)


@pytest.fixture(params=["memory", "sqlite", "postgres"])
async def store(request, tmp_path):
    """A freshly opened store of each backend."""
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
        await _truncate(opened)
    try:
        yield opened
    finally:
        await opened.close()


async def _truncate(store) -> None:
    async with store._require().acquire() as conn:
        await conn.execute("TRUNCATE events, sessions")


def make_event(**overrides) -> Event:
    fields = {
        "type": EventType.MARKET_UPDATE,
        "ts_ms": 1_700_000_000_000,
        "source": "TIDAL",
        "schema_name": "Probe",
        "payload": {"value": 1},
    }
    fields.update(overrides)
    return Event(**fields)


async def drain(store, session_id, **kwargs) -> list[Event]:
    return [event async for event in store.read(session_id, **kwargs)]


class TestRoundTrip:
    async def test_an_event_survives_the_round_trip_intact(self, store):
        await store.start_session("s1", 1_700_000_000_000, label="probe", config_hash="abc")
        event = make_event(
            payload={"symbol": "BTC-USD", "price": 50_000.5, "levels": [1, 2, 3]},
            correlation_id="corr-1",
        )
        await store.append("s1", event)

        (read,) = await drain(store, "s1")
        assert read.id == event.id
        assert read.type is EventType.MARKET_UPDATE
        assert read.ts_ms == event.ts_ms
        assert read.source == "TIDAL"
        assert read.schema_name == "Probe"
        assert read.correlation_id == "corr-1"
        assert read.payload == event.payload

    async def test_an_empty_session_reads_back_empty(self, store):
        await store.start_session("s1", 1)
        assert await drain(store, "s1") == []

    async def test_sessions_do_not_leak_into_each_other(self, store):
        await store.start_session("a", 1)
        await store.start_session("b", 1)
        await store.append("a", make_event(payload={"which": "a"}))
        await store.append("b", make_event(payload={"which": "b"}))

        assert [e.payload["which"] for e in await drain(store, "a")] == ["a"]
        assert [e.payload["which"] for e in await drain(store, "b")] == ["b"]

    async def test_append_many_is_equivalent_to_repeated_append(self, store):
        await store.start_session("s1", 1)
        batch = [make_event(ts_ms=1_000 + i, sequence=i, payload={"i": i}) for i in range(20)]
        await store.append_many("s1", batch)

        read = await drain(store, "s1")
        assert [e.payload["i"] for e in read] == list(range(20))

    async def test_an_empty_batch_is_not_an_error(self, store):
        await store.start_session("s1", 1)
        await store.append_many("s1", [])
        assert await drain(store, "s1") == []


class TestOrdering:
    async def test_events_come_back_in_timestamp_order(self, store):
        await store.start_session("s1", 1)
        for ts in (300, 100, 200):
            await store.append("s1", make_event(ts_ms=ts, payload={"ts": ts}))
        assert [e.ts_ms for e in await drain(store, "s1")] == [100, 200, 300]

    async def test_a_shared_timestamp_is_broken_by_sequence(self, store):
        """The case that makes replay reproducible.

        Several events can carry the same millisecond. If the store only
        orders by timestamp, their relative order is whatever the backend
        felt like, and two replays of one session can disagree.
        """
        await store.start_session("s1", 1)
        batch = [make_event(ts_ms=5_000, sequence=i, payload={"i": i}) for i in range(50)]
        await store.append_many("s1", batch)

        for _ in range(3):
            assert [e.payload["i"] for e in await drain(store, "s1")] == list(range(50))

    async def test_ordering_is_total_even_without_sequence_numbers(self, store):
        """Identical ts and identical seq must still be deterministic."""
        await store.start_session("s1", 1)
        batch = [make_event(ts_ms=5_000, sequence=0, payload={"i": i}) for i in range(30)]
        await store.append_many("s1", batch)

        runs = [[e.id for e in await drain(store, "s1")] for _ in range(3)]
        assert runs[0] == runs[1] == runs[2]
        assert sorted(runs[0]) == runs[0], "ties must break on a stable key"


class TestFiltering:
    async def test_reads_can_be_restricted_by_type(self, store):
        await store.start_session("s1", 1)
        await store.append("s1", make_event(type=EventType.MARKET_UPDATE, ts_ms=1))
        await store.append("s1", make_event(type=EventType.PAPER_FILL, ts_ms=2))
        await store.append("s1", make_event(type=EventType.PAPER_FILL, ts_ms=3))

        fills = await drain(store, "s1", types=[EventType.PAPER_FILL])
        assert [e.ts_ms for e in fills] == [2, 3]

    async def test_reads_can_be_restricted_by_time_window(self, store):
        await store.start_session("s1", 1)
        for ts in (100, 200, 300, 400):
            await store.append("s1", make_event(ts_ms=ts))

        window = await drain(store, "s1", start_ms=200, end_ms=300)
        assert [e.ts_ms for e in window] == [200, 300]

    async def test_the_window_is_inclusive_at_both_ends(self, store):
        await store.start_session("s1", 1)
        for ts in (100, 200):
            await store.append("s1", make_event(ts_ms=ts))
        assert len(await drain(store, "s1", start_ms=100, end_ms=200)) == 2


class TestIdempotency:
    async def test_the_same_event_appended_twice_is_stored_once(self, store):
        """At-least-once delivery means the recorder will re-send."""
        await store.start_session("s1", 1)
        event = make_event()
        await store.append("s1", event)
        await store.append("s1", event)

        assert len(await drain(store, "s1")) == 1
        assert await store.count("s1") == 1

    async def test_a_duplicate_inside_a_batch_is_collapsed(self, store):
        await store.start_session("s1", 1)
        event = make_event()
        await store.append_many("s1", [event, event])
        assert await store.count("s1") == 1

    async def test_the_same_id_in_two_sessions_is_two_events(self, store):
        """Identity is scoped to a session, not global."""
        await store.start_session("a", 1)
        await store.start_session("b", 1)
        event = make_event()
        await store.append("a", event)
        await store.append("b", event)
        assert await store.count("a") == 1
        assert await store.count("b") == 1

    async def test_restarting_a_session_does_not_destroy_its_events(self, store):
        await store.start_session("s1", 1, label="first")
        await store.append("s1", make_event())
        await store.start_session("s1", 2, label="second")
        assert await store.count("s1") == 1


class TestSessionMetadata:
    async def test_a_session_reports_its_own_metadata(self, store):
        await store.start_session("s1", 1_700_000_000_000, label="probe", config_hash="deadbeef")
        info = await store.session("s1")
        assert info is not None
        assert info.session_id == "s1"
        assert info.started_at == 1_700_000_000_000
        assert info.label == "probe"
        assert info.config_hash == "deadbeef"
        assert info.ended_at is None

    async def test_ending_a_session_records_the_time(self, store):
        await store.start_session("s1", 1_000)
        await store.end_session("s1", 2_000)
        info = await store.session("s1")
        assert info.ended_at == 2_000

    async def test_the_event_count_is_reported(self, store):
        await store.start_session("s1", 1)
        await store.append_many("s1", [make_event(ts_ms=i, sequence=i) for i in range(7)])
        info = await store.session("s1")
        assert info.event_count == 7

    async def test_an_unknown_session_is_none_not_an_error(self, store):
        assert await store.session("never-existed") is None

    async def test_sessions_are_listed_in_start_order(self, store):
        await store.start_session("second", 2_000)
        await store.start_session("first", 1_000)
        listed = [s.session_id for s in await store.sessions()]
        assert listed.index("first") < listed.index("second")


class TestTypeFidelity:
    """Values must not change meaning on the way through the store."""

    async def test_floats_keep_their_precision(self, store):
        await store.start_session("s1", 1)
        price = 50123.456789012345
        await store.append("s1", make_event(payload={"price": price}))
        (read,) = await drain(store, "s1")
        assert read.payload["price"] == price

    async def test_large_millisecond_timestamps_are_not_truncated(self, store):
        """A ms epoch does not fit in 32 bits; an INTEGER column would wrap."""
        far_future = 4_102_444_800_000  # 2100-01-01
        await store.start_session("s1", 1)
        await store.append("s1", make_event(ts_ms=far_future))
        (read,) = await drain(store, "s1")
        assert read.ts_ms == far_future

    async def test_enums_come_back_as_enums(self, store):
        await store.start_session("s1", 1)
        await store.append("s1", make_event(type=EventType.RECONCILIATION_MISMATCH))
        (read,) = await drain(store, "s1")
        assert read.type is EventType.RECONCILIATION_MISMATCH

    async def test_nested_payloads_survive(self, store):
        payload = {
            "book": {"bids": [[100.0, 1.5], [99.5, 2.0]], "asks": []},
            "flags": {"stale": False, "source": None},
            "count": 3,
        }
        await store.start_session("s1", 1)
        await store.append("s1", make_event(payload=payload))
        (read,) = await drain(store, "s1")
        assert read.payload == payload

    async def test_unicode_survives(self, store):
        await store.start_session("s1", 1)
        await store.append("s1", make_event(payload={"note": "état — 日本語 — 🚀"}))
        (read,) = await drain(store, "s1")
        assert read.payload["note"] == "état — 日本語 — 🚀"

    async def test_an_empty_payload_is_preserved(self, store):
        await store.start_session("s1", 1)
        await store.append("s1", make_event(payload={}))
        (read,) = await drain(store, "s1")
        assert read.payload == {}

    async def test_a_real_sequence_survives(self, store):
        await store.start_session("s1", 1)
        await store.append("s1", make_event(sequence=0, payload={"i": 0}))
        (read,) = await drain(store, "s1")
        assert read.sequence == 0

    async def test_an_unsequenced_event_does_not_come_back_claiming_zero(self, store):
        """``int(event.sequence or 0)`` conflated "never sequenced" with "first".

        The bus assigns a sequence before middleware runs, so on the recording
        path this never fired. A direct append — replay tooling, a repair
        script — stored None and read back 0, which is not what went in and
        which sorts ahead of every genuinely-sequenced event sharing its
        millisecond.
        """
        await store.start_session("s1", 1)
        await store.append("s1", make_event(sequence=None))
        (read,) = await drain(store, "s1")
        assert read.sequence is None

    async def test_unsequenced_events_sort_where_sort_key_says_they_do(self, store):
        """Ordering must agree with Event.sort_key on every backend.

        SQL engines disagree about where NULLs sort by default, so leaving it
        to the backend would make the same session read back in two different
        orders depending on where it was stored.
        """
        await store.start_session("s1", 1)
        events = [
            make_event(ts_ms=100, sequence=None, payload={"tag": "unsequenced"}),
            make_event(ts_ms=100, sequence=5, payload={"tag": "five"}),
            make_event(ts_ms=100, sequence=1, payload={"tag": "one"}),
        ]
        await store.append_many("s1", events)

        read = await drain(store, "s1")
        expected = sorted(events, key=lambda e: e.sort_key())
        assert [e.payload["tag"] for e in read] == [e.payload["tag"] for e in expected]


class TestNonFiniteNumbersAreRejected:
    """RFC 8259 has no Infinity or NaN, and JSONB refuses them — P0-C3."""

    @pytest.mark.parametrize("bad", [float("inf"), float("-inf"), float("nan")])
    async def test_a_non_finite_value_never_reaches_storage(self, store, bad):
        await store.start_session("s1", 1)
        with pytest.raises(Exception):  # noqa: B017 - backends raise different types
            await store.append("s1", make_event(payload={"impact_bps": bad}))

    async def test_a_sanitised_payload_is_accepted(self, store):
        """What the fix actually produces: null, not a bare Infinity token."""
        from core.models.common import sanitize_json

        payload = sanitize_json({"impact_bps": math.inf, "edge": 1.5})
        assert payload == {"impact_bps": None, "edge": 1.5}
        await store.start_session("s1", 1)
        await store.append("s1", make_event(payload=payload))
        (read,) = await drain(store, "s1")
        assert read.payload["impact_bps"] is None

    async def test_stored_payloads_are_strict_json(self, store):
        """Whatever the store holds must parse under a strict reader."""
        await store.start_session("s1", 1)
        await store.append("s1", make_event(payload={"a": 1.0, "b": [None, True]}))
        (read,) = await drain(store, "s1")

        def reject(token):  # pragma: no cover - only runs on a violation
            raise AssertionError(f"non-JSON constant {token} came out of the store")

        assert json.loads(json.dumps(read.payload), parse_constant=reject) == read.payload


@postgres_only
class TestPostgresSpecifics:
    """Behaviour only a real server can demonstrate."""

    @pytest.fixture
    async def pg(self):
        from storage.postgres_store import PostgresEventStore

        store = PostgresEventStore(POSTGRES_DSN)
        await store.open()
        await _truncate(store)
        try:
            yield store
        finally:
            await store.close()

    async def test_opening_twice_is_idempotent(self, pg):
        await pg.open()
        await pg.start_session("s1", 1)
        assert await pg.count("s1") == 0

    async def test_using_a_closed_store_is_a_clear_error(self, pg):
        await pg.close()
        with pytest.raises(RuntimeError, match="not open"):
            await pg.count("s1")

    async def test_data_survives_a_reconnect(self, pg):
        """The point of the backend: it outlives the process.

        A new store object, a new pool, the same server.
        """
        from storage.postgres_store import PostgresEventStore

        await pg.start_session("durable", 1_700_000_000_000, label="kept")
        await pg.append_many(
            "durable", [make_event(ts_ms=1_000 + i, sequence=i, payload={"i": i}) for i in range(5)]
        )
        await pg.close()

        reopened = PostgresEventStore(POSTGRES_DSN)
        await reopened.open()
        try:
            assert await reopened.count("durable") == 5
            read = [e async for e in reopened.read("durable")]
            assert [e.payload["i"] for e in read] == [0, 1, 2, 3, 4]
            info = await reopened.session("durable")
            assert info.label == "kept"
        finally:
            await reopened.close()

    async def test_the_payload_column_is_queryable_jsonb_not_text(self, pg):
        """If it were TEXT the schema would still work but be useless."""
        await pg.start_session("s1", 1)
        await pg.append("s1", make_event(payload={"symbol": "BTC-USD", "price": 50_000.0}))
        async with pg._require().acquire() as conn:
            data_type = await conn.fetchval(
                "SELECT data_type FROM information_schema.columns "
                "WHERE table_name = 'events' AND column_name = 'payload'"
            )
            assert data_type == "jsonb"
            found = await conn.fetchval(
                "SELECT COUNT(*) FROM events WHERE payload->>'symbol' = 'BTC-USD'"
            )
            assert found == 1

    async def test_timestamps_are_stored_as_bigint(self, pg):
        async with pg._require().acquire() as conn:
            for column in ("ts_ms", "seq"):
                data_type = await conn.fetchval(
                    "SELECT data_type FROM information_schema.columns "
                    "WHERE table_name = 'events' AND column_name = $1",
                    column,
                )
                assert data_type == "bigint", f"{column} is {data_type}, which will wrap"

    async def test_the_server_itself_refuses_a_non_finite_number(self, pg):
        """Belt and braces: even if the sanitiser were removed."""
        import asyncpg

        await pg.start_session("s1", 1)
        async with pg._require().acquire() as conn:
            with pytest.raises(asyncpg.PostgresError):
                await conn.execute(
                    "INSERT INTO events (session_id, event_id, seq, ts_ms, type, source, "
                    "schema_name, correlation_id, payload) "
                    "VALUES ('s1', 'e1', 0, 1, 'MARKET_UPDATE', 'T', 'S', NULL, $1::jsonb)",
                    '{"impact_bps": Infinity}',
                )

    async def test_a_large_batch_inserts_atomically_and_in_order(self, pg):
        await pg.start_session("s1", 1)
        batch = [make_event(ts_ms=1_000 + i, sequence=i, payload={"i": i}) for i in range(2_000)]
        await pg.append_many("s1", batch)
        assert await pg.count("s1") == 2_000
        read = [e async for e in pg.read("s1")]
        assert [e.payload["i"] for e in read] == list(range(2_000))

    async def test_concurrent_writers_do_not_lose_events(self, pg):
        """The pool is shared; appends from several tasks must all land."""
        import asyncio

        await pg.start_session("s1", 1)

        async def writer(offset: int):
            await pg.append_many(
                "s1",
                [
                    make_event(ts_ms=offset * 100 + i, sequence=i, payload={"w": offset, "i": i})
                    for i in range(50)
                ],
            )

        await asyncio.gather(*(writer(w) for w in range(8)))
        assert await pg.count("s1") == 400
