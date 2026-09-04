"""Replay engine.

Takes a recorded session and feeds it back through the platform.  Replay is a
product feature: it is how "strategy v1.2 vs v1.3 on exactly the same market"
becomes a meaningful sentence.

Determinism comes from five rules:

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
5. Each tick sees EXACTLY the market inputs the original tick's snapshot
   had actually applied — not merely every input published before it. A
   market-input event's own sequence number says when it was *published*;
   under an asynchronous bus that is not the same moment it was *applied*
   to TIDAL's book (see ``Tidal.processed_input_sequence``). Each
   ``ORCHESTRATOR_TICK`` marker therefore carries the exact watermark of
   applied inputs as of that tick's snapshot, and replay defers publishing
   any input whose sequence exceeds the current tick's watermark until
   whichever later tick's watermark does cover it. A session recorded
   before this watermark existed has a verified CADENCE but not a verified
   input-visibility CUT; see ``ReplaySession.legacy_input_visibility``.
"""

from __future__ import annotations

import logging
from collections import deque
from collections.abc import AsyncIterator, Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from core.bus import EventBus
from core.clock import ManualClock
from core.events import MARKET_INPUT_TYPES, Event, EventType
from core.ids import DeterministicIdGenerator, IdGenerator, set_id_generator
from core.logging import bind_clock
from core.models.common import Millis, StrEnum
from storage.base import EventStore, SessionInfo, SessionStatus

#: Distinguishes "nothing was bound" from "None was bound".
_UNBOUND = object()

#: The one event type replay treats as a tick boundary rather than data.
#: Never included in ``MARKET_INPUT_TYPES``; never republished onto the bus.
TICK_MARKER_TYPE: EventType = EventType.ORCHESTRATOR_TICK

#: The tick-marker payload key carrying ``Tidal.last_snapshot_input_sequence``.
WATERMARK_KEY = "processed_input_sequence"

#: Reported on :class:`ReplayStats` when a session predates tick-boundary
#: markers entirely (or the requested start_ms/end_ms range excludes every
#: one the full session has) and was replayed anyway via
#: ``legacy_timeline=True``. Distinct from ``None`` (verified) so nothing
#: can mistake a legacy replay for an exact one.
LEGACY_REPLAY_UNVERIFIED_TIMELINE = "LEGACY_REPLAY_UNVERIFIED_TIMELINE"

#: Reported when tick markers exist but predate the input-visibility
#: watermark (recorded by a Batch-1-only build) and were replayed anyway via
#: ``legacy_input_visibility=True``. The tick CADENCE is verified; the exact
#: market-input CUT each tick observed is not.
LEGACY_REPLAY_UNVERIFIED_INPUT_VISIBILITY = "LEGACY_REPLAY_UNVERIFIED_INPUT_VISIBILITY"

#: Reported when a session whose RECORDING integrity was never verified
#: (OPEN, INCOMPLETE, or LEGACY_UNVERIFIED) was replayed anyway via
#: ``allow_incomplete_session=True``. The events present may be replayed
#: faithfully; what is unverified is whether they are ALL of them.
UNVERIFIED_RECORDING_INTEGRITY = "UNVERIFIED_RECORDING_INTEGRITY"

#: Reported when ``start_ms`` skips part of the session's true beginning and
#: the caller explicitly accepted that via ``allow_partial_range=True``. Every
#: component (TIDAL's books included) starts empty at ``start_ms`` with no
#: checkpoint of whatever state had accumulated before it in the original run.
PARTIAL_REPLAY_UNVERIFIED_STARTING_STATE = "PARTIAL_REPLAY_UNVERIFIED_STARTING_STATE"

log = logging.getLogger(__name__)


class LegacyTimelineRequired(RuntimeError):
    """Raised when a session (or the requested replay range) has no recorded
    tick-boundary markers.

    Replaying such a session by falling back to one tick per market event
    would silently reintroduce the exact defect this marker exists to fix,
    under the name "replay". Pass ``legacy_timeline=True`` to
    :class:`ReplaySession` to accept that explicitly and proceed anyway --
    the result is then reported as ``LEGACY_REPLAY_UNVERIFIED_TIMELINE``,
    never as a verified reproduction of the original tick cadence.
    """


class LegacyInputVisibilityRequired(LegacyTimelineRequired):
    """Raised when tick markers exist but carry no input-visibility watermark.

    Such a session was recorded by a build that fixed tick CADENCE but not
    the finer publication-vs-delivery race: a market input published before
    a tick marker is not guaranteed to have been applied to TIDAL by the
    time that tick's snapshot was taken. Pass
    ``legacy_input_visibility=True`` to replay it anyway, explicitly
    accepting that the exact input cut each tick saw is unverified --
    reported as ``LEGACY_REPLAY_UNVERIFIED_INPUT_VISIBILITY``.
    """


class InvalidTickMarkerWatermark(RuntimeError):
    """Raised mid-replay when a tick marker's watermark cannot be trusted.

    ``open()`` establishes ``_input_visibility_verified`` from the FIRST
    tick marker in range alone -- that only proves the session's build
    recorded watermarks at all, not that every later marker's watermark is
    sane. A marker with a missing/non-integer watermark, one lower than an
    earlier marker's (watermarks must be monotonically non-decreasing --
    TIDAL's applied-input count never goes backwards), or one claiming to
    have applied a higher ``Event.sequence`` than any event this session's
    range ever recorded (a ceiling proven once, by a full scan, in
    ``open()``) means the recorded timeline cannot be trusted to release
    inputs to the tick that actually saw them. Replay must stop loudly at
    the offending marker rather than silently keep going on the strength of
    a check performed only once, at the start, against a different marker.
    """


class UnverifiedRecordingError(RuntimeError):
    """Base for refusing to replay a session whose history may be incomplete."""


class IncompleteSessionError(UnverifiedRecordingError):
    """Raised when the session is not COMPLETE.

    OPEN means the recorder never finalised it -- the process died, or is
    still running -- so whatever it still held in memory is simply not in the
    store. INCOMPLETE means the recorder finalised it while knowing history
    had been abandoned. Neither is a basis for calling a replay exact, so
    ``open()`` refuses unless ``allow_incomplete_session=True`` says the
    caller accepts that explicitly.
    """


class UnverifiedLegacySessionError(UnverifiedRecordingError):
    """Raised for a session recorded before recording integrity was tracked.

    Such a session may well be complete -- but the build that wrote it could
    fail a write, discard the batch, count the loss in memory only, and still
    end the session, so ``ended_at`` is not evidence of anything. It is
    readable and replayable behind the explicit override; it is never
    silently certified as exact.
    """


class PartialReplayUnsupported(RuntimeError):
    """Raised when ``start_ms`` skips part of a session's true beginning.

    Replay always constructs a fresh platform -- TIDAL's books start empty.
    Replaying from the true start of a session, that is correct: the
    original session's books were empty too. Replaying from some later
    ``start_ms`` is not: the original session's books were NOT empty at that
    point, and there is no checkpoint here to reconstruct that state from.
    Pretending a fresh platform started at an arbitrary ``start_ms`` is
    equivalent to the original session would be silently wrong. Pass
    ``allow_partial_range=True`` to proceed anyway, explicitly accepting
    that the starting state is unverified -- reported as
    ``PARTIAL_REPLAY_UNVERIFIED_STARTING_STATE``.
    """


def _watermark_of(marker: Event) -> int | None:
    value = marker.payload.get(WATERMARK_KEY)
    return value if isinstance(value, int) else None


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
    #: ``None`` once every fidelity check below is satisfied; otherwise the
    #: single worst applicable constant among ``LEGACY_REPLAY_UNVERIFIED_TIMELINE``,
    #: ``LEGACY_REPLAY_UNVERIFIED_INPUT_VISIBILITY``, and
    #: ``PARTIAL_REPLAY_UNVERIFIED_STARTING_STATE``.
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
    #: Required to replay a session (or requested range) with no recorded
    #: ORCHESTRATOR_TICK markers. Without it, ``open()`` raises
    #: ``LegacyTimelineRequired`` rather than silently falling back to one
    #: tick per market event.
    legacy_timeline: bool = False
    #: Required to replay a session whose tick markers predate the
    #: input-visibility watermark (a Batch-1-only recording). Without it,
    #: ``open()`` raises ``LegacyInputVisibilityRequired`` rather than
    #: silently assuming publication order is delivery order.
    legacy_input_visibility: bool = False
    #: Required to replay a session whose recording integrity was never
    #: verified -- OPEN, INCOMPLETE or LEGACY_UNVERIFIED. Without it,
    #: ``open()`` raises rather than presenting a possibly-truncated history
    #: as an exact reproduction.
    allow_incomplete_session: bool = False
    #: Required when ``start_ms`` skips part of the session's true
    #: beginning. Without it, ``open()`` raises ``PartialReplayUnsupported``
    #: rather than silently starting every component empty at an arbitrary
    #: mid-session point with no checkpoint of what came before it.
    allow_partial_range: bool = False
    _id_generator: IdGenerator | None = None
    _previous_ids: IdGenerator | None = None
    stats: ReplayStats = field(default_factory=ReplayStats)
    _iterator: AsyncIterator[Event] | None = None
    _finished: bool = False
    #: Sentinel-guarded so "no clock was bound" is distinguishable from
    #: "None was bound", which close() must restore faithfully.
    _previous_log_clock: Any = _UNBOUND
    #: True once ``open()`` has confirmed the FIRST tick marker in range
    #: carries a real input-visibility watermark. When False, ``step()``
    #: falls back to applying inputs immediately as they are read (Batch-1
    #: semantics) instead of the deferred-release mechanism below. Every
    #: SUBSEQUENT marker is still checked as it is read (see
    #: ``InvalidTickMarkerWatermark``) -- this flag only gates whether that
    #: checking (and the deferred-release mechanism) applies at all.
    _input_visibility_verified: bool = False
    #: Highest watermark validated so far, for monotonicity. Watermarks are
    #: a running count of applied inputs, so -1 (below the lowest possible
    #: real watermark of 0) is the correct "nothing validated yet" floor.
    _last_watermark: int = -1
    #: The highest ``Event.sequence`` recorded anywhere in this session's
    #: replay range, of ANY event type -- computed once, by a full scan, in
    #: ``open()``. A marker claiming a watermark above this is claiming to
    #: have applied an input that was never even recorded, which is
    #: impossible regardless of how ``ts_ms`` and ``sequence`` order
    #: relate to each other elsewhere in the range. ``None`` when input
    #: visibility is not being verified at all (the scan is skipped).
    _max_possible_sequence: int | None = None
    #: Market-input events read but not yet applied, in the order read
    #: (which is sequence order): each is held back until a tick marker's
    #: own watermark covers its sequence, so a tick can never be handed an
    #: input its ORIGINAL snapshot had not actually applied yet, even if
    #: that input was published (and so appears in the stream) earlier.
    _pending_inputs: deque[Event] = field(default_factory=deque)
    #: Items ready to hand back from ``step()``. A single underlying stream
    #: item can produce several of these in one internal pump (a tick
    #: marker releasing a batch of previously-deferred inputs ahead of
    #: itself), while ``step()``'s own contract still returns one at a time.
    _output_queue: deque[Event] = field(default_factory=deque)

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

        info = await self.store.session(self.session_id)
        self._check_recording_integrity(info)
        partial_range = (
            self.start_ms is not None and info is not None and self.start_ms > info.started_at
        )
        if partial_range and not self.allow_partial_range:
            raise PartialReplayUnsupported(
                f"session {self.session_id!r}: start_ms={self.start_ms} is after "
                f"the session's true start ({info.started_at}). Replay always "
                "constructs a fresh platform, so every component (TIDAL's books "
                "included) would start empty at start_ms, at a point where the "
                "original session already had accumulated state -- there is no "
                "checkpoint here to reconstruct that state from. Pass "
                "allow_partial_range=True to proceed anyway, explicitly "
                "accepting that the starting state is unverified."
            )

        has_tick_markers = False
        first_marker: Event | None = None
        async for marker in self.store.read(
            self.session_id, types=[TICK_MARKER_TYPE], start_ms=self.start_ms, end_ms=self.end_ms
        ):
            has_tick_markers = True
            first_marker = marker
            break

        read_types = set(self.input_types)
        if not has_tick_markers:
            if not self.legacy_timeline:
                raise LegacyTimelineRequired(
                    f"session {self.session_id!r} has no recorded ORCHESTRATOR_TICK "
                    "markers in the requested range -- either it predates "
                    "tick-boundary recording entirely, or the requested "
                    "start_ms/end_ms excludes every marker the full session has. "
                    "Replaying it would have to fall back to one tick per market "
                    "event, which is not a verified reproduction of the original "
                    "tick cadence. Pass legacy_timeline=True to ReplaySession to "
                    "replay it anyway, explicitly accepting unverified timeline "
                    "fidelity."
                )
            self.stats.timeline_fidelity = LEGACY_REPLAY_UNVERIFIED_TIMELINE
        else:
            read_types.add(TICK_MARKER_TYPE)
            assert first_marker is not None
            if _watermark_of(first_marker) is None:
                if not self.legacy_input_visibility:
                    raise LegacyInputVisibilityRequired(
                        f"session {self.session_id!r} has tick-boundary markers "
                        "but they carry no recorded input-visibility watermark "
                        f"({WATERMARK_KEY!r}) -- it was recorded before that "
                        "existed. Replay can reproduce the original tick CADENCE "
                        "but not necessarily the exact market-input CUT each "
                        "tick observed. Pass legacy_input_visibility=True to "
                        "replay it anyway, explicitly accepting unverified "
                        "input-visibility fidelity."
                    )
                self.stats.timeline_fidelity = LEGACY_REPLAY_UNVERIFIED_INPUT_VISIBILITY
            else:
                self._input_visibility_verified = True
                # A one-time, full-range scan proving a real ceiling for
                # every later marker's watermark (Phase 2 Batch 1.2,
                # Section 10) -- not merely trusting the first marker and
                # never checking again. Unfiltered by type: the ceiling
                # must hold against every sequence this range ever
                # recorded, not only the market-input/marker subset this
                # session's own read stream (below) is restricted to.
                max_seq = -1
                async for event in self.store.read(
                    self.session_id, start_ms=self.start_ms, end_ms=self.end_ms
                ):
                    if event.sequence is not None and event.sequence > max_seq:
                        max_seq = event.sequence
                self._max_possible_sequence = max_seq

        if partial_range and self.stats.timeline_fidelity is None:
            self.stats.timeline_fidelity = PARTIAL_REPLAY_UNVERIFIED_STARTING_STATE

        self._iterator = self.store.read(
            self.session_id,
            types=sorted(read_types, key=lambda t: t.value),
            start_ms=self.start_ms,
            end_ms=self.end_ms,
        ).__aiter__()

    def _check_recording_integrity(self, info: SessionInfo | None) -> None:
        """Refuse a session whose history is not known to be complete.

        Exact replay is a claim about the WHOLE run. Every other check in
        this class verifies that the recorded events are replayed faithfully;
        this one asks the prior question of whether they are all of them.
        """
        if info is None:
            # An unknown session has no integrity claim to check. The read
            # below will simply produce nothing, and the tick-marker checks
            # already refuse an empty timeline.
            return
        if info.status.is_verified_complete:
            return
        if not self.allow_incomplete_session:
            if info.status is SessionStatus.LEGACY_UNVERIFIED:
                raise UnverifiedLegacySessionError(
                    f"session {self.session_id!r} was recorded before "
                    "recording integrity was tracked, so whether it contains "
                    "every event it accepted is unknown -- the build that "
                    "wrote it could discard a failed batch and still end the "
                    "session. Pass allow_incomplete_session=True to replay it "
                    "anyway, explicitly accepting unverified recording "
                    "integrity."
                )
            raise IncompleteSessionError(
                f"session {self.session_id!r} is {info.status.value}, not "
                f"COMPLETE"
                + (f" ({info.failure_reason})" if info.failure_reason else "")
                + (
                    f"; {info.events_lost} accepted events were never "
                    "durably recorded"
                    if info.events_lost
                    else ""
                )
                + ". Pass allow_incomplete_session=True to replay it anyway, "
                "explicitly accepting unverified recording integrity."
            )
        self.stats.timeline_fidelity = UNVERIFIED_RECORDING_INTEGRITY

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

    async def _apply_and_queue(self, event: Event) -> None:
        """Publish one market-input event, drain it, and queue it for return.

        The one place that actually applies an input to the bus (hence to
        TIDAL). Called either immediately as an input is read (when the
        session's input-visibility is unverified -- Batch-1 semantics) or
        later, once a tick marker's watermark confirms this input belongs
        before that tick (see ``step()``).
        """
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
        self._output_queue.append(replayed)

    async def _pump_one(self) -> bool:
        """Advance the underlying stream by exactly one stored item.

        Returns ``False`` only once the stream is exhausted AND every
        deferred input has been flushed (nothing left to ever release);
        returns ``True`` otherwise, whether or not this particular pump
        produced anything in ``_output_queue`` yet (a buffered input
        produces nothing until its release point).
        """
        assert self._iterator is not None
        try:
            event = await self._iterator.__anext__()
        except StopAsyncIteration:
            # Nothing left to gate these on: apply whatever is still
            # pending, in the order it was read, rather than silently
            # dropping it.
            while self._pending_inputs:
                await self._apply_and_queue(self._pending_inputs.popleft())
            self._finished = True
            return False

        self.stats.events_read += 1
        if self.stats.first_ts is None:
            self.stats.first_ts = event.ts_ms
        self.stats.last_ts = event.ts_ms

        if event.type is TICK_MARKER_TYPE:
            if self._input_visibility_verified:
                watermark = _watermark_of(event)
                # Every marker is checked here, not only the first one
                # open() inspected to decide _input_visibility_verified --
                # a session can carry a mix of valid and broken markers,
                # and the first being valid says nothing about the rest.
                if watermark is None:
                    raise InvalidTickMarkerWatermark(
                        f"session {self.session_id!r}: tick marker at "
                        f"sequence={event.sequence} (tick "
                        f"{event.payload.get('tick')!r}) has a missing or "
                        f"non-integer {WATERMARK_KEY!r}; the first marker "
                        "in this range had a valid watermark, so every "
                        "later one must too."
                    )
                if watermark < self._last_watermark:
                    raise InvalidTickMarkerWatermark(
                        f"session {self.session_id!r}: tick marker at "
                        f"sequence={event.sequence} has watermark "
                        f"{watermark}, lower than an earlier marker's "
                        f"{self._last_watermark} -- watermarks must be "
                        "monotonically non-decreasing."
                    )
                if (
                    self._max_possible_sequence is not None
                    and watermark > self._max_possible_sequence
                ):
                    raise InvalidTickMarkerWatermark(
                        f"session {self.session_id!r}: tick marker at "
                        f"sequence={event.sequence} claims "
                        f"{WATERMARK_KEY}={watermark}, higher than the "
                        f"highest sequence recorded anywhere in this "
                        f"range ({self._max_possible_sequence}) -- not a "
                        "logically possible watermark."
                    )
                self._last_watermark = watermark
                # Release exactly the inputs THIS tick's original snapshot
                # had actually applied -- not merely everything published
                # before this marker. Pending inputs are held in read
                # (sequence) order, so this is a simple prefix release.
                while (
                    self._pending_inputs
                    and watermark is not None
                    and self._pending_inputs[0].sequence is not None
                    and self._pending_inputs[0].sequence <= watermark
                ):
                    await self._apply_and_queue(self._pending_inputs.popleft())
            # The replay clock is the recorded clock. Nothing downstream can
            # tell the difference between this and the original session.
            if event.ts_ms > self.clock.now_ms():
                self.clock.set(event.ts_ms)
            self.stats.ticks_read += 1
            if self.on_event is not None:
                self.on_event(event)
            self._output_queue.append(event)
            return True

        if self._input_visibility_verified:
            # Held back until a tick marker's watermark says the original
            # tick had actually applied it -- publishing (hence recording)
            # order is not applying (hence TIDAL-visible) order.
            self._pending_inputs.append(event)
            return True

        await self._apply_and_queue(event)
        return True

    async def step(self) -> Event | None:
        """Advance replay by exactly one logical item. Returns ``None`` at
        the end.

        The returned item is either a market-input event -- already
        published to the bus and drained before this returns -- or an
        ``ORCHESTRATOR_TICK`` boundary marker, which is NOT published (it is
        not data). Callers that need tick fidelity check
        ``event.type is TICK_MARKER_TYPE`` and call the platform's own
        ``orchestrator.tick()`` in response; callers that only care about
        market data (e.g. anything reading ``MARKET_INPUT_TYPES`` payloads)
        can ignore the distinction entirely, since a marker never reaches the
        bus for them to see.

        A single call can do more work internally than "read one stored
        item": when input-visibility is verified, a market input read here
        may not become the return value of THIS call at all -- it can sit
        deferred across any number of ``step()`` calls until the tick marker
        whose watermark covers it is reached, at which point it (and any
        other inputs deferred alongside it) are applied and surface through
        subsequent calls before that marker itself does.
        """
        if self._iterator is None:
            await self.open()
        assert self._iterator is not None
        while not self._output_queue:
            if not await self._pump_one():
                if not self._output_queue:
                    return None
                break
        return self._output_queue.popleft()

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
