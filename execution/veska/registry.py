"""The execution registry — VESKA's record of what it has asked to happen.

WHAT THIS IS
============
One authoritative place holding an :class:`ExecutionPlanRecord` per plan, so
that "what is happening with this trade's execution?" has an answer that is not
assembled from ``OrderManager.orders``, ``PaperExecutor._pending`` and an
orchestrator dictionary by whoever happened to ask.

WHAT THIS IS NOT
================
It does not submit, cancel, fill or size anything. It holds state and answers
questions. Every method that changes a timestamp takes ``now_ms`` explicitly —
there is no clock here, for the same reason there is none in the executor: a
record written at a clock read is a record replay cannot reconstruct (P2-14).

RETENTION
=========
``compact`` exists and is deliberately conservative. Terminal plan records may
one day be archived to aggregates the way ``OrderManager`` archives terminal
orders; an unresolved plan must never be dropped, because dropping unresolved
truth is how a platform forgets a position it may still hold. The method
therefore refuses to touch anything that is not terminal, and the retention
policy itself is left for a later pass.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field

from core.models.common import Millis
from core.models.execution import (
    ExecutionPlanRecord,
    ExecutionPlanStatus,
    ExecutionRole,
    OrderStatus,
    PaperOrder,
)
from core.models.opportunity import ExecutionPlan

log = logging.getLogger(__name__)


# ======================================================================
# status derivation
# ======================================================================


def derive_plan_status(
    record: ExecutionPlanRecord,
    orders: Iterable[PaperOrder],
) -> ExecutionPlanStatus:
    """Compute a plan's status from the orders it owns.

    Pure: same record, same orders, same answer. Kept in one place rather than
    inlined at each update site so that a later pass refining the edge cases
    changes one function.

    The precedence, in order:

    1. **No orders yet** — the record keeps whatever it has (CREATED or
       SUBMITTING). A plan that has been asked to submit but whose orders have
       not appeared is not "complete".
    2. **Anything UNKNOWN** — the plan is UNKNOWN. Unresolved truth dominates
       every other reading: a plan cannot be called complete or cancelled while
       part of it might still be working. This is the same fail-closed rule the
       order model applies.
    3. **Anything still working** — CANCEL_PENDING if a cancel is in flight,
       PARTIALLY_FILLED if something has traded, otherwise WORKING.
    4. **All terminal** — COMPLETE if anything filled; otherwise CANCELLED,
       EXPIRED or FAILED according to what the orders actually did, preferring
       the most specific single answer and falling back to CANCELLED for a
       mixture.

    Construction-phase note: the exact treatment of mixed terminal states, and
    of a partially-filled plan whose remainder was cancelled, is stated here as
    a starting point rather than as a proven rule. Validation will decide
    whether COMPLETE is the right word for "filled 0.02 of 1.0 and cancelled
    the rest".
    """
    resident = list(orders)
    if not resident:
        return record.status

    if any(o.status is OrderStatus.UNKNOWN for o in resident):
        return ExecutionPlanStatus.UNKNOWN

    filled = any(o.filled_quantity > 0 for o in resident)

    if not all(o.is_terminal for o in resident):
        if any(o.status is OrderStatus.CANCEL_PENDING for o in resident):
            return ExecutionPlanStatus.CANCEL_PENDING
        if filled:
            return ExecutionPlanStatus.PARTIALLY_FILLED
        return ExecutionPlanStatus.WORKING

    if filled:
        return ExecutionPlanStatus.COMPLETE

    statuses = {o.status for o in resident}
    if statuses == {OrderStatus.EXPIRED}:
        return ExecutionPlanStatus.EXPIRED
    if statuses == {OrderStatus.REJECTED}:
        return ExecutionPlanStatus.FAILED
    return ExecutionPlanStatus.CANCELLED


# ======================================================================
# the registry
# ======================================================================


@dataclass
class ExecutionRegistry:
    """Plan records, keyed by ``plan_id``.

    Construction-only in this pass: VESKA writes to it as plans move, and
    everything else reads. No component's decisions depend on it yet.
    """

    records: dict[str, ExecutionPlanRecord] = field(default_factory=dict)
    #: The plans themselves, retained so a caller can ask what was intended as
    #: well as what became of it. Kept beside the records rather than inside
    #: them so a record stays small and cheap to update.
    plans: dict[str, ExecutionPlan] = field(default_factory=dict)

    #: Lifetime counters, unaffected by any future compaction.
    plans_registered: int = 0
    plans_submitted: int = 0
    plans_completed: int = 0
    plans_cancelled: int = 0
    plans_failed: int = 0

    # -- registration ------------------------------------------------------

    def register_plan(
        self,
        plan: ExecutionPlan,
        now_ms: Millis,
        *,
        requested_notional: float | None = None,
        approved_notional: float | None = None,
        execution_role: ExecutionRole | None = None,
    ) -> ExecutionPlanRecord:
        """Record a newly built plan. Idempotent by ``plan_id``.

        Re-registering an existing plan returns the record already held rather
        than replacing it: a plan id names one execution attempt, and silently
        starting a second under the same name is how a record loses the orders
        the first one created.
        """
        existing = self.records.get(plan.plan_id)
        if existing is not None:
            return existing

        record = ExecutionPlanRecord(
            plan_id=plan.plan_id,
            intent_id=plan.intent_id,
            correlation_id=plan.correlation_id,
            strategy=plan.strategy,
            symbol=plan.symbol,
            execution_role=execution_role
            if execution_role is not None
            else plan.execution_role,
            status=ExecutionPlanStatus.CREATED,
            created_at=now_ms,
            updated_at=now_ms,
            deadline_ms=plan.deadline_ms,
            requested_notional=(
                plan.requested_notional
                if requested_notional is None
                else requested_notional
            ),
            approved_notional=(
                plan.notional if approved_notional is None else approved_notional
            ),
        )
        self.records[plan.plan_id] = record
        self.plans[plan.plan_id] = plan
        self.plans_registered += 1
        return record

    # -- mutation ----------------------------------------------------------

    def attach_order(self, plan_id: str, client_order_id: str, now_ms: Millis) -> None:
        """Associate one order with its plan. Duplicate ids are ignored."""
        self.attach_orders(plan_id, [client_order_id], now_ms)

    def attach_orders(
        self, plan_id: str, client_order_ids: Iterable[str], now_ms: Millis
    ) -> None:
        record = self.records.get(plan_id)
        if record is None:
            # A plan the registry never saw. Recorded rather than raised: this
            # is observability infrastructure, and it must not be able to stop
            # an execution path that is otherwise working.
            log.warning("attach_orders for unregistered plan %s", plan_id)
            return
        known = set(record.order_ids)
        added = [oid for oid in client_order_ids if oid not in known]
        if not added:
            return
        record.order_ids = [*record.order_ids, *added]
        record.updated_at = now_ms

    def set_status(
        self,
        plan_id: str,
        status: ExecutionPlanStatus,
        now_ms: Millis,
        *,
        note: str = "",
    ) -> ExecutionPlanRecord | None:
        record = self.records.get(plan_id)
        if record is None:
            log.warning("set_status for unregistered plan %s", plan_id)
            return None
        if record.status is status and not note:
            record.updated_at = now_ms
            return record

        previous = record.status
        record.status = status
        record.updated_at = now_ms
        if status in (
            ExecutionPlanStatus.COMPLETE,
            ExecutionPlanStatus.CANCELLED,
            ExecutionPlanStatus.EXPIRED,
            ExecutionPlanStatus.FAILED,
        ):
            record.terminal_at = now_ms
        if note:
            record.notes = [*record.notes, note]

        if previous is not status:
            if status is ExecutionPlanStatus.SUBMITTING:
                self.plans_submitted += 1
            elif status is ExecutionPlanStatus.COMPLETE:
                self.plans_completed += 1
            elif status is ExecutionPlanStatus.CANCELLED:
                self.plans_cancelled += 1
            elif status is ExecutionPlanStatus.FAILED:
                self.plans_failed += 1
        return record

    def refresh(
        self, plan_id: str, orders: Iterable[PaperOrder], now_ms: Millis
    ) -> ExecutionPlanRecord | None:
        """Recompute one plan's status from its current orders."""
        record = self.records.get(plan_id)
        if record is None:
            return None
        return self.set_status(plan_id, derive_plan_status(record, orders), now_ms)

    def note(self, plan_id: str, text: str, now_ms: Millis) -> None:
        record = self.records.get(plan_id)
        if record is None:
            return
        record.notes = [*record.notes, text]
        record.updated_at = now_ms

    # -- queries -----------------------------------------------------------

    def get(self, plan_id: str) -> ExecutionPlanRecord | None:
        return self.records.get(plan_id)

    def plan(self, plan_id: str) -> ExecutionPlan | None:
        return self.plans.get(plan_id)

    def all_records(self) -> list[ExecutionPlanRecord]:
        return list(self.records.values())

    def active_plans(self) -> list[ExecutionPlanRecord]:
        """Plans still expected to move without anyone intervening."""
        return [r for r in self.records.values() if r.is_active]

    def unresolved_plans(self) -> list[ExecutionPlanRecord]:
        """Plans holding at least one order whose venue truth is unknown.

        Deliberately separate from :meth:`active_plans`: an unresolved plan is
        not working and not finished, and a caller that treats those two as one
        category is the shape of mistake this split exists to prevent.
        """
        return [r for r in self.records.values() if r.is_unresolved]

    def terminal_plans(self) -> list[ExecutionPlanRecord]:
        return [r for r in self.records.values() if r.is_terminal]

    def plans_for_intent(self, intent_id: str) -> list[ExecutionPlanRecord]:
        return [r for r in self.records.values() if r.intent_id == intent_id]

    def plans_for_correlation(self, correlation_id: str) -> list[ExecutionPlanRecord]:
        return [
            r for r in self.records.values() if r.correlation_id == correlation_id
        ]

    def plan_for_order(self, client_order_id: str) -> ExecutionPlanRecord | None:
        for record in self.records.values():
            if client_order_id in record.order_ids:
                return record
        return None

    # -- retention ---------------------------------------------------------

    def compact(self, *, keep_terminal: bool = True) -> int:
        """Release finished plan records. Conservative by construction.

        Only terminal records are eligible, and with ``keep_terminal`` at its
        default nothing is released at all — the framework provides the hook
        and the safety rule, and leaves the policy for a later pass that has
        measured what retention actually costs.

        An unresolved plan is never eligible, whatever the arguments say.
        Silently dropping unresolved execution truth would trade a memory
        question for a correctness one.

        Returns the number of records released.
        """
        if keep_terminal:
            return 0
        doomed = [r.plan_id for r in self.records.values() if r.is_terminal]
        for plan_id in doomed:
            del self.records[plan_id]
            self.plans.pop(plan_id, None)
        return len(doomed)

    @property
    def resident_plans(self) -> int:
        return len(self.records)


__all__ = ["ExecutionRegistry", "derive_plan_status"]
