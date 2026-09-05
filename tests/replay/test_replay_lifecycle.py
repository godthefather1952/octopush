"""Phase 2 finalization, P2-4: a replay leaves the process as it found it.

``ReplaySession.open()`` installs two pieces of PROCESS-GLOBAL state: a
deterministic id generator (so two replays of one session can be diffed
entity by entity) and a bound logging clock (so log lines carry replay time).
Both are swapped back by ``close()``.

The defect was that close() was not reliably reached. The CLI carried an
explicit note saying it deliberately did not call it, and -- more subtly --
``open()`` installed both globals BEFORE validating the recording, so any
refusal left the process altered on the way out. A caller who never received
a usable session has no reason to suspect it must clean one up, and the next
thing to mint an id in that process would have got replay's deterministic
stream instead of a random one.

The invariant, for every path: after any replay attempt -- success, early
stop, exception, cancellation, failed open, failed validation -- the id
generator and the logging clock are exactly what they were before.
"""

from __future__ import annotations

import asyncio

import pytest

from core.bus import InMemoryEventBus
from core.clock import ManualClock
from core.events import Event, EventType
from core.ids import current_generator
from core.logging import bind_clock
from replay.engine import (
    IncompleteSessionError,
    LegacyTimelineRequired,
    PartialReplayUnsupported,
    ReplayConfigMismatch,
    ReplaySession,
    UnsupportedEventSchemaVersion,
)
from storage import InMemoryEventStore
from storage.base import SessionStatus

START_MS = 1_788_000_000_000
SESSION_ID = "s1"
CONFIG_HASH = "cfg-recorded"


def _input(seq: int, ts_ms: int, **overrides) -> Event:
    fields: dict = {
        "type": EventType.BOOK_SNAPSHOT,
        "ts_ms": ts_ms,
        "sequence": seq,
        "source": "VENUE_A",
        "schema_name": "Test",
        "payload": {},
    }
    fields.update(overrides)
    return Event(**fields)


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
    _marker(2, START_MS + 100, 1, 1),
    _input(3, START_MS + 200),
    _marker(4, START_MS + 300, 2, 3),
]


async def _store(
    events=EVENTS,
    *,
    status: SessionStatus | None = SessionStatus.COMPLETE,
    config_hash: str = CONFIG_HASH,
) -> InMemoryEventStore:
    store = InMemoryEventStore()
    await store.open()
    await store.start_session(SESSION_ID, START_MS, config_hash=config_hash)
    await store.append_many(SESSION_ID, list(events))
    if status is not None:
        await store.finalize_session(SESSION_ID, START_MS + 999, status=status)
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


class GlobalState:
    """The exact pair of process-globals a replay is allowed to borrow."""

    def __init__(self) -> None:
        self.generator = current_generator()
        # bind_clock returns the previous value, so binding what is already
        # bound reads it without changing anything.
        self.log_clock = bind_clock(None)
        bind_clock(self.log_clock)

    def assert_restored(self, note: str = "") -> None:
        assert current_generator() is self.generator, (
            f"the id generator was not restored{': ' + note if note else ''}"
        )
        current_log_clock = bind_clock(None)
        bind_clock(current_log_clock)
        assert current_log_clock is self.log_clock, (
            f"the logging clock was not restored{': ' + note if note else ''}"
        )


@pytest.fixture
def globals_before():
    """Capture the globals, and put them back whatever the test does.

    The fixture's own restoration exists so that a test proving a LEAK cannot
    contaminate every test that runs after it.
    """
    state = GlobalState()
    yield state
    from core.ids import set_id_generator

    set_id_generator(state.generator)
    bind_clock(state.log_clock)


class TestSuccessRestoresGlobalState:
    async def test_a_completed_replay_restores_both(self, globals_before):
        store = await _store()
        async with _session(store) as session:
            await session.run()
            assert current_generator() is not globals_before.generator, (
                "the replay really did install its own generator"
            )
        globals_before.assert_restored("after a clean run")

    async def test_the_sync_context_manager_restores_both(self, globals_before):
        store = await _store()
        session = _session(store)
        with session:
            await session.open()
            await session.run()
        globals_before.assert_restored("after a sync `with` block")

    async def test_close_is_idempotent(self, globals_before):
        store = await _store()
        session = _session(store)
        await session.open()
        session.close()
        session.close()
        globals_before.assert_restored("after closing twice")


class TestEveryRefusalRestoresGlobalState:
    """The heart of P2-4: a refusal must not leave the process altered.

    Each case makes ``open()`` raise for a different reason, at a different
    point in the sequence, and asserts the same invariant.
    """

    async def test_a_failed_recording_integrity_check(self, globals_before):
        store = await _store(status=None)  # never finalised: OPEN
        with pytest.raises(IncompleteSessionError):
            await _session(store).open()
        globals_before.assert_restored("after an incomplete-session refusal")

    async def test_a_failed_config_check(self, globals_before):
        store = await _store()
        with pytest.raises(ReplayConfigMismatch):
            await _session(store, current_config_hash="cfg-different").open()
        globals_before.assert_restored("after a config-mismatch refusal")

    async def test_a_failed_legacy_timeline_check(self, globals_before):
        store = await _store(events=[_input(1, START_MS)])  # no markers at all
        with pytest.raises(LegacyTimelineRequired):
            await _session(store).open()
        globals_before.assert_restored("after a legacy-timeline refusal")

    async def test_a_failed_partial_range_check(self, globals_before):
        store = await _store()
        with pytest.raises(PartialReplayUnsupported):
            await _session(store, start_ms=START_MS + 150).open()
        globals_before.assert_restored("after a partial-range refusal")

    async def test_a_failed_schema_check(self, globals_before):
        store = await _store(
            events=[
                _input(1, START_MS),
                _marker(2, START_MS + 100, 1, 1),
                _input(3, START_MS + 200, schema_version=99),
            ]
        )
        with pytest.raises(UnsupportedEventSchemaVersion):
            await _session(store).open()
        globals_before.assert_restored("after a schema refusal")

    async def test_an_invalid_speed(self, globals_before):
        from replay.engine import ReplayMode

        store = await _store()
        with pytest.raises(ValueError):
            await _session(store, mode=ReplayMode.REALTIME, speed=0).open()
        globals_before.assert_restored("after an invalid-speed refusal")

    async def test_the_async_context_manager_restores_a_failed_open(
        self, globals_before
    ):
        store = await _store(status=None)
        with pytest.raises(IncompleteSessionError):
            async with _session(store):
                pytest.fail("the body must never run")
        globals_before.assert_restored("after `async with` failed to open")


class TestFailureDuringTheRunRestoresGlobalState:
    async def test_an_exception_mid_run(self, globals_before):
        store = await _store()

        class Boom(RuntimeError):
            pass

        with pytest.raises(Boom):
            async with _session(store) as session:
                await session.step()
                raise Boom("something downstream failed")
        globals_before.assert_restored("after an exception inside the block")

    async def test_an_early_break(self, globals_before):
        store = await _store()
        async with _session(store) as session:
            await session.step()
            # ...and the caller simply stops, as `--max-items` does.
        globals_before.assert_restored("after stopping early")

    async def test_cancellation(self, globals_before):
        """A cancelled replay task must not strand the process either."""
        store = await _store()
        started = asyncio.Event()

        async def run_forever() -> None:
            async with _session(store) as session:
                started.set()
                while True:
                    await session.step()
                    await asyncio.sleep(0)

        task = asyncio.create_task(run_forever())
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        globals_before.assert_restored("after cancellation")


class TestSequentialAndNestedUse:
    async def test_two_sequential_replays_do_not_leak(self, globals_before):
        """Serial use is the supported pattern, and it must not accumulate."""
        for _ in range(2):
            store = await _store()
            async with _session(store) as session:
                await session.run()
            globals_before.assert_restored("between sequential replays")

    async def test_a_second_replay_gets_the_same_deterministic_stream(self):
        """Two replays of one session mint identical ids -- which is only true
        if the first one put the previous generator back."""
        seen = []
        for _ in range(2):
            store = await _store()
            async with _session(store) as session:
                await session.run()
                from core.models.common import new_id

                seen.append(new_id("probe"))
        assert seen[0] == seen[1]

    async def test_nested_replays_restore_in_reverse_order(self, globals_before):
        """Nesting is not the supported pattern, but its behaviour is defined:
        strict LIFO, because each session remembers exactly the generator it
        displaced.

        The inner replay's generator is live inside the inner block; closing
        it restores the OUTER replay's, not the process's original; only
        closing the outer one gets back to where the process started.
        """
        outer_store = await _store()
        inner_store = await _store()
        async with _session(outer_store) as outer:
            await outer.step()
            outer_generator = current_generator()
            async with _session(inner_store, id_seed="inner") as inner:
                await inner.step()
                assert current_generator() is not outer_generator
            assert current_generator() is outer_generator, (
                "closing the inner replay must restore the OUTER one, "
                "not the process's original"
            )
        globals_before.assert_restored("after both nested replays closed")
