"""The hedge registry — what became of each request to close a residual.

OKAPI owns hedge economics; this registry records the resulting request and
links it to downstream identifiers. It is observational only: persistence or
reader behaviour must never become trading control flow.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from core.models.common import Millis, Side
from core.models.hedging import (
    HedgeMetrics,
    HedgeOutcomeSummary,
    HedgeRequestRecord,
    HedgeRequestStatus,
    ResidualCause,
)
from core.models.ops import DeltaReport, HedgeIntent

log = logging.getLogger(__name__)


class HedgeStore(ABC):
    """Optional persistence seam for hedge history."""

    @abstractmethod
    def put_hedge(self, record: HedgeRequestRecord) -> None: ...

    @abstractmethod
    def get_hedge(self, hedge_id: str) -> HedgeRequestRecord | None: ...

    @abstractmethod
    def outstanding_hedges(self) -> list[HedgeRequestRecord]: ...


@dataclass
class HedgeRegistry:
    """Hedge requests and their execution linkage, in memory."""

    hedges: dict[str, HedgeRequestRecord] = field(default_factory=dict)
    outcomes: dict[str, HedgeOutcomeSummary] = field(default_factory=dict)
    store: HedgeStore | None = None
    _by_intent: dict[str, str] = field(default_factory=dict)
    _order: list[str] = field(default_factory=list)

    delta_snapshots: int = 0
    residuals_detected: int = 0
    hedges_proposed: int = 0
    hedges_submitted: int = 0
    hedges_completed: int = 0
    hedges_cancelled: int = 0
    hedges_unknown: int = 0
    hedges_failed: int = 0
    requested_notional_total: float = 0.0
    completed_notional_total: float = 0.0

    def register_request(
        self,
        intent: HedgeIntent,
        now_ms: Millis,
        *,
        strategy: str = "",
        tolerance: float = 0.0,
        cause: ResidualCause = ResidualCause.UNCLASSIFIED,
        status: HedgeRequestStatus = HedgeRequestStatus.PROPOSED,
    ) -> HedgeRequestRecord:
        """Record an already-built hedge intent, idempotently by intent id."""
        existing = self._by_intent.get(intent.hedge_id)
        if existing is not None:
            held = self.hedges.get(existing)
            if held is not None:
                return held

        record = HedgeRequestRecord(
            created_at=now_ms,
            updated_at=now_ms,
            symbol=intent.symbol,
            strategy=strategy,
            status=status,
            cause=cause,
            target_delta=intent.target_delta,
            observed_delta=intent.current_delta,
            residual_delta=intent.current_delta - intent.target_delta,
            requested_notional=intent.notional,
            side=intent.side,
            venue=intent.venue,
            tolerance=tolerance,
            urgency=intent.urgency,
            reason_codes=list(intent.reason_codes),
            hedge_intent_id=intent.hedge_id,
        )
        self.hedges[record.hedge_id] = record
        self._by_intent[intent.hedge_id] = record.hedge_id
        self._order.append(record.hedge_id)
        self.hedges_proposed += 1
        self.residuals_detected += 1
        self.requested_notional_total += intent.notional
        self._persist(record)
        return record

    def register_residual(
        self,
        report: DeltaReport,
        now_ms: Millis,
        *,
        strategy: str = "",
        side: Side | None = None,
        venue: str | None = None,
    ) -> HedgeRequestRecord:
        record = HedgeRequestRecord(
            created_at=now_ms,
            updated_at=now_ms,
            symbol=report.symbol,
            strategy=strategy,
            status=HedgeRequestStatus.DETECTED,
            target_delta=report.desired_delta,
            observed_delta=report.actual_delta,
            residual_delta=report.unhedged_delta,
            tolerance=report.tolerance,
            side=side,
            venue=venue,
        )
        self.hedges[record.hedge_id] = record
        self._order.append(record.hedge_id)
        self.residuals_detected += 1
        self._persist(record)
        return record

    def set_status(
        self,
        hedge_id: str,
        status: HedgeRequestStatus,
        now_ms: Millis,
        *,
        note: str = "",
    ) -> HedgeRequestRecord | None:
        record = self.hedges.get(hedge_id)
        if record is None:
            return None
        previous = record.status
        record.status = status
        record.updated_at = now_ms
        if note:
            record.notes = [*record.notes, note]

        if status is not previous:
            if status is HedgeRequestStatus.SUBMITTING:
                self.hedges_submitted += 1
            elif status is HedgeRequestStatus.COMPLETE:
                self.hedges_completed += 1
            elif status is HedgeRequestStatus.CANCELLED:
                self.hedges_cancelled += 1
            elif status is HedgeRequestStatus.FAILED:
                self.hedges_failed += 1
            elif status is HedgeRequestStatus.UNKNOWN:
                self.hedges_unknown += 1

        if record.is_terminal and record.terminal_at is None:
            record.terminal_at = now_ms
        elif not record.is_terminal:
            record.terminal_at = None

        self._persist(record)
        return record

    def set_cause(
        self, hedge_id: str, cause: ResidualCause, now_ms: Millis
    ) -> HedgeRequestRecord | None:
        record = self.hedges.get(hedge_id)
        if record is None:
            return None
        record.cause = cause
        record.updated_at = now_ms
        self._persist(record)
        return record

    def attach_hedge_intent(
        self, hedge_id: str, hedge_intent_id: str, now_ms: Millis
    ) -> HedgeRequestRecord | None:
        record = self.hedges.get(hedge_id)
        if record is None:
            return None
        record.hedge_intent_id = hedge_intent_id
        record.updated_at = now_ms
        self._by_intent[hedge_intent_id] = hedge_id
        self._persist(record)
        return record

    def attach_trade_intent(
        self, hedge_id: str, trade_intent_id: str, now_ms: Millis
    ) -> HedgeRequestRecord | None:
        record = self.hedges.get(hedge_id)
        if record is None:
            return None
        record.trade_intent_id = trade_intent_id
        record.updated_at = now_ms
        self._persist(record)
        return record

    def attach_execution_plan(
        self, hedge_id: str, plan_id: str, now_ms: Millis
    ) -> HedgeRequestRecord | None:
        record = self.hedges.get(hedge_id)
        if record is None:
            return None
        if plan_id not in record.execution_plan_ids:
            record.execution_plan_ids = [*record.execution_plan_ids, plan_id]
        record.updated_at = now_ms
        self._persist(record)
        return record

    def attach_orders(
        self, hedge_id: str, order_ids: list[str], now_ms: Millis
    ) -> HedgeRequestRecord | None:
        record = self.hedges.get(hedge_id)
        if record is None:
            return None
        for order_id in order_ids:
            if order_id not in record.order_ids:
                record.order_ids = [*record.order_ids, order_id]
        record.updated_at = now_ms
        self._persist(record)
        return record

    def attach_source_opportunity(
        self, hedge_id: str, opportunity_id: str, now_ms: Millis
    ) -> HedgeRequestRecord | None:
        record = self.hedges.get(hedge_id)
        if record is None:
            return None
        if opportunity_id not in record.source_opportunity_ids:
            record.source_opportunity_ids = [
                *record.source_opportunity_ids,
                opportunity_id,
            ]
        record.updated_at = now_ms
        self._persist(record)
        return record

    def link_reconciliation_run(
        self, hedge_id: str, run_id: str, now_ms: Millis
    ) -> HedgeRequestRecord | None:
        record = self.hedges.get(hedge_id)
        if record is None:
            return None
        if run_id not in record.reconciliation_run_ids:
            record.reconciliation_run_ids = [*record.reconciliation_run_ids, run_id]
        record.updated_at = now_ms
        self._persist(record)
        return record

    def record_outcome(
        self, outcome: HedgeOutcomeSummary
    ) -> HedgeOutcomeSummary | None:
        """Store measured outcome with idempotent lifetime accounting."""
        if outcome.hedge_id not in self.hedges:
            return None
        previous = self.outcomes.get(outcome.hedge_id)
        previous_filled = previous.filled_notional if previous is not None else 0.0
        held = outcome.model_copy(deep=True)
        self.outcomes[outcome.hedge_id] = held
        self.completed_notional_total += held.filled_notional - previous_filled
        return held.model_copy(deep=True)

    def _copy_record(self, record: HedgeRequestRecord | None) -> HedgeRequestRecord | None:
        return record.model_copy(deep=True) if record is not None else None

    def _resident_all(self) -> list[HedgeRequestRecord]:
        return [self.hedges[hid] for hid in self._order if hid in self.hedges]

    def get(self, hedge_id: str) -> HedgeRequestRecord | None:
        return self._copy_record(self.hedges.get(hedge_id))

    def for_intent(self, hedge_intent_id: str) -> HedgeRequestRecord | None:
        hedge_id = self._by_intent.get(hedge_intent_id)
        return self._copy_record(self.hedges.get(hedge_id) if hedge_id else None)

    def all(self) -> list[HedgeRequestRecord]:
        return [record.model_copy(deep=True) for record in self._resident_all()]

    def active(self) -> list[HedgeRequestRecord]:
        return [
            record.model_copy(deep=True)
            for record in self._resident_all()
            if record.is_active
        ]

    def outstanding(self) -> list[HedgeRequestRecord]:
        return [
            record.model_copy(deep=True)
            for record in self._resident_all()
            if record.is_outstanding
        ]

    def unknown(self) -> list[HedgeRequestRecord]:
        return [
            record.model_copy(deep=True)
            for record in self._resident_all()
            if record.is_unknown
        ]

    def for_symbol(self, symbol: str) -> list[HedgeRequestRecord]:
        return [
            record.model_copy(deep=True)
            for record in self._resident_all()
            if record.symbol == symbol
        ]

    def for_opportunity(self, opportunity_id: str) -> list[HedgeRequestRecord]:
        return [
            record.model_copy(deep=True)
            for record in self._resident_all()
            if opportunity_id in record.source_opportunity_ids
        ]

    def outcome(self, hedge_id: str) -> HedgeOutcomeSummary | None:
        outcome = self.outcomes.get(hedge_id)
        return outcome.model_copy(deep=True) if outcome is not None else None

    def recent(self, limit: int = 20) -> list[HedgeRequestRecord]:
        if limit <= 0:
            return []
        return [
            self.hedges[hid].model_copy(deep=True)
            for hid in self._order[-limit:]
            if hid in self.hedges
        ]

    def metrics(self) -> HedgeMetrics:
        return HedgeMetrics(
            delta_snapshots=self.delta_snapshots,
            residuals_detected=self.residuals_detected,
            hedges_proposed=self.hedges_proposed,
            hedges_submitted=self.hedges_submitted,
            hedges_completed=self.hedges_completed,
            hedges_cancelled=self.hedges_cancelled,
            hedges_unknown=self.hedges_unknown,
            hedges_failed=self.hedges_failed,
            requested_notional=self.requested_notional_total,
            completed_notional=self.completed_notional_total,
        )

    def compact(self, *, keep_terminal: bool = True) -> int:
        if keep_terminal:
            return 0
        doomed = [
            record
            for record in self._resident_all()
            if record.is_terminal
            and not record.is_active
            and not record.reconciliation_run_ids
        ]
        for record in doomed:
            self.hedges.pop(record.hedge_id, None)
            self.outcomes.pop(record.hedge_id, None)
            if record.hedge_intent_id is not None:
                if self._by_intent.get(record.hedge_intent_id) == record.hedge_id:
                    self._by_intent.pop(record.hedge_intent_id, None)
        self._order = [hid for hid in self._order if hid in self.hedges]
        return len(doomed)

    @property
    def resident_hedges(self) -> int:
        return len(self.hedges)

    def _persist(self, record: HedgeRequestRecord) -> None:
        """Best-effort mirror: persistence can never become hedge control flow."""
        if self.store is None:
            return
        try:
            self.store.put_hedge(record)
        except Exception:
            log.exception(
                "optional hedge store write failed",
                extra={"hedge_id": record.hedge_id},
            )


__all__ = ["HedgeRegistry", "HedgeStore"]
