"""Hedging vocabulary — measuring a residual, not deciding what to do about it.

WHAT THIS MODULE IS FOR
=======================
OKAPI already does the economics. It knows what exposure the strategies intend
to carry, it measures what they actually carry, and when the gap exceeds
tolerance it asks for an offsetting trade. What the platform has never had is a
way to *say what happened to that request*: which residual was detected, why a
hedge was proposed, which trade intent it became, which plan worked it, which
orders came out, and whether any of them is still unresolved.

Every model here is that record.

THE LINE THIS MODULE MUST NOT CROSS
===================================
``Okapi.desired_delta`` is the economic authority for intent.
``PortfolioState.net_delta_by_symbol()`` is the economic authority for actual
exposure. ``DeltaReport`` is the authority for the difference between them. Not
one of those is recomputed here.

* :class:`HedgeTarget` mirrors ``desired_delta``. It does not replace it, and
  where the two disagree the dict is right and the mirror is stale.
* :class:`DeltaSnapshot` carries ``DeltaReport`` objects. It does not compute a
  second delta formula — a platform with two answers to "how much are we
  unhedged?" has no answer at all.
* :class:`HedgeRouteSnapshot` records which venue ``Okapi._hedge_venue``
  selected. It does not select.
* :class:`HedgeRequestRecord` links identifiers. Phase 6's registry still owns
  plan and order truth.
* :class:`OkapiReadiness` reports. RUNE still gates on
  ``Okapi.hedge_available``, unchanged.

NO CLOCK, NO IMPORTS OUTWARD
============================
Every timestamp is supplied by the caller. Nothing here imports from ``apps/``,
``agents/`` or ``execution/``.
"""

from __future__ import annotations

from pydantic import Field

from core.models.common import Base, DataQuality, Millis, Side, StrEnum, new_id
from core.models.ops import DeltaReport

# ======================================================================
# what the platform intends to carry
# ======================================================================


class HedgeTargetSource(StrEnum):
    """Who asked for this exposure target.

    Only ``STRATEGY`` is reachable today: ``wiring.py`` sets a desired delta of
    zero for every symbol because cross-venue relative value intends to carry
    no directional exposure. The other three name futures the architecture
    should not have to be reshaped to accept — a risk-driven target, a recovery
    target after reconciliation, an operator override — and none of them has a
    caller.
    """

    STRATEGY = "STRATEGY"
    RISK = "RISK"
    RECOVERY = "RECOVERY"
    OPERATOR = "OPERATOR"


class HedgeTarget(Base):
    """One symbol's intended exposure, written down.

    A mirror of an entry in ``Okapi.desired_delta``, which remains the value
    OKAPI actually measures against. Nothing here calculates a target, and
    nothing reads this to choose one.

    ``target_notional`` is a *signed* quote notional, matching the dict it
    mirrors: positive is long, negative is short, and zero — the shipped value
    for every symbol — means "carry no directional exposure", which is a real
    target rather than the absence of one.
    """

    target_id: str = Field(default_factory=lambda: new_id("htgt"))
    created_at: Millis
    updated_at: Millis

    symbol: str
    strategy: str = ""

    target_notional: float = 0.0
    source: HedgeTargetSource = HedgeTargetSource.STRATEGY

    reason_codes: list[str] = Field(default_factory=list)
    #: False marks a target the platform no longer maintains. Nothing sets it
    #: False today; ``desired_delta`` has no concept of retirement.
    active: bool = True


# ======================================================================
# why a residual exists
# ======================================================================


class ResidualCause(StrEnum):
    """What left the portfolio holding exposure it did not intend.

    **The platform cannot currently tell.** OKAPI measures a residual from
    portfolio delta alone, and a signed number carries no history: a residual
    of +4,000 looks identical whether it came from a partial entry fill, an
    exit that left a stub, a cancel race, or a mark that moved.

    So the default is ``UNCLASSIFIED``, and it stays that way. The other values
    exist so that a later phase — one with the fill and order history to make
    the attribution honestly — has somewhere to put the answer. Guessing a
    cause from the residual's size or sign would produce a field that reads
    like evidence and is not.
    """

    PARTIAL_ENTRY_FILL = "PARTIAL_ENTRY_FILL"
    ASYMMETRIC_ENTRY_FILL = "ASYMMETRIC_ENTRY_FILL"
    EXIT_RESIDUAL = "EXIT_RESIDUAL"
    HEDGE_RESIDUAL = "HEDGE_RESIDUAL"
    CANCEL_RACE = "CANCEL_RACE"
    UNKNOWN_ORDER = "UNKNOWN_ORDER"
    MARK_MOVEMENT = "MARK_MOVEMENT"
    RECONCILIATION = "RECONCILIATION"
    MANUAL = "MANUAL"
    #: The only value this build ever assigns.
    UNCLASSIFIED = "UNCLASSIFIED"


# ======================================================================
# exposure measurement
# ======================================================================


class SymbolDeltaSummary(Base):
    """One symbol's delta, flattened for display.

    A pure restatement of a :class:`~core.models.ops.DeltaReport`. Every field
    is copied; `residual` is the report's own ``unhedged_delta`` under the name
    the hedging vocabulary uses for it, not a second subtraction.
    """

    symbol: str
    target_delta: float
    actual_delta: float
    residual: float
    within_tolerance: bool
    tolerance: float
    measured_at: Millis | None = None

    @property
    def needs_hedge(self) -> bool:
        """Mirrors the condition ``build_hedges`` already applies.

        Reported, never consulted: ``Okapi.build_hedges`` evaluates the same
        condition itself, and a caller that branched on this property instead
        would be taking the decision somewhere the tests do not look.
        """
        return not self.within_tolerance and abs(self.residual) > 0


def summarize_delta_report(
    report: DeltaReport, *, measured_at: Millis | None = None
) -> SymbolDeltaSummary:
    """Adapt a ``DeltaReport`` into a display summary. Pure, and a copy only."""
    return SymbolDeltaSummary(
        symbol=report.symbol,
        target_delta=report.desired_delta,
        actual_delta=report.actual_delta,
        residual=report.unhedged_delta,
        within_tolerance=report.within_tolerance,
        tolerance=report.tolerance,
        measured_at=report.created_at if measured_at is None else measured_at,
    )


class DeltaSnapshot(Base):
    """Every symbol's exposure at one instant.

    Carries the ``DeltaReport`` objects OKAPI produced rather than a
    reconstruction of them. ``total_unhedged`` is supplied by the caller as the
    sum of those reports' absolute residuals — the same expression
    ``Okapi.total_unhedged`` evaluates — so the snapshot and the health
    heartbeat cannot report two different numbers for one instant.
    """

    snapshot_id: str = Field(default_factory=lambda: new_id("dsnap"))
    created_at: Millis

    reports: list[DeltaReport] = Field(default_factory=list)
    targets: list[HedgeTarget] = Field(default_factory=list)
    summaries: list[SymbolDeltaSummary] = Field(default_factory=list)

    #: Copied from ``Okapi.total_unhedged``. Never recomputed here.
    total_unhedged: float = 0.0
    tolerance: float = 0.0


# ======================================================================
# hedge request lifecycle
# ======================================================================


class HedgeRequestStatus(StrEnum):
    """Lifecycle of one hedge request.

    Distinct from the status of the orders working it: an order is what a venue
    has, a request is what OKAPI asked for. The two can disagree — a request
    whose single order is UNKNOWN is not finished, and a request whose orders
    all filled is.

    ``UNKNOWN`` is not resolved and not terminal. It means the venue-side truth
    of some order behind this hedge is not known, and the platform does not
    guess: an UNKNOWN hedge stays OUTSTANDING until something authoritative
    says otherwise.
    """

    DETECTED = "DETECTED"
    PROPOSED = "PROPOSED"
    SUBMITTING = "SUBMITTING"
    WORKING = "WORKING"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    CANCEL_PENDING = "CANCEL_PENDING"
    COMPLETE = "COMPLETE"
    CANCELLED = "CANCELLED"
    #: Venue-side truth unresolved. Never assumed to mean failure.
    UNKNOWN = "UNKNOWN"
    FAILED = "FAILED"


#: States from which nothing further happens on its own. ``UNKNOWN`` is
#: deliberately absent, exactly as it is from execution's terminal sets.
HEDGE_TERMINAL_STATUSES: frozenset[HedgeRequestStatus] = frozenset(
    {
        HedgeRequestStatus.COMPLETE,
        HedgeRequestStatus.CANCELLED,
        HedgeRequestStatus.FAILED,
    }
)

#: States in which the hedge is known to be doing something.
HEDGE_ACTIVE_STATUSES: frozenset[HedgeRequestStatus] = frozenset(
    {
        HedgeRequestStatus.SUBMITTING,
        HedgeRequestStatus.WORKING,
        HedgeRequestStatus.PARTIALLY_FILLED,
        HedgeRequestStatus.CANCEL_PENDING,
    }
)


class HedgeRequestRecord(Base):
    """One hedge, from residual to whatever became of it.

    **Identifiers, not copies.** Phase 6's ``ExecutionRegistry`` owns plan and
    order truth; Phase 7's ``ReconciliationRegistry`` owns run truth. This
    record links them so the question "which residual produced this order?" has
    an answer, and duplicating either would create a second version that could
    drift from the first.

    Every economic field — side, venue, notional, deltas, urgency — is copied
    from the ``HedgeIntent`` OKAPI built. Nothing is recomputed.
    """

    hedge_id: str = Field(default_factory=lambda: new_id("hedge"))

    created_at: Millis
    updated_at: Millis
    terminal_at: Millis | None = None

    symbol: str
    strategy: str = ""

    status: HedgeRequestStatus = HedgeRequestStatus.DETECTED
    #: Always UNCLASSIFIED in this build. See :class:`ResidualCause`.
    cause: ResidualCause = ResidualCause.UNCLASSIFIED

    #: Copied from the ``DeltaReport`` / ``HedgeIntent`` that produced this.
    target_delta: float = 0.0
    observed_delta: float = 0.0
    residual_delta: float = 0.0

    requested_notional: float = 0.0

    side: Side | None = None
    venue: str | None = None

    tolerance: float = 0.0
    urgency: float = 0.0

    reason_codes: list[str] = Field(default_factory=list)

    #: ``HedgeIntent.hedge_id`` — the id OKAPI minted and the orchestrator uses
    #: as the correlation id all the way down.
    hedge_intent_id: str | None = None
    #: The ``TradeIntent`` the orchestrator built from it.
    trade_intent_id: str | None = None

    #: References into Phase 6's registry.
    execution_plan_ids: list[str] = Field(default_factory=list)
    order_ids: list[str] = Field(default_factory=list)

    #: Opportunities whose residual this hedge is believed to close. Left empty
    #: rather than inferred: portfolio delta does not say which trade left the
    #: exposure behind.
    source_opportunity_ids: list[str] = Field(default_factory=list)
    #: References into Phase 7's registry, attached by an explicit caller.
    reconciliation_run_ids: list[str] = Field(default_factory=list)

    notes: list[str] = Field(default_factory=list)

    @property
    def is_terminal(self) -> bool:
        return self.status in HEDGE_TERMINAL_STATUSES

    @property
    def is_active(self) -> bool:
        """Currently known to be working.

        An UNKNOWN hedge is **not** active: nobody knows that it is working.
        It is outstanding, which is a different question — see
        :attr:`is_outstanding`.
        """
        return self.status in HEDGE_ACTIVE_STATUSES

    @property
    def is_outstanding(self) -> bool:
        """Final execution truth is not known.

        The wider of the two. ``is_active`` asks "is this working?";
        ``is_outstanding`` asks "could this still change what we hold?". They
        differ exactly on UNKNOWN, and conflating them is how a platform
        decides an order it cannot see stopped existing.
        """
        return not self.is_terminal

    @property
    def is_unknown(self) -> bool:
        return self.status is HedgeRequestStatus.UNKNOWN


class HedgeOutcomeSummary(Base):
    """What one hedge was asked to close, and what it left behind.

    Nothing computes one automatically. The "after" measurements need a delta
    snapshot taken once the hedge's orders are terminal, and deciding *when*
    that is — how long after the last fill the portfolio has settled — is an
    economic judgement this phase is not allowed to make.
    """

    hedge_id: str

    target_before: float = 0.0
    actual_before: float = 0.0
    residual_before: float = 0.0

    actual_after: float | None = None
    residual_after: float | None = None

    requested_notional: float = 0.0
    filled_notional: float = 0.0

    completed_at: Millis | None = None


# ======================================================================
# venue selection, observed
# ======================================================================


class HedgeVenueCandidate(Base):
    """One venue considered for a hedge, as it looked at the time.

    Descriptive. ``usable`` mirrors ``DataQuality.is_usable`` for the venue
    state, which is the same test ``_hedge_venue`` already applies; it is
    recorded so a reader can see why a venue was or was not in the running.
    """

    venue: str
    symbol: str
    side: Side

    best_bid: float | None = None
    best_ask: float | None = None

    quality: DataQuality = DataQuality.UNAVAILABLE
    usable: bool = False

    source_data_timestamp: Millis | None = None


class HedgeRouteSnapshot(Base):
    """Which venue was chosen, and what the alternatives looked like.

    ``selected_venue`` **copies** what ``Okapi._hedge_venue`` returned. This
    snapshot performs no comparison of its own: a buy hedge still wants the
    lowest ask and a sell hedge the highest bid, decided in exactly one place,
    and a second implementation here could quietly disagree with the first.

    ``None`` means the existing selector found no usable venue — the same
    condition that makes ``build_hedges`` skip the symbol.
    """

    created_at: Millis
    symbol: str
    side: Side

    candidates: list[HedgeVenueCandidate] = Field(default_factory=list)
    #: Copied from ``_hedge_venue``. Never chosen here.
    selected_venue: str | None = None


# ======================================================================
# metrics, readiness and the snapshot
# ======================================================================


class HedgeMetrics(Base):
    """Counters. No thresholds, no rates, no adaptive anything.

    Explicitly not an input to hedging: no tolerance, size or venue choice
    reads these. A hedger that widened its own tolerance because it had hedged
    a lot lately would be making a risk decision out of its own history.
    """

    delta_snapshots: int = 0
    residuals_detected: int = 0

    hedges_proposed: int = 0
    hedges_submitted: int = 0
    hedges_completed: int = 0
    hedges_cancelled: int = 0
    hedges_unknown: int = 0
    hedges_failed: int = 0

    requested_notional: float = 0.0
    completed_notional: float = 0.0


class OkapiReadiness(Base):
    """Whether hedging is in a fit state. **Reporting only.**

    This gates nothing. RUNE still calls ``Okapi.hedge_available(symbol,
    market)`` as its mandatory pre-entry gate, and that call is untouched;
    substituting this model for it would replace a tested decision with an
    untested one.

    ``ready`` is False whenever anything is unestablished, including when
    nothing has been checked. Absence of evidence is not readiness.
    """

    ready: bool = False
    created_at: Millis | None = None

    targets_established: bool = False
    market_available: bool = False
    #: Mirrors ``hedge_available`` across the symbols checked. Never replaces it.
    hedging_available: bool = False

    unknown_hedges: int = 0
    outstanding_hedges: int = 0

    total_unhedged: float = 0.0
    tolerance: float = 0.0

    reason_codes: list[str] = Field(default_factory=list)
    detail: str = ""


class OkapiSnapshot(Base):
    """One serializable view of hedging state.

    Compact: targets, the current delta reports, and *ids* for the hedges in
    flight. A snapshot that embedded every hedge record with its orders and
    fills would be sized by session history rather than by what is currently
    outstanding.

    ``created_at`` is supplied by the caller and never read from a clock.
    """

    created_at: Millis

    targets: list[HedgeTarget] = Field(default_factory=list)
    delta_reports: list[DeltaReport] = Field(default_factory=list)

    total_unhedged: float = 0.0
    hedge_tolerance: float = 0.0

    active_hedge_ids: list[str] = Field(default_factory=list)
    outstanding_hedge_ids: list[str] = Field(default_factory=list)
    unknown_hedge_ids: list[str] = Field(default_factory=list)

    #: Symbol -> what ``Okapi.hedge_available`` answered for it. Copied.
    hedge_available_by_symbol: dict[str, bool] = Field(default_factory=dict)

    metrics: HedgeMetrics = Field(default_factory=HedgeMetrics)
    readiness: OkapiReadiness | None = None


__all__ = [
    "HEDGE_ACTIVE_STATUSES",
    "HEDGE_TERMINAL_STATUSES",
    "DeltaSnapshot",
    "HedgeMetrics",
    "HedgeOutcomeSummary",
    "HedgeRequestRecord",
    "HedgeRequestStatus",
    "HedgeRouteSnapshot",
    "HedgeTarget",
    "HedgeTargetSource",
    "HedgeVenueCandidate",
    "OkapiReadiness",
    "OkapiSnapshot",
    "ResidualCause",
    "SymbolDeltaSummary",
    "summarize_delta_report",
]
