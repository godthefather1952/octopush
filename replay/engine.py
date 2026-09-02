"""Replay engine.

Takes a recorded session and feeds it back through the platform.  Replay is a
product feature: it is how "strategy v1.2 vs v1.3 on exactly the same market"
becomes a meaningful sentence.

Determinism comes from three rules:

1. Time comes from a :class:`ManualClock` driven by the recorded timestamps,
   so nothing observes wall-clock time.
2. Events are ordered by ``(ts_ms, sequence, id)`` — the sequence breaks ties
   between events recorded in the same millisecond.
3. Only *market inputs* are replayed.  Everything the platform derived is
   recomputed, which is the whole point: if the derived output differs, the
   code changed.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable, Iterable
from dataclasses import dataclass, field

from core.bus import EventBus
from core.clock import ManualClock
from core.events import MARKET_INPUT_TYPES, Event, EventType
from core.ids import DeterministicIdGenerator, IdGenerator, set_id_generator
from core.models.common import Millis, StrEnum
from storage.base import EventStore

log = logging.getLogger(__name__)


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
    first_ts: Millis | None = None
    last_ts: Millis | None = None

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
    _id_generator: IdGenerator | None = None
    _previous_ids: IdGenerator | None = None
    stats: ReplayStats = field(default_factory=ReplayStats)
    _iterator: AsyncIterator[Event] | None = None
    _finished: bool = False

    async def open(self) -> None:
        if self.deterministic_ids and self._id_generator is None:
            seed = f"{self.session_id}|{self.id_seed}"
            self._id_generator = DeterministicIdGenerator(seed)
            self._previous_ids = set_id_generator(self._id_generator)
        await self.store.open()
        self._iterator = self.store.read(
            self.session_id,
            types=sorted(self.input_types, key=lambda t: t.value),
            start_ms=self.start_ms,
            end_ms=self.end_ms,
        ).__aiter__()

    @property
    def finished(self) -> bool:
        return self._finished

    def close(self) -> None:
        """Restore the previous id generator. Safe to call more than once."""
        if self._previous_ids is not None:
            set_id_generator(self._previous_ids)
            self._previous_ids = None

    def __enter__(self) -> ReplaySession:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    async def step(self) -> Event | None:
        """Publish exactly one recorded event. Returns ``None`` at the end."""
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


def config_digest(payload: dict) -> str:
    """Stable digest of configuration, recorded with each session."""
    import hashlib
    import json

    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode()
    ).hexdigest()[:16]


async def collect(
    store: EventStore,
    session_id: str,
    types: Iterable[EventType] | None = None,
) -> list[Event]:
    """Read a session's events into a list, in deterministic order."""
    await store.open()
    return [event async for event in store.read(session_id, types=types)]
