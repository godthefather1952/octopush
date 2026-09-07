"""Reconciliation vocabulary — the Phase 7 framework's models.

FOUR KINDS OF TRUTH, DELIBERATELY NOT MERGED
============================================
Reconciliation only means something if there is more than one account of what
happened. This module names the accounts and keeps them apart:

* **EXECUTION** — what VESKA and the executor believe about orders and fills.
* **ACCOUNT** — what the internal ledger believes resulted: cash, positions,
  realised P&L, fees.
* **VENUE** — what an external venue authoritatively reports. No such source
  exists in this build; the shapes exist so one can be added.
* **RECORDED** — what durable event history reconstructs.
* **OPERATOR** — explicit human evidence, supplied to resolve something the
  platform cannot resolve itself.

The temptation with reconciliation is to collapse these into one shared mutable
object and call agreement "consistency". That object cannot disagree with
itself, so it can never find anything. Each snapshot here is an immutable
capture of one source at one instant, and comparison is a separate act.

NOTHING HERE READS A CLOCK OR DECIDES ANYTHING
==============================================
Every timestamp is supplied by the caller, so a snapshot captured during replay
carries the instant the original run recorded. No model in this file compares
sources, computes a verdict, mutates state, or performs a side effect because
it was constructed.

EXISTING MODELS ARE UNCHANGED
=============================
``Mismatch``, ``MismatchKind``, ``Severity`` and ``ReconciliationResult`` stay
in :mod:`core.models.ops` and keep their exact meaning.
:class:`ReconciliationDiscrepancy` is the *workflow record around* a mismatch,
not a replacement for it — a mismatch is a measurement, a discrepancy is a
measurement someone is tracking through to a resolution.

A NOTE ON ONE IMPORT
====================
This module imports the venue snapshot models from :mod:`execution.gateway`,
which is the one place in ``core/`` that reaches into ``execution/``. Phase 6
put those transport-neutral shapes there, and duplicating them here to preserve
the layering would create two definitions of venue truth that could drift —
much worse than one import in one direction. There is no import cycle:
``execution.gateway`` depends only on ``core.models.common`` and
``core.models.execution``, neither of which knows this module exists. Relocating
the venue models to ``core/models/`` is a reasonable later cleanup; it is not
worth churning Phase 6 for during a construction pass.
"""

from __future__ import annotations

from pydantic import Field

from core.models.common import Base, Envelope, Millis, StrEnum, new_id
from core.models.execution import ExecutionSnapshot, OrderStatus
from core.models.ops import Mismatch, MismatchKind, Severity
from core.models.portfolio import PositionState
from execution.gateway import (
    VenueBalanceSnapshot,
    VenueFillSnapshot,
    VenueOrderSnapshot,
    VenuePositionSnapshot,
)

# ======================================================================
# sources
# ======================================================================


class ReconciliationSourceKind(StrEnum):
    """Which account of events a snapshot represents.

    The kind is not a ranking. A later pass will decide which source wins a
    given disagreement, and the answer differs by question: a venue is
    authoritative about whether an order exists, and says nothing about what
    the platform *intended*.
    """

    #: What VESKA and the executor believe about orders and fills.
    EXECUTION = "EXECUTION"
    #: What the internal ledger believes about cash, positions and P&L.
    ACCOUNT = "ACCOUNT"
    #: What an external venue authoritatively reports. None exists yet.
    VENUE = "VENUE"
    #: What durable event history reconstructs.
    RECORDED = "RECORDED"
    #: Explicit human evidence.
    OPERATOR = "OPERATOR"


class SourceAuthority(StrEnum):
    """How much weight a source's account can carry, structurally.

    Descriptive metadata, not comparison logic. Nothing in this build treats
    AUTHORITATIVE as "always right"; the point is that when a venue source
    eventually exists, it is identifiable as external evidence about order
    existence, fills, balances and positions rather than as one more internal
    opinion.
    """

    #: External and definitive about its own domain. A venue, or an operator.
    AUTHORITATIVE = "AUTHORITATIVE"
    #: Internal state the platform maintains as it works.
    INTERNAL = "INTERNAL"
    #: Rebuilt from something else rather than maintained directly.
    DERIVED = "DERIVED"


class SourceHealth(Base):
    """Whether a source could be captured, and how completely.

    A snapshot must be able to *say* that venue truth was unavailable. Silently
    omitting an absent source is how a reconciler concludes that two accounts
    agree when only one of them was ever asked.
    """

    kind: ReconciliationSourceKind
    name: str
    authority: SourceAuthority = SourceAuthority.INTERNAL
    #: Whether the source answered at all.
    available: bool = True
    #: Whether the answer covers everything it claims to cover. A paged venue
    #: query that stopped early is available but not complete.
    complete: bool = True
    #: Supplied by the caller, never read from a clock.
    captured_at: Millis | None = None
    detail: str = ""

    @property
    def usable(self) -> bool:
        return self.available and self.complete


# ======================================================================
# run lifecycle
# ======================================================================


class ReconciliationTrigger(StrEnum):
    """Why a reconciliation run exists.

    The current platform runs only PERIODIC reconciliation, on the existing
    cadence. The rest are named so that when a run happens for another reason,
    the record says which — a startup run and a post-fill run answer different
    questions and should not be indistinguishable afterwards.
    """

    STARTUP = "STARTUP"
    PERIODIC = "PERIODIC"
    POST_FILL = "POST_FILL"
    POST_CANCEL = "POST_CANCEL"
    UNKNOWN_ORDER = "UNKNOWN_ORDER"
    MANUAL = "MANUAL"
    RECOVERY = "RECOVERY"
    REPLAY = "REPLAY"


class ReconciliationRunStatus(StrEnum):
    """Lifecycle of a reconciliation WORKFLOW.

    Distinct from ``ReconciliationResult.ok``, which stays the answer to "did
    this comparison find a critical mismatch?". This is the state of the larger
    process around it: capture, compare, and — where discrepancies are found —
    the resolution that follows, which can outlive many comparisons.
    """

    CREATED = "CREATED"
    CAPTURING = "CAPTURING"
    COMPARING = "COMPARING"
    DISCREPANCIES_FOUND = "DISCREPANCIES_FOUND"
    CLEAN = "CLEAN"
    AWAITING_RESOLUTION = "AWAITING_RESOLUTION"
    RESOLVING = "RESOLVING"
    RESOLVED = "RESOLVED"
    FAILED = "FAILED"


#: Run states from which nothing further happens on its own.
RUN_TERMINAL_STATUSES: frozenset[ReconciliationRunStatus] = frozenset(
    {
        ReconciliationRunStatus.CLEAN,
        ReconciliationRunStatus.RESOLVED,
        ReconciliationRunStatus.FAILED,
    }
)


# ======================================================================
# account truth
# ======================================================================


class PositionSummary(Base):
    """One position, captured rather than referenced.

    Values are copied from :class:`~core.models.portfolio.PositionState`
    unchanged. Nothing here recomputes an economic quantity differently from
    ``PaperAccount``: a reconciler that derived its own P&L would be comparing
    the ledger against this module's arithmetic rather than against itself.
    """

    venue: str
    symbol: str
    quantity: float = 0.0
    average_price: float = 0.0
    mark_price: float | None = None
    notional: float = 0.0
    signed_notional: float = 0.0
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    fees_paid: float = 0.0
    updated_at: Millis | None = None

    @property
    def key(self) -> str:
        return f"{self.venue}:{self.symbol}"

    @classmethod
    def of(cls, position: PositionState) -> PositionSummary:
        return cls(
            venue=position.venue,
            symbol=position.symbol,
            quantity=position.quantity,
            average_price=position.average_entry_price,
            mark_price=position.mark_price,
            notional=position.notional,
            signed_notional=position.signed_notional,
            realized_pnl=position.realized_pnl,
            unrealized_pnl=position.unrealized_pnl,
            fees_paid=position.fees_paid,
            updated_at=position.updated_at,
        )


class AccountSnapshot(Envelope):
    """What the internal ledger believes, at one logical instant.

    Immutable and detached: the positions are summaries rather than live
    objects, so a snapshot taken now still describes now when it is read later.
    That is the whole reason it exists — a "snapshot" that aliases mutable
    account state is a live view wearing a snapshot's name, and comparing two
    such views finds nothing.

    Deliberately separate from ``PaperAccount.snapshot()``, which returns a
    ``PortfolioState`` for the risk and dashboard paths and is unchanged.
    """

    snapshot_id: str = Field(default_factory=lambda: new_id("acctsnap"))

    initial_balance: float = 0.0
    cash: float = 0.0
    equity: float = 0.0
    peak_equity: float = 0.0

    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    fees_paid: float = 0.0
    day_realized_pnl: float = 0.0

    #: Lifetime count, including fills sealed into the checkpoint.
    fills_applied: int = 0
    #: How many fills the checkpoint has absorbed.
    fills_sealed: int = 0
    #: Fill ids still resident in the unsealed tail. What a reconciler can
    #: still compare fill-by-fill; anything older is covered by the totals.
    unsealed_fill_ids: list[str] = Field(default_factory=list)
    retained_fills: int = 0

    positions: list[PositionSummary] = Field(default_factory=list)

    #: Checkpoint totals, so a reader can tell a sealed prefix from a tail.
    checkpoint_cash: float = 0.0
    checkpoint_realized_pnl: float = 0.0
    checkpoint_fees_paid: float = 0.0
    checkpoint_sealed_at: Millis | None = None

    @property
    def open_positions(self) -> list[PositionSummary]:
        return [p for p in self.positions if p.quantity != 0.0]


# ======================================================================
# venue truth (no implementation exists)
# ======================================================================


class VenueTruthSnapshot(Envelope):
    """What one external venue authoritatively reports.

    **Nothing produces one of these.** This build has no authenticated venue
    connection, and Phase 7 adds none. The shape exists so that a future venue
    adapter has somewhere to normalise into, and so that reconciliation code
    written now is written against the eventual answer rather than against its
    absence.

    ``complete`` matters as much as the contents. A venue query that paged out,
    timed out halfway, or returned only open orders is a partial account, and a
    reconciler must be able to tell that from "the venue holds nothing".
    """

    snapshot_id: str = Field(default_factory=lambda: new_id("venuesnap"))
    venue: str

    orders: list[VenueOrderSnapshot] = Field(default_factory=list)
    fills: list[VenueFillSnapshot] = Field(default_factory=list)
    positions: list[VenuePositionSnapshot] = Field(default_factory=list)
    balances: list[VenueBalanceSnapshot] = Field(default_factory=list)

    #: The venue's own ordering marker, where it publishes one.
    source_sequence: int | None = None
    #: Where a paged query stopped, so the next capture can continue.
    cursor: str | None = None
    #: The venue's own timestamp for this view, as opposed to ours.
    as_of: Millis | None = None
    #: False when this is a partial account of the venue's state.
    complete: bool = True


# ======================================================================
# recorded truth (seam only)
# ======================================================================


class RecordedTruthSnapshot(Envelope):
    """What durable event history reconstructs.

    **Nothing produces one of these either.** Phase 2's replay engine is
    untouched, and no reconstruction is implemented here. The seam exists
    because "rebuild the ledger from the recorded events and compare" is the
    check that catches a class of bug the other sources cannot: one where
    execution and the account agree with each other and both disagree with what
    was actually published.

    ``through_sequence`` bounds the claim. A reconstruction is only an account
    of the events it actually read.
    """

    snapshot_id: str = Field(default_factory=lambda: new_id("recsnap"))
    session_id: str | None = None
    #: The last event sequence folded into this reconstruction.
    through_sequence: int | None = None

    orders: list[str] = Field(default_factory=list)
    fill_ids: list[str] = Field(default_factory=list)
    positions: list[PositionSummary] = Field(default_factory=list)
    cash: float = 0.0
    realized_pnl: float = 0.0
    fees_paid: float = 0.0

    complete: bool = False


# ======================================================================
# the captured bundle
# ======================================================================


class ReconciliationSnapshot(Envelope):
    """Every available account of the truth, captured at one logical instant.

    A bundle, and nothing more. It states what was captured and what was not;
    it does not say whether the sources agree, because that is a comparison and
    comparison is a separate act performed by something that can be tested on
    its own.

    ``missing_sources`` is load-bearing. A reconciler handed a bundle with no
    venue snapshot must be able to tell "the venue was not asked" from "the
    venue reported nothing", and only the bundle knows which.
    """

    snapshot_id: str = Field(default_factory=lambda: new_id("recon"))

    execution: ExecutionSnapshot | None = None
    account: AccountSnapshot | None = None
    venue: VenueTruthSnapshot | None = None
    recorded: RecordedTruthSnapshot | None = None

    #: Per-source capture outcome, including the ones that failed.
    sources: list[SourceHealth] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)

    @property
    def available_sources(self) -> list[ReconciliationSourceKind]:
        return [s.kind for s in self.sources if s.available]

    @property
    def missing_sources(self) -> list[ReconciliationSourceKind]:
        return [s.kind for s in self.sources if not s.available]

    @property
    def incomplete_sources(self) -> list[ReconciliationSourceKind]:
        return [s.kind for s in self.sources if s.available and not s.complete]

    @property
    def is_fully_captured(self) -> bool:
        """Whether every configured source answered completely."""
        return bool(self.sources) and all(s.usable for s in self.sources)


# ======================================================================
# discrepancies
# ======================================================================


class DiscrepancyStatus(StrEnum):
    """Where a tracked disagreement is in its workflow.

    Nothing in this build moves a discrepancy out of OPEN on its own. IGNORED
    in particular is only ever reachable by an explicit caller: a reconciler
    that can quietly decide a disagreement does not matter is a reconciler that
    reports agreement it has not established.
    """

    OPEN = "OPEN"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    RESOLVING = "RESOLVING"
    RESOLVED = "RESOLVED"
    IGNORED = "IGNORED"


#: Statuses that no longer need attention.
CLOSED_DISCREPANCY_STATUSES: frozenset[DiscrepancyStatus] = frozenset(
    {DiscrepancyStatus.RESOLVED, DiscrepancyStatus.IGNORED}
)


class DiscrepancyEntityType(StrEnum):
    """What kind of thing a disagreement is about."""

    ORDER = "ORDER"
    FILL = "FILL"
    POSITION = "POSITION"
    CASH = "CASH"
    PNL = "PNL"
    FEES = "FEES"
    PLAN = "PLAN"
    ACCOUNT = "ACCOUNT"


def discrepancy_identity(
    kind: MismatchKind,
    entity_type: DiscrepancyEntityType,
    entity_id: str,
    source_a: ReconciliationSourceKind,
    source_b: ReconciliationSourceKind,
) -> str:
    """The stable key for "this same disagreement, seen again".

    ONE PLACE, ON PURPOSE
    =====================
    Deduplication is the difference between a registry that reports one
    persistent cash discrepancy and one that reports four hundred copies of it
    across four hundred runs. Getting it wrong in either direction is bad —
    too loose and two real disagreements merge, too tight and every run
    manufactures a new one — so the rule lives in exactly one function that a
    later pass can change once.

    The basis is what the disagreement is *about*, not what it measured: the
    same cash key disagreeing by a different amount on the next run is the same
    discrepancy getting worse, not a new one. The source pair is ordered so
    that A-versus-B and B-versus-A are one disagreement.

    Construction-phase note: whether this basis is sufficient is a validation
    question. It is deliberately simple rather than clever.
    """
    pair = "|".join(sorted((source_a.value, source_b.value)))
    return f"{kind.value}:{entity_type.value}:{entity_id}:{pair}"


class ReconciliationDiscrepancy(Base):
    """A disagreement someone is tracking through to a resolution.

    Distinct from :class:`~core.models.ops.Mismatch`, which is unchanged and
    stays the measurement: one comparison, one moment. This is the record
    around it — how long the disagreement has persisted, how many times it has
    been seen, what is being done about it.

    A ``Mismatch`` cannot answer "is this the same problem we saw an hour ago?"
    because it has no identity beyond its own run. That question is why this
    model exists.
    """

    discrepancy_id: str
    #: The run that first observed it. Later sightings update, not replace.
    run_id: str

    kind: MismatchKind
    severity: Severity

    entity_type: DiscrepancyEntityType
    entity_id: str

    source_a: ReconciliationSourceKind
    source_b: ReconciliationSourceKind

    expected: float | str | None = None
    actual: float | str | None = None
    difference: float | None = None

    first_seen_at: Millis
    last_seen_at: Millis
    #: How many runs have observed this same disagreement.
    occurrences: int = 1

    status: DiscrepancyStatus = DiscrepancyStatus.OPEN

    detail: str = ""
    #: Free-form supporting evidence, keyed by whoever supplied it.
    evidence: dict[str, str] = Field(default_factory=dict)
    #: Ids of every resolution proposed against this discrepancy.
    resolution_ids: list[str] = Field(default_factory=list)

    @property
    def is_open(self) -> bool:
        return self.status not in CLOSED_DISCREPANCY_STATUSES

    @property
    def is_critical(self) -> bool:
        return self.severity is Severity.CRITICAL


def entity_type_for(kind: MismatchKind) -> DiscrepancyEntityType:
    """Map a mismatch kind onto what it is a disagreement about."""
    return {
        MismatchKind.POSITION_MISMATCH: DiscrepancyEntityType.POSITION,
        MismatchKind.CASH_MISMATCH: DiscrepancyEntityType.CASH,
        MismatchKind.FEE_MISMATCH: DiscrepancyEntityType.FEES,
        MismatchKind.PNL_MISMATCH: DiscrepancyEntityType.PNL,
        MismatchKind.UNKNOWN_FILL: DiscrepancyEntityType.FILL,
        MismatchKind.MISSING_FILL: DiscrepancyEntityType.FILL,
        MismatchKind.DUPLICATE_FILL: DiscrepancyEntityType.FILL,
        MismatchKind.ORDER_STATE_MISMATCH: DiscrepancyEntityType.ORDER,
    }[kind]


def discrepancy_from_mismatch(
    mismatch: Mismatch,
    *,
    run_id: str,
    now_ms: Millis,
    source_a: ReconciliationSourceKind = ReconciliationSourceKind.EXECUTION,
    source_b: ReconciliationSourceKind = ReconciliationSourceKind.ACCOUNT,
) -> ReconciliationDiscrepancy:
    """Adapt one measurement into a trackable workflow record.

    Pure, and deliberately lossless in the direction that matters: the kind,
    the severity, the key and the numbers are carried across untouched. This
    adapter exists so the current reconciliation algorithm can feed the new
    workflow without being rewritten — no comparison logic moves, no severity
    is reinterpreted, and no new mismatch is detected.

    The source pair defaults to EXECUTION versus ACCOUNT because that is what
    the current algorithm compares. A caller comparing something else says so.
    """
    entity_type = entity_type_for(mismatch.kind)
    return ReconciliationDiscrepancy(
        discrepancy_id=discrepancy_identity(
            mismatch.kind, entity_type, mismatch.key, source_a, source_b
        ),
        run_id=run_id,
        kind=mismatch.kind,
        severity=mismatch.severity,
        entity_type=entity_type,
        entity_id=mismatch.key,
        source_a=source_a,
        source_b=source_b,
        expected=mismatch.expected,
        actual=mismatch.actual,
        difference=mismatch.difference,
        first_seen_at=now_ms,
        last_seen_at=now_ms,
        detail=mismatch.detail,
    )


# ======================================================================
# resolutions
# ======================================================================


class ResolutionStatus(StrEnum):
    """Where a proposed fix is in its own workflow.

    A resolution is PROPOSED by whoever noticed the problem and APPROVED by
    whoever is allowed to act on it. Those are two steps on purpose: a
    reconciler that proposes and applies in one motion is a reconciler that
    can silently rewrite the ledger.
    """

    PROPOSED = "PROPOSED"
    APPROVED = "APPROVED"
    APPLYING = "APPLYING"
    APPLIED = "APPLIED"
    REJECTED = "REJECTED"
    FAILED = "FAILED"


class ResolutionAction(StrEnum):
    """What a resolution intends to do.

    A vocabulary, not an implementation. Nothing in this build performs any of
    these automatically, and the dangerous ones — forcing a balance, forcing a
    position, deleting a fill, inventing a fill — are deliberately absent from
    the vocabulary entirely, so that no future caller can name them by
    accident.
    """

    #: The disagreement is understood and needs nothing done.
    NO_ACTION = "NO_ACTION"
    #: Re-capture the sources and look again; some disagreements are timing.
    REFRESH = "REFRESH"
    #: Ask an authoritative source specifically about this entity.
    REQUERY = "REQUERY"
    #: Apply an authoritative order status to an UNKNOWN order.
    RESOLVE_ORDER = "RESOLVE_ORDER"
    #: Correct internal state to match authoritative evidence. Requires an
    #: authoritative source and an explicit approval; never automatic.
    ADJUST_INTERNAL_STATE = "ADJUST_INTERNAL_STATE"
    #: Rebuild from durable event history rather than patching.
    REBUILD_FROM_EVENTS = "REBUILD_FROM_EVENTS"
    #: Hand it to a human. Always available, and the honest answer whenever
    #: the platform cannot establish the truth by itself.
    ESCALATE_OPERATOR = "ESCALATE_OPERATOR"


class ReconciliationResolution(Base):
    """A proposed or applied response to one discrepancy.

    Constructing one has no side effect. The model records an intention and,
    later, what became of it; something else has to actually do the work, and
    in this build only an explicit caller can.
    """

    resolution_id: str = Field(default_factory=lambda: new_id("res"))
    discrepancy_id: str
    run_id: str | None = None

    created_at: Millis
    updated_at: Millis
    applied_at: Millis | None = None

    status: ResolutionStatus = ResolutionStatus.PROPOSED
    action: ResolutionAction = ResolutionAction.ESCALATE_OPERATOR

    #: Which account of the truth this resolution treats as correct.
    authoritative_source: ReconciliationSourceKind | None = None

    target_entity: str | None = None
    #: For RESOLVE_ORDER: the status the authoritative source reports.
    target_status: OrderStatus | None = None

    reason: str = ""
    evidence: dict[str, str] = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)

    @property
    def is_terminal(self) -> bool:
        return self.status in (
            ResolutionStatus.APPLIED,
            ResolutionStatus.REJECTED,
            ResolutionStatus.FAILED,
        )


# ======================================================================
# run record
# ======================================================================


class ReconciliationRunRecord(Base):
    """One reconciliation workflow, from capture through to resolution.

    Holds ids rather than objects: a run that embedded its snapshots would be
    sized by the state it examined, and a registry of such runs would grow with
    the square of a session's activity.
    """

    run_id: str = Field(default_factory=lambda: new_id("rrun"))

    created_at: Millis
    updated_at: Millis
    completed_at: Millis | None = None

    status: ReconciliationRunStatus = ReconciliationRunStatus.CREATED
    trigger: ReconciliationTrigger = ReconciliationTrigger.PERIODIC
    reason: str = ""

    #: Which accounts of the truth this run was able to consult.
    source_kinds: list[ReconciliationSourceKind] = Field(default_factory=list)
    missing_source_kinds: list[ReconciliationSourceKind] = Field(default_factory=list)

    snapshot_id: str | None = None
    execution_snapshot_id: str | None = None
    account_snapshot_id: str | None = None
    venue_snapshot_id: str | None = None
    recorded_snapshot_id: str | None = None

    #: The legacy ``ReconciliationResult.run_id`` this record mirrors, when it
    #: was produced by the existing algorithm rather than by a new comparison.
    legacy_result_id: str | None = None

    discrepancy_ids: list[str] = Field(default_factory=list)
    resolution_ids: list[str] = Field(default_factory=list)

    critical_count: int = 0
    warning_count: int = 0
    info_count: int = 0

    notes: list[str] = Field(default_factory=list)

    @property
    def is_terminal(self) -> bool:
        return self.status in RUN_TERMINAL_STATUSES

    @property
    def found_discrepancies(self) -> bool:
        return bool(self.discrepancy_ids)


# ======================================================================
# readiness, metrics, retention
# ======================================================================


class ReconciliationReadiness(Base):
    """Whether reconciliation says the platform is in a known-good state.

    **Observability only.** Nothing gates trading on this, and Phase 7 does not
    wire it into the kill switch, the orchestrator or any risk decision. It
    exists because a future live-money startup will need exactly this question
    answered before enabling execution, and building the answer now means that
    phase adds a caller rather than a concept.
    """

    ready: bool = False
    created_at: Millis | None = None

    last_run_id: str | None = None
    last_run_status: ReconciliationRunStatus | None = None
    last_run_at: Millis | None = None

    open_critical: int = 0
    open_warning: int = 0
    #: Orders whose venue-side truth is unresolved. Never zero by assumption.
    unresolved_orders: int = 0

    execution_source_available: bool = False
    account_source_available: bool = False
    venue_sources_available: int = 0
    recorded_source_available: bool = False

    #: Why ``ready`` is False. Empty when it is True.
    reason_codes: list[str] = Field(default_factory=list)
    detail: str = ""


class ReconciliationMetrics(Base):
    """Counters. No thresholds, no rates, no alerting, no backend."""

    runs_started: int = 0
    runs_completed: int = 0
    clean_runs: int = 0
    runs_with_discrepancies: int = 0

    discrepancies_open: int = 0
    discrepancies_resolved: int = 0
    critical_seen: int = 0
    warnings_seen: int = 0

    unknown_orders_seen: int = 0

    resolution_requests: int = 0
    resolution_applied: int = 0
    resolution_failed: int = 0

    snapshots_captured: int = 0


class ArchivedReconciliationRuns(Base):
    """Totals for runs compacted out of memory.

    The shape ``OrderManager.ArchivedOrders`` established: when a record leaves
    memory, its aggregate stays, so a later question about the session's shape
    still has an answer. Nothing populates this yet — retention policy is
    deferred, and the hooks that would use it default to releasing nothing.
    """

    count: int = 0
    clean: int = 0
    with_discrepancies: int = 0
    critical_total: int = 0
    warning_total: int = 0
    by_trigger: dict[str, int] = Field(default_factory=dict)

    def absorb(self, record: ReconciliationRunRecord) -> None:
        self.count += 1
        if record.found_discrepancies:
            self.with_discrepancies += 1
        else:
            self.clean += 1
        self.critical_total += record.critical_count
        self.warning_total += record.warning_count
        self.by_trigger[record.trigger.value] = (
            self.by_trigger.get(record.trigger.value, 0) + 1
        )


class StartupReconciliationRequest(Base):
    """What a future live startup would have to reconcile before trading.

    Framework only. Nothing blocks ``Platform.start()`` on this, nothing
    queries a venue, and paper trading is unaffected.

    The sequence it describes — connect the private venues, capture
    authoritative truth, reconcile, and only then enable execution — is the one
    a live platform cannot safely skip. Writing it down now means the phase
    that implements it fills in a shape rather than inventing one under
    pressure.
    """

    created_at: Millis
    trigger: ReconciliationTrigger = ReconciliationTrigger.STARTUP
    #: Sources that must answer before trading may be enabled.
    required_sources: list[ReconciliationSourceKind] = Field(default_factory=list)
    #: Sources that were actually available when the request was prepared.
    available_sources: list[ReconciliationSourceKind] = Field(default_factory=list)
    #: Whether unresolved critical discrepancies must be zero. Always True for
    #: a live start; stated as a field so it is a decision, not an assumption.
    require_no_critical: bool = True
    require_no_unknown_orders: bool = True
    reason: str = ""
    notes: list[str] = Field(default_factory=list)

    @property
    def missing_required(self) -> list[ReconciliationSourceKind]:
        available = set(self.available_sources)
        return [k for k in self.required_sources if k not in available]

    @property
    def sources_satisfied(self) -> bool:
        return not self.missing_required


__all__ = [
    "CLOSED_DISCREPANCY_STATUSES",
    "RUN_TERMINAL_STATUSES",
    "AccountSnapshot",
    "ArchivedReconciliationRuns",
    "DiscrepancyEntityType",
    "DiscrepancyStatus",
    "PositionSummary",
    "ReconciliationDiscrepancy",
    "ReconciliationMetrics",
    "ReconciliationReadiness",
    "ReconciliationResolution",
    "ReconciliationRunRecord",
    "ReconciliationRunStatus",
    "ReconciliationSnapshot",
    "ReconciliationSourceKind",
    "ReconciliationTrigger",
    "RecordedTruthSnapshot",
    "ResolutionAction",
    "ResolutionStatus",
    "SourceAuthority",
    "SourceHealth",
    "StartupReconciliationRequest",
    "VenueTruthSnapshot",
    "discrepancy_from_mismatch",
    "discrepancy_identity",
    "entity_type_for",
]
