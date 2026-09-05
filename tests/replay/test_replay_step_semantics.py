"""Phase 2 finalization, P2-10: what "one step" and "N items" mean.

Two problems met here.

**A mode that was not a mode.** ``ReplayMode.STEP`` existed and did nothing:
``run()`` never behaved differently under it, and the CLI's stepping was a
hand-written loop calling ``step()`` directly, with the mode set to STEP
purely as decoration. A mode that changes no behaviour is worse than no mode,
because it reads as though something is configured. ReplayMode is now FAST and
REALTIME -- how fast the HOST runs -- and stepping is what ``step()`` does.

**Two counters that looked synonymous.** The CLI had ``--step N`` and
``--max-events N``, checked in the same loop, both stopping the replay -- but
``--step`` counted only market events while tick markers slipped past
uncounted. So "--step 10" meant a different amount of replay depending on how
much market data happened to sit between ticks. There is now one limit,
``--max-items``, over one defined unit: a logical replay item, which is one
applied input OR one ORCHESTRATOR_TICK boundary.
"""

from __future__ import annotations

import pytest

from core.bus import InMemoryEventBus
from core.clock import ManualClock
from core.events import Event, EventType
from replay.engine import ReplayMode, ReplaySession
from storage import InMemoryEventStore
from storage.base import SessionStatus

START_MS = 1_788_000_000_000
SESSION_ID = "s1"
CONFIG_HASH = "cfg"


def _input(seq: int, ts_ms: int) -> Event:
    return Event(
        type=EventType.BOOK_SNAPSHOT,
        ts_ms=ts_ms,
        sequence=seq,
        source="VENUE_A",
        schema_name="Test",
        payload={"seq": seq},
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


#: Alternating input/marker: six logical items, three of them ticks.
EVENTS = [
    _input(1, START_MS + 100),
    _marker(2, START_MS + 100, 1, 1),
    _input(3, START_MS + 200),
    _marker(4, START_MS + 200, 2, 3),
    _input(5, START_MS + 300),
    _marker(6, START_MS + 300, 3, 5),
]


async def _store(events=EVENTS) -> InMemoryEventStore:
    store = InMemoryEventStore()
    await store.open()
    await store.start_session(SESSION_ID, START_MS, config_hash=CONFIG_HASH)
    await store.append_many(SESSION_ID, list(events))
    await store.finalize_session(
        SESSION_ID, START_MS + 9_999, status=SessionStatus.COMPLETE
    )
    return store


def _session(store, **kwargs) -> ReplaySession:
    kwargs.setdefault("current_config_hash", CONFIG_HASH)
    return ReplaySession(
        store=store,
        bus=InMemoryEventBus(raise_on_handler_error=True),
        clock=ManualClock(START_MS),
        session_id=SESSION_ID,
        **kwargs,
    )


class TestReplayModeHasOnlyRealModes:
    def test_step_is_gone(self):
        assert not hasattr(ReplayMode, "STEP"), (
            "a mode that changed no behaviour read as though something was "
            "configured when nothing was"
        )

    def test_the_remaining_modes_are_host_pacing_only(self):
        assert {m.value for m in ReplayMode} == {"FAST", "REALTIME"}


class TestOneLogicalItem:
    async def test_step_returns_inputs_and_markers_alike(self):
        store = await _store()
        seen = []
        async with _session(store) as session:
            while (event := await session.step()) is not None:
                seen.append(event.type)
        assert seen == [
            EventType.BOOK_SNAPSHOT,
            EventType.ORCHESTRATOR_TICK,
            EventType.BOOK_SNAPSHOT,
            EventType.ORCHESTRATOR_TICK,
            EventType.BOOK_SNAPSHOT,
            EventType.ORCHESTRATOR_TICK,
        ]

    async def test_step_returns_none_exactly_once_at_the_end(self):
        store = await _store()
        async with _session(store) as session:
            for _ in range(len(EVENTS)):
                assert await session.step() is not None
            assert await session.step() is None
            assert await session.step() is None, "exhaustion is stable"
            assert session.finished


class TestMaxItemsCountsEverything:
    """Ticks count. A limit that skipped them would mean different amounts of
    replay depending on how much market data sat between ticks."""

    @pytest.mark.parametrize(
        ("limit", "expected_returned_ticks"),
        [
            (1, 0),  # the first input only
            (2, 1),  # ...and the marker after it
            (3, 1),
            (4, 2),
            (6, 3),  # the whole session
        ],
    )
    async def test_a_limit_stops_after_exactly_n_items(
        self, limit, expected_returned_ticks
    ):
        """Counted on what ``step()`` HANDS BACK, which is the unit the flag
        names -- not ``stats.ticks_read``, which counts markers READ. Those
        differ on purpose: a marker is read (and its watermark applied) before
        the inputs it releases surface ahead of it.
        """
        store = await _store()
        returned: list[Event] = []
        async with _session(store) as session:
            while len(returned) < limit:
                event = await session.step()
                if event is None:
                    break
                returned.append(event)
        assert len(returned) == limit
        ticks = sum(1 for e in returned if e.type is EventType.ORCHESTRATOR_TICK)
        assert ticks == expected_returned_ticks

    async def test_run_stops_after_exactly_n_items(self):
        """``run()`` applies the same limit, over the same unit."""
        store = await _store()
        async with _session(store) as session:
            await session.run(max_items=3)
            # Three items handed back; the fourth was never requested.
            assert not session.finished
            remaining = []
            while (event := await session.step()) is not None:
                remaining.append(event)
        assert len(remaining) == len(EVENTS) - 3

    async def test_no_limit_replays_everything(self):
        store = await _store()
        async with _session(store) as session:
            stats = await session.run()
        assert stats.ticks_read == 3
        assert stats.events_published == 3
        assert session.finished

    async def test_a_limit_of_zero_replays_nothing(self):
        store = await _store()
        async with _session(store) as session:
            stats = await session.run(max_items=0)
        assert stats.ticks_read == 0
        assert stats.events_published == 0
        assert not session.finished

    async def test_a_limit_larger_than_the_session_is_harmless(self):
        store = await _store()
        async with _session(store) as session:
            stats = await session.run(max_items=1_000)
        assert stats.ticks_read == 3
        assert session.finished

    async def test_a_limit_can_be_resumed(self):
        """``run()`` is a loop over ``step()``, so stopping is not closing."""
        store = await _store()
        async with _session(store) as session:
            await session.run(max_items=2)
            assert session.stats.ticks_read == 1
            await session.run(max_items=4)
            assert session.stats.ticks_read == 3
            # Six items were handed back, but the stream has not yet reported
            # exhaustion -- that takes one more pump, which nothing asked for.
            assert await session.step() is None
            assert session.finished


class TestTheCliExposesOneLimit:
    def test_max_items_is_the_flag(self):
        from replay.__main__ import build_parser

        args = build_parser().parse_args(["--session", "s1", "--max-items", "7"])
        assert args.max_items == 7

    @pytest.mark.parametrize("alias", ["--step", "--max-events"])
    def test_the_old_flags_are_aliases_for_it(self, alias):
        """Kept so existing commands keep working -- but they now mean the
        one thing, instead of two things that differed by tick markers."""
        from replay.__main__ import build_parser

        args = build_parser().parse_args(["--session", "s1", alias, "5"])
        assert args.max_items == 5

    def test_omitting_the_limit_means_the_whole_session(self):
        from replay.__main__ import build_parser

        args = build_parser().parse_args(["--session", "s1"])
        assert args.max_items == 0
