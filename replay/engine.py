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

import asyncio
import logging
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
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

#: Reported when the recorded configuration digest differs from the digest of
#: the configuration this replay is running under, and the caller explicitly
#: accepted that. The run is then a COUNTERFACTUAL -- "what would this market
#: have done under different settings" -- which is a legitimate question and a
#: different one from "what did this session do".
COUNTERFACTUAL_CONFIGURATION = "COUNTERFACTUAL_CONFIGURATION"

#: Reported when the recorded configuration cannot be compared at all: the
#: session carries no digest (recorded before digests existed), or the caller
#: supplied no current digest to compare against. Not evidence of a mismatch;
#: evidence that equality was never established.
UNVERIFIED_CONFIGURATION = "UNVERIFIED_CONFIGURATION"

#: Reported when a session carries externally-produced intelligence (see
#: ``EXTERNAL_INTELLIGENCE_SOURCES``) that this replay deliberately did NOT
#: replay. Those opinions carry consensus weight, so a run without them is
#: not the run that was recorded.
UNVERIFIED_EXTERNAL_INTELLIGENCE = "UNVERIFIED_EXTERNAL_INTELLIGENCE"

log = logging.getLogger(__name__)


class FidelityDimension(StrEnum):
    """The independent questions "was this replay exact?" decomposes into.

    A single string could only ever name one problem, so a run that was both
    replayed from an incomplete recording AND under changed configuration
    reported whichever check happened to run last. Each dimension is tracked
    separately, and ``ReplayFidelity.is_exact`` is the conjunction.
    """

    #: Does the recording contain every event the recorder accepted?
    RECORDING_INTEGRITY = "recording_integrity"
    #: Did ticks happen at the original logical boundaries?
    TIMELINE = "timeline"
    #: Did each tick see exactly the market inputs its original snapshot had?
    INPUT_VISIBILITY = "input_visibility"
    #: Did the replay start from the session's true beginning?
    STARTING_STATE = "starting_state"
    #: Is the configuration materially the same as the one recorded?
    CONFIGURATION = "configuration"
    #: Does this build understand every event envelope in the range?
    SCHEMA = "schema"
    #: Were externally-produced (non-reproducible) inputs replayed?
    EXTERNAL_INTELLIGENCE = "external_intelligence"


@dataclass
class ReplayFidelity:
    """What this replay can and cannot claim, dimension by dimension.

    Every dimension starts verified and is demoted by name as ``open()``
    establishes a specific reason it cannot be claimed. Demotions accumulate:
    nothing here overwrites an earlier finding, which is precisely the defect
    the single ``timeline_fidelity`` string had.
    """

    #: dimension -> the constant naming why it is unverified.
    issues: dict[FidelityDimension, str] = field(default_factory=dict)

    def demote(self, dimension: FidelityDimension, reason: str) -> None:
        """Record that ``dimension`` cannot be claimed, keeping the first
        reason if one is already present (the earliest check is the most
        specific -- a later, broader one must not paper over it)."""
        self.issues.setdefault(dimension, reason)

    def verified(self, dimension: FidelityDimension) -> bool:
        return dimension not in self.issues

    @property
    def is_exact(self) -> bool:
        """True only when EVERY dimension is verified."""
        return not self.issues

    @property
    def unverified_dimensions(self) -> list[FidelityDimension]:
        """Unverified dimensions, most fundamental first.

        Declaration order, not alphabetical: a caller reading only the first
        issue should see the one that undermines the most. "The recording may
        be missing events" is a deeper problem than "the schema is unknown",
        and both are deeper than "the settings were not compared".
        """
        order = list(FidelityDimension)
        return sorted(self.issues, key=order.index)

    @property
    def reasons(self) -> list[str]:
        """Every applicable issue constant, in dimension order."""
        return [self.issues[d] for d in self.unverified_dimensions]

    def as_dict(self) -> dict[str, str]:
        """Every dimension and its verdict, for a summary or a log line."""
        return {
            dimension.value: self.issues.get(dimension, "VERIFIED")
            for dimension in FidelityDimension
        }


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


class ReplaySchemaCompatibilityError(RuntimeError):
    """Base for refusing a recording this build cannot faithfully read."""


class UnsupportedEventSchemaVersion(ReplaySchemaCompatibilityError):
    """Raised when a recorded event's envelope version is not supported.

    A NEWER version describes fields this build does not know about: reading
    it either fails somewhere unhelpful or -- when the change was purely
    additive -- validates and behaves differently from the run it recorded,
    which is worse. An OLDER version is refused for the mirror-image reason:
    "older" is not a synonym for "compatible", and this build ships no
    upcaster that could prove otherwise (see ``SUPPORTED_SCHEMA_VERSIONS``).
    """


class ReplayConfigError(RuntimeError):
    """Base for refusing a replay whose configuration cannot be trusted."""


class ReplayConfigMismatch(ReplayConfigError):
    """Raised when the recorded and current configuration digests differ.

    Risk limits, fee schedules, latency parameters, consensus weights and
    execution settings all change what the platform does with identical
    market data. Replaying under different settings answers a different
    question -- a legitimate one, but a COUNTERFACTUAL, never an exact
    reproduction. Pass ``allow_config_mismatch=True`` to say so deliberately.
    """


class ReplayConfigVerificationRequired(ReplayConfigError):
    """Raised when configuration equality cannot be established at all.

    Either the session carries no recorded digest, or the caller supplied no
    current digest to compare it against. Neither is evidence of a mismatch;
    both are evidence that nothing was checked, which is not a basis for
    calling a replay exact.
    """


#: Envelope versions this build can replay. There is deliberately no upcaster
#: framework here: with exactly one version in existence, a registry would be
#: speculative scaffolding, and inventing conversions for versions that do not
#: exist yet is how a replay quietly reinterprets history. When a second
#: version appears, it is added here together with the tested upcaster that
#: earns it a place -- not before.
SUPPORTED_SCHEMA_VERSIONS: frozenset[int] = frozenset({Event.CURRENT_SCHEMA_VERSION})

#: Event sources whose output is EXOGENOUS: produced outside the platform's
#: deterministic pipeline and therefore not reproducible from market data.
#:
#: LUMEN reads the information environment through an intelligence provider
#: (Claude, by configuration) and publishes an ``AGENT_OPINION`` carrying
#: consensus weight. Nothing in a recording lets replay recompute what a
#: language model said, and calling the model again would produce a different
#: answer -- so its recorded output is replayed as an INPUT, exactly like a
#: recorded book snapshot.
#:
#: This is deliberately a source allow-list rather than an event type: TIDAL,
#: NORO and ZEPHR publish the same ``AGENT_OPINION`` type, but theirs are
#: derived deterministically from market data and MUST be recomputed. Replaying
#: those back would defeat the entire purpose of replay -- a code change to an
#: agent would no longer show up as a different answer.
EXTERNAL_INTELLIGENCE_SOURCES: frozenset[str] = frozenset({"LUMEN"})

#: The event type external intelligence arrives on.
EXTERNAL_INTELLIGENCE_TYPE: EventType = EventType.AGENT_OPINION


def _watermark_of(marker: Event) -> int | None:
    value = marker.payload.get(WATERMARK_KEY)
    return value if isinstance(value, int) else None


class ReplayMode(StrEnum):
    """How fast the host runs. NOT what the replay computes.

    Both modes advance the replay's logical clock identically and produce
    identical economic output; they differ only in whether the host process
    is delayed between logical instants. There is deliberately no STEP member:
    stepping is what :meth:`ReplaySession.step` does, and having a *mode* of
    the same name implied ``run()`` behaved differently under it, which it
    never did (P2-10).
    """

    #: Advance the clock to each event's timestamp as fast as the CPU allows.
    FAST = "FAST"
    #: Additionally delay the host between logical instants, scaled by ``speed``.
    REALTIME = "REALTIME"


@dataclass
class ReplayStats:
    events_read: int = 0
    events_published: int = 0
    events_skipped: int = 0
    #: Tick-boundary markers read. Zero for a legacy (marker-less) session.
    ticks_read: int = 0
    first_ts: Millis | None = None
    last_ts: Millis | None = None
    #: The authoritative, per-dimension answer to "was this exact?".
    fidelity: ReplayFidelity = field(default_factory=ReplayFidelity)

    @property
    def span_ms(self) -> int:
        if self.first_ts is None or self.last_ts is None:
            return 0
        return self.last_ts - self.first_ts

    @property
    def is_exact(self) -> bool:
        return self.fidelity.is_exact

    @property
    def fidelity_issues(self) -> list[str]:
        """Every applicable issue constant. Empty when the replay is exact."""
        return self.fidelity.reasons

    @property
    def timeline_fidelity(self) -> str | None:
        """The first applicable issue, or ``None`` when the replay is exact.

        Kept because it is the shape callers and recorded summaries already
        read. It is lossy by construction -- several dimensions can be
        unverified at once and this names one of them -- so anything deciding
        what a replay may claim must read ``fidelity`` instead. ``None`` here
        still means exactly what it always meant: nothing was unverified.
        """
        reasons = self.fidelity.reasons
        return reasons[0] if reasons else None


@dataclass
class ReplaySession:
    """Drives recorded events through a live pipeline."""

    store: EventStore
    bus: EventBus
    clock: ManualClock
    session_id: str
    mode: ReplayMode = ReplayMode.FAST
    #: REALTIME only: host delay divisor. 2.0 replays twice as fast as the
    #: recorded timeline, 0.5 half as fast. Must be positive.
    speed: float = 1.0
    #: How REALTIME delays the host between logical instants. Injected so
    #: tests can assert the exact delays requested without ever waiting for
    #: them, and so nothing here reaches for wall-clock time directly.
    #: Deliberately NOT ``ManualClock.sleep``: that clock is the replay's own
    #: logical clock, driven by this engine, so sleeping on it is a no-op and
    #: cannot pace anything (P2-3).
    host_sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    #: Which event types are treated as inputs. Defaults to market inputs so
    #: derived state is recomputed rather than replayed back at itself.
    input_types: frozenset[EventType] = MARKET_INPUT_TYPES
    #: Replay recorded EXTERNAL intelligence (see
    #: ``EXTERNAL_INTELLIGENCE_SOURCES``) as an exogenous input rather than
    #: dropping it. Set False only to deliberately answer "what would this
    #: session have done with no intelligence layer" -- which is reported as
    #: ``UNVERIFIED_EXTERNAL_INTELLIGENCE``, never as exact.
    replay_external_intelligence: bool = True
    #: Digest of the configuration this replay is running under, to compare
    #: with the one recorded alongside the session. ``None`` means the caller
    #: supplied nothing to compare, which cannot establish equality.
    current_config_hash: str | None = None
    #: Required to replay under configuration that differs from (or cannot be
    #: compared with) the recorded configuration. The result is then a
    #: counterfactual, reported as such.
    allow_config_mismatch: bool = False
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
    #: Whether the preflight found any EXTERNAL intelligence in range. Decides
    #: whether AGENT_OPINION is worth reading back at all.
    _has_external_intelligence: bool = False
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
        """Validate the recording, then install replay's process-global state.

        The ordering is the whole point (P2-4). Every refusal below happens
        BEFORE the deterministic id generator and the bound logging clock are
        installed, so a session this build declines to replay cannot leave the
        process altered on its way out. The belt-and-braces ``try`` around the
        installation covers the remainder: if anything at all fails after the
        first global is swapped, ``close()`` puts both back.

        Callers get the same guarantee for the *whole* replay, not just
        ``open()``, from ``with session:`` / ``async with session:``.
        """
        await self.store.open()

        # ---- validation: nothing process-global has been touched yet -----
        info = await self.store.session(self.session_id)
        self._check_recording_integrity(info)
        self._check_configuration(info)
        if self.mode is ReplayMode.REALTIME and self.speed <= 0:
            raise ValueError(
                f"REALTIME replay needs a positive speed; got {self.speed!r}. "
                "Speed divides the recorded gap to produce a host delay, so "
                "zero or negative has no meaning (use ReplayMode.FAST for no "
                "pacing at all)."
            )
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
            self.stats.fidelity.demote(
                FidelityDimension.TIMELINE, LEGACY_REPLAY_UNVERIFIED_TIMELINE
            )
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
                self.stats.fidelity.demote(
                    FidelityDimension.INPUT_VISIBILITY,
                    LEGACY_REPLAY_UNVERIFIED_INPUT_VISIBILITY,
                )
            else:
                self._input_visibility_verified = True

        if partial_range:
            self.stats.fidelity.demote(
                FidelityDimension.STARTING_STATE,
                PARTIAL_REPLAY_UNVERIFIED_STARTING_STATE,
            )

        await self._preflight()

        # External intelligence is exogenous and must be replayed, not
        # recomputed -- see EXTERNAL_INTELLIGENCE_SOURCES. Added only when the
        # preflight actually found some: AGENT_OPINION is dominated by derived
        # TIDAL/NORO/ZEPHR opinions that replay must recompute, and reading
        # every one of them back only to skip it is pure cost on the sessions
        # (the overwhelming majority) that have no intelligence layer at all.
        if self.replay_external_intelligence and self._has_external_intelligence:
            read_types.add(EXTERNAL_INTELLIGENCE_TYPE)

        # ---- every refusal is behind us: NOW install global state --------
        try:
            if self._previous_log_clock is _UNBOUND:
                # Bind the replay clock for logging, so log lines carry replay
                # time alongside host time and can be aligned with the events
                # they describe. Restored by close().
                self._previous_log_clock = bind_clock(self.clock)
            if self.deterministic_ids and self._id_generator is None:
                seed = f"{self.session_id}|{self.id_seed}"
                self._id_generator = DeterministicIdGenerator(seed)
                self._previous_ids = set_id_generator(self._id_generator)

            self._iterator = self.store.read(
                self.session_id,
                types=sorted(read_types, key=lambda t: t.value),
                start_ms=self.start_ms,
                end_ms=self.end_ms,
            ).__aiter__()
        except BaseException:
            # Nothing may survive a failed open, including a half-installed
            # one -- a caller that never got a usable session has no reason
            # to suspect it must clean one up.
            self.close()
            raise

    async def _preflight(self) -> None:
        """One pass over the replay range, before anything is published.

        Deliberately a SINGLE scan doing three jobs, because each of them
        wants the same rows and re-reading a whole session per question is
        how a replay loop acquires an accidental O(N^2):

        1. **Schema compatibility (P2-8).** Every event replay actually
           depends on is checked here, not lazily as it is reached. Half a
           session replayed before discovering event 400 is unreadable has
           already published events, executed ticks and possibly written a
           durable output session -- the refusal has to come first or it is
           not a refusal at all.
        2. **The watermark ceiling.** The highest ``sequence`` recorded
           anywhere in range, of any type, so a later tick marker cannot
           claim to have applied an input that was never recorded.
        3. **External intelligence presence**, so a session carrying LUMEN
           opinions that this replay is not replaying can be reported as
           unverified rather than silently answering a different question.
        """
        max_seq = -1
        saw_external = False
        checked_types = set(self.input_types) | {
            TICK_MARKER_TYPE,
            EXTERNAL_INTELLIGENCE_TYPE,
        }

        async for event in self.store.read(
            self.session_id, start_ms=self.start_ms, end_ms=self.end_ms
        ):
            if event.sequence is not None and event.sequence > max_seq:
                max_seq = event.sequence
            is_external = (
                event.type is EXTERNAL_INTELLIGENCE_TYPE
                and event.source in EXTERNAL_INTELLIGENCE_SOURCES
            )
            if is_external:
                saw_external = True
            # Only what replay READS has to be readable. A derived event this
            # build never feeds back in cannot change the replayed result, so
            # refusing the whole session over it would block replays that are
            # in fact perfectly reproducible.
            relevant = event.type in checked_types and (
                event.type is not EXTERNAL_INTELLIGENCE_TYPE or is_external
            )
            if relevant and event.schema_version not in SUPPORTED_SCHEMA_VERSIONS:
                raise UnsupportedEventSchemaVersion(
                    f"session {self.session_id!r}: event {event.id!r} "
                    f"(type={event.type.value}, schema_name="
                    f"{event.schema_name!r}) was recorded with envelope "
                    f"schema_version={event.schema_version}, which this build "
                    "cannot replay. Supported: "
                    f"{sorted(SUPPORTED_SCHEMA_VERSIONS)} (current="
                    f"{Event.CURRENT_SCHEMA_VERSION}). No upcaster exists for "
                    "that version, and reinterpreting an envelope this build "
                    "does not describe would silently change what the "
                    "recording means."
                )

        if self._input_visibility_verified:
            self._max_possible_sequence = max_seq
        self._has_external_intelligence = saw_external
        if saw_external and not self.replay_external_intelligence:
            self.stats.fidelity.demote(
                FidelityDimension.EXTERNAL_INTELLIGENCE,
                UNVERIFIED_EXTERNAL_INTELLIGENCE,
            )

    def _check_configuration(self, info: SessionInfo | None) -> None:
        """Establish whether this replay runs under the recorded configuration.

        A digest can prove equality; it cannot reconstruct the settings that
        produced it. That is a deliberate Phase 2 boundary (see
        ``docs/phase2-validation.md``): exact replay requires the caller to
        supply the same material configuration, and this check verifies they
        did rather than reconstructing it for them.

        Three outcomes, and the difference between them matters:

        **Verified.** Both digests exist and match. Nothing to report.

        **Refused.** The caller asked for verification by supplying a current
        digest, and verification failed -- the digests differ, or the session
        carries none to compare against. Refusing is right here because the
        caller said what they expected and the recording contradicts it;
        ``allow_config_mismatch=True`` turns it into a declared counterfactual.

        **Unverified, but allowed.** The caller supplied no current digest at
        all, so they never asked. Refusing would force an override flag on
        every programmatic caller for a check they did not request; silently
        proceeding would let the run call itself exact having compared
        nothing. So it proceeds and the CONFIGURATION dimension is demoted --
        ``is_exact`` is False, and the reason says why.
        """
        recorded = (info.config_hash if info is not None else "") or ""
        current = self.current_config_hash

        if current is None:
            self.stats.fidelity.demote(
                FidelityDimension.CONFIGURATION, UNVERIFIED_CONFIGURATION
            )
            return

        if recorded and recorded == current:
            return

        if not recorded:
            reason = (
                f"session {self.session_id!r} carries no recorded configuration "
                "digest, so the settings it ran under cannot be compared with "
                f"the ones this replay is using ({current!r})"
            )
            error: type[ReplayConfigError] = ReplayConfigVerificationRequired
            issue = UNVERIFIED_CONFIGURATION
        else:
            reason = (
                f"session {self.session_id!r} was recorded under configuration "
                f"digest {recorded!r}; this replay is running under {current!r}. "
                "Risk limits, fee schedules, latency, consensus weights and "
                "execution settings all change what the platform does with "
                "identical market data, so this run would answer a different "
                "question from the one the session recorded"
            )
            error = ReplayConfigMismatch
            issue = COUNTERFACTUAL_CONFIGURATION

        if not self.allow_config_mismatch:
            raise error(
                reason
                + ". Pass allow_config_mismatch=True to proceed anyway, "
                "explicitly accepting a counterfactual rather than an exact "
                "reproduction."
            )
        self.stats.fidelity.demote(FidelityDimension.CONFIGURATION, issue)

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
        self.stats.fidelity.demote(
            FidelityDimension.RECORDING_INTEGRITY, UNVERIFIED_RECORDING_INTEGRITY
        )

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

    async def __aenter__(self) -> ReplaySession:
        """Open the session, restoring global state if opening fails.

        The supported lifecycle for programmatic callers: whatever happens
        inside the block -- a clean finish, an early ``break``, an exception,
        or task cancellation -- ``__aexit__`` runs and the process-global id
        generator and logging clock go back to what they were.
        """
        try:
            await self.open()
        except BaseException:
            self.close()
            raise
        return self

    async def __aexit__(self, *exc) -> None:
        self.close()

    async def _advance_replay_clock(self, target_ms: Millis) -> None:
        """Move logical replay time forward, pacing the host if asked to.

        The single place logical time advances, so REALTIME pacing follows
        the replay's actual clock advancement rather than "one sleep per item
        ``step()`` happened to return". Those are different: a tick marker can
        release a batch of previously-deferred inputs, so several items can
        surface at one logical instant (which must not be paced several times)
        and a single item can span a large logical gap (which must).

        Time never moves backwards here, and a non-advancing target costs no
        delay -- a same-millisecond event is not something the original run
        waited for either.
        """
        now = self.clock.now_ms()
        if target_ms <= now:
            return
        if self.mode is ReplayMode.REALTIME:
            # speed is validated positive in open(); dividing the recorded gap
            # by it is what "2x" means -- half the wall time for the same
            # logical span.
            await self.host_sleep((target_ms - now) / 1000.0 / self.speed)
        self.clock.set(target_ms)

    async def _apply_and_queue(self, event: Event) -> None:
        """Publish one replayed INPUT, drain it, and queue it for return.

        The one place that actually applies an input to the bus (hence to
        TIDAL, and to the orchestrator for exogenous intelligence). Called
        either immediately as an input is read (when the session's
        input-visibility is unverified -- Batch-1 semantics -- or for
        non-market inputs, which no watermark gates) or later, once a tick
        marker's watermark confirms this market input belongs before that
        tick (see ``step()``).
        """
        await self._advance_replay_clock(event.ts_ms)
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
            await self._advance_replay_clock(event.ts_ms)
            self.stats.ticks_read += 1
            if self.on_event is not None:
                self.on_event(event)
            self._output_queue.append(event)
            return True

        if event.type is EXTERNAL_INTELLIGENCE_TYPE:
            if (
                not self.replay_external_intelligence
                or event.source not in EXTERNAL_INTELLIGENCE_SOURCES
            ):
                # A derived opinion (TIDAL/NORO/ZEPHR) recomputed by this
                # replay, or external intelligence the caller chose to drop.
                # Either way it is read, not applied.
                self.stats.events_skipped += 1
                return True
            # Exogenous, so no watermark gates it: the input-visibility
            # watermark counts market inputs TIDAL had APPLIED, and an
            # intelligence opinion is not one of those. Applying it here, at
            # the position it was recorded in, preserves the only ordering
            # that matters economically -- whether it reached the orchestrator
            # before or after a given tick marker.
            await self._apply_and_queue(event)
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

    async def run(self, max_items: int | None = None) -> ReplayStats:
        """Replay to the end, or until ``max_items`` logical items are read.

        A logical item is exactly what ``step()`` returns: one applied input,
        or one ``ORCHESTRATOR_TICK`` marker. Markers count -- they are how the
        original run's decision cadence is expressed, so a limit that ignored
        them would mean something different depending on how much market data
        happened to sit between ticks.

        Pacing is NOT done here. REALTIME delays happen inside
        ``_advance_replay_clock``, at the moments logical time actually moves,
        which is not the same as once per item returned (P2-3).
        """
        items = 0
        while True:
            if max_items is not None and items >= max_items:
                break
            event = await self.step()
            if event is None:
                break
            items += 1
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


#: Settings deliberately EXCLUDED from the configuration digest, because they
#: cannot change what a replay computes from identical market data.
#:
#: This list is the difference between a check that works and one that gets
#: reflexively overridden. The digest gates exact replay (P2-9): a mismatch
#: refuses the run. Digesting the whole settings object means moving the
#: database file, or lowering the log level, counts as "the configuration
#: materially changed" -- so every operator replaying a session recorded on
#: another machine reaches for the override, and the moment the override is
#: habitual the gate has stopped protecting anything.
#:
#: Each entry is here because it is addressed to the OUTSIDE of the
#: computation -- where events are stored, where logs go, where the dashboard
#: listens -- and is read by no agent, strategy, risk gate or execution
#: simulator. ``tests/contract/test_replay_config_reproducibility.py`` proves
#: that claim the only way it can be proved: by replaying the same recording
#: under changed values and asserting the economics are identical.
#:
#: Anything that touches prices, sizes, fees, latency, thresholds, weights,
#: seeds, symbols, venues or balances is NOT here and never should be.
#: ``bus`` stays material despite the Batch 1.2 ordering parity work -- an
#: unproven exclusion is worth less than a false positive.
DIGEST_EXCLUDED_PATHS: frozenset[tuple[str, ...]] = frozenset(
    {
        ("environment",),  # a deployment label
        ("log_level",),  # how much is logged
        ("log_format",),  # json vs text
        # WHERE events live and the credential to reach it -- not what they
        # contain. ``storage.backend`` is deliberately NOT excluded: the
        # choice of store is the one part of this section with a conceivable
        # behavioural difference (a failing recorder degrades storage health,
        # which the kill switch reads), and an unproven exclusion is worth
        # less than a false positive.
        ("storage", "sqlite_path"),
        ("storage", "postgres_dsn"),
        ("redis_url",),  # which server, not which bus semantics
        ("api_host",),  # the dashboard's socket
        ("api_port",),
    }
)


def _material(payload: dict, prefix: tuple[str, ...] = ()) -> dict:
    """Drop the excluded paths, keeping everything replay actually depends on."""
    return {
        key: (
            _material(value, (*prefix, key))
            if isinstance(value, dict)
            else value
        )
        for key, value in payload.items()
        if (*prefix, key) not in DIGEST_EXCLUDED_PATHS
    }


def config_digest(payload: dict) -> str:
    """Stable digest of the MATERIAL configuration, recorded with each session.

    "Material" means: able to change what the platform does with identical
    market data. See ``DIGEST_EXCLUDED_PATHS`` for what is left out and why.

    Pass the *model* dump (``settings.model_dump()``, not ``mode="json"``) so
    secrets arrive as ``SecretStr`` and can be hashed rather than arriving
    pre-masked and indistinguishable.
    """
    import hashlib
    import json

    return hashlib.sha256(
        json.dumps(_unmask(_material(payload)), sort_keys=True, default=str).encode()
    ).hexdigest()[:16]


async def collect(
    store: EventStore,
    session_id: str,
    types: Iterable[EventType] | None = None,
) -> list[Event]:
    """Read a session's events into a list, in deterministic order."""
    await store.open()
    return [event async for event in store.read(session_id, types=types)]
