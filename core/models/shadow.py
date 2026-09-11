"""Shadow vocabulary — a rehearsal against real prices, executed on paper.

WHAT SHADOW IS
==============
A shadow session runs the **entire platform** — market data, strategy, agents,
consensus, RUNE, VESKA planning, ``PaperExecutor``, OKAPI, MARIN, LUMEN where
configured — against **real public market data**, and records what happened.

It answers: *what would Octopush have done in actual market conditions?*

WHAT SHADOW IS NOT
==================
**Shadow is not a trading mode.** ``TradingMode.PAPER`` is the only mode this
build has, and a shadow session runs under it. It is an
:class:`~core.models.runtime.OperationalProfile`, which is a different axis
entirely.

**Shadow is not decision-only.** There is deliberately no "shadow executor"
that stops the lifecycle after planning. Stopping there would leave fills,
partial fills, hedging, exits, position management and MARIN untested against
live conditions — which is most of what a rehearsal is for. Shadow runs the
whole paper execution stack and records it.

**Shadow is not live.** No order reaches a venue. No authenticated endpoint is
contacted. No private channel is opened. The market data is public and
read-only, and the execution is simulated.

THE DISTINCTION THAT MATTERS MOST
=================================
An authorised ``ExecutionPlan`` in a shadow session represents **what the
platform would have submitted**, subject to a live executor that does not exist
translating it. A ``PaperFill`` represents **the simulator's estimate of what
might have filled**.

A paper fill is not a real fill. It is a model's output, produced by a fill
simulator with its own assumptions about queue position, latency and slippage,
against a book nobody actually traded into. Every record here says so, and
:class:`ShadowExecutionRecord` carries the provenance fields that make the
claim impossible to lose.

Nothing in this module imports from ``apps/``, ``agents/`` or ``execution/``,
and every timestamp is supplied by the caller.
"""

from __future__ import annotations

from pydantic import Field

from core.models.common import Base, Millis, Side, StrEnum, new_id

# ======================================================================
# provenance — stated, not implied
# ======================================================================


class ExecutionProvenance(StrEnum):
    """Where an execution record's numbers came from.

    Exists so that no reader, and no future comparison tool, can mistake a
    simulator's output for a venue's report. Only ``PAPER_SIMULATOR`` is
    reachable in this build; ``VENUE_REPORTED`` names what a future live
    executor would produce and has no producer.
    """

    #: Produced by ``FillSimulator`` / ``PaperExecutor``. Hypothetical.
    PAPER_SIMULATOR = "PAPER_SIMULATOR"
    #: Reported by an authenticated venue. **Nothing produces this.**
    VENUE_REPORTED = "VENUE_REPORTED"


class MarketDataProvenance(StrEnum):
    """Where the prices behind a shadow record came from.

    ``PUBLIC_LIVE_FEED`` is the intended source for a shadow session and is
    read-only market data — books and trade prints from public endpoints, with
    no authentication and no order path. ``SIMULATED`` is structurally possible
    (the profile does not force the feed) and readiness reports it as not a
    genuine live-market rehearsal.
    """

    PUBLIC_LIVE_FEED = "PUBLIC_LIVE_FEED"
    SIMULATED = "SIMULATED"
    UNKNOWN = "UNKNOWN"


# ======================================================================
# the rehearsal lifecycle
# ======================================================================


class ShadowDecisionStatus(StrEnum):
    """How far one opportunity got through the rehearsal.

    **This records the lifecycle. It does not control it.** Every transition
    named here is driven by the platform's existing state machine — the
    orchestrator's ``StrategyState``, RUNE's verdict, VESKA's plan status,
    ``PaperOrder``'s status — and the shadow registry copies the outcome down.
    No branch anywhere reads this to decide what happens next.

    ``UNKNOWN`` carries the meaning it carries everywhere else in this
    platform: some part of the venue-side truth is unresolved, it is not
    terminal, and it is never assumed to mean failure.
    """

    OBSERVED = "OBSERVED"
    CONSENSUS_RECORDED = "CONSENSUS_RECORDED"
    RISK_REJECTED = "RISK_REJECTED"
    REJECTED = "REJECTED"
    AUTHORIZED = "AUTHORIZED"
    PLANNED = "PLANNED"
    PAPER_WORKING = "PAPER_WORKING"
    PAPER_COMPLETE = "PAPER_COMPLETE"
    CLOSED = "CLOSED"
    UNKNOWN = "UNKNOWN"
    FAILED = "FAILED"


#: States from which nothing further happens on its own. ``UNKNOWN`` is
#: deliberately absent, as it is from every other terminal set in this
#: repository.
SHADOW_TERMINAL_STATUSES: frozenset[ShadowDecisionStatus] = frozenset(
    {
        ShadowDecisionStatus.RISK_REJECTED,
        ShadowDecisionStatus.REJECTED,
        ShadowDecisionStatus.PAPER_COMPLETE,
        ShadowDecisionStatus.CLOSED,
        ShadowDecisionStatus.FAILED,
    }
)


class ShadowDecisionRecord(Base):
    """One opportunity, rehearsed end to end.

    **Identifiers, not copies.** Phase 8's ``CoordinationRegistry`` owns the
    consensus evaluation, Phase 6's ``ExecutionRegistry`` owns the plan, the
    OMS owns the orders. This record links them, using the identities those
    layers already mint — ``opportunity_id``, ``correlation_id``,
    ``intent_id``, ``plan_id``, ``client_order_id`` — rather than inventing
    parallel names for things already named.

    ``shadow_decision_id`` is the one new identifier, and only because the
    registry needs its own key.
    """

    shadow_decision_id: str = Field(default_factory=lambda: new_id("sdec"))

    created_at: Millis
    updated_at: Millis
    terminal_at: Millis | None = None

    #: Existing platform identities. Never re-minted.
    opportunity_id: str
    correlation_id: str | None = None

    strategy: str = ""
    symbol: str = ""

    #: Into Phase 8's registry. The consensus itself is not copied.
    consensus_evaluation_id: str | None = None
    consensus_request_ids: list[str] = Field(default_factory=list)

    intent_id: str | None = None
    risk_decision_id: str | None = None

    #: What RUNE answered. Copied from the decision, never re-derived.
    approved: bool | None = None
    requested_notional: float = 0.0
    approved_notional: float = 0.0

    #: Into Phase 6's registry and the OMS.
    execution_plan_ids: list[str] = Field(default_factory=list)
    paper_order_ids: list[str] = Field(default_factory=list)

    #: Into Phase 9's registry, where a residual from this trade was hedged.
    hedge_ids: list[str] = Field(default_factory=list)
    #: Into Phase 7's registry, attached by an explicit caller.
    reconciliation_run_ids: list[str] = Field(default_factory=list)

    status: ShadowDecisionStatus = ShadowDecisionStatus.OBSERVED

    #: Where the prices behind this decision came from.
    market_data: MarketDataProvenance = MarketDataProvenance.UNKNOWN

    reason_codes: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)

    @property
    def is_terminal(self) -> bool:
        return self.status in SHADOW_TERMINAL_STATUSES

    @property
    def is_active(self) -> bool:
        """Currently known to be working. UNKNOWN is **not** active."""
        return self.status in (
            ShadowDecisionStatus.CONSENSUS_RECORDED,
            ShadowDecisionStatus.AUTHORIZED,
            ShadowDecisionStatus.PLANNED,
            ShadowDecisionStatus.PAPER_WORKING,
        )

    @property
    def is_unknown(self) -> bool:
        return self.status is ShadowDecisionStatus.UNKNOWN


class ShadowOrderSummary(Base):
    """One paper order behind a shadow decision, compacted.

    Copied from the OMS's own view. ``provenance`` is stated on every one of
    these: these quantities and prices are a simulator's, not a venue's.
    """

    client_order_id: str
    venue: str = ""
    symbol: str = ""
    side: Side | None = None
    status: str = ""
    quantity: float = 0.0
    filled_quantity: float = 0.0
    average_price: float | None = None
    fees_paid: float = 0.0
    #: Always PAPER_SIMULATOR in this build.
    provenance: ExecutionProvenance = ExecutionProvenance.PAPER_SIMULATOR


class ShadowExecutionRecord(Base):
    """What the paper stack did with one authorised plan.

    **No separate fill simulation exists.** Every number here is copied from
    what ``PaperExecutor`` and ``PaperAccount`` produced; the shadow layer runs
    no simulator of its own, because a second simulator could disagree with the
    one whose P&L the account actually carries.

    ``execution_provenance`` is ``PAPER_SIMULATOR``, always, and it is a field
    rather than a comment so that a future comparison tool cannot read these
    fills as venue fills by omission.
    """

    shadow_execution_id: str = Field(default_factory=lambda: new_id("sexec"))

    decision_id: str
    plan_id: str | None = None

    created_at: Millis
    updated_at: Millis
    terminal_at: Millis | None = None

    orders: list[ShadowOrderSummary] = Field(default_factory=list)
    paper_fill_ids: list[str] = Field(default_factory=list)

    planned_notional: float = 0.0
    #: What the simulator filled. **Not what a venue filled.**
    paper_filled_notional: float = 0.0
    paper_fees: float = 0.0
    paper_slippage_bps: float = 0.0

    status: ShadowDecisionStatus = ShadowDecisionStatus.PLANNED

    #: Always PAPER_SIMULATOR. See the class docstring.
    execution_provenance: ExecutionProvenance = ExecutionProvenance.PAPER_SIMULATOR
    market_data: MarketDataProvenance = MarketDataProvenance.UNKNOWN

    @property
    def fill_ratio(self) -> float | None:
        """Simulated filled notional over planned. ``None`` when nothing was planned."""
        if self.planned_notional <= 0:
            return None
        return self.paper_filled_notional / self.planned_notional


# ======================================================================
# market follow-through
# ======================================================================


class ShadowMarketCheckpoint(Base):
    """The market as it stood at one moment, against one decision.

    A copy of observable state at a caller-supplied instant. **No horizon
    policy exists**: `horizon_ms` records what the caller was measuring toward,
    and nothing schedules, times or triggers a capture.

    No default horizons are baked in. One second, five, thirty, a minute — all
    of them are common, and picking one here would smuggle a research decision
    into a construction phase. Later validation chooses what is useful.
    """

    checkpoint_id: str = Field(default_factory=lambda: new_id("schk"))
    created_at: Millis

    decision_id: str
    symbol: str = ""

    reference_price: float | None = None
    #: Per-venue top of book at capture: ``"VENUE:SYMBOL" -> (bid, ask)`` as a
    #: pair of floats, flattened so the checkpoint stays serializable and small.
    venue_touches: dict[str, list[float]] = Field(default_factory=dict)

    source_data_timestamp: Millis | None = None
    #: What the caller intends to measure toward. Nothing acts on it.
    horizon_ms: int | None = None

    market_data: MarketDataProvenance = MarketDataProvenance.UNKNOWN


class ShadowOutcomeCheckpoint(Base):
    """Where the market went, and what the paper position was worth.

    **Nothing here says whether an outcome was good.** There is no pass, no
    fail, no classification and no score. A price moved and a simulated
    position had a value; what that means about the decision is research, and
    research needs a hypothesis this phase has not got.

    The P&L figures are the paper account's, which is the one source of
    simulated P&L truth in the platform.
    """

    decision_id: str
    created_at: Millis

    horizon_ms: int | None = None

    reference_price: float | None = None
    current_price: float | None = None
    gross_move_bps: float | None = None

    paper_position_notional: float = 0.0
    paper_unrealized_pnl: float = 0.0
    paper_realized_pnl: float = 0.0

    market_data: MarketDataProvenance = MarketDataProvenance.UNKNOWN


# ======================================================================
# comparison
# ======================================================================


class ShadowExecutionComparison(Base):
    """What the platform expected against what the simulator produced.

    Both sides of this comparison are the platform's own: the expectation came
    from the intent and the plan, the outcome from ``PaperExecutor``. It is
    **not** a comparison against real venue fills, because no private venue
    truth exists in this build and inventing one side of the comparison would
    make the whole thing meaningless.

    What it can honestly show is internal consistency — whether the cost model
    and the fill simulator agree with each other.
    """

    decision_id: str
    plan_id: str | None = None
    created_at: Millis | None = None

    expected_price: float | None = None
    paper_average_fill_price: float | None = None

    expected_fee: float = 0.0
    paper_fee: float = 0.0

    expected_slippage_bps: float = 0.0
    paper_slippage_bps: float = 0.0

    fill_ratio: float | None = None

    #: Both sides are the platform's. Stated so the comparison is not read as
    #: validation against a venue.
    outcome_provenance: ExecutionProvenance = ExecutionProvenance.PAPER_SIMULATOR


class ShadowVsVenueExecutionComparison(Base):
    """A shape for a comparison that cannot yet be made.

    **Nothing populates one of these**, and nothing can: it needs a venue's
    report of what actually filled, which requires an authenticated executor
    this build does not have and this phase may not add.

    It is written down so that when a live executor eventually exists, the
    question "how close was the simulator?" has a defined shape rather than
    being invented under pressure — and so that the absence of the venue side
    is visible rather than assumed.
    """

    decision_id: str
    plan_id: str | None = None

    #: From ``PaperExecutor``. Available today.
    paper_average_fill_price: float | None = None
    paper_filled_quantity: float = 0.0
    paper_fee: float = 0.0
    paper_slippage_bps: float = 0.0

    #: From an authenticated venue. **Always None. Nothing produces these.**
    venue_average_fill_price: float | None = None
    venue_filled_quantity: float | None = None
    venue_fee: float | None = None
    venue_slippage_bps: float | None = None

    #: False, always, in this build.
    venue_truth_available: bool = False


# ======================================================================
# readiness and the snapshot
# ======================================================================


class ShadowReadiness(Base):
    """Whether a shadow rehearsal is set up the way one should be.

    **Observational. It blocks nothing** — not startup, not a tick, not
    execution. A shadow session with a misconfigured feed still runs; it is
    simply not the rehearsal the operator probably wanted, and this says so
    instead of failing at configuration load where it would be a policy.

    ``private_execution_absent`` is the field worth reading twice. It reports
    **True** when only ``PaperExecutor`` exists, which is the safe state and
    the only state this build has. It is phrased as an absence on purpose: a
    field named ``live_execution_available`` would be a place for someone to
    later set True, and there must be no such place.
    """

    ready: bool = False
    created_at: Millis | None = None

    profile_is_shadow: bool = False
    paper_mode_confirmed: bool = False

    #: The intended configuration: read-only public market data.
    public_live_feed_configured: bool = False
    market_data_available: bool = False

    recording_active: bool = False

    coordination_available: bool = False
    risk_available: bool = False
    paper_execution_available: bool = False
    reconciliation_available: bool = False
    hedging_available: bool = False
    observer_healthy: bool = True

    #: True when no authenticated executor exists — the safe state, and the
    #: only one this build has.
    private_execution_absent: bool = True

    reason_codes: list[str] = Field(default_factory=list)
    detail: str = ""


class ShadowSnapshot(Base):
    """One serializable view of the rehearsal.

    Counts and **ids**, not records. A snapshot embedding every decision with
    its orders and checkpoints would be sized by session history rather than by
    what is currently in flight.

    ``current_paper_equity`` and ``current_paper_pnl`` come from the one
    ``PaperAccount`` the platform has. There is no second shadow ledger — one
    source of simulated P&L truth, or the two would eventually disagree and
    nobody would know which to believe.
    """

    created_at: Millis

    #: The recorder's session id. Not a second identifier.
    session_id: str | None = None
    #: False under the PAPER profile, where the observer records nothing.
    enabled: bool = False

    #: Lifetime decisions observed. Compaction never decreases this.
    decisions_total: int = 0
    #: Decisions still resident in the in-memory registry.
    resident_decisions: int = 0
    authorized: int = 0
    rejected: int = 0

    paper_plans: int = 0
    paper_orders: int = 0
    paper_fills: int = 0

    active_decision_ids: list[str] = Field(default_factory=list)
    unknown_decision_ids: list[str] = Field(default_factory=list)

    #: From the platform's single ``PaperAccount``. Simulated, by definition.
    current_paper_equity: float = 0.0
    current_paper_pnl: float = 0.0

    market_checkpoints: int = 0
    outcome_checkpoints: int = 0

    observer_events_seen: int = 0
    observer_intentionally_ignored: int = 0
    observer_unattributable: int = 0
    observer_failures: int = 0

    market_data: MarketDataProvenance = MarketDataProvenance.UNKNOWN
    execution_provenance: ExecutionProvenance = ExecutionProvenance.PAPER_SIMULATOR

    readiness: ShadowReadiness | None = None


__all__ = [
    "SHADOW_TERMINAL_STATUSES",
    "ExecutionProvenance",
    "MarketDataProvenance",
    "ShadowDecisionRecord",
    "ShadowDecisionStatus",
    "ShadowExecutionComparison",
    "ShadowExecutionRecord",
    "ShadowMarketCheckpoint",
    "ShadowOrderSummary",
    "ShadowOutcomeCheckpoint",
    "ShadowReadiness",
    "ShadowSnapshot",
    "ShadowVsVenueExecutionComparison",
]
