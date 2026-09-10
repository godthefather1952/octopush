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

The legacy comparison remains the safety answer.  The framework records and
exposes that truth without inventing venue evidence or silently correcting
internal state.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from core.bus import EventBus
from core.clock import Clock
from core.events import Event, EventType
from core.health import HealthRegistry
from core.models.common import MONEY_EPSILON, QTY_EPSILON, Millis
from core.models.execution import ExecutionCommandResult, ExecutionSnapshot, OrderStatus
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
from agents.marin.policy import is_authoritative
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
    registry: ReconciliationRegistry = field(
        default_factory=ReconciliationRegistry
    )
    execution_source: ReconciliationSource | None = None
    account_source: ReconciliationSource | None = None
    venue_sources: list[ReconciliationSource] = field(default_factory=list)
    recorded_source: ReconciliationSource | None = None
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
        """Wire truth sources after construction."""
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

        # 3b. Lifetime counts.
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

        # 4. Fees.
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

        # 5. Order bookkeeping.
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

        # 6. UNKNOWN is unresolved truth, not a critical mismatch by itself.
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
        """Capture every configured source into one bundle at ``now_ms``."""
        execution: ExecutionSnapshot | None = None
        account: AccountSnapshot | None = None
        venues: list[VenueTruthSnapshot] = []
        recorded: RecordedTruthSnapshot | None = None
        healths: list[SourceHealth] = []
        notes: list[str] = []

        for source in self._sources():
            try:
                capture = source.capture(now_ms)
            except Exception as exc:
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
        """Open a workflow run, capture truth, and stop at CAPTURING."""
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
        """Record an already-produced result into the workflow registry."""
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
        """Record an intended response to a discrepancy without applying it."""
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
    ) -> ExecutionCommandResult:
        """Apply explicit authoritative evidence to one UNKNOWN order.

        The request is always recorded.  Internal source kinds, missing evidence,
        and fill-like statuses without already-recorded fill economics are
        rejected before VESKA is called.  Nothing here invents a fill.
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
        order = self.oms.get(client_order_id)
        current_status = order.status if order is not None else None

        def reject_request(message: str) -> ExecutionCommandResult:
            self.registry.set_resolution_status(
                resolution.resolution_id,
                ResolutionStatus.REJECTED,
                now_ms,
                note=message,
            )
            return ExecutionCommandResult(
                accepted=False,
                client_order_id=client_order_id,
                status=current_status,
                reason=message,
                at_ms=now_ms,
            )

        if not is_authoritative(source):
            return reject_request(
                f"{source.value} is not authoritative for UNKNOWN resolution"
            )
        if not evidence:
            return reject_request("authoritative resolution requires explicit evidence")

        if authoritative_status in (
            OrderStatus.FILLED,
            OrderStatus.PARTIALLY_FILLED,
        ):
            if order is None:
                return reject_request("fill economics unavailable for unknown order")
            filled_from_events = sum(fill.quantity for fill in order.fills)
            if authoritative_status is OrderStatus.FILLED:
                complete = (
                    abs(filled_from_events - order.quantity) <= QTY_TOLERANCE
                    and abs(order.filled_quantity - order.quantity) <= QTY_TOLERANCE
                )
                if not complete:
                    return reject_request(
                        "FILLED resolution requires complete fill economics"
                    )
            else:
                partial = (
                    filled_from_events > QTY_TOLERANCE
                    and filled_from_events < order.quantity - QTY_TOLERANCE
                    and abs(order.filled_quantity - filled_from_events)
                    <= QTY_TOLERANCE
                )
                if not partial:
                    return reject_request(
                        "PARTIALLY_FILLED resolution requires partial fill economics"
                    )

        self.registry.set_resolution_status(
            resolution.resolution_id, ResolutionStatus.APPLYING, now_ms
        )
        try:
            result = await veska.resolve_unknown(
                client_order_id, authoritative_status, now_ms
            )
        except Exception as exc:
            self.registry.set_resolution_status(
                resolution.resolution_id,
                ResolutionStatus.FAILED,
                now_ms,
                note=f"{type(exc).__name__}: {exc}",
            )
            raise

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
        """Whether reconciliation says the platform is in a known-good state."""
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
        """Describe what a future live start would have to reconcile first."""
        snapshot = self.capture_snapshot(now_ms)
        return StartupReconciliationRequest(
            created_at=now_ms,
            trigger=ReconciliationTrigger.STARTUP,
            required_sources=[
                ReconciliationSourceKind.EXECUTION,
                ReconciliationSourceKind.ACCOUNT,
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
        return self.registry.metrics()

    # -- retention ---------------------------------------------------------

    def compact_reconciliation_history(self, *, keep_terminal: bool = True) -> int:
        return self.registry.compact(keep_terminal=keep_terminal)

    # -- compaction --------------------------------------------------------

    def _seal_boundary(self) -> int:
        log = self.account.fill_log
        boundary = len(log)
        for index, fill in enumerate(log):
            order = self.oms.get(fill.client_order_id)
            if order is None or not order.is_terminal:
                boundary = index
                break
        if boundary == 0:
            return 0
        straddling = {fill.client_order_id for fill in log[boundary:]}
        while boundary > 0 and log[boundary - 1].client_order_id in straddling:
            boundary -= 1
        return boundary

    def compact(self) -> tuple[int, int]:
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
        """Reconcile first; accept the event before workflow/compaction commit."""
        result = self.reconcile(now_ms)
        event = Event(
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
        await self.bus.publish(event)

        # The event is now accepted.  Only now may derived workflow state and
        # clean-history compaction advance.  If publication raises, the legacy
        # safety answer remains available through last_result but the registry
        # and retained history do not claim a run the event stream never saw.
        if result.ok:
            self.compact()
        self.mirror_result(
            result, result.created_at, trigger=ReconciliationTrigger.PERIODIC
        )
        self._heartbeat(result)
        return result

    def heartbeat(self) -> None:
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
