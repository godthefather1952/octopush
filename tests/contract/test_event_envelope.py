"""The bus envelope must carry version and causation — P0-M2.

The audit found two gaps in ``Event``:

* ``schema_name`` is a bare string with no version, so a payload shape change
  silently breaks replay of older recordings. The replay reads an event,
  validates it against today's model, and either fails with a confusing
  validation error or — worse — succeeds because the change was additive and
  produces different behaviour from the recorded run.

* There is only ``correlation_id``, which groups every event belonging to one
  opportunity but says nothing about what caused what. The grouping answers
  "which events belong together"; it cannot answer "what did this event come
  from", which is the question attribution and post-mortems actually ask.
"""

from __future__ import annotations

import pytest

from core.events import Event, EventType

START_MS = 1_700_000_000_000


def make_event(**overrides) -> Event:
    fields = {
        "type": EventType.SYSTEM_EVENT,
        "ts_ms": START_MS,
        "source": "TEST",
        "payload": {},
    }
    fields.update(overrides)
    return Event(**fields)


class TestSchemaVersion:
    def test_every_event_carries_a_schema_version(self):
        assert make_event().schema_version == Event.CURRENT_SCHEMA_VERSION

    def test_the_version_is_a_positive_integer(self):
        assert isinstance(Event.CURRENT_SCHEMA_VERSION, int)
        assert Event.CURRENT_SCHEMA_VERSION >= 1
        with pytest.raises(ValueError):
            make_event(schema_version=0)
        with pytest.raises(ValueError):
            make_event(schema_version=-1)

    def test_an_older_version_is_readable(self):
        """Recordings predating a bump must still load, not explode."""
        old = make_event(schema_version=1)
        assert old.schema_version == 1

    def test_the_version_survives_a_round_trip(self):
        import json

        event = make_event(schema_version=1)
        restored = Event.model_validate(json.loads(event.model_dump_json()))
        assert restored.schema_version == 1

    def test_an_event_knows_whether_this_build_can_read_it(self):
        """The check replay needs before trusting a recorded payload."""
        assert make_event(schema_version=Event.CURRENT_SCHEMA_VERSION).is_readable
        assert make_event(schema_version=1).is_readable
        assert not make_event(schema_version=Event.CURRENT_SCHEMA_VERSION + 1).is_readable

    def test_a_future_version_is_reported_not_silently_accepted(self):
        """Reading an event written by a newer build is not a warning.

        Silently proceeding means validating a payload against a model that
        does not describe it — which either errors somewhere confusing or,
        if the change was additive, quietly changes behaviour.
        """
        future = make_event(schema_version=Event.CURRENT_SCHEMA_VERSION + 5)
        assert not future.is_readable


class TestCausation:
    def test_an_event_can_name_what_caused_it(self):
        cause = make_event()
        effect = make_event(causation_id=cause.id)
        assert effect.causation_id == cause.id

    def test_causation_defaults_to_absent(self):
        """A root event was caused by nothing, and must not claim otherwise."""
        assert make_event().causation_id is None

    def test_causation_and_correlation_are_different_questions(self):
        """Correlation groups; causation orders within the group."""
        root = make_event(correlation_id="opp-1")
        middle = make_event(correlation_id="opp-1", causation_id=root.id)
        leaf = make_event(correlation_id="opp-1", causation_id=middle.id)

        assert {e.correlation_id for e in (root, middle, leaf)} == {"opp-1"}
        assert leaf.causation_id != root.id, "causation must be the direct parent"

    def test_a_causal_chain_can_be_walked_back_to_its_root(self):
        chain = [make_event(correlation_id="opp-1")]
        for _ in range(5):
            chain.append(make_event(correlation_id="opp-1", causation_id=chain[-1].id))

        by_id = {event.id: event for event in chain}
        walked, cursor = [], chain[-1]
        while cursor is not None:
            walked.append(cursor.id)
            cursor = by_id.get(cursor.causation_id) if cursor.causation_id else None

        assert walked == [event.id for event in reversed(chain)]

    def test_caused_by_builds_the_link(self):
        """A helper, so callers do not have to remember to copy correlation."""
        cause = make_event(correlation_id="opp-1")
        effect = Event.caused_by(
            cause, type=EventType.TRADE_INTENT, ts_ms=START_MS + 1, source="ORCH"
        )
        assert effect.causation_id == cause.id
        assert effect.correlation_id == "opp-1", "correlation must be inherited"
        assert effect.id != cause.id

    def test_caused_by_does_not_overwrite_an_explicit_correlation(self):
        cause = make_event(correlation_id="opp-1")
        effect = Event.caused_by(
            cause,
            type=EventType.TRADE_INTENT,
            ts_ms=START_MS + 1,
            source="ORCH",
            correlation_id="opp-2",
        )
        assert effect.correlation_id == "opp-2"


class TestPersistence:
    """Both fields must survive the store, or they are decoration."""

    @pytest.fixture
    async def store(self, tmp_path):
        from storage import SQLiteEventStore

        opened = SQLiteEventStore(str(tmp_path / "events.db"))
        await opened.open()
        try:
            yield opened
        finally:
            await opened.close()

    async def test_version_and_causation_round_trip_through_storage(self, store):
        await store.start_session("s1", START_MS)
        cause = make_event(sequence=0)
        effect = make_event(sequence=1, causation_id=cause.id, correlation_id="opp-1")
        await store.append_many("s1", [cause, effect])

        read = [event async for event in store.read("s1")]
        assert [e.schema_version for e in read] == [Event.CURRENT_SCHEMA_VERSION] * 2
        assert read[0].causation_id is None
        assert read[1].causation_id == cause.id

    async def test_a_recorded_causal_chain_is_reconstructable(self, store):
        await store.start_session("s1", START_MS)
        chain = [make_event(sequence=0, correlation_id="opp-1")]
        for i in range(1, 6):
            chain.append(
                make_event(sequence=i, correlation_id="opp-1", causation_id=chain[-1].id)
            )
        await store.append_many("s1", chain)

        read = [event async for event in store.read("s1")]
        by_id = {event.id: event for event in read}
        leaf = read[-1]
        depth = 0
        cursor = leaf
        while cursor.causation_id:
            cursor = by_id[cursor.causation_id]
            depth += 1
        assert depth == 5
        assert cursor.causation_id is None
