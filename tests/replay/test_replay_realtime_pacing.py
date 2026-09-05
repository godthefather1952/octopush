"""Phase 2 finalization, P2-3: REALTIME actually paces.

The old implementation was::

    if mode is REALTIME and speed > 0:
        await self.clock.sleep(0)

which paces nothing. ``clock`` is the replay's own ManualClock -- the clock
this engine drives -- so sleeping on it returns immediately by construction,
and a "REALTIME 1x" replay ran at exactly FAST speed. Worse, it was called
once per item returned by ``step()``, which is not where logical time moves:
a tick marker can release a batch of previously-deferred inputs, so several
items surface at one logical instant.

REALTIME is now defined as: the logical clock advances exactly as it does in
FAST mode, and the HOST is additionally delayed by the logical gap divided by
``speed``. The delay is requested through an injected sleeper, so these tests
assert the exact delays without ever waiting for one.
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


class FakeSleeper:
    """Records what was asked for; never actually waits."""

    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)

    @property
    def total(self) -> float:
        return sum(self.delays)


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


async def _store(events) -> InMemoryEventStore:
    store = InMemoryEventStore()
    await store.open()
    await store.start_session(SESSION_ID, START_MS, config_hash=CONFIG_HASH)
    await store.append_many(SESSION_ID, list(events))
    await store.finalize_session(
        SESSION_ID, START_MS + 100_000, status=SessionStatus.COMPLETE
    )
    return store


def _session(store, sleeper=None, **kwargs) -> ReplaySession:
    kwargs.setdefault("current_config_hash", CONFIG_HASH)
    return ReplaySession(
        store=store,
        bus=InMemoryEventBus(raise_on_handler_error=True),
        clock=ManualClock(START_MS),
        session_id=SESSION_ID,
        host_sleep=sleeper or FakeSleeper(),
        **kwargs,
    )


#: One input then one marker, per step, with a 1000ms gap between each pair.
EVENTS = [
    _input(1, START_MS + 1_000),
    _marker(2, START_MS + 1_000, 1, 1),
    _input(3, START_MS + 2_000),
    _marker(4, START_MS + 2_000, 2, 3),
    _input(5, START_MS + 3_000),
    _marker(6, START_MS + 3_000, 3, 5),
]


class TestFastModeNeverSleeps:
    async def test_fast_requests_no_host_delay_at_all(self):
        store = await _store(EVENTS)
        sleeper = FakeSleeper()
        async with _session(store, sleeper, mode=ReplayMode.FAST) as session:
            await session.run()
        assert sleeper.delays == []


class TestRealtimePacesTheLogicalGap:
    @pytest.mark.parametrize(
        ("speed", "expected_total"),
        [
            (0.5, 6.0),  # 3000ms of logical time at half speed
            (1.0, 3.0),
            (2.0, 1.5),
            (10.0, 0.3),
        ],
    )
    async def test_total_delay_scales_with_speed(self, speed, expected_total):
        store = await _store(EVENTS)
        sleeper = FakeSleeper()
        async with _session(
            store, sleeper, mode=ReplayMode.REALTIME, speed=speed
        ) as session:
            await session.run()
        assert sleeper.total == pytest.approx(expected_total)

    async def test_each_delay_is_the_gap_it_paces(self):
        store = await _store(EVENTS)
        sleeper = FakeSleeper()
        async with _session(store, sleeper, mode=ReplayMode.REALTIME) as session:
            await session.run()
        # Three 1000ms advances from START_MS: 1s each. Nothing else -- the
        # marker sharing a timestamp with its input adds no delay.
        assert sleeper.delays == [1.0, 1.0, 1.0]

    @pytest.mark.parametrize("gap_ms", [0, 1, 250, 1_000])
    async def test_a_single_gap_of_any_size(self, gap_ms):
        store = await _store(
            [
                _input(1, START_MS),
                _marker(2, START_MS, 1, 1),
                _input(3, START_MS + gap_ms),
                _marker(4, START_MS + gap_ms, 2, 3),
            ]
        )
        sleeper = FakeSleeper()
        async with _session(store, sleeper, mode=ReplayMode.REALTIME) as session:
            await session.run()
        assert sleeper.total == pytest.approx(gap_ms / 1000)

    async def test_same_timestamp_events_cost_nothing(self):
        """Five events at one instant is one instant, not five delays."""
        store = await _store(
            [
                _input(1, START_MS + 500),
                _input(2, START_MS + 500),
                _input(3, START_MS + 500),
                _marker(4, START_MS + 500, 1, 3),
                _input(5, START_MS + 500),
                _marker(6, START_MS + 500, 2, 5),
            ]
        )
        sleeper = FakeSleeper()
        async with _session(store, sleeper, mode=ReplayMode.REALTIME) as session:
            await session.run()
        assert sleeper.delays == [0.5], (
            "only the initial advance to the first instant is a real gap"
        )

    async def test_a_non_advancing_timestamp_costs_nothing(self):
        """A marker stamped BEFORE the input it follows must not sleep.

        Recorded timestamps can invert under a concurrent bus (that race is
        why tick markers carry watermarks at all). Time never moves backwards
        in replay, so nothing is paced backwards either.
        """
        store = await _store(
            [
                _input(1, START_MS + 1_000),
                _marker(2, START_MS + 500, 1, 1),  # stamped earlier than its input
                _input(3, START_MS + 2_000),
                _marker(4, START_MS + 2_000, 2, 3),
            ]
        )
        sleeper = FakeSleeper()
        async with _session(store, sleeper, mode=ReplayMode.REALTIME) as session:
            await session.run()
        assert all(d >= 0 for d in sleeper.delays)
        assert sleeper.total == pytest.approx(2.0), (
            "paced from START_MS to +2000ms in total, never backwards"
        )


class TestPacingFollowsTheClockNotTheOutputQueue:
    async def test_a_marker_releasing_deferred_inputs_paces_once(self):
        """The case the old per-item sleep got wrong.

        Three inputs are published early and held back by the watermark, then
        released together when one tick marker's watermark covers them. That
        is FOUR items out of ``step()`` at ONE logical instant -- and exactly
        one host delay, for the one gap the clock actually crossed.
        """
        store = await _store(
            [
                _input(1, START_MS + 1_000),
                _input(2, START_MS + 1_000),
                _input(3, START_MS + 1_000),
                _marker(4, START_MS + 1_000, 1, 3),
            ]
        )
        sleeper = FakeSleeper()
        items = 0
        async with _session(store, sleeper, mode=ReplayMode.REALTIME) as session:
            while await session.step() is not None:
                items += 1
        assert items == 4, "three released inputs plus the marker"
        assert sleeper.delays == [1.0], (
            "four items at one logical instant is one gap, not four sleeps"
        )


class TestSpeedIsValidated:
    @pytest.mark.parametrize("speed", [0, -1, -0.5])
    async def test_a_non_positive_speed_is_refused(self, speed):
        store = await _store(EVENTS)
        with pytest.raises(ValueError, match="positive speed"):
            await _session(store, mode=ReplayMode.REALTIME, speed=speed).open()

    async def test_fast_mode_ignores_speed_entirely(self):
        """FAST never divides by it, so even a nonsense value is inert."""
        store = await _store(EVENTS)
        sleeper = FakeSleeper()
        async with _session(
            store, sleeper, mode=ReplayMode.FAST, speed=0
        ) as session:
            await session.run()
        assert sleeper.delays == []


class TestPacingCannotChangeTheAnswer:
    """The whole point: REALTIME changes how long it takes, nothing else."""

    @pytest.mark.parametrize("speed", [0.5, 1.0, 2.0, 10.0])
    async def test_fast_and_realtime_produce_identical_replays(self, speed):
        def fingerprint(session, seen):
            return {
                "items": [(e.type.value, e.ts_ms, e.sequence) for e in seen],
                "events_read": session.stats.events_read,
                "events_published": session.stats.events_published,
                "ticks_read": session.stats.ticks_read,
                "span_ms": session.stats.span_ms,
                "clock": session.clock.now_ms(),
                "is_exact": session.stats.is_exact,
            }

        results = []
        for mode, spd in ((ReplayMode.FAST, 1.0), (ReplayMode.REALTIME, speed)):
            store = await _store(EVENTS)
            seen: list[Event] = []
            async with _session(
                store, FakeSleeper(), mode=mode, speed=spd
            ) as session:
                while (event := await session.step()) is not None:
                    seen.append(event)
                results.append(fingerprint(session, seen))
        assert results[0] == results[1]
