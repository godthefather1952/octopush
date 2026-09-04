"""Phase 2 Batch 1.1: partial replay range semantics.

``ReplaySession`` has always accepted ``start_ms``/``end_ms``, but its
tick-marker existence check used to query the WHOLE session, unscoped -- so
a selected replay interval containing zero markers could still be reported
as timeline-verified: it would publish market events and execute zero
ticks while claiming exact-replay fidelity.

Separately, ANY ``start_ms`` that skips part of the session's true
beginning starts every component (TIDAL's books included) empty at a point
where the original session already had accumulated state. There is no
checkpoint here to reconstruct that state from, so this is never a verified
exact replay -- regardless of whether markers exist in the requested range.
Both conditions are refused loudly by default, each with its own explicit,
separately-named opt-in and fidelity report.

Truncating only the END of a session (a normal, valid prefix) needs neither
opt-in: TIDAL starting empty is exactly correct there, since the original
session's books were empty at its true start too.
"""

from __future__ import annotations

import pytest

from core.bus import InMemoryEventBus
from core.clock import ManualClock
from replay.engine import (
    LEGACY_REPLAY_UNVERIFIED_TIMELINE,
    PARTIAL_REPLAY_UNVERIFIED_STARTING_STATE,
    LegacyTimelineRequired,
    PartialReplayUnsupported,
    ReplaySession,
)
from tests.conftest import START_MS
from tests.replay.test_replay import _record_session


class TestPartialReplayRange:
    async def test_a_range_with_no_markers_is_refused_not_silently_verified(self, settings):
        """E1/E5: the full session has markers; the requested range has
        none. This must not be silently reported as a verified replay.
        """
        recorded = await _record_session(settings, ticks=20)

        session = ReplaySession(
            store=recorded.store,
            bus=InMemoryEventBus(),
            clock=ManualClock(START_MS),
            session_id=recorded.session_id,
            end_ms=START_MS - 1,  # strictly before the session's own start
        )
        with pytest.raises(LegacyTimelineRequired):
            await session.open()

        forced = ReplaySession(
            store=recorded.store,
            bus=InMemoryEventBus(),
            clock=ManualClock(START_MS),
            session_id=recorded.session_id,
            end_ms=START_MS - 1,
            legacy_timeline=True,
        )
        await forced.open()
        assert forced.stats.timeline_fidelity == LEGACY_REPLAY_UNVERIFIED_TIMELINE
        assert await forced.step() is None, "nothing at all should be in this empty range"

    async def test_a_range_beginning_between_ticks_is_refused(self, settings):
        """E2: start_ms falls after the session's true beginning (between
        the session start and the first tick) -- TIDAL would start empty at
        a point the original session's books were not.
        """
        recorded = await _record_session(settings, ticks=20)
        info = await recorded.store.session(recorded.session_id)
        assert info is not None

        session = ReplaySession(
            store=recorded.store,
            bus=InMemoryEventBus(),
            clock=ManualClock(START_MS),
            session_id=recorded.session_id,
            start_ms=info.started_at + 50,
        )
        with pytest.raises(PartialReplayUnsupported):
            await session.open()

        forced = ReplaySession(
            store=recorded.store,
            bus=InMemoryEventBus(),
            clock=ManualClock(START_MS),
            session_id=recorded.session_id,
            start_ms=info.started_at + 50,
            allow_partial_range=True,
        )
        await forced.open()
        assert forced.stats.timeline_fidelity == PARTIAL_REPLAY_UNVERIFIED_STARTING_STATE

    async def test_a_range_ending_between_ticks_needs_no_opt_in(self, settings):
        """E3: end_ms truncates the session early (a normal prefix, no
        start_ms skipped) -- fully supported, no flag required, and any
        market inputs left over at truncation are still flushed rather than
        silently dropped.
        """
        recorded = await _record_session(settings, ticks=20)
        info = await recorded.store.session(recorded.session_id)
        assert info is not None

        # Ends 50ms after the first tick's own marker (started_at + 100),
        # so it lands strictly between the first and second tick.
        session = ReplaySession(
            store=recorded.store,
            bus=InMemoryEventBus(),
            clock=ManualClock(START_MS),
            session_id=recorded.session_id,
            end_ms=info.started_at + 150,
        )
        await session.open()
        assert session.stats.timeline_fidelity is None

        ticks_fired = 0
        while True:
            event = await session.step()
            if event is None:
                break
            if event.type.value == "ORCHESTRATOR_TICK":
                ticks_fired += 1
        assert ticks_fired == 1, "only the first tick's marker falls in this range"

    async def test_exactly_one_marker_in_range(self, settings):
        """E4: a range containing precisely one tick marker replays exactly
        one tick and reports full fidelity.
        """
        recorded = await _record_session(settings, ticks=20)
        info = await recorded.store.session(recorded.session_id)
        assert info is not None

        session = ReplaySession(
            store=recorded.store,
            bus=InMemoryEventBus(),
            clock=ManualClock(START_MS),
            session_id=recorded.session_id,
            end_ms=info.started_at + 100,  # exactly the first tick's own ts_ms
        )
        await session.open()
        assert session.stats.timeline_fidelity is None

        ticks_fired = 0
        while True:
            event = await session.step()
            if event is None:
                break
            if event.type.value == "ORCHESTRATOR_TICK":
                ticks_fired += 1
        assert ticks_fired == 1
