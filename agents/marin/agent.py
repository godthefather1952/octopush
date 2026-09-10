"""MARIN — reconciliation.

Compares what the platform *believes* happened with what the execution
subsystem *reports* happened.  In paper mode both sides are ours, which is
exactly why it matters: if the two paths disagree here, they would disagree
with a real venue too, and this is the cheap place to find out.

Three independent views are compared:

1. the account's running cash/position state;
2. the same state recomputed from the account's fill log;
3. the fills the OMS holds against its orders.

A critical mismatch suspends new trading.  It is never logged and ignored.

THE PHASE 7 FRAMEWORK AROUND IT
===============================
``reconcile()`` above is unchanged, and remains the platform's current
reconciliation answer: the same comparisons, the same tolerances, the same
severities, feeding the same ``ReconciliationResult`` that the orchestrator's
protection path reads.

Phase 7 adds a framework *around* it rather than through it:

* **sources** — each account of the truth (execution, account, and the venue
  and recorded seams that have no implementation) captured through one
  interface, at a supplied logical instant;
* **snapshots** — an immutable bundle of those accounts, which states what it
  could not capture rather than silently omitting it;
* **a registry** — runs, tracked discrepancies and proposed resolutions, so
  "is this the same problem as last time?" has an answer;
* **readiness** — the question a future live start must ask before enabling
  execution.

Nothing in that framework decides anything. The registry takes no safety
action, ``readiness`` gates nothing, and no discrepancy is acknowledged,
resolved or ignored except by an explicit caller. ``run()`` mirrors its result
into the registry strictly *after* the existing algorithm has produced it, so
the mirror cannot influence the answer.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from core.bus import EventBus
from core.clock import Clock
from core.events import Event, EventType
from core.health import HealthRegistry
from core.models.common import MONEY_EPSILON, QTY_EPSILON, Millis
from core.models.execution import ExecutionSnapshot, OrderStatus
from core.models.ops import (
    HealthStatus,
    Mismatch,
    MismatchKind,
    ReconciliationResult,
    Severity,
)
from core.models.reconciliation import (
    AccountSnapshot,
    DiscrepancyStatus,
    ReconciliationDiscrepancy,
    ReconciliationMetrics,
    ReconciliationReadiness,
    ReconciliationResolution,
    ReconciliationRunRecord,
    ReconciliationRunStatus,
    ReconciliationSnapshot,
    ReconciliationSourceKind,
    ReconciliationTrigger,
    RecordedTruthSnapshot,
    ResolutionAction,
    ResolutionStatus,
    SourceHealth,
    StartupReconciliationRequest,
    VenueTruthSnapshot,
)
from agents.marin.registry import ReconciliationRegistry
from agents.marin.source import ReconciliationSource
from execution.oms import OrderManager
from execution.paper.account import PaperAccount

SERVICE = "MARIN"
VERSION = "marin-0.2"

#: Cash tolerance. Prices and sizes are float64, so two arithmetically
#: identical paths can differ in the last bits; anything larger is a real bug.
#:
#: Aliases, not copies. These were separate literals that happened to equal
#: the shared epsilons, which meant tightening one of them left reconciliation
#: comparing on a different tolerance from the rest of the system — and the
#: divergence would have shown up as a reconciliation mismatch blamed on
#: arithmetic rather than on configuration.
CASH_TOLERANCE = MONEY_EPSILON
QTY_TOLERANCE = QTY_EPSILON


@dataclass
class Marin:
    bus: EventBus
    clock: Clock
    health: HealthRegistry
    oms: OrderManager
    account: PaperAccount
    runs: int = 0
    last_result: ReconciliationResult | None = None
    #: Lifetime compaction totals, for observability.
    fills_sealed: int = 0
    orders_archived: int = 0

    # -- Phase 7 framework, all optional ----------------------------------
    #
    # Every field below defaults to something inert, so an existing
    # construction site that passes only ``bus``, ``clock``, ``health``,
    # ``oms`` and ``account`` builds exactly the MARIN it built before.

    #: Runs, tracked discrepancies and proposed resolutions.
    registry: ReconciliationRegistry = field(
        default_factory=ReconciliationRegistry
    )
    #: What execution believes. ``None`` when not wired.
    execution_source: ReconciliationSource | None = None
    #: What the ledger believes. ``None`` when not wired.
    account_source: ReconciliationSource | None = None
    #: External venue accounts. Empty, and no implementation exists.
    venue_sources: list[ReconciliationSource] = field(default_factory=list)
    #: Reconstruction from durable events. ``None``; no implementation exists.
    recorded_source: ReconciliationSource | None = None
    #: The most recent captured bundle, for inspection and readiness evidence.
    last_snapshot: ReconciliationSnapshot | None = None

    def __post_init__(self) -> None:
        self.health.register(SERVICE, VERSION)

    def attach_sources(
        self,
        *,
        execution: ReconciliationSource | None = None,
        account: ReconciliationSource | None = None,
        venues: list[ReconciliationSource] | None = None,
        recorded: ReconciliationSource | None = None,
    ) -> None:
        """Wire truth sources after construction.

        Offered so the composition root can build MARIN, then hand it sources
        that depend on components built later, without changing the existing
        constructor call. Passing ``None`` leaves a source as it was.
        """
        if execution is not None:
            self.execution_source = execution
        if account is not None:
            self.account_source = account
        if venues is not None:
            self.venue_sources = list(venues)
        if recorded is not None:
            self.recorded_source = recorded

    def reconcile(self, now_ms: Millis | None = None) -> ReconciliationResult:
        now = self.clock.now_ms() if now_ms is None else now_ms
        mismatches: list[Mismatch] = []

        oms_fills = {fill.fill_id: fill for fill in self.oms.all_fills()}
        account_fills = {fill.fill_id: fill for fill in self.account.fill_log}

        # 1. Fill sets must agree in both directions.
        for fill_id in oms_fills.keys() - account_fills.keys():
            fill = oms_fills[fill_id]
            mismatches.append(
                Mismatch(
                    kind=MismatchKind.MISSING_FILL,
                    severity=Severity.CRITICAL,
                    key=fill_id,
                    detail=(
                        f"OMS holds a fill for {fill.venue}:{fill.symbol} that never "
                        "reached the account"
                    ),
                )
            )
        for fill_id in account_fills.keys() - oms_fills.keys():
            mismatches.append(
                Mismatch(
                    kind=MismatchKind.UNKNOWN_FILL,
                    severity=Severity.CRITICAL,
                    key=fill_id,
                    detail="account holds a fill the OMS has no order for",
                )
            )

        # 2. Cash and realised P&L, recomputed independently from the log.
        cash, positions, realized = self.account.recompute_from_fills()
        if abs(cash - self.account.cash) > CASH_TOLERANCE:
            mismatches.append(
                Mismatch(
                    kind=MismatchKind.CASH_MISMATCH,
                    severity=Severity.CRITICAL,
                    key="cash",
                    expected=cash,
                    actual=self.account.cash,
                    difference=self.account.cash - cash,
                    detail="running cash disagrees with cash rebuilt from fills",
                )
            )
        if abs(realized - self.account.realized_pnl) > CASH_TOLERANCE:
            mismatches.append(
                Mismatch(
                    kind=MismatchKind.PNL_MISMATCH,
                    severity=Severity.CRITICAL,
                    key="realized_pnl",
                    expected=realized,
                    actual=self.account.realized_pnl,
                    difference=self.account.realized_pnl - realized,
                )
            )

        # 3. Positions, key by key.
        keys = set(positions) | set(self.account.positions)
        for key in sorted(keys):
            expected_qty = positions[key].quantity if key in positions else 0.0
            actual_qty = (
                self.account.positions[key].quantity if key in self.account.positions else 0.0
            )
            if abs(expected_qty - actual_qty) > QTY_TOLERANCE:
                mismatches.append(
                    Mismatch(
                        kind=MismatchKind.POSITION_MISMATCH,
                        severity=Severity.CRITICAL,
                        key=key,
                        expected=expected_qty,
                        actual=actual_qty,
                        difference=actual_qty - expected_qty,
                    )
                )

        # 3b. Lifetime counts. The set comparison above only sees the unsealed
        #     window; these totals span the whole session and are what makes a
        #     fill dropped before the last checkpoint still detectable.
        if self.oms.fills_applied != self.account.fills_applied:
            mismatches.append(
                Mismatch(
                    kind=MismatchKind.MISSING_FILL,
                    severity=Severity.CRITICAL,
                    key="fills_applied",
                    expected=float(self.oms.fills_applied),
                    actual=float(self.account.fills_applied),
                    difference=float(self.account.fills_applied - self.oms.fills_applied),
                    detail="lifetime fill counts diverge between the OMS and the account",
                )
            )

        # 4. Fees. Sealed fees live in the checkpoint; only the tail is resident.
        expected_fees = self.account.checkpoint.fees_paid + sum(
            fill.fee for fill in account_fills.values()
        )
        if abs(expected_fees - self.account.fees_paid) > CASH_TOLERANCE:
            mismatches.append(
                Mismatch(
                    kind=MismatchKind.FEE_MISMATCH,
                    severity=Severity.WARNING,
                    key="fees",
                    expected=expected_fees,
                    actual=self.account.fees_paid,
                    difference=self.account.fees_paid - expected_fees,
                )
            )

        # 5. Order bookkeeping: filled quantity must equal the sum of fills,
        #    and an order in a terminal state must not still be filling.
        for order in self.oms.orders.values():
            fills_qty = sum(fill.quantity for fill in order.fills)
            if abs(fills_qty - order.filled_quantity) > QTY_TOLERANCE:
                mismatches.append(
                    Mismatch(
                        kind=MismatchKind.ORDER_STATE_MISMATCH,
                        severity=Severity.CRITICAL,
                        key=order.client_order_id,
                        expected=fills_qty,
                        actual=order.filled_quantity,
                        difference=order.filled_quantity - fills_qty,
                        detail="order filled_quantity disagrees with its fills",
                    )
                )
            if order.status is OrderStatus.FILLED and order.remaining_quantity > QTY_TOLERANCE:
                mismatches.append(
                    Mismatch(
                        kind=MismatchKind.ORDER_STATE_MISMATCH,
                        severity=Severity.CRITICAL,
                        key=order.client_order_id,
                        expected=0.0,
                        actual=order.remaining_quantity,
                        detail="order marked FILLED with quantity outstanding",
                    )
                )

        # 6. Orders in UNKNOWN are not a mismatch, but they are unresolved
        #    truth and must be surfaced until something settles them.
        for order in self.oms.unknown_orders():
            mismatches.append(
                Mismatch(
                    kind=MismatchKind.ORDER_STATE_MISMATCH,
                    severity=Severity.WARNING,
                    key=order.client_order_id,
                    actual=OrderStatus.UNKNOWN.value,
                    detail="order state is unknown and must be resolved",
                )
            )

        self.runs += 1
        result = ReconciliationResult(
            created_at=now,
            ok=not any(m.severity is Severity.CRITICAL for m in mismatches),
            mismatches=mismatches,
            orders_checked=len(self.oms.orders),
            fills_checked=len(oms_fills),
            positions_checked=len(keys),
        )
        self.last_result = result
        return result

    # ==================================================================
    # Phase 7 framework
    #
    # Everything below is additive. None of it is consulted by
    # ``reconcile()``, by the orchestrator's protection path, or by any risk
    # decision. It captures, records and answers questions.
    # ==================================================================

    # -- capture -----------------------------------------------------------

    def _sources(self) -> list[ReconciliationSource]:
        configured: list[ReconciliationSource] = []
        if self.execution_source is not None:
            configured.append(self.execution_source)
        if self.account_source is not None:
            configured.append(self.account_source)
        configured.extend(self.venue_sources)
        if self.recorded_source is not None:
            configured.append(self.recorded_source)
        return configured

    def _latest_source_health(
        self, source: ReconciliationSource | None
    ) -> SourceHealth | None:
        """Health for this exact configured source in the latest capture."""
        if source is None or self.last_snapshot is None:
            return None
        return next(
            (
                health
                for health in self.last_snapshot.sources
                if health.kind is source.kind and health.name == source.name
            ),
            None,
        )

    def capture_snapshot(self, now_ms: Millis) -> ReconciliationSnapshot:
        """Capture every configured source into one bundle at ``now_ms``.

        Records no mismatch and declares no agreement — it is a bundle, and
        comparison is a separate act.

        A source that fails is recorded as unavailable with the reason,
        rather than being dropped. The distinction is the difference between
        "the venue reported nothing" and "the venue was never asked", which are
        opposite conclusions, and a bundle that cannot express the second one
        invites a reconciler to invent agreement out of silence.
        """
        execution: ExecutionSnapshot | None = None
        account: AccountSnapshot | None = None
        venues: list[VenueTruthSnapshot] = []
        recorded: RecordedTruthSnapshot | None = None
        healths: list[SourceHealth] = []
        notes: list[str] = []

        for source in self._sources():
            try:
                capture = source.capture(now_ms)
            except Exception as exc:  # a source must never take the run down
                healths.append(
                    SourceHealth(
                        kind=source.kind,
                        name=source.name,
                        authority=source.authority,
                        available=False,
                        complete=False,
                        captured_at=now_ms,
                        detail=f"{type(exc).__name__}: {exc}",
                    )
                )
                notes.append(f"{source.name} could not be captured")
                continue

            healths.append(capture.health)
            if capture.snapshot is None:
                continue
            if source.kind is ReconciliationSourceKind.EXECUTION:
                execution = capture.snapshot
            elif source.kind is ReconciliationSourceKind.ACCOUNT:
                account = capture.snapshot
            elif source.kind is ReconciliationSourceKind.VENUE:
                venues.append(capture.snapshot)
            elif source.kind is ReconciliationSourceKind.RECORDED:
                recorded = capture.snapshot

        snapshot = ReconciliationSnapshot(
            created_at=now_ms,
            execution=execution,
            account=account,
            venues=venues,
            recorded=recorded,
            sources=healths,
            notes=notes,
        )
        self.last_snapshot = snapshot
        return snapshot

    # -- run lifecycle -----------------------------------------------------

    def begin_reconciliation(
        self,
        now_ms: Millis,
        *,
        trigger: ReconciliationTrigger = ReconciliationTrigger.MANUAL,
        reason: str = "",
    ) -> ReconciliationRunRecord:
        """Open a workflow run and capture the available truth.

        Stops at CAPTURING. It deliberately does **not** compare anything: the
        existing algorithm in :meth:`reconcile` remains the platform's
        comparison, and forcing it through a new engine during a construction
        pass would change behaviour under the guise of structure.

        Nothing calls this automatically. The orchestrator's cadence is
        unchanged, and no new run is triggered anywhere.
        """
        record = self.registry.register_run(
            now_ms, trigger=trigger, reason=reason
        )
        self.registry.set_run_status(
            record.run_id, ReconciliationRunStatus.CAPTURING, now_ms
        )
        snapshot = self.capture_snapshot(now_ms)
        self.registry.attach_snapshot(
            record.run_id,
            now_ms,
            snapshot_id=snapshot.snapshot_id,
            execution_snapshot_id=(
                snapshot.execution.event_id if snapshot.execution else None
            ),
            account_snapshot_id=(
                snapshot.account.snapshot_id if snapshot.account else None
            ),
            venue_snapshot_ids=[venue.snapshot_id for venue in snapshot.venues],
            recorded_snapshot_id=(
                snapshot.recorded.snapshot_id if snapshot.recorded else None
            ),
            source_kinds=snapshot.available_sources,
            missing_source_kinds=snapshot.missing_sources,
        )
        return record

    def mirror_result(
        self,
        result: ReconciliationResult,
        now_ms: Millis,
        *,
        trigger: ReconciliationTrigger = ReconciliationTrigger.PERIODIC,
        reason: str = "",
    ) -> ReconciliationRunRecord:
        """Record an already-produced result into the workflow registry.

        Strictly after the fact. The existing algorithm has already decided
        everything by the time this runs, and nothing here can change the
        answer — it reads ``result`` and writes to the registry, in that
        direction only. That is what makes it safe to call from :meth:`run`
        without altering reconciliation behaviour.

        Each mismatch becomes, or updates, a tracked discrepancy. Severity,
        kind, key and numbers cross over untouched; no comparison logic moves
        here and no new mismatch is detected.
        """
        record = self.registry.register_run(
            now_ms, trigger=trigger, reason=reason, run_id=result.run_id
        )
        self.registry.set_run_status(
            record.run_id, ReconciliationRunStatus.COMPARING, now_ms
        )
        for mismatch in result.mismatches:
            self.registry.add_mismatch(mismatch, run_id=record.run_id, now_ms=now_ms)
            if mismatch.actual == OrderStatus.UNKNOWN.value:
                self.registry.unknown_orders_seen += 1

        self.registry.set_run_status(
            record.run_id,
            (
                ReconciliationRunStatus.DISCREPANCIES_FOUND
                if result.mismatches
                else ReconciliationRunStatus.CLEAN
            ),
            now_ms,
        )
        return record

    # -- resolution --------------------------------------------------------

    def propose_resolution(
        self,
        discrepancy_id: str,
        now_ms: Millis,
        *,
        action: ResolutionAction = ResolutionAction.ESCALATE_OPERATOR,
        authoritative_source: ReconciliationSourceKind | None = None,
        target_entity: str | None = None,
        target_status: OrderStatus | None = None,
        reason: str = "",
        evidence: dict[str, str] | None = None,
    ) -> ReconciliationResolution:
        """Record an intended response to a discrepancy.

        Proposing has no side effect whatsoever. The resolution is written down
        as PROPOSED and nothing acts on it; something else has to, and in this
        build only an explicit caller can.
        """
        discrepancy = self.registry.get_discrepancy(discrepancy_id)
        resolution = ReconciliationResolution(
            discrepancy_id=discrepancy_id,
            run_id=discrepancy.run_id if discrepancy is not None else None,
            created_at=now_ms,
            updated_at=now_ms,
            action=action,
            authoritative_source=authoritative_source,
            target_entity=target_entity,
            target_status=target_status,
            reason=reason,
            evidence=dict(evidence or {}),
        )
        return self.registry.register_resolution(resolution, now_ms)

    async def apply_order_resolution(
        self,
        veska,
        client_order_id: str,
        authoritative_status: OrderStatus,
        now_ms: Millis,
        *,
        source: ReconciliationSourceKind = ReconciliationSourceKind.OPERATOR,
        evidence: dict[str, str] | None = None,
        discrepancy_id: str | None = None,
        reason: str = "",
    ):
        """Apply authoritative evidence to an order whose state is unknown.

        THE BRIDGE, AND WHY IT IS NEVER AUTOMATIC
        =========================================
        Phase 6 gave the execution layer one explicit door out of UNKNOWN
        (``veska.resolve_unknown``). This is the reconciliation side of that
        door: it records what is being applied and on whose evidence, then
        opens it.

        **Nothing calls this.** Not ``reconcile``, not ``run``, not a
        heartbeat, not a timeout, not the registry. An order is UNKNOWN
        precisely because the platform does not know what happened to it, and
        the only honest way out is a caller arriving with an answer from
        somewhere that does. MARIN adds no judgement of its own: it does not
        query a venue, does not guess a status, and does not invent a fill.

        The resolution is recorded before the attempt and updated with the
        outcome, so an application that fails leaves evidence rather than
        silence.
        """
        resolution = self.propose_resolution(
            discrepancy_id or f"order:{client_order_id}",
            now_ms,
            action=ResolutionAction.RESOLVE_ORDER,
            authoritative_source=source,
            target_entity=client_order_id,
            target_status=authoritative_status,
            reason=reason or "authoritative order status supplied by caller",
            evidence=evidence,
        )
        self.registry.set_resolution_status(
            resolution.resolution_id, ResolutionStatus.APPLYING, now_ms
        )

        result = await veska.resolve_unknown(
            client_order_id, authoritative_status, now_ms
        )

        self.registry.set_resolution_status(
            resolution.resolution_id,
            ResolutionStatus.APPLIED if result.accepted else ResolutionStatus.FAILED,
            now_ms,
            note=result.reason,
        )
        if result.accepted and discrepancy_id is not None:
            self.registry.set_discrepancy_status(
                discrepancy_id,
                DiscrepancyStatus.RESOLVED,
                now_ms,
                note=f"resolved to {authoritative_status.value}",
            )
        return result

    # -- readiness ---------------------------------------------------------

    def readiness(self, now_ms: Millis) -> ReconciliationReadiness:
        """Whether reconciliation says the platform is in a known-good state.

        **Observability only.** Nothing gates trading on this. The
        orchestrator's protection path still reads ``last_result.ok`` exactly
        as it did, and no kill-switch trigger consults it.

        It exists because a live start will eventually have to ask this
        question — connect the venues, capture authoritative truth, reconcile,
        and only then enable execution — and building the answer now means that
        phase adds a caller rather than a concept.

        ``ready`` is False whenever anything is unestablished, including when
        no run has happened. Absence of evidence is not readiness.
        """
        latest = self.registry.latest_run()
        open_critical = self.registry.open_critical()
        open_warning = self.registry.open_warnings()
        unresolved = len(self.oms.unknown_orders())

        execution_health = self._latest_source_health(self.execution_source)
        account_health = self._latest_source_health(self.account_source)
        recorded_health = self._latest_source_health(self.recorded_source)

        execution_available = bool(
            execution_health is not None
            and execution_health.usable
            and self.last_snapshot is not None
            and self.last_snapshot.execution is not None
        )
        account_available = bool(
            account_health is not None
            and account_health.usable
            and self.last_snapshot is not None
            and self.last_snapshot.account is not None
        )
        recorded_available = bool(
            recorded_health is not None
            and recorded_health.usable
            and self.last_snapshot is not None
            and self.last_snapshot.recorded is not None
        )
        venue_available = sum(
            1
            for source in self.venue_sources
            if (
                (health := self._latest_source_health(source)) is not None
                and health.usable
            )
        )

        reasons: list[str] = []
        if self.execution_source is None:
            reasons.append("NO_EXECUTION_SOURCE")
        elif execution_health is None:
            reasons.append("NO_EXECUTION_CAPTURE")
        elif not execution_available:
            reasons.append("EXECUTION_SOURCE_UNUSABLE")

        if self.account_source is None:
            reasons.append("NO_ACCOUNT_SOURCE")
        elif account_health is None:
            reasons.append("NO_ACCOUNT_CAPTURE")
        elif not account_available:
            reasons.append("ACCOUNT_SOURCE_UNUSABLE")

        if self.last_result is None:
            reasons.append("NO_RECONCILIATION_YET")
        elif not self.last_result.ok:
            reasons.append("LAST_RUN_HAD_CRITICAL_MISMATCH")
        if open_critical:
            reasons.append("OPEN_CRITICAL_DISCREPANCY")
        if unresolved:
            reasons.append("UNRESOLVED_ORDERS")

        return ReconciliationReadiness(
            ready=not reasons,
            created_at=now_ms,
            last_run_id=latest.run_id if latest is not None else None,
            last_run_status=latest.status if latest is not None else None,
            last_run_at=latest.created_at if latest is not None else None,
            open_critical=len(open_critical),
            open_warning=len(open_warning),
            unresolved_orders=unresolved,
            execution_source_available=execution_available,
            account_source_available=account_available,
            venue_sources_available=venue_available,
            recorded_source_available=recorded_available,
            reason_codes=reasons,
        )

    def prepare_startup_reconciliation(
        self, now_ms: Millis, *, reason: str = ""
    ) -> StartupReconciliationRequest:
        """Describe what a live start would have to reconcile first.

        Framework only. ``Platform.start()`` is unchanged and does not block on
        this; no venue is queried; paper trading is unaffected.

        What it produces is the shape of the requirement: which sources a live
        start must have, which are actually present, and the two conditions
        that cannot be waived — no unresolved critical discrepancy, and no
        order whose venue-side state is unknown. A platform that enabled
        execution while holding either would be trading against books it could
        not vouch for.
        """
        snapshot = self.capture_snapshot(now_ms)
        return StartupReconciliationRequest(
            created_at=now_ms,
            trigger=ReconciliationTrigger.STARTUP,
            required_sources=[
                ReconciliationSourceKind.EXECUTION,
                ReconciliationSourceKind.ACCOUNT,
                # A live start additionally requires the venue's own account.
                # Listed as required so the request reports it missing rather
                # than quietly succeeding without it.
                ReconciliationSourceKind.VENUE,
            ],
            available_sources=snapshot.available_sources,
            require_no_critical=True,
            require_no_unknown_orders=True,
            reason=reason or "startup reconciliation requirement",
        )

    # -- queries -----------------------------------------------------------

    def get_run(self, run_id: str) -> ReconciliationRunRecord | None:
        return self.registry.get_run(run_id)

    def latest_run(self) -> ReconciliationRunRecord | None:
        return self.registry.latest_run()

    def open_discrepancies(self) -> list[ReconciliationDiscrepancy]:
        return self.registry.open_discrepancies()

    def open_critical(self) -> list[ReconciliationDiscrepancy]:
        return self.registry.open_critical()

    def discrepancies_for_run(self, run_id: str) -> list[ReconciliationDiscrepancy]:
        return self.registry.discrepancies_for_run(run_id)

    def resolutions_for(self, discrepancy_id: str) -> list[ReconciliationResolution]:
        return self.registry.resolutions_for_discrepancy(discrepancy_id)

    def metrics(self) -> ReconciliationMetrics:
        """Counters. No thresholds, no rates, no alerting, no backend."""
        return self.registry.metrics()

    # -- retention ---------------------------------------------------------

    def compact_reconciliation_history(self, *, keep_terminal: bool = True) -> int:
        """Release finished workflow records. Releases nothing by default.

        Separate from :meth:`compact`, which seals the fill log and archives
        orders and is unchanged. This one touches only the Phase 7 registry.

        An OPEN discrepancy, a pending resolution, and any run holding either
        are never eligible, whatever the arguments say.
        """
        return self.registry.compact(keep_terminal=keep_terminal)

    # -- compaction --------------------------------------------------------

    def _seal_boundary(self) -> int:
        """How much of the fill log can leave memory.

        A fill is sealable when its order is terminal *and* every fill of
        that order sits inside the same prefix. Both halves matter:

        * terminal, because a live order can still produce fills that change
          the position the checkpoint would have frozen;
        * wholly inside the prefix, because otherwise an order could keep one
          sealed fill and one unsealed fill. It would then stay resident in
          the OMS while the account had already sealed part of it away, and
          the fill-set comparison would report a missing fill that never went
          missing — compaction manufacturing its own mismatch.

        Returns an index into ``account.fill_log``.
        """
        log = self.account.fill_log
        boundary = len(log)
        for index, fill in enumerate(log):
            order = self.oms.get(fill.client_order_id)
            if order is None or not order.is_terminal:
                boundary = index
                break
        if boundary == 0:
            return 0
        # Pull the boundary back past any order that straddles it.
        straddling = {fill.client_order_id for fill in log[boundary:]}
        while boundary > 0 and log[boundary - 1].client_order_id in straddling:
            boundary -= 1
        return boundary

    def compact(self) -> tuple[int, int]:
        """Seal verified history and archive the orders it belongs to.

        Only ever called after a reconciliation run with no critical
        mismatch, so nothing leaves memory unverified.

        Sealing is a *backstop*, not a routine step: history stays fully
        resident — and therefore fully re-verified on every run — until it
        exceeds the account's retention window. A session that never reaches
        the window is never compacted at all and keeps end-to-end replay
        coverage from the first fill. Only beyond it does the ledger trade
        re-verification of old, already-checked history for a flat footprint.

        Returns ``(fills_sealed, orders_archived)``.
        """
        excess = len(self.account.fill_log) - self.account.retained_fills
        if excess <= 0:
            return 0, 0
        sealed = self.account.seal(min(self._seal_boundary(), excess))
        unsealed = {fill.fill_id for fill in self.account.fill_log}
        archived = self.oms.compact(unsealed)
        self.fills_sealed += sealed
        self.orders_archived += archived
        return sealed, archived

    async def run(self, now_ms: Millis | None = None) -> ReconciliationResult:
        result = self.reconcile(now_ms)
        if result.ok:
            self.compact()
        # Phase 7: record the workflow, strictly after the answer exists.
        # ``result`` is already final here — the mirror reads it and writes to
        # the registry, never the other way round — so it cannot influence
        # reconciliation, the published event, or the orchestrator's
        # protection path. Nothing downstream consults the registry.
        self.mirror_result(
            result, result.created_at, trigger=ReconciliationTrigger.PERIODIC
        )
        await self.bus.publish(
            Event(
                type=(
                    EventType.RECONCILIATION_MISMATCH
                    if not result.ok
                    else EventType.RECONCILIATION_COMPLETE
                ),
                ts_ms=result.created_at,
                source=SERVICE,
                schema_name="ReconciliationResult",
                payload=result.to_json_dict(),
            )
        )
        self._heartbeat(result)
        return result

    def heartbeat(self) -> None:
        """Report liveness between full reconciliation runs.

        "I am alive" and "I just reconciled everything" are different claims,
        and they run on different cadences. Tying them together would make a
        component that works every N ticks look dead for N-1 of them.
        """
        if self.last_result is None:
            self.health.heartbeat(
                SERVICE,
                status=HealthStatus.OFFLINE,
                version=VERSION,
                detail="no reconciliation has run yet",
            )
            return
        self._heartbeat(self.last_result)

    def _heartbeat(self, result: ReconciliationResult) -> None:
        status = HealthStatus.HEALTHY
        detail = ""
        if not result.ok:
            status = HealthStatus.OFFLINE
            detail = f"{len(result.critical)} critical mismatches"
        elif result.mismatches:
            status = HealthStatus.DEGRADED
            detail = f"{len(result.mismatches)} warnings"
        self.health.heartbeat(
            SERVICE,
            status=status,
            queue_depth=self.bus.queue_depth,
            version=VERSION,
            detail=detail,
        )


__all__ = [
    "CASH_TOLERANCE",
    "MONEY_EPSILON",
    "QTY_EPSILON",
    "QTY_TOLERANCE",
    "SERVICE",
    "VERSION",
    "Marin",
]
