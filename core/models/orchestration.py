"""Orchestration vocabulary — coordination, not decision.

WHAT THIS MODULE IS FOR
=======================
The platform already decides. It detects opportunities, gathers opinions,
combines them, sizes trades, checks risk, executes and reconciles. What it has
never had is a way to *say what it did* — which tick, which phase, which agents
were expected, who answered, which consensus produced which intent, which intent
produced which decision, which decision produced which plan.

Every model here is that record. Not one of them is consulted to make a choice.

THE LINE THIS MODULE MUST NOT CROSS
==================================
A coordination registry that starts influencing decisions stops being a record
and becomes a second, untested decision authority — one nobody knows is there.
So:

* :class:`ConsensusEvaluationRecord` **copies** score and agreement from
  ``ConsensusResult``. It never recomputes them. If it did, the platform would
  have two consensus engines that could disagree, and the one nobody tested
  would eventually win an argument.
* Thresholds appear on the record so a reader can see what the decision was
  measured against. They are carried, never applied.
* :class:`CoordinationReadiness` reports. It gates nothing. Warm-up, entry and
  execution are decided exactly where they were decided before.
* :class:`OrchestrationControlRequest` names an operator action. Nothing
  executes one.

NO CLOCK, NO IMPORTS OUTWARD
============================
Every timestamp is supplied by the caller. Nothing here imports from ``apps/``,
``agents/`` or ``execution/`` — a core model that reached into a component
would make the record depend on the thing it is supposed to observe.
"""

from __future__ import annotations

from pydantic import Field

from core.models.agent import AgentContribution, ConsensusResult
from core.models.common import AgentId, Base, DataQuality, Millis, StrEnum, new_id
from core.models.opportunity import StrategyState

# ======================================================================
# tick lifecycle
# ======================================================================


class OrchestrationPhase(StrEnum):
    """The phases of one tick, in the order the orchestrator runs them.

    This is the platform's existing canonical sequence, written down rather
    than invented: observe the market, settle what execution has done, measure
    the portfolio, protect it, manage open positions, and only then seek new
    ones. Protection precedes management precedes seeking on purpose — a
    platform that looked for new trades before checking whether it should still
    be trading would be deciding in the wrong order.

    Nothing dispatches on this enum. The orchestrator's control flow is
    unchanged; these values name what it is already doing.
    """

    #: Outside a tick.
    IDLE = "IDLE"
    OBSERVE = "OBSERVE"
    SETTLE = "SETTLE"
    MEASURE = "MEASURE"
    PROTECT = "PROTECT"
    MANAGE = "MANAGE"
    SEEK = "SEEK"


#: The working phases, in execution order. ``IDLE`` is deliberately absent: it
#: is the absence of a phase, not one of them.
TICK_PHASE_ORDER: tuple[OrchestrationPhase, ...] = (
    OrchestrationPhase.OBSERVE,
    OrchestrationPhase.SETTLE,
    OrchestrationPhase.MEASURE,
    OrchestrationPhase.PROTECT,
    OrchestrationPhase.MANAGE,
    OrchestrationPhase.SEEK,
)


class OrchestrationTickStatus(StrEnum):
    """What became of one tick.

    There is no CANCELLED: the platform has no concept of cancelling a tick in
    flight, and adding a value the system cannot reach would invite someone to
    implement a behaviour to justify it.
    """

    CREATED = "CREATED"
    RUNNING = "RUNNING"
    COMPLETE = "COMPLETE"
    FAILED = "FAILED"


class OrchestrationPhaseRecord(Base):
    """When one phase of one tick started and finished.

    Logical instants supplied by the caller, not wall-clock durations. A phase
    that took 40ms of real time is a performance measurement; a phase that ran
    at tick instant T is what replay has to reproduce.
    """

    phase: OrchestrationPhase
    started_at: Millis
    completed_at: Millis | None = None
    #: False when the phase raised. The exception itself is never caught here.
    ok: bool = True
    detail: str = ""

    @property
    def finished(self) -> bool:
        return self.completed_at is not None


class OrchestrationTickRecord(Base):
    """One tick, observed.

    Nothing reads this to decide how the tick proceeds. Its whole purpose is to
    make the question "what happened in tick 4,271?" answerable afterwards
    without reconstructing it from logs.

    Every timestamp is the tick's own logical instant — the one
    ``Orchestrator.tick_time`` fixes at the market-snapshot boundary — so a
    replayed tick records the instants the original recorded.
    """

    tick_id: str = Field(default_factory=lambda: new_id("tick"))
    #: The orchestrator's own monotonic tick counter.
    tick_number: int = 0

    created_at: Millis
    updated_at: Millis
    completed_at: Millis | None = None

    status: OrchestrationTickStatus = OrchestrationTickStatus.CREATED
    current_phase: OrchestrationPhase = OrchestrationPhase.IDLE
    phases: list[OrchestrationPhaseRecord] = Field(default_factory=list)

    #: The instant of the market snapshot this tick reasoned about, and the
    #: oldest venue observation behind it.
    market_timestamp: Millis | None = None
    source_data_timestamp: Millis | None = None

    #: Whether this tick ran during warm-up, when seeking is suppressed.
    warming_up: bool = False

    opportunities_seen: int = 0
    opportunities_created: int = 0
    consensus_requests: int = 0
    risk_evaluations: int = 0
    execution_plans: int = 0

    #: Set when the tick raised. The exception still propagates; this is a note
    #: about it, never a substitute for it.
    error: str = ""

    notes: list[str] = Field(default_factory=list)

    @property
    def is_terminal(self) -> bool:
        return self.status in (
            OrchestrationTickStatus.COMPLETE,
            OrchestrationTickStatus.FAILED,
        )

    def phase_record(
        self, phase: OrchestrationPhase
    ) -> OrchestrationPhaseRecord | None:
        for record in self.phases:
            if record.phase is phase:
                return record
        return None


# ======================================================================
# consensus coordination
# ======================================================================


class ConsensusPurpose(StrEnum):
    """Why consensus was sought.

    The platform already asks two different questions of the same machinery,
    against two different thresholds: *should we open this?* and *should we
    stay in it?*. Until now the difference lived only in which method the
    caller happened to call. This names it, so a record of a consensus can say
    which question it answered.

    Neither threshold moves. ENTRY still uses ``entry_threshold`` and
    CONTINUATION still uses ``exit_threshold``, exactly as before.
    """

    ENTRY = "ENTRY"
    CONTINUATION = "CONTINUATION"


class ConsensusRequestStatus(StrEnum):
    """Lifecycle of the *coordination* around one consensus.

    Distinct from ``ConsensusResult.complete``, which is unchanged and stays
    the answer to "did every required agent supply a usable opinion?". This is
    the state of the request: created, waiting on responders, ready to decide,
    or out of time.

    TIMED_OUT is not an error. A missing agent is an expected outcome the
    consensus engine already models, and the platform decides with what
    arrived rather than raising.
    """

    CREATED = "CREATED"
    WAITING = "WAITING"
    READY = "READY"
    TIMED_OUT = "TIMED_OUT"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class OpinionReference(Base):
    """A compact reference to the opinion a consensus actually used.

    Not the opinion itself. A record holding live ``AgentOpinion`` objects
    would keep a mutable view of state that has since moved on, and a record of
    what was used has to describe the moment it was used.

    Enough to answer "what did TIDAL say, and was it fresh?" without retaining
    a ``SystemState`` reference.
    """

    agent_id: AgentId
    #: What the opinion was about: an opportunity id, or a symbol.
    subject: str = ""
    created_at: Millis | None = None
    expires_at: Millis | None = None
    quality: DataQuality | None = None
    signal: float = 0.0
    confidence: float = 0.0
    #: The agent answered and cast no directional vote. Distinct from missing.
    abstain: bool = False
    model_version: str = ""


class ConsensusRequestRecord(Base):
    """One request for agent opinions, and what came back.

    The coordination record around a ``ResponseBarrier`` cycle. The barrier
    itself is short-lived — it forgets a correlation id as soon as the wait
    resolves, which is correct for a synchronisation primitive and useless for
    history. This record is what survives.

    No waiting logic lives here. The barrier decides who answered; this
    records the answer.
    """

    request_id: str = Field(default_factory=lambda: new_id("creq"))
    correlation_id: str
    purpose: ConsensusPurpose = ConsensusPurpose.ENTRY

    symbol: str = ""
    strategy: str = ""

    created_at: Millis
    updated_at: Millis
    #: The instant after which the platform stops waiting.
    deadline_ms: Millis | None = None

    required_agents: list[AgentId] = Field(default_factory=list)
    responded_agents: list[AgentId] = Field(default_factory=list)
    missing_agents: list[AgentId] = Field(default_factory=list)

    timed_out: bool = False
    waited_ms: int = 0

    status: ConsensusRequestStatus = ConsensusRequestStatus.CREATED

    #: The evaluation this request produced, once one exists.
    consensus_evaluation_id: str | None = None

    @property
    def complete(self) -> bool:
        return not self.missing_agents


class ConsensusEvaluationRecord(Base):
    """What one consensus computation used, and what it produced.

    **Observability. The authority is** ``ConsensusEngine``, and its
    ``ConsensusResult`` is the decision. Every number here is copied from that
    result — nothing is recomputed, because a record that recalculated the
    score would be a second consensus engine, and two engines that can disagree
    are worse than one that might be wrong.

    The thresholds are carried so a reader can see what the agreement was
    measured against. They are not applied here.
    """

    evaluation_id: str = Field(default_factory=lambda: new_id("ceval"))
    request_id: str | None = None
    correlation_id: str | None = None

    purpose: ConsensusPurpose = ConsensusPurpose.ENTRY
    created_at: Millis

    symbol: str = ""
    strategy: str = ""

    #: What each participating agent supplied.
    opinion_refs: list[OpinionReference] = Field(default_factory=list)
    #: The engine's own per-agent attribution, copied verbatim.
    contributions: list[AgentContribution] = Field(default_factory=list)

    required_agents: list[AgentId] = Field(default_factory=list)
    missing_agents: list[AgentId] = Field(default_factory=list)
    degraded_agents: list[AgentId] = Field(default_factory=list)
    abstained_agents: list[AgentId] = Field(default_factory=list)

    #: Copied from ``ConsensusResult.complete``.
    complete: bool = False
    #: Copied from ``ConsensusResult``. Never recomputed.
    score: float = 0.0
    agreement: float = 0.0

    #: Carried for context, never applied here.
    entry_threshold: float | None = None
    continuation_threshold: float | None = None

    #: What the engine's own ``entry_allowed`` / ``continuation_allowed``
    #: answered, where the caller recorded it. ``None`` means not recorded —
    #: deliberately not inferred from the numbers above, because inferring it
    #: would be re-deciding.
    allowed: bool | None = None

    @property
    def threshold_for_purpose(self) -> float | None:
        if self.purpose is ConsensusPurpose.ENTRY:
            return self.entry_threshold
        return self.continuation_threshold


def consensus_evaluation_from_result(
    result: ConsensusResult,
    *,
    purpose: ConsensusPurpose,
    now_ms: Millis,
    request_id: str | None = None,
    required_agents: list[AgentId] | None = None,
    opinion_refs: list[OpinionReference] | None = None,
    entry_threshold: float | None = None,
    continuation_threshold: float | None = None,
    allowed: bool | None = None,
) -> ConsensusEvaluationRecord:
    """Adapt a decided ``ConsensusResult`` into an observability record.

    Pure, and strictly a copy in one direction. ``score``, ``agreement``,
    ``complete``, the missing/degraded/abstained sets and the contributions all
    come straight off the result. **Nothing is recalculated**, which is what
    keeps the registry from becoming a second consensus engine.

    ``allowed`` is passed in by whoever called ``entry_allowed`` or
    ``continuation_allowed`` rather than derived from ``agreement`` and a
    threshold. Deriving it would be re-deciding, and a record that re-decides
    can disagree with the decision it is recording.
    """
    return ConsensusEvaluationRecord(
        request_id=request_id,
        correlation_id=result.correlation_id,
        purpose=purpose,
        created_at=now_ms,
        symbol=result.symbol,
        strategy=result.strategy,
        opinion_refs=list(opinion_refs or []),
        contributions=list(result.contributions),
        required_agents=list(required_agents or []),
        missing_agents=list(result.missing_agents),
        degraded_agents=list(result.degraded_agents),
        abstained_agents=list(result.abstained_agents),
        complete=result.complete,
        score=result.score,
        agreement=result.agreement,
        entry_threshold=entry_threshold,
        continuation_threshold=continuation_threshold,
        allowed=allowed,
    )


class BarrierSnapshot(Base):
    """One pending response barrier, observed without disturbing it.

    ``ResponseBarrier`` is deliberately short-lived: it forgets a correlation
    id the moment its wait resolves. That is right for a synchronisation
    primitive and leaves nothing to inspect while a wait is in flight. This is
    the read-only view.

    Reading a snapshot changes nothing — it does not register, record, forget
    or complete anything.
    """

    correlation_id: str
    required: list[AgentId] = Field(default_factory=list)
    responded: list[AgentId] = Field(default_factory=list)
    missing: list[AgentId] = Field(default_factory=list)
    complete: bool = False
    #: Supplied by the caller, never read from a clock.
    captured_at: Millis | None = None


# ======================================================================
# agent directory
# ======================================================================


class AgentSubjectScope(StrEnum):
    """What a participant's opinions are about.

    Metadata. Nothing routes, schedules or requires an agent based on this —
    the strategy decides what it asks for, and ``ConsensusConfig`` decides who
    is required.
    """

    #: An opinion per opportunity, correlated to it.
    OPPORTUNITY = "OPPORTUNITY"
    #: An opinion per symbol, independent of any one opportunity.
    SYMBOL = "SYMBOL"
    #: One opinion about the platform or the market as a whole.
    GLOBAL = "GLOBAL"
    #: Not established. Better than a wrong guess.
    UNSPECIFIED = "UNSPECIFIED"


class AgentCadence(StrEnum):
    """How often a participant is expected to produce something.

    Descriptive only. No scheduling behaviour reads this; the platform's loops
    are unchanged. It exists so that a slow intelligence agent and a per-tick
    analytical agent are distinguishable in a directory, which matters as soon
    as any of them runs somewhere else.
    """

    #: Answers within the tick that asked.
    FAST = "FAST"
    #: Runs on its own longer loop and publishes when it has something.
    SLOW = "SLOW"
    #: Produces only in response to a specific event.
    EVENT_DRIVEN = "EVENT_DRIVEN"
    UNSPECIFIED = "UNSPECIFIED"


class AgentDescriptor(Base):
    """What the platform knows about one participant.

    Metadata only: no callable, no address, no credential, no transport. A
    descriptor cannot be used to invoke anything, which is deliberate — the
    directory answers "what participants does this platform know about?", and
    nothing else.

    ``required_by_default`` records what the shipped configuration says. It is
    **not** the authority: ``ConsensusConfig.required_agents`` decides who is
    required, and the directory never overrides it.
    """

    agent_id: AgentId
    service: str = ""
    version: str = ""
    scope: AgentSubjectScope = AgentSubjectScope.UNSPECIFIED
    cadence: AgentCadence = AgentCadence.UNSPECIFIED
    #: Mirrors ``ConsensusConfig.required_agents`` at registration time.
    required_by_default: bool = False
    #: The configured consensus weight, mirrored for display.
    weight: float | None = None
    description: str = ""


class AgentEndpointDescriptor(Base):
    """Where a future remote participant would live.

    **Nothing constructs or uses one of these.** It is a note about a shape,
    not a transport: the ``EventBus`` is already the platform's message
    transport, and adding a second one would give the system two ways for an
    opinion to arrive and two sets of ordering guarantees to reconcile.

    A future distributed agent publishes ``AgentOpinion`` onto the same bus, is
    tracked by the same ``ResponseBarrier``, and is combined by the same
    ``ConsensusEngine``. All this would record is which process it happens to
    run in.
    """

    agent_id: AgentId
    #: e.g. "in-process", "bus". Deliberately not an enum: nothing consumes it.
    transport: str = "in-process"
    location: str = ""
    detail: str = ""


class AgentDirectorySnapshot(Base):
    """The directory, captured for display or inspection."""

    created_at: Millis
    agents: list[AgentDescriptor] = Field(default_factory=list)
    #: Mirrors ``ConsensusConfig.required_agents``, which remains authoritative.
    required_agents: list[AgentId] = Field(default_factory=list)


# ======================================================================
# decision traceability
# ======================================================================


class DecisionTrace(Base):
    """The causal spine of one opportunity, in identifiers.

    Answers the question the platform could not previously answer without
    grepping logs: *which consensus produced this intent, which intent produced
    this risk decision, which decision produced this plan, and which orders
    came out the other end?*

    **Identifiers, not copies.** Phase 6's ``ExecutionRegistry`` owns plan
    truth and Phase 7's ``ReconciliationRegistry`` owns run truth; duplicating
    either here would create a second version that could drift from the first.
    A trace links, and the registries answer.

    One trace per opportunity. That is the unit the platform's whole lifecycle
    is organised around, and any other key would need translating at every
    step.
    """

    trace_id: str = Field(default_factory=lambda: new_id("trace"))

    opportunity_id: str
    correlation_id: str | None = None
    strategy: str = ""
    symbol: str = ""

    created_at: Millis
    updated_at: Millis

    #: Every consensus computed for this opportunity, entry and continuation.
    consensus_evaluation_ids: list[str] = Field(default_factory=list)
    consensus_request_ids: list[str] = Field(default_factory=list)

    intent_id: str | None = None
    risk_decision_id: str | None = None

    #: References into Phase 6's registry.
    execution_plan_ids: list[str] = Field(default_factory=list)
    order_ids: list[str] = Field(default_factory=list)

    #: References into Phase 7's registry. Left empty where no clean
    #: relationship exists rather than invented — reconciliation runs are
    #: periodic and platform-wide, not per-opportunity, so most traces will
    #: carry none.
    reconciliation_run_ids: list[str] = Field(default_factory=list)

    #: An attribution reference, once one exists for this opportunity.
    trade_ref: str | None = None

    #: Mirrors ``OpportunityRecord.state``; the record remains authoritative.
    state: StrategyState | None = None
    rejected_reason: str | None = None

    notes: list[str] = Field(default_factory=list)

    @property
    def is_closed(self) -> bool:
        return self.state in (StrategyState.CLOSED, StrategyState.REJECTED)


class OpportunityWorkflowSummary(Base):
    """One opportunity's current position in the whole pipeline.

    A flattened read of what already exists in ``OpportunityRecord`` and the
    registries beside it. It introduces no state machine and owns no truth —
    every field is copied from somewhere that remains authoritative.
    """

    opportunity_id: str
    strategy: str = ""
    symbol: str = ""

    strategy_state: StrategyState | None = None

    created_at: Millis | None = None
    updated_at: Millis | None = None

    trace_id: str | None = None
    intent_id: str | None = None
    risk_decision_id: str | None = None
    execution_plan_ids: list[str] = Field(default_factory=list)
    order_ids: list[str] = Field(default_factory=list)

    entry_agreement: float | None = None
    last_agreement: float | None = None

    filled_notional: float = 0.0
    fees: float = 0.0
    realized_pnl: float = 0.0

    rejected_reason: str | None = None


# ======================================================================
# readiness
# ======================================================================


class ComponentReadiness(Base):
    """Whether one component reports itself usable.

    Reporting only. Nothing gates on a ``ComponentReadiness``; the platform's
    warm-up and health paths are unchanged.
    """

    component: str
    ready: bool = False
    #: The component's own health status value, where it has one.
    status: str = ""
    required: bool = False
    reason_codes: list[str] = Field(default_factory=list)
    last_update: Millis | None = None


class CoordinationReadiness(Base):
    """Whether the platform's coordination layer says it is in a fit state.

    **OBSERVABILITY ONLY.** This model gates nothing. Warm-up still decides
    when trading may begin, the kill switch still decides when it must stop,
    and ``ConsensusEngine.complete`` still decides whether a consensus counts.
    Substituting this for any of them would replace three tested decisions with
    one untested one.

    It exists because a future shadow or live start will need exactly this
    question answered, and answering it now — without acting on it — means that
    phase adds a caller rather than a concept.

    ``ready`` is False whenever anything is unestablished, including when
    nothing has been checked. Absence of evidence is not readiness.
    """

    ready: bool = False
    created_at: Millis | None = None

    #: Mirrors the orchestrator's own warm-up state; never replaces it.
    warmup_complete: bool = False
    warmup_ticks: int = 0

    required_agents_known: bool = False
    required_agents_healthy: bool = False
    consensus_available: bool = False

    risk_available: bool = False
    execution_available: bool = False
    reconciliation_available: bool = False

    kill_switch_clear: bool = False
    open_unknown_orders: int = 0

    components: list[ComponentReadiness] = Field(default_factory=list)
    reason_codes: list[str] = Field(default_factory=list)
    detail: str = ""


class PlatformReadinessSnapshot(Base):
    """What a future shadow or live start would have to establish first.

    Framework only. ``Platform.start()`` is unchanged and blocks on none of
    this. The sequence a live platform cannot safely skip is written down here
    so the phase that implements it fills in a shape rather than inventing one
    under pressure.
    """

    created_at: Millis

    market_data_ready: bool = False
    agents_ready: bool = False
    consensus_ready: bool = False
    risk_ready: bool = False
    execution_ready: bool = False
    reconciliation_ready: bool = False
    kill_switch_ready: bool = False
    recording_ready: bool = False

    coordination: CoordinationReadiness | None = None
    reason_codes: list[str] = Field(default_factory=list)

    @property
    def all_ready(self) -> bool:
        return all(
            (
                self.market_data_ready,
                self.agents_ready,
                self.consensus_ready,
                self.risk_ready,
                self.execution_ready,
                self.reconciliation_ready,
                self.kill_switch_ready,
                self.recording_ready,
            )
        )


# ======================================================================
# control plane
# ======================================================================


class OrchestrationControlKind(StrEnum):
    """Operator actions the platform will eventually accept.

    **Vocabulary only. Nothing here is executable.** These name the control
    plane so it is distinguishable from the data plane; the only operator
    control that currently works is the existing kill-switch API, and this
    phase does not touch it.

    Naming an action is not implementing it, and deliberately so: a control
    model that could be dispatched would be an unguarded path into the trading
    loop, built before anyone decided who may use it or what it must check.
    """

    PAUSE_NEW_ENTRIES = "PAUSE_NEW_ENTRIES"
    RESUME_NEW_ENTRIES = "RESUME_NEW_ENTRIES"
    REQUEST_RECONCILIATION = "REQUEST_RECONCILIATION"
    ENGAGE_KILL_SWITCH = "ENGAGE_KILL_SWITCH"
    CLEAR_KILL_SWITCH = "CLEAR_KILL_SWITCH"
    CANCEL_ALL = "CANCEL_ALL"
    FLATTEN = "FLATTEN"


class OrchestrationControlStatus(StrEnum):
    """Where a control request stands. Nothing advances one automatically."""

    REQUESTED = "REQUESTED"
    ACCEPTED = "ACCEPTED"
    APPLYING = "APPLYING"
    APPLIED = "APPLIED"
    REJECTED = "REJECTED"
    FAILED = "FAILED"


class OrchestrationControlRequest(Base):
    """A requested operator action.

    No credential, no user model, no route. ``requested_by`` is a free-text
    label for the record, not an identity the platform authenticates — building
    an auth model before there is anything to authorise would be inventing a
    security boundary nobody has specified.
    """

    control_id: str = Field(default_factory=lambda: new_id("ctrl"))
    created_at: Millis
    kind: OrchestrationControlKind
    requested_by: str = ""
    reason: str = ""
    parameters: dict[str, str] = Field(default_factory=dict)


class OrchestrationControlRecord(Base):
    """A control request and what became of it. Nothing advances it."""

    request: OrchestrationControlRequest
    status: OrchestrationControlStatus = OrchestrationControlStatus.REQUESTED
    created_at: Millis
    updated_at: Millis
    applied_at: Millis | None = None
    detail: str = ""
    notes: list[str] = Field(default_factory=list)

    @property
    def control_id(self) -> str:
        return self.request.control_id

    @property
    def is_terminal(self) -> bool:
        return self.status in (
            OrchestrationControlStatus.APPLIED,
            OrchestrationControlStatus.REJECTED,
            OrchestrationControlStatus.FAILED,
        )


# ======================================================================
# metrics and the whole-platform snapshot
# ======================================================================


class CoordinationMetrics(Base):
    """Counters. No thresholds, no rates, no adaptive anything.

    Explicitly *not* an input to consensus: no weight, threshold or decision
    anywhere reads these. A platform that let its own hit rate move its
    consensus weights would be optimising against its own history, which is a
    strategy decision and a much later one.
    """

    ticks_started: int = 0
    ticks_completed: int = 0
    ticks_failed: int = 0

    consensus_requests: int = 0
    consensus_completed: int = 0
    consensus_timeouts: int = 0

    agent_responses: int = 0
    late_agent_responses: int = 0

    entry_consensus: int = 0
    continuation_consensus: int = 0

    entry_allowed: int = 0
    entry_blocked: int = 0
    continuation_allowed: int = 0
    continuation_blocked: int = 0

    traces_created: int = 0
    traces_closed: int = 0


class OrchestrationSnapshot(Base):
    """One serializable view of the whole coordination layer.

    Built so a future dashboard, API or operator surface can inspect tick
    state, agent state, consensus state, workflow state and readiness **without
    reading orchestrator internals**. Compact references and summaries rather
    than embedded snapshots: a view that carried every order and every fill
    would be sized by session history rather than by what is currently
    happening.

    ``created_at`` is supplied by the caller and never read from a clock.
    """

    created_at: Millis

    ticks_completed: int = 0
    current_tick_id: str | None = None
    current_tick_number: int = 0
    current_phase: OrchestrationPhase = OrchestrationPhase.IDLE

    warmed_up: bool = False
    warmup_ticks: int = 0

    agent_directory: AgentDirectorySnapshot | None = None

    #: Response barriers still waiting, and their outstanding count.
    barrier_outstanding: int = 0
    barriers: list[BarrierSnapshot] = Field(default_factory=list)

    pending_consensus_requests: list[ConsensusRequestRecord] = Field(
        default_factory=list
    )

    open_opportunities: list[OpportunityWorkflowSummary] = Field(
        default_factory=list
    )

    #: Compact references into the other phases' own truth.
    kill_switch_engaged: bool = False
    kill_switch_triggers: list[str] = Field(default_factory=list)
    execution_open_orders: int = 0
    execution_outstanding_orders: int = 0
    execution_unknown_orders: int = 0
    execution_active_plans: int = 0
    reconciliation_ok: bool | None = None
    reconciliation_open_critical: int = 0

    coordination_metrics: CoordinationMetrics = Field(
        default_factory=CoordinationMetrics
    )
    readiness: CoordinationReadiness | None = None


__all__ = [
    "TICK_PHASE_ORDER",
    "AgentCadence",
    "AgentDescriptor",
    "AgentDirectorySnapshot",
    "AgentEndpointDescriptor",
    "AgentSubjectScope",
    "BarrierSnapshot",
    "ComponentReadiness",
    "ConsensusEvaluationRecord",
    "ConsensusPurpose",
    "ConsensusRequestRecord",
    "ConsensusRequestStatus",
    "CoordinationMetrics",
    "CoordinationReadiness",
    "DecisionTrace",
    "OpinionReference",
    "OpportunityWorkflowSummary",
    "OrchestrationControlKind",
    "OrchestrationControlRecord",
    "OrchestrationControlRequest",
    "OrchestrationControlStatus",
    "OrchestrationPhase",
    "OrchestrationPhaseRecord",
    "OrchestrationSnapshot",
    "OrchestrationTickRecord",
    "OrchestrationTickStatus",
    "PlatformReadinessSnapshot",
    "consensus_evaluation_from_result",
]
