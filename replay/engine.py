"""Replay engine.

Takes a recorded session and feeds it back through the platform.  Replay is a
product feature: it is how "strategy v1.2 vs v1.3 on exactly the same market"
becomes a meaningful sentence.

Determinism comes from four rules:

1. Time comes from a :class:`ManualClock` driven by the recorded timestamps,
   so nothing observes wall-clock time.
2. Events are ordered by ``(ts_ms, sequence, id)`` — the sequence breaks ties
   between events recorded in the same millisecond.
3. Only *market inputs* are replayed.  Everything the platform derived is
   recomputed, which is the whole point: if the derived output differs, the
   code changed.
4. Orchestrator ticks happen at the SAME logical boundaries they happened at
   in the original run, recovered from a durable ``ORCHESTRATOR_TICK``
   marker recorded once per tick — not one tick per replayed market event.
   A session recorded before this marker existed has no verified tick
   cadence at all; see ``ReplaySession.legacy_timeline``.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from core.bus import EventBus
from core.clock import ManualClock
from core.events import MARKET_INPUT_TYPES, Event, EventType
from core.ids import DeterministicIdGenerator, IdGenerator, set_id_generator
from core.logging import bind_clock
from core.models.common import Millis, StrEnum
from storage.base import EventStore

#: Distinguishes "nothing was bound" from "None was bound".
_UNBOUND = object()

#: The one event type replay treats as a tick boundary rather than data.
#: Never included in ``MARKET_INPUT_TYPES``; never republished onto the bus.
TICK_MARKER_TYPE: EventType = EventType.ORCHESTRATOR_TICK

#: Reported on :class:`ReplayStats` when a session predates tick-boundary
#: markers and was replayed anyway via ``legacy_timeline=True``. Distinct
#: from ``None`` (verified: every tick boundary came from a real marker) so
#: nothing can mistake a legacy replay for an exact one.
LEGACY_REPLAY_UNVERIFIED_TIMELINE = "LEGACY_REPLAY_UNVERIFIED_TIMELINE"

log = logging.getLogger(__name__)


class LegacyTimelineRequired(RuntimeError):
    """Raised when a session has no recorded tick-boundary markers.

    Replaying such a session by falling back to one tick per market event
    would silently reintroduce the exact defect this marker exists to fix,
    under the name "replay". Pass ``legacy_timeline=True`` to
    :class:`ReplaySession` to accept that explicitly and proceed anyway --
    the result is then reported as ``LEGACY_REPLAY_UNVERIFIED_TIMELINE``,
    never as a verified reproduction of the original tick cadence.
    """


class ReplayMode(StrEnum):
    #: Advance the clock to each event's timestamp as fast as the CPU allows.
    FAST = "FAST"
    #: Pace against wall-clock time, scaled by ``speed``.
    REALTIME = "REALTIME"
    #: Advance exactly one event per :meth:`ReplaySession.step` call.
    STEP = "STEP"


@dataclass
class ReplayStats:
    events_read: int = 0
    events_published: int = 0
    events_skipped: int = 0
    #: Tick-boundary markers read. Zero for a legacy (marker-less) session.
    ticks_read: int = 0
    first_ts: Millis | None = None
    last_ts: Millis | None = None
    #: ``None`` once the session's tick cadence is verified from real
    #: markers; ``LEGACY_REPLAY_UNVERIFIED_TIMELINE`` if it was replayed
    #: under ``legacy_timeline=True`` because no markers exist.
    timeline_fidelity: str | None = None

    @property
    def span_ms(self) -> int:
        if self.first_ts is None or self.last_ts is None:
            return 0
        return self.last_ts - self.first_ts


@dataclass
class ReplaySession:
    """Drives recorded events through a live pipeline."""

    store: EventStore
    bus: EventBus
    clock: ManualClock
    session_id: str
    mode: ReplayMode = ReplayMode.FAST
    speed: float = 1.0
    #: Which event types are treated as inputs. Defaults to market inputs so
    #: derived state is recomputed rather than replayed back at itself.
    input_types: frozenset[EventType] = MARKET_INPUT_TYPES
    start_ms: Millis | None = None
    end_ms: Millis | None = None
    #: Called after each event is published and the bus has drained.
    on_event: Callable[[Event], None] | None = None
    #: Install reproducible identifiers for the duration of the replay, so two
    #: replays of one session can be diffed entity by entity. Set False only to
    #: replay with live-style random ids.
    deterministic_ids: bool = True
    #: Extra seed material, so the same session can be replayed under different
    #: id streams when comparing two code versions side by side.
    id_seed: str = ""
    #: Required to replay a session recorded before ORCHESTRATOR_TICK markers
    #: existed. Without it, ``open()`` raises ``LegacyTimelineRequired``
    #: rather than silently falling back to one tick per market event.
    legacy_timeline: bool = False
    _id_generator: IdGenerator | None = None
    _previous_ids: IdGenerator | None = None
    stats: ReplayStats = field(default_factory=ReplayStats)
    _iterator: AsyncIterator[Event] | None = None
    _finished: bool = False
    #: Sentinel-guarded so "no clock was bound" is distinguishable from
    #: "None was bound", which close() must restore faithfully.
    _previous_log_clock: Any = _UNBOUND

    async def open(self) -> None:
        # Bind the replay clock for logging, so log lines carry replay time
        # alongside host time and can be aligned with the events they
        # describe. Restored by close().
        if self._previous_log_clock is _UNBOUND:
            self._previous_log_clock = bind_clock(self.clock)
        if self.deterministic_ids and self._id_generator is None:
            seed = f"{self.session_id}|{self.id_seed}"
            self._id_generator = DeterministicIdGenerator(seed)
            self._previous_ids = set_id_generator(self._id_generator)
        await self.store.open()

        has_tick_markers = False
        async for _ in self.store.read(self.session_id, types=[TICK_MARKER_TYPE]):
            has_tick_markers = True
            break

        read_types = set(self.input_types)
        if has_tick_markers:
            read_types.add(TICK_MARKER_TYPE)
        elif not self.legacy_timeline:
            raise LegacyTimelineRequired(
                f"session {self.session_id!r} has no recorded ORCHESTRATOR_TICK "
                "markers -- it predates tick-boundary recording. Replaying it "
                "would have to fall back to one tick per market event, which "
                "is not a verified reproduction of the original tick cadence. "
                "Pass legacy_timeline=True to ReplaySession to replay it "
                "anyway, explicitly accepting unverified timeline fidelity."
            )
        else:
            self.stats.timeline_fidelity = LEGACY_REPLAY_UNVERIFIED_TIMELINE

        self._iterator = self.store.read(
            self.session_id,
            types=sorted(read_types, key=lambda t: t.value),
            start_ms=self.start_ms,
            end_ms=self.end_ms,
        ).__aiter__()

    @property
    def finished(self) -> bool:
        return self._finished

    def close(self) -> None:
        """Undo what open() installed. Safe to call more than once."""
        if self._previous_ids is not None:
            set_id_generator(self._previous_ids)
            self._previous_ids = None
        if self._previous_log_clock is not _UNBOUND:
            bind_clock(self._previous_log_clock)
            self._previous_log_clock = _UNBOUND

    def __enter__(self) -> ReplaySession:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    async def step(self) -> Event | None:
        """Advance replay by exactly one stored item. Returns ``None`` at the
        end.

        The returned item is either a market-input event -- already
        published to the bus and drained before this returns -- or an
        ``ORCHESTRATOR_TICK`` boundary marker, which is NOT published (it is
        not data). Callers that need tick fidelity check
        ``event.type is TICK_MARKER_TYPE`` and call the platform's own
        ``orchestrator.tick()`` in response; callers that only care about
        market data (e.g. anything reading ``MARKET_INPUT_TYPES`` payloads)
        can ignore the distinction entirely, since a marker never reaches the
        bus for them to see.
        """
        if self._iterator is None:
            await self.open()
        assert self._iterator is not None
        try:
            event = await self._iterator.__anext__()
        except StopAsyncIteration:
            self._finished = True
            return None

        self.stats.events_read += 1
        if self.stats.first_ts is None:
            self.stats.first_ts = event.ts_ms
        self.stats.last_ts = event.ts_ms

        # The replay clock is the recorded clock. Nothing downstream can tell
        # the difference between this and the original session.
        if event.ts_ms > self.clock.now_ms():
            self.clock.set(event.ts_ms)

        if event.type is TICK_MARKER_TYPE:
            self.stats.ticks_read += 1
            if self.on_event is not None:
                self.on_event(event)
            return event

        replayed = event.model_copy(deep=True)
        replayed.id = event.id
        replayed.sequence = event.sequence
        await self.bus.publish(replayed)
        await self.bus.drain()
        self.stats.events_published += 1
        if self.on_event is not None:
            self.on_event(replayed)
        return replayed

    async def run(self, max_events: int | None = None) -> ReplayStats:
        """Replay to the end (or ``max_events``)."""
        published = 0
        while True:
            if max_events is not None and published >= max_events:
                break
            event = await self.step()
            if event is None:
                break
            published += 1
            if self.mode is ReplayMode.REALTIME and self.speed > 0:
                # Pace using the clock so the pipeline's own timers still fire.
                await self.clock.sleep(0)
        return self.stats


def _unmask(value: object) -> object:
    """Replace masked secrets with a digest of what they hide.

    ``model_dump`` renders a ``SecretStr`` as ``'**********'``, so every
    distinct credential dumps identically. Hashing the real value keeps a
    changed setting detectable — which is the whole point of the digest —
    without putting the credential into a recorded session.
    """
    import hashlib

    from pydantic import SecretStr

    if isinstance(value, SecretStr):
        return "secret:" + hashlib.sha256(
            value.get_secret_value().encode()
        ).hexdigest()[:16]
    if isinstance(value, dict):
        return {key: _unmask(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_unmask(item) for item in value]
    return value


def config_digest(payload: dict) -> str:
    """Stable digest of configuration, recorded with each session.

    Pass the *model* dump (``settings.model_dump()``, not ``mode="json"``) so
    secrets arrive as ``SecretStr`` and can be hashed rather than arriving
    pre-masked and indistinguishable.
    """
    import hashlib
    import json

    return hashlib.sha256(
        json.dumps(_unmask(payload), sort_keys=True, default=str).encode()
    ).hexdigest()[:16]


async def collect(
    store: EventStore,
    session_id: str,
    types: Iterable[EventType] | None = None,
) -> list[Event]:
    """Read a session's events into a list, in deterministic order."""
    await store.open()
    return [event async for event in store.read(session_id, types=types)]
