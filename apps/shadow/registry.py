"""The shadow registry — what the platform would have done, recorded.

WHAT THIS IS
============
During a shadow session the whole platform runs against real public market
data: detection, consensus, RUNE, VESKA planning, ``PaperExecutor``, OKAPI,
MARIN. :class:`ShadowRegistry` records one decision record per opportunity and
links it to the identities every other layer already mints.

WHAT THIS IS NOT
================
* **No economic authority.** It decides nothing, sizes nothing, routes nothing
  and gates nothing. Every value it holds was produced elsewhere and copied.
* **No second simulator.** Execution numbers come from what ``PaperExecutor``
  and ``PaperAccount`` produced. A second fill model could disagree with the
  one whose P&L the account actually carries.
* **No second ledger.** There is one ``PaperAccount``, and the shadow snapshot
  summarises it rather than keeping its own.
* **No parallel identities.** Records key on ``opportunity_id``,
  ``correlation_id``, ``intent_id``, ``plan_id`` and ``client_order_id`` as
  those layers minted them. ``shadow_decision_id`` exists only because the
  registry needs its own key.

Every mutation takes ``now_ms`` explicitly. Nothing here reads a clock.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from core.models.common import Millis
from core.models.shadow import (
    ExecutionProvenance,
    MarketDataProvenance,
    ShadowDecisionRecord,
    ShadowDecisionStatus,
    ShadowExecutionRecord,
    ShadowMarketCheckpoint,
    ShadowOrderSummary,
    ShadowOutcomeCheckpoint,
    ShadowReadiness,
    ShadowSnapshot,
)

log = logging.getLogger(__name__)


class ShadowStore(ABC):
    """The persistence seam for shadow history.

    **No implementation exists.** In-memory is sufficient for a rehearsal whose
    events are already being recorded by the ``Recorder``; choosing a schema
    now would fix the shape of queries nobody has written.
    """

    @abstractmethod
    def put_decision(self, record: ShadowDecisionRecord) -> None: ...

    @abstractmethod
    def put_execution(self, record: ShadowExecutionRecord) -> None: ...

    @abstractmethod
    def get_decision(self, decision_id: str) -> ShadowDecisionRecord | None: ...


@dataclass
class ShadowRegistry:
    """Shadow decisions, executions and checkpoints, in memory."""

    decisions: dict[str, ShadowDecisionRecord] = field(default_factory=dict)
    executions: dict[str, ShadowExecutionRecord] = field(default_factory=dict)
    market_checkpoints: dict[str, list[ShadowMarketCheckpoint]] = field(
        default_factory=dict
    )
    outcome_checkpoints: dict[str, list[ShadowOutcomeCheckpoint]] = field(
        default_factory=dict
    )

    #: Optional write-through persistence. See :class:`ShadowStore`.
    store: ShadowStore | None = None

    #: Where prices come from in this session. Stamped onto every record so no
    #: reader has to infer it.
    market_data: MarketDataProvenance = MarketDataProvenance.UNKNOWN

    #: ``opportunity_id`` -> ``shadow_decision_id``.
    _by_opportunity: dict[str, str] = field(default_factory=dict)
    #: ``plan_id`` -> ``shadow_execution_id``.
    _by_plan: dict[str, str] = field(default_factory=dict)
    #: Registration order, so "recent" is a slice.
    _order: list[str] = field(default_factory=list)

    #: Lifetime counters, unaffected by any future compaction.
    decisions_observed: int = 0
    decisions_authorized: int = 0
    decisions_rejected: int = 0
    plans_recorded: int = 0
    orders_recorded: int = 0
    fills_recorded: int = 0
    checkpoints_captured: int = 0
    outcomes_captured: int = 0

    # ------------------------------------------------------------------
    # decisions
    # ------------------------------------------------------------------

    def register_decision(
        self,
        opportunity_id: str,
        now_ms: Millis,
        *,
        correlation_id: str | None = None,
        strategy: str = "",
        symbol: str = "",
    ) -> ShadowDecisionRecord:
        """Open the record for one rehearsed opportunity.

        Idempotent by ``opportunity_id``: an opportunity is worked over several
        ticks, and a second record for the same one would split its history.
        """
        existing_id = self._by_opportunity.get(opportunity_id)
        if existing_id is not None:
            existing = self.decisions.get(existing_id)
            if existing is not None:
                return existing

        record = ShadowDecisionRecord(
            created_at=now_ms,
            updated_at=now_ms,
            opportunity_id=opportunity_id,
            correlation_id=correlation_id or opportunity_id,
            strategy=strategy,
            symbol=symbol,
            status=ShadowDecisionStatus.OBSERVED,
            market_data=self.market_data,
        )
        self.decisions[record.shadow_decision_id] = record
        self._by_opportunity[opportunity_id] = record.shadow_decision_id
        self._order.append(record.shadow_decision_id)
        self.decisions_observed += 1
        self._persist_decision(record)
        return record

    def set_status(
        self,
        decision_id: str,
        status: ShadowDecisionStatus,
        now_ms: Millis,
        *,
        reason: str = "",
    ) -> ShadowDecisionRecord | None:
        """Move a decision to a status the platform has already reached.

        The registry derives nothing. The orchestrator's ``StrategyState``,
        RUNE's verdict and the order lifecycle decide; this copies the outcome
        down. Counters advance on the transition, so a status re-applied twice
        is counted once.
        """
        record = self.decisions.get(decision_id)
        if record is None:
            return None
        previous = record.status
        record.status = status
        record.updated_at = now_ms
        if reason and reason not in record.reason_codes:
            record.reason_codes = [*record.reason_codes, reason]

        if status is not previous:
            if status is ShadowDecisionStatus.AUTHORIZED:
                self.decisions_authorized += 1
            elif status is ShadowDecisionStatus.RISK_REJECTED:
                self.decisions_rejected += 1

        if record.is_terminal and record.terminal_at is None:
            record.terminal_at = now_ms
        elif not record.is_terminal:
            record.terminal_at = None

        self._persist_decision(record)
        return record

    # -- linkage -----------------------------------------------------------

    def link_consensus(
        self,
        decision_id: str,
        evaluation_id: str,
        now_ms: Millis,
        *,
        request_id: str | None = None,
    ) -> ShadowDecisionRecord | None:
        """Link Phase 8's consensus evaluation. The consensus is not copied."""
        record = self.decisions.get(decision_id)
        if record is None:
            return None
        record.consensus_evaluation_id = evaluation_id
        if request_id and request_id not in record.consensus_request_ids:
            record.consensus_request_ids = [*record.consensus_request_ids, request_id]
        record.updated_at = now_ms
        self._persist_decision(record)
        return record

    def link_intent(
        self, decision_id: str, intent_id: str, now_ms: Millis
    ) -> ShadowDecisionRecord | None:
        record = self.decisions.get(decision_id)
        if record is None:
            return None
        record.intent_id = intent_id
        record.updated_at = now_ms
        self._persist_decision(record)
        return record

    def link_risk(
        self,
        decision_id: str,
        risk_decision_id: str,
        now_ms: Millis,
        *,
        approved: bool | None = None,
        requested_notional: float = 0.0,
        approved_notional: float = 0.0,
    ) -> ShadowDecisionRecord | None:
        """Link RUNE's decision, copying what it answered.

        ``approved`` is what the verdict said, passed down by the caller that
        read it. It is never derived from the notionals — deriving it would put
        a second, untested risk reading beside the real one.
        """
        record = self.decisions.get(decision_id)
        if record is None:
            return None
        record.risk_decision_id = risk_decision_id
        record.approved = approved
        record.requested_notional = requested_notional
        record.approved_notional = approved_notional
        record.updated_at = now_ms
        self._persist_decision(record)
        return record

    def link_plan(
        self, decision_id: str, plan_id: str, now_ms: Millis
    ) -> ShadowDecisionRecord | None:
        """Link a plan by id. Phase 6's registry still owns the plan itself."""
        record = self.decisions.get(decision_id)
        if record is None:
            return None
        if plan_id not in record.execution_plan_ids:
            record.execution_plan_ids = [*record.execution_plan_ids, plan_id]
            self.plans_recorded += 1
        self._by_plan.setdefault(plan_id, "")
        record.updated_at = now_ms
        self._persist_decision(record)
        return record

    def link_orders(
        self, decision_id: str, order_ids: list[str], now_ms: Millis
    ) -> ShadowDecisionRecord | None:
        record = self.decisions.get(decision_id)
        if record is None:
            return None
        for order_id in order_ids:
            if order_id not in record.paper_order_ids:
                record.paper_order_ids = [*record.paper_order_ids, order_id]
                self.orders_recorded += 1
        record.updated_at = now_ms
        self._persist_decision(record)
        return record

    def link_hedge(
        self, decision_id: str, hedge_id: str, now_ms: Millis
    ) -> ShadowDecisionRecord | None:
        """Link a Phase 9 hedge whose residual came from this trade.

        Nothing calls this from the tick path. OKAPI measures residuals from
        portfolio delta, which does not say which trade left them; an explicit
        caller with that evidence attaches the link.
        """
        record = self.decisions.get(decision_id)
        if record is None:
            return None
        if hedge_id not in record.hedge_ids:
            record.hedge_ids = [*record.hedge_ids, hedge_id]
        record.updated_at = now_ms
        self._persist_decision(record)
        return record

    def link_reconciliation_run(
        self, decision_id: str, run_id: str, now_ms: Millis
    ) -> ShadowDecisionRecord | None:
        """Link a Phase 7 reconciliation run, for observability only.

        MARIN keeps reconciling the paper OMS against the paper account, and
        **no new comparison is introduced**. There is no venue side to
        reconcile against, because no authenticated venue exists.
        """
        record = self.decisions.get(decision_id)
        if record is None:
            return None
        if run_id not in record.reconciliation_run_ids:
            record.reconciliation_run_ids = [*record.reconciliation_run_ids, run_id]
        record.updated_at = now_ms
        self._persist_decision(record)
        return record

    # ------------------------------------------------------------------
    # execution records
    # ------------------------------------------------------------------

    def register_execution(
        self,
        decision_id: str,
        now_ms: Millis,
        *,
        plan_id: str | None = None,
        planned_notional: float = 0.0,
    ) -> ShadowExecutionRecord | None:
        """Open the execution record for one plan.

        ``execution_provenance`` is ``PAPER_SIMULATOR`` and stays that way.
        These are the simulator's numbers, not a venue's, and the field says so
        rather than leaving it to a docstring nobody reads at query time.
        """
        if decision_id not in self.decisions:
            return None
        if plan_id is not None:
            existing_id = self._by_plan.get(plan_id)
            if existing_id:
                existing = self.executions.get(existing_id)
                if existing is not None:
                    return existing

        record = ShadowExecutionRecord(
            decision_id=decision_id,
            plan_id=plan_id,
            created_at=now_ms,
            updated_at=now_ms,
            planned_notional=planned_notional,
            status=ShadowDecisionStatus.PLANNED,
            execution_provenance=ExecutionProvenance.PAPER_SIMULATOR,
            market_data=self.market_data,
        )
        self.executions[record.shadow_execution_id] = record
        if plan_id is not None:
            self._by_plan[plan_id] = record.shadow_execution_id
        self._persist_execution(record)
        return record

    def record_orders(
        self,
        execution_id: str,
        orders: list[ShadowOrderSummary],
        now_ms: Millis,
    ) -> ShadowExecutionRecord | None:
        """Copy the paper orders behind one plan.

        Replaces rather than appends: an order's status, filled quantity and
        average price all move, and keeping both the old and new copies would
        make the record report two states for one order.
        """
        record = self.executions.get(execution_id)
        if record is None:
            return None
        record.orders = list(orders)
        record.updated_at = now_ms
        self._persist_execution(record)
        return record

    def link_fills(
        self,
        execution_id: str,
        fill_ids: list[str],
        now_ms: Millis,
        *,
        paper_filled_notional: float | None = None,
        paper_fees: float | None = None,
        paper_slippage_bps: float | None = None,
    ) -> ShadowExecutionRecord | None:
        """Copy what the simulator filled.

        **These are not real fills.** They are ``FillSimulator``'s estimate of
        what might have executed, against a book nobody traded into. Every
        value here is copied from what ``PaperExecutor`` produced; the shadow
        layer computes no fill of its own.
        """
        record = self.executions.get(execution_id)
        if record is None:
            return None
        for fill_id in fill_ids:
            if fill_id not in record.paper_fill_ids:
                record.paper_fill_ids = [*record.paper_fill_ids, fill_id]
                self.fills_recorded += 1
        if paper_filled_notional is not None:
            record.paper_filled_notional = paper_filled_notional
        if paper_fees is not None:
            record.paper_fees = paper_fees
        if paper_slippage_bps is not None:
            record.paper_slippage_bps = paper_slippage_bps
        record.updated_at = now_ms
        self._persist_execution(record)
        return record

    def set_execution_status(
        self, execution_id: str, status: ShadowDecisionStatus, now_ms: Millis
    ) -> ShadowExecutionRecord | None:
        record = self.executions.get(execution_id)
        if record is None:
            return None
        record.status = status
        record.updated_at = now_ms
        if status in (
            ShadowDecisionStatus.PAPER_COMPLETE,
            ShadowDecisionStatus.CLOSED,
            ShadowDecisionStatus.FAILED,
        ):
            record.terminal_at = now_ms
        self._persist_execution(record)
        return record

    # ------------------------------------------------------------------
    # market follow-through
    # ------------------------------------------------------------------

    def add_market_checkpoint(
        self, checkpoint: ShadowMarketCheckpoint
    ) -> ShadowMarketCheckpoint | None:
        """Store a captured market observation.

        **Nothing schedules these.** There is no timer, no horizon policy and
        no default set of horizons — one second, five, thirty, a minute are all
        common, and choosing here would smuggle a research decision into a
        construction phase. A caller captures when it wants to.
        """
        if checkpoint.decision_id not in self.decisions:
            return None
        self.market_checkpoints.setdefault(checkpoint.decision_id, []).append(
            checkpoint
        )
        self.checkpoints_captured += 1
        return checkpoint

    def add_outcome_checkpoint(
        self, checkpoint: ShadowOutcomeCheckpoint
    ) -> ShadowOutcomeCheckpoint | None:
        """Store where the market went and what the paper position was worth.

        **Nothing classifies the outcome.** No pass, no fail, no score. A price
        moved and a simulated position had a value; what that means about the
        decision is research this phase has no hypothesis for.
        """
        if checkpoint.decision_id not in self.decisions:
            return None
        self.outcome_checkpoints.setdefault(checkpoint.decision_id, []).append(
            checkpoint
        )
        self.outcomes_captured += 1
        return checkpoint

    def checkpoints_for(self, decision_id: str) -> list[ShadowMarketCheckpoint]:
        return list(self.market_checkpoints.get(decision_id, []))

    def outcomes_for(self, decision_id: str) -> list[ShadowOutcomeCheckpoint]:
        return list(self.outcome_checkpoints.get(decision_id, []))

    # ------------------------------------------------------------------
    # queries
    # ------------------------------------------------------------------

    def get(self, decision_id: str) -> ShadowDecisionRecord | None:
        return self.decisions.get(decision_id)

    def for_opportunity(self, opportunity_id: str) -> ShadowDecisionRecord | None:
        decision_id = self._by_opportunity.get(opportunity_id)
        return self.decisions.get(decision_id) if decision_id else None

    def all(self) -> list[ShadowDecisionRecord]:
        """Every resident decision, in registration order."""
        return [self.decisions[did] for did in self._order if did in self.decisions]

    def recent(self, limit: int = 20) -> list[ShadowDecisionRecord]:
        if limit <= 0:
            return []
        return [
            self.decisions[did] for did in self._order[-limit:] if did in self.decisions
        ]

    def active(self) -> list[ShadowDecisionRecord]:
        """Decisions currently known to be working. Excludes UNKNOWN."""
        return [record for record in self.all() if record.is_active]

    def unknown(self) -> list[ShadowDecisionRecord]:
        """Decisions whose venue-side truth is unresolved. Never auto-resolved."""
        return [record for record in self.all() if record.is_unknown]

    def for_symbol(self, symbol: str) -> list[ShadowDecisionRecord]:
        return [record for record in self.all() if record.symbol == symbol]

    def execution_records(
        self, decision_id: str | None = None
    ) -> list[ShadowExecutionRecord]:
        records = list(self.executions.values())
        if decision_id is None:
            return records
        return [r for r in records if r.decision_id == decision_id]

    def snapshot(
        self,
        now_ms: Millis,
        *,
        session_id: str | None = None,
        enabled: bool = False,
        paper_equity: float = 0.0,
        paper_pnl: float = 0.0,
        readiness: ShadowReadiness | None = None,
    ) -> ShadowSnapshot:
        """Counts and ids, never records.

        ``paper_equity`` and ``paper_pnl`` are supplied by the caller from the
        platform's single ``PaperAccount``. The registry keeps no ledger of its
        own — two ledgers would eventually disagree and nobody would know which
        to believe.
        """
        return ShadowSnapshot(
            created_at=now_ms,
            session_id=session_id,
            enabled=enabled,
            decisions_total=len(self.decisions),
            authorized=self.decisions_authorized,
            rejected=self.decisions_rejected,
            paper_plans=self.plans_recorded,
            paper_orders=self.orders_recorded,
            paper_fills=self.fills_recorded,
            active_decision_ids=[r.shadow_decision_id for r in self.active()],
            unknown_decision_ids=[r.shadow_decision_id for r in self.unknown()],
            current_paper_equity=paper_equity,
            current_paper_pnl=paper_pnl,
            market_checkpoints=self.checkpoints_captured,
            outcome_checkpoints=self.outcomes_captured,
            market_data=self.market_data,
            execution_provenance=ExecutionProvenance.PAPER_SIMULATOR,
            readiness=readiness,
        )

    # ------------------------------------------------------------------
    # retention
    # ------------------------------------------------------------------

    def compact(self, *, keep_all: bool = True) -> int:
        """Release finished decisions. Conservative by construction.

        With the default **nothing is released**, and there is no arbitrary
        count limit. Even when asked to release, these are absolute:

        * an active decision is never released;
        * an UNKNOWN decision is never released — UNKNOWN means the venue-side
          truth is unresolved, and a rehearsal that discarded the cases it
          could not account for would be rehearsing only the easy ones;
        * a decision linked to a hedge or a reconciliation run is never
          released, because those may still be establishing what happened;
        * a decision with checkpoints is never released, since the follow-
          through is the thing a shadow session exists to capture.

        Returns the number of decision records released.
        """
        if keep_all:
            return 0
        doomed = [
            record
            for record in self.all()
            if record.is_terminal
            and not record.is_active
            and not record.hedge_ids
            and not record.reconciliation_run_ids
            and not self.market_checkpoints.get(record.shadow_decision_id)
            and not self.outcome_checkpoints.get(record.shadow_decision_id)
        ]
        for record in doomed:
            for execution in self.execution_records(record.shadow_decision_id):
                self.executions.pop(execution.shadow_execution_id, None)
                if execution.plan_id is not None:
                    self._by_plan.pop(execution.plan_id, None)
            self.decisions.pop(record.shadow_decision_id, None)
            self._by_opportunity.pop(record.opportunity_id, None)
        self._order = [did for did in self._order if did in self.decisions]
        return len(doomed)

    @property
    def resident_decisions(self) -> int:
        return len(self.decisions)

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _persist_decision(self, record: ShadowDecisionRecord) -> None:
        if self.store is not None:
            self.store.put_decision(record)

    def _persist_execution(self, record: ShadowExecutionRecord) -> None:
        if self.store is not None:
            self.store.put_execution(record)


__all__ = ["ShadowRegistry", "ShadowStore"]
