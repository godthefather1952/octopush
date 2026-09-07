"""The hedge registry — what became of each request to close a residual.

WHAT THIS IS
============
OKAPI measures a residual and builds a ``HedgeIntent``. The orchestrator turns
that into a ``TradeIntent``, RUNE's approval is synthesised for it, VESKA plans
it, orders go out, and fills come back. Every one of those steps is recorded
somewhere — and nowhere are they recorded *together*, so "which residual
produced this order, and is it finished?" has no answer short of reading logs.

:class:`HedgeRegistry` is that answer. It holds one record per hedge request and
links it to the intent, the plan, the orders and, when a caller says so, a
reconciliation run.

WHAT IT IS NOT
==============
* **No hedge decision.** ``Okapi.build_hedges`` decides whether to hedge, which
  side, and how much. This records the result.
* **No venue selection.** ``Okapi._hedge_venue`` chooses. This copies.
* **No submission.** VESKA executes. This links ids.
* **No risk check.** RUNE owns risk, and does not read this.
* **No kill-switch action.** Nothing here can halt or flatten anything.
* **Nothing reads it to decide.** The orchestrator's ``working_hedges`` dict and
  ``_hedge_in_flight`` remain the authority on whether a hedge is in flight; no
  branch consults the registry. Deleting the registry entirely would leave the
  platform's behaviour identical.

Every mutation takes ``now_ms`` explicitly. Nothing in this module reads a
clock.
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
    """The persistence seam for hedge history.

    **No implementation exists, and none is built here.** In-memory is right
    for a paper session, and choosing a schema now would fix the shape of
    queries nobody has written yet.

    The seam is real rather than decorative: :class:`HedgeRegistry` accepts one
    and writes through when present, so a later pass adds a class instead of
    restructuring the registry. Reads still come from memory — turning the
    registry into a cache needs an invalidation story, and that belongs with
    whoever implements the backend.
    """

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

    #: Optional write-through persistence. See :class:`HedgeStore`.
    store: HedgeStore | None = None

    #: ``HedgeIntent.hedge_id`` -> our record id. The orchestrator uses the
    #: intent's id as the correlation id on every downstream record, so this is
    #: how a plan or an order finds its way back to a hedge.
    _by_intent: dict[str, str] = field(default_factory=dict)
    #: Registration order, so "the last N hedges" is a slice.
    _order: list[str] = field(default_factory=list)

    #: Lifetime counters, unaffected by any future compaction.
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

    # ------------------------------------------------------------------
    # registration
    # ------------------------------------------------------------------

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
        """Record a ``HedgeIntent`` OKAPI has already built.

        Every economic value is **copied** off the intent: side, venue,
        notional, the two deltas, urgency, reason codes. The residual is
        derived as ``current_delta - target_delta``, which is the definition
        ``delta_reports`` used to produce the intent in the first place, not a
        second opinion about it.

        ``cause`` defaults to UNCLASSIFIED and this build never passes anything
        else — portfolio delta does not say what left the exposure behind, and
        a guess would read like evidence.

        Idempotent by ``HedgeIntent.hedge_id``: re-registering one returns the
        record already held rather than opening a second.
        """
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
        """Record a residual that has been detected but not yet proposed.

        Nothing in the current hedging path calls this — ``build_hedges``
        produces an intent in the same pass that detects the residual, so
        DETECTED and PROPOSED coincide. It exists so a residual OKAPI declined
        to hedge (no usable venue, say) has somewhere to be recorded by a later
        caller, rather than vanishing.
        """
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

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    def set_status(
        self,
        hedge_id: str,
        status: HedgeRequestStatus,
        now_ms: Millis,
        *,
        note: str = "",
    ) -> HedgeRequestRecord | None:
        """Move a hedge to a status a caller has established.

        The registry does not derive the status. ``derive_hedge_status`` in
        ``agents/okapi/policy.py`` reads Phase 6's execution view and offers an
        answer; a caller decides whether to apply it. Splitting those two
        apart is what keeps the record from becoming a lifecycle engine of its
        own.

        Counters advance on the transition into a terminal or unknown state, so
        a status re-applied twice is counted once.
        """
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
            # A hedge that leaves a terminal state -- an UNKNOWN resolved back
            # to working, say -- is no longer finished, and a stale
            # ``terminal_at`` would say it was.
            record.terminal_at = None

        self._persist(record)
        return record

    def set_cause(
        self, hedge_id: str, cause: ResidualCause, now_ms: Millis
    ) -> HedgeRequestRecord | None:
        """Attribute a residual, once a caller has evidence for the cause.

        Nothing calls this. It exists so that a later phase with fill and order
        history can attribute honestly, rather than this one inferring a cause
        from a signed number.
        """
        record = self.hedges.get(hedge_id)
        if record is None:
            return None
        record.cause = cause
        record.updated_at = now_ms
        self._persist(record)
        return record

    # ------------------------------------------------------------------
    # linkage
    # ------------------------------------------------------------------

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
        """Link the ``TradeIntent`` the orchestrator built from the hedge."""
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
        """Link a plan by id. Phase 6's registry still owns the plan itself."""
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
        """Link orders by id, without duplicating their lifecycle."""
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
        """Say which trade left this residual behind.

        Nothing calls this either. OKAPI measures residuals from portfolio
        delta, which does not carry that history, and inventing an attribution
        would be worse than an empty list.
        """
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
        """Associate an authoritative reconciliation run with this hedge.

        The seam MARIN will need to resolve an UNKNOWN hedge: reconciliation
        establishes what the account and execution actually agree on, and this
        is where that finding gets attached. **No MARIN behaviour changes in
        this phase** — nothing calls this yet, and MARIN's comparison logic is
        untouched.
        """
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
        """Store what a hedge closed, once a caller has measured it.

        Nothing computes one automatically: the "after" residual needs a delta
        snapshot taken once the hedge's orders are terminal, and deciding when
        the portfolio has settled is an economic judgement this phase may not
        make.
        """
        if outcome.hedge_id not in self.hedges:
            return None
        self.outcomes[outcome.hedge_id] = outcome
        self.completed_notional_total += outcome.filled_notional
        return outcome

    # ------------------------------------------------------------------
    # queries
    # ------------------------------------------------------------------

    def get(self, hedge_id: str) -> HedgeRequestRecord | None:
        return self.hedges.get(hedge_id)

    def for_intent(self, hedge_intent_id: str) -> HedgeRequestRecord | None:
        """The record for a ``HedgeIntent.hedge_id``.

        The orchestrator uses that id as the correlation id on the trade
        intent, the plan and every order, so this is the usual way in from a
        downstream record.
        """
        hedge_id = self._by_intent.get(hedge_intent_id)
        return self.hedges.get(hedge_id) if hedge_id else None

    def all(self) -> list[HedgeRequestRecord]:
        """Every resident record, in registration order."""
        return [self.hedges[hid] for hid in self._order if hid in self.hedges]

    def active(self) -> list[HedgeRequestRecord]:
        """Hedges currently known to be working. Excludes UNKNOWN."""
        return [record for record in self.all() if record.is_active]

    def outstanding(self) -> list[HedgeRequestRecord]:
        """Hedges whose final execution truth is not known. Includes UNKNOWN."""
        return [record for record in self.all() if record.is_outstanding]

    def unknown(self) -> list[HedgeRequestRecord]:
        """Hedges whose venue-side truth is unresolved.

        Never auto-resolved. Only an explicit caller, with authoritative
        evidence, may move one of these out of UNKNOWN.
        """
        return [record for record in self.all() if record.is_unknown]

    def for_symbol(self, symbol: str) -> list[HedgeRequestRecord]:
        return [record for record in self.all() if record.symbol == symbol]

    def for_opportunity(self, opportunity_id: str) -> list[HedgeRequestRecord]:
        return [
            record
            for record in self.all()
            if opportunity_id in record.source_opportunity_ids
        ]

    def outcome(self, hedge_id: str) -> HedgeOutcomeSummary | None:
        return self.outcomes.get(hedge_id)

    def recent(self, limit: int = 20) -> list[HedgeRequestRecord]:
        """The most recently registered hedges, newest last."""
        if limit <= 0:
            return []
        return [
            self.hedges[hid] for hid in self._order[-limit:] if hid in self.hedges
        ]

    def metrics(self) -> HedgeMetrics:
        """Counters, for display. Nothing reads these to decide anything."""
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

    # ------------------------------------------------------------------
    # retention
    # ------------------------------------------------------------------

    def compact(self, *, keep_terminal: bool = True) -> int:
        """Release finished hedges. Conservative by construction.

        With ``keep_terminal`` at its default **nothing is released** — the
        framework supplies the hook and the safety rules and leaves the policy
        to a later pass that has measured what retention costs. There is no
        arbitrary count limit, because no measurement supports choosing one.

        Even when asked to release, these are absolute:

        * an ACTIVE hedge is never released;
        * an OUTSTANDING hedge is never released — which covers UNKNOWN, since
          UNKNOWN is outstanding;
        * a hedge linked to a reconciliation run is never released, because the
          run may still be establishing what actually happened to it.

        Dropping any of those is how a platform decides an order it cannot see
        stopped existing. Lifetime counters are unaffected, so a compacted
        session still reports what it did.

        Returns the number of records released.
        """
        if keep_terminal:
            return 0
        doomed = [
            record
            for record in self.all()
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

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _persist(self, record: HedgeRequestRecord) -> None:
        if self.store is not None:
            self.store.put_hedge(record)


__all__ = ["HedgeRegistry", "HedgeStore"]
