"""Session and runtime vocabulary — what a run *is*, not what it decides.

WHY ``runtime.py`` AND NOT ``operations.py``
============================================
``core/models/ops.py`` already exists and holds system-operational and risk
objects: ``HedgeIntent``, ``DeltaReport``, ``KillSwitchState``, ``SystemEvent``,
reconciliation mismatches. An ``operations.py`` beside it, differing by three
letters and describing something else entirely, would be a name nobody could
disambiguate at a glance six months from now.

This module is the **session / runtime** layer: what a run is, how it started,
what profile it is operating under, and what it looked like while it ran. Not
one model here participates in a trading decision.

THE THREE AXES
==============
The platform has three independent concepts that are easy to conflate and
expensive to confuse:

1. **Trading mode** — ``TradingMode.PAPER``, and there is no other value. It is
   a structural property of the build, not a runtime flag: no exchange
   order-submission implementation exists to switch to.
2. **Operational profile** — :class:`OperationalProfile`. PAPER or SHADOW. What
   the run is *for*, and how closely it is being observed.
3. **Market feed** — ``simulated`` or ``live``. Where prices come from.

They are orthogonal. A PAPER profile can run against a live public feed, and a
SHADOW profile is still ``TradingMode.PAPER`` executing through
``PaperExecutor``. **Shadow is not a trading mode**, and nothing here may make
it look like one.

WHAT THIS MODULE MUST NOT DO
============================
* :class:`OperationalReadiness` reports. It gates nothing — not
  ``Platform.start()``, not ``Orchestrator.tick()``, not ``_seek``, not
  execution.
* :class:`OperationalSessionRecord` witnesses a startup or a shutdown. It never
  catches, retries or translates a failure.
* :class:`PreLiveReadinessSnapshot` describes what does not exist. It starts
  nothing, and every live-side field on it is ``NOT_IMPLEMENTED`` because that
  is the truth.

Every timestamp is supplied by the caller. Nothing here imports from ``apps/``,
``agents/`` or ``execution/``.
"""

from __future__ import annotations

from pydantic import Field

from core.models.common import Base, Millis, StrEnum, TradingMode, new_id

# ======================================================================
# the operational profile
# ======================================================================


class OperationalProfile(StrEnum):
    """What a session is for. **Not a trading mode.**

    ``TradingMode`` says what the platform is permitted to do with money, and
    it has exactly one value. This says what a particular run is *for*, and
    both values run under ``TradingMode.PAPER`` through ``PaperExecutor``.

    * ``PAPER`` — ordinary paper experimentation, against either feed.
    * ``SHADOW`` — a structured rehearsal against real public market data, with
      extra observation recorded. Same executor, same account, same absence of
      any venue-side effect.

    Putting SHADOW here rather than in ``TradingMode`` is the whole point. A
    profile cannot become a trading mode by accident, and no combination of
    these values produces live execution — there is no live execution to
    produce.
    """

    PAPER = "PAPER"
    SHADOW = "SHADOW"


class FeedKind(StrEnum):
    """Where market data comes from — the third axis.

    Mirrors the existing ``TF_FEED`` values, which are unchanged. ``LIVE`` here
    means *read-only public market data*: order books and trade prints from
    public endpoints. It carries no authentication, no private channel and no
    order path, and it is available under both profiles.
    """

    SIMULATED = "SIMULATED"
    LIVE = "LIVE"


# ======================================================================
# session lifecycle
# ======================================================================


class SessionStatus(StrEnum):
    """What became of one run.

    There is deliberately no RESUMED or RESTARTING. A session is one process
    lifetime; the platform has no resume semantics, and a value it could never
    reach would invite someone to implement one to justify it.
    """

    CREATED = "CREATED"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"
    FAILED = "FAILED"


class StartupStage(StrEnum):
    """How far startup got.

    These name the order ``Platform.start()`` and the composition root already
    work in. **Nothing dispatches on them** — no stage runner, no generic
    sequencer. Startup is not restructured to fit this enum; the enum is
    written to fit startup.
    """

    CREATED = "CREATED"
    STORAGE = "STORAGE"
    BUS = "BUS"
    MARKET_FEEDS = "MARKET_FEEDS"
    AGENTS = "AGENTS"
    EXECUTION = "EXECUTION"
    RECONCILIATION = "RECONCILIATION"
    ORCHESTRATION = "ORCHESTRATION"
    READY = "READY"


class ShutdownStage(StrEnum):
    """How far shutdown got.

    Named after what ``Platform.stop()`` and ``__main__`` already do, in the
    order they already do it. Shutdown ordering was not rewritten to match this
    vocabulary — the ordering is load-bearing (the API unwinds its own lifespan
    before anything is cancelled, and the recorder closes last so the events
    explaining a shutdown are persisted), and reordering it to make an enum
    tidy would break that.
    """

    REQUESTED = "REQUESTED"
    STOPPING_API = "STOPPING_API"
    STOPPING_LOOPS = "STOPPING_LOOPS"
    STOPPING_FEEDS = "STOPPING_FEEDS"
    DRAINING_BUS = "DRAINING_BUS"
    STOPPING_RECORDER = "STOPPING_RECORDER"
    STOPPED = "STOPPED"


class SessionManifest(Base):
    """What this session was configured to be.

    Written once, at construction, from settings that are already fixed. It is
    the answer to "what was this run?" for anyone reading a recording months
    later, and it is deliberately explicit about the three axes rather than
    leaving them to be inferred.

    ``session_id`` is **the recorder's session id**, not a second identifier.
    The recording already carries that id on every event; minting another would
    give one run two names and force every future reader to learn which is
    canonical.

    ``paper_executor``, ``private_venue_access`` and ``real_order_submission``
    state the boundary in the record itself, so a SHADOW manifest cannot be
    misread as a live one.
    """

    session_id: str
    created_at: Millis

    label: str = ""

    #: PAPER. There is no other value in this build.
    trading_mode: TradingMode = TradingMode.PAPER
    operational_profile: OperationalProfile = OperationalProfile.PAPER
    feed: FeedKind = FeedKind.SIMULATED

    symbols: list[str] = Field(default_factory=list)
    venues: list[str] = Field(default_factory=list)

    bus_backend: str = ""
    storage_backend: str = ""

    initial_paper_balance: float = 0.0
    intelligence_provider: str = ""

    #: The digest the recorder stamps on the session, so a replay can prove it
    #: is reading a run made under the same configuration.
    config_digest: str = ""

    recording_requested: bool = False

    #: True whenever execution is simulated, which in this build is always.
    paper_executor: bool = True
    #: No authenticated venue exists. Always False.
    private_venue_access: bool = False
    #: No order reaches a venue. Always False.
    real_order_submission: bool = False

    notes: list[str] = Field(default_factory=list)


class OperationalSessionRecord(Base):
    """One run, observed.

    A witness, not a supervisor. ``failure`` records that startup or shutdown
    raised; the exception itself still propagates untouched, because a record
    that swallowed a startup failure would let a half-built platform look like
    a running one.
    """

    session_id: str
    created_at: Millis
    updated_at: Millis
    started_at: Millis | None = None
    stopped_at: Millis | None = None

    status: SessionStatus = SessionStatus.CREATED

    startup_stage: StartupStage = StartupStage.CREATED
    shutdown_stage: ShutdownStage | None = None

    manifest: SessionManifest | None = None

    ticks: int = 0
    events_recorded: int = 0

    #: The exception's type and message. Never a substitute for the exception.
    failure: str = ""

    reason_codes: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)

    @property
    def is_running(self) -> bool:
        return self.status is SessionStatus.RUNNING

    @property
    def is_terminal(self) -> bool:
        return self.status in (SessionStatus.STOPPED, SessionStatus.FAILED)


# ======================================================================
# metrics, components, readiness
# ======================================================================


class OperationalMetrics(Base):
    """Counters across a session. No thresholds, no alerting.

    Nothing reads these to decide anything, and nothing here classifies a
    session. A number that triggered a behaviour would be a policy, and policy
    needs evidence this phase does not have.
    """

    sessions_started: int = 0
    sessions_completed: int = 0
    sessions_failed: int = 0

    ticks: int = 0
    events_recorded: int = 0

    paper_orders: int = 0
    paper_fills: int = 0

    opportunities: int = 0
    risk_rejections: int = 0

    hedges: int = 0
    reconciliations: int = 0
    intelligence_analyses: int = 0

    shadow_decisions: int = 0


class OperationalComponentSummary(Base):
    """One component's health, flattened for a session view.

    Every field is copied from ``HealthRegistry``'s own snapshot. **No new
    health semantics**: `status` is the component's own status value, and
    `required` mirrors the existing required-component list rather than
    introducing a second notion of what matters.
    """

    component: str
    status: str = ""
    version: str = ""
    #: Mirrors the platform's existing required-component set. LUMEN is not in
    #: it, and this phase does not add it.
    required: bool = False
    last_heartbeat_ms: Millis | None = None
    queue_depth: int = 0
    error_count: int = 0
    detail: str = ""


class OperationalReadiness(Base):
    """Whether the platform is in a fit state to operate. **Reporting only.**

    This gates nothing. ``Platform.start()`` still starts, the orchestrator's
    warm-up still decides when trading may begin, the kill switch still decides
    when it must stop, and ``_seek`` is still guarded by exactly what guarded
    it before. Substituting this for any of them would replace tested decisions
    with one untested one.

    **LUMEN's absence never makes this unready.** LUMEN is optional — it is not
    in the platform's required-component set and not in
    ``ConsensusConfig.required_agents`` — and the shipped default provider is
    permanently unavailable. A readiness model that went False because the
    default configuration is the default configuration would report a fault
    where there is none.

    ``ready`` is False whenever anything required is unestablished, including
    when nothing has been checked. Absence of evidence is not readiness.
    """

    ready: bool = False
    created_at: Millis | None = None

    profile: OperationalProfile = OperationalProfile.PAPER
    feed: FeedKind = FeedKind.SIMULATED

    #: The structural guarantee, restated per session.
    paper_mode_confirmed: bool = False

    feed_ready: bool = False
    storage_ready: bool = False
    bus_ready: bool = False
    market_ready: bool = False

    required_agents_ready: bool = False
    risk_ready: bool = False
    execution_ready: bool = False
    reconciliation_ready: bool = False
    hedging_ready: bool = False

    recording_ready: bool = False
    kill_switch_clear: bool = False

    #: Reported, never required. See the class docstring.
    intelligence_available: bool = False

    reason_codes: list[str] = Field(default_factory=list)
    detail: str = ""


# ======================================================================
# incidents and annotations
# ======================================================================


class OperationalIncidentSeverity(StrEnum):
    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


class OperationalIncident(Base):
    """Something notable that happened during a session.

    **No automatic incident policy exists.** Nothing raises one of these on its
    own, nothing escalates one, and nothing acts on one. ``HealthRegistry``
    remains the authority on component health and the kill switch remains the
    authority on halting; an incident record that could trigger either would be
    a third, untested authority beside them.
    """

    incident_id: str = Field(default_factory=lambda: new_id("incident"))
    created_at: Millis
    component: str = ""
    severity: OperationalIncidentSeverity = OperationalIncidentSeverity.INFO
    reason: str = ""
    detail: str = ""
    resolved_at: Millis | None = None

    @property
    def resolved(self) -> bool:
        return self.resolved_at is not None


class OperatorAnnotation(Base):
    """A note attached to a session, for later analysis.

    Free text with a free-text author label. **No identity system, no
    authentication, and no API write route** — building an auth model before
    there is anything to authorise would be inventing a security boundary
    nobody has specified.
    """

    created_at: Millis
    text: str = ""
    author: str = ""


# ======================================================================
# snapshots and summaries
# ======================================================================


class OperationalSnapshot(Base):
    """One serializable view of the whole running platform.

    The single aggregation surface, so a future dashboard, API or operator tool
    does not have to rummage through every component to answer "what is this
    platform doing?".

    Compact by construction: component summaries and the other phases'
    snapshots, which are themselves compact, rather than lifetime histories.
    A view carrying every order and every fill would be sized by session
    history rather than by what is currently happening.

    Each nested field is typed ``dict`` rather than the owning phase's model,
    because ``core/models`` must not import from ``apps/`` or ``agents/`` to
    reference them. The producer fills them from those phases' own snapshot
    methods; this model neither computes nor interprets them.
    """

    created_at: Millis

    session: OperationalSessionRecord | None = None
    components: list[OperationalComponentSummary] = Field(default_factory=list)

    #: Phase 8 coordination, Phase 6 execution, Phase 7 reconciliation,
    #: Phase 9 hedging, Phase 10 intelligence, Phase 5 risk — each as that
    #: phase's own snapshot, serialized. Copied, never recomputed.
    coordination: dict = Field(default_factory=dict)
    risk: dict = Field(default_factory=dict)
    execution: dict = Field(default_factory=dict)
    reconciliation: dict = Field(default_factory=dict)
    hedging: dict = Field(default_factory=dict)
    intelligence: dict = Field(default_factory=dict)
    portfolio: dict = Field(default_factory=dict)
    recording: dict = Field(default_factory=dict)

    metrics: OperationalMetrics = Field(default_factory=OperationalMetrics)
    readiness: OperationalReadiness | None = None

    incidents: list[OperationalIncident] = Field(default_factory=list)


class SessionSummary(Base):
    """What one session did, once it is over.

    Facts only. **Nothing here classifies a session** as good, bad, profitable
    enough or ready for anything. A P&L number is a measurement; deciding what
    it means about a strategy is research, and deciding what it means about
    readiness for real money is a governance question neither this model nor
    this phase may answer.
    """

    session_id: str
    profile: OperationalProfile = OperationalProfile.PAPER
    feed: FeedKind = FeedKind.SIMULATED

    started_at: Millis | None = None
    stopped_at: Millis | None = None

    ticks: int = 0
    events_recorded: int = 0

    opportunities: int = 0
    orders: int = 0
    fills: int = 0
    rejections: int = 0
    hedges: int = 0
    reconciliations: int = 0

    starting_equity: float = 0.0
    ending_equity: float = 0.0

    gross_pnl: float = 0.0
    net_pnl: float = 0.0
    fees: float = 0.0

    kill_switch_triggers: list[str] = Field(default_factory=list)


# ======================================================================
# the pre-live bridge
# ======================================================================


class LiveComponentStatus(StrEnum):
    """State of a component a live deployment would need.

    ``NOT_IMPLEMENTED`` is the honest answer for every live-side concern in
    this build, and it is a distinct value from ``ABSENT`` on purpose: nothing
    is missing by accident.
    """

    IMPLEMENTED = "IMPLEMENTED"
    #: Exists as an interface with no implementation behind it.
    SEAM_ONLY = "SEAM_ONLY"
    NOT_IMPLEMENTED = "NOT_IMPLEMENTED"
    #: Built, but never validated.
    NOT_VALIDATED = "NOT_VALIDATED"


class PreLiveReadinessSnapshot(Base):
    """What a live deployment would need, and what actually exists.

    **This starts nothing.** There is no promotion path, no flag it sets, and
    no code that reads it to permit anything. It exists so that the gap between
    "the framework is built" and "this platform could trade real money" is
    written down honestly rather than assumed away.

    Today every live-side field is ``NOT_IMPLEMENTED``, and every framework
    field is ``NOT_VALIDATED``. A framework existing is not a framework
    working, and a snapshot that reported otherwise would be the single most
    dangerous object in the repository.
    """

    created_at: Millis

    #: Built, unvalidated.
    paper_framework: LiveComponentStatus = LiveComponentStatus.NOT_VALIDATED
    shadow_framework: LiveComponentStatus = LiveComponentStatus.NOT_VALIDATED
    market_feeds: LiveComponentStatus = LiveComponentStatus.NOT_VALIDATED
    risk: LiveComponentStatus = LiveComponentStatus.NOT_VALIDATED
    paper_execution: LiveComponentStatus = LiveComponentStatus.NOT_VALIDATED
    reconciliation: LiveComponentStatus = LiveComponentStatus.NOT_VALIDATED
    hedging: LiveComponentStatus = LiveComponentStatus.NOT_VALIDATED
    recording: LiveComponentStatus = LiveComponentStatus.NOT_VALIDATED
    operator_controls: LiveComponentStatus = LiveComponentStatus.NOT_VALIDATED

    #: Does not exist. Phase 6's ``ExecutionVenueGateway`` is the seam, with
    #: nothing behind it.
    private_venue_connectivity: LiveComponentStatus = (
        LiveComponentStatus.NOT_IMPLEMENTED
    )
    live_executor: LiveComponentStatus = LiveComponentStatus.NOT_IMPLEMENTED
    credential_boundary: LiveComponentStatus = LiveComponentStatus.NOT_IMPLEMENTED
    live_reconciliation: LiveComponentStatus = LiveComponentStatus.NOT_IMPLEMENTED
    deployment_authorization: LiveComponentStatus = (
        LiveComponentStatus.NOT_IMPLEMENTED
    )

    notes: list[str] = Field(default_factory=list)

    @property
    def live_capable(self) -> bool:
        """Always False, and structurally so.

        Not a policy toggle: this build contains no live executor, no
        authenticated venue and no credential path. There is nothing to flip.
        """
        return False


__all__ = [
    "FeedKind",
    "LiveComponentStatus",
    "OperationalComponentSummary",
    "OperationalIncident",
    "OperationalIncidentSeverity",
    "OperationalMetrics",
    "OperationalProfile",
    "OperationalReadiness",
    "OperationalSessionRecord",
    "OperationalSnapshot",
    "OperatorAnnotation",
    "PreLiveReadinessSnapshot",
    "SessionManifest",
    "SessionStatus",
    "SessionSummary",
    "ShutdownStage",
    "StartupStage",
]
