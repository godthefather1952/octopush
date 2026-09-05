"""Phase 2 Batch 1.2 Section 10: every tick marker's watermark must be
validated, not only the first one.

``ReplaySession.open()`` decides ``_input_visibility_verified`` by peeking at
the FIRST ``ORCHESTRATOR_TICK`` marker in range alone. Before this batch,
that was also the LAST time any marker's watermark was checked for sanity:
``_pump_one()`` read ``_watermark_of(event)`` fresh for every later marker,
but a missing/non-int watermark just silently released nothing for that
marker (no error), a decreasing watermark was never checked at all, and a
watermark claiming to have applied input sequences that were never even
recorded was never checked either. A session with marker 1 valid, marker 2
valid, marker 3 broken, marker 4 valid would replay all the way through,
"certified" by marker 1's validity alone, silently mishandling marker 3.

These tests build a session directly against an ``InMemoryEventStore`` (no
platform, no agents) so the marker sequence/timestamp/watermark values are
exact and independent of anything else in the pipeline.
"""

from __future__ import annotations

import pytest

from core.bus import InMemoryEventBus
from core.clock import ManualClock
from core.events import Event, EventType
from replay.engine import InvalidTickMarkerWatermark, LegacyInputVisibilityRequired, ReplaySession
from storage.base import SessionStatus
from storage.memory import InMemoryEventStore

START_MS = 1_788_000_000_000
#: These sessions are hand-built, so they carry a digest of their own and
#: the replay is given the matching one -- otherwise every replay here
#: would be reported non-exact for a configuration nobody changed.
CONFIG_HASH = "test-config"
SESSION_ID = "s1"


def _input(seq: int, ts_ms: int) -> Event:
    return Event(
        type=EventType.BOOK_SNAPSHOT,
        ts_ms=ts_ms,
        sequence=seq,
        source="VENUE_A",
        schema_name="Test",
        payload={},
    )


_OMIT = object()


def _marker(seq: int, ts_ms: int, tick: int, watermark: object) -> Event:
    payload: dict[str, object] = {"tick": tick, "warmed_up": True}
    if watermark is not _OMIT:
        payload["processed_input_sequence"] = watermark
    return Event(
        type=EventType.ORCHESTRATOR_TICK,
        ts_ms=ts_ms,
        sequence=seq,
        source="ORCHESTRATOR",
        schema_name="OrchestratorTick",
        payload=payload,
    )


async def _build_session(events: list[Event]) -> InMemoryEventStore:
    store = InMemoryEventStore()
    await store.open()
    await store.start_session(SESSION_ID, events[0].ts_ms, config_hash=CONFIG_HASH)
    await store.append_many(SESSION_ID, events)
    # Exact replay requires a verified-complete recording (Phase 2 Batch 2).
    # These sessions are hand-built and complete by construction, so they say
    # so explicitly rather than relying on replay to assume it.
    await store.finalize_session(
        SESSION_ID, events[-1].ts_ms, status=SessionStatus.COMPLETE
    )
    return store


def _session(store: InMemoryEventStore) -> ReplaySession:
    return ReplaySession(
        store=store,
        bus=InMemoryEventBus(raise_on_handler_error=True),
        clock=ManualClock(START_MS),
        session_id=SESSION_ID,
        current_config_hash=CONFIG_HASH,
    )


class TestEveryMarkerIsValidated:
    async def test_a_all_markers_valid_replays_cleanly(self):
        events = [
            _input(1, START_MS),
            _marker(2, START_MS + 1, 1, 1),
            _input(3, START_MS + 2),
            _marker(4, START_MS + 3, 2, 3),
            _input(5, START_MS + 4),
            _marker(6, START_MS + 5, 3, 5),
        ]
        store = await _build_session(events)
        session = _session(store)
        with session:
            await session.open()
            assert session.stats.timeline_fidelity is None
            stats = await session.run()
        assert stats.ticks_read == 3
        assert stats.events_published == 3

    async def test_b_first_marker_invalid_raises_at_open(self):
        """Already-existing behaviour (LegacyInputVisibilityRequired), kept
        here so the full required matrix (Section 10) lives in one file.
        """
        events = [
            _input(1, START_MS),
            _marker(2, START_MS + 1, 1, _OMIT),
            _input(3, START_MS + 2),
            _marker(4, START_MS + 3, 2, 3),
        ]
        store = await _build_session(events)
        session = _session(store)
        with pytest.raises(LegacyInputVisibilityRequired):
            await session.open()

    async def test_c_middle_marker_missing_watermark_fails_loudly_at_that_marker(self):
        events = [
            _input(1, START_MS),
            _marker(2, START_MS + 1, 1, 1),
            _input(3, START_MS + 2),
            _marker(4, START_MS + 3, 2, _OMIT),  # broken -- not the first
            _input(5, START_MS + 4),
            _marker(6, START_MS + 5, 3, 5),
        ]
        store = await _build_session(events)
        session = _session(store)
        with session:
            await session.open()
            assert session.stats.timeline_fidelity is None, (
                "the first marker alone is valid -- open() must not be "
                "fooled into certifying the whole range"
            )
            with pytest.raises(InvalidTickMarkerWatermark):
                await session.run()
            # It must fail AT marker 2 (tick=2), not merely "eventually" or
            # only at the end of the stream.
            assert session.stats.ticks_read == 1

    async def test_d_final_marker_missing_watermark_fails_loudly_at_that_marker(self):
        events = [
            _input(1, START_MS),
            _marker(2, START_MS + 1, 1, 1),
            _input(3, START_MS + 2),
            _marker(4, START_MS + 3, 2, 3),
            _input(5, START_MS + 4),
            _marker(6, START_MS + 5, 3, _OMIT),  # broken -- the last one
        ]
        store = await _build_session(events)
        session = _session(store)
        with session:
            await session.open()
            with pytest.raises(InvalidTickMarkerWatermark):
                await session.run()
            assert session.stats.ticks_read == 2

    async def test_e_non_int_watermark_on_a_later_marker_fails_loudly(self):
        events = [
            _input(1, START_MS),
            _marker(2, START_MS + 1, 1, 1),
            _input(3, START_MS + 2),
            _marker(4, START_MS + 3, 2, "not-a-number"),
            _input(5, START_MS + 4),
            _marker(6, START_MS + 5, 3, 5),
        ]
        store = await _build_session(events)
        session = _session(store)
        with session:
            await session.open()
            with pytest.raises(InvalidTickMarkerWatermark):
                await session.run()
            assert session.stats.ticks_read == 1

    async def test_f_decreasing_watermark_fails_loudly(self):
        events = [
            _input(1, START_MS),
            _marker(2, START_MS + 1, 1, 3),
            _input(3, START_MS + 2),
            _marker(4, START_MS + 3, 2, 1),  # lower than tick 1's watermark
        ]
        store = await _build_session(events)
        session = _session(store)
        with session:
            await session.open()
            with pytest.raises(InvalidTickMarkerWatermark):
                await session.run()
            assert session.stats.ticks_read == 1

    async def test_g_watermark_above_any_recorded_sequence_fails_loudly(self):
        """A watermark this session's own range never made possible: no
        event anywhere in the range carries a sequence anywhere near it.
        Detectable soundly (Section 10's "if detectable") via the full,
        unfiltered range scan ``open()`` performs once -- not by comparing
        against only what has been read so far, which cross-venue clock
        skew could make an unreliable, false-positive-prone bound.
        """
        events = [
            _input(1, START_MS),
            _marker(2, START_MS + 1, 1, 1),
            _input(3, START_MS + 2),
            _marker(4, START_MS + 3, 2, 999),  # impossible: max seq in range is 4
        ]
        store = await _build_session(events)
        session = _session(store)
        with session:
            await session.open()
            assert session._max_possible_sequence == 4
            with pytest.raises(InvalidTickMarkerWatermark):
                await session.run()
            assert session.stats.ticks_read == 1
