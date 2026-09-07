"""Pure hedging vocabulary, and the plan-to-hedge status seam.

WHAT THIS IS
============
Small, side-effect-free helpers that name conditions OKAPI already evaluates,
plus one adapter that reads Phase 6's execution view and says what a hedge's
status looks like from there.

WHAT IT IS NOT
==============
**The economic authority stays where it is.** ``Okapi.build_hedges`` still
decides whether a residual is worth hedging, which side closes it, and how
urgent it is; ``Okapi._hedge_venue`` still picks the venue. Nothing in
``agents/okapi/agent.py`` was rewritten to consume these helpers, because a
refactor that routes a live decision through new code is not behaviour-neutral
however carefully it is written — and behaviour-neutrality is the whole
constraint of this phase.

These exist so that a *reader*, a snapshot, or a later validated migration has
one place to look for the conditions in question. They duplicate the existing
logic deliberately, and the duplication is the point: if they ever disagree
with the agent, the agent is right.
"""

from __future__ import annotations

from core.models.common import Side
from core.models.execution import (
    ExecutionPlanRecord,
    ExecutionPlanStatus,
    OrderStatus,
    OrderSummary,
)
from core.models.hedging import HedgeRequestStatus
from core.models.ops import DeltaReport


def is_outstanding(status: HedgeRequestStatus) -> bool:
    """Whether final execution truth for this hedge is unknown.

    True for UNKNOWN, which is the case worth being careful about: an UNKNOWN
    hedge is not finished and is not known to be working, and treating it as
    either would be a guess.
    """
    return status not in (
        HedgeRequestStatus.COMPLETE,
        HedgeRequestStatus.CANCELLED,
        HedgeRequestStatus.FAILED,
    )


def is_active(status: HedgeRequestStatus) -> bool:
    """Whether the hedge is currently known to be working.

    Narrower than :func:`is_outstanding`. UNKNOWN is outstanding but not
    active — nobody knows that it is working.
    """
    return status in (
        HedgeRequestStatus.SUBMITTING,
        HedgeRequestStatus.WORKING,
        HedgeRequestStatus.PARTIALLY_FILLED,
        HedgeRequestStatus.CANCEL_PENDING,
    )


def needs_hedge(report: DeltaReport) -> bool:
    """The condition ``build_hedges`` already applies, written down.

    ``build_hedges`` skips a report that is within tolerance or whose residual
    is zero. This says the same thing positively. It is not called from the
    hedging path — the agent evaluates its own condition — so a disagreement
    here can never change what the platform hedges.
    """
    return not report.within_tolerance and abs(report.unhedged_delta) > 0


def side_for_residual(residual: float) -> Side | None:
    """Which side closes a residual: long too much sells, short too much buys.

    ``None`` for a zero residual, because there is no side that closes nothing
    — and returning an arbitrary one would let a caller submit an order to
    correct an exposure that does not exist.
    """
    if residual > 0:
        return Side.SELL
    if residual < 0:
        return Side.BUY
    return None


def derive_hedge_status(
    plans: list[ExecutionPlanRecord],
    orders: list[OrderSummary] | None = None,
) -> HedgeRequestStatus:
    """What a hedge's status looks like from Phase 6's execution view.

    Conservative by construction, and in one direction only: **unresolved
    beats resolved.** If any plan behind this hedge is UNKNOWN, the hedge is
    UNKNOWN, whatever the other plans say. A hedge whose venue-side truth is
    partly unknown has not finished, and the expensive mistake is deciding it
    has.

    Ordering, most cautious first:

    1. no plans at all              -> PROPOSED (asked for, nothing working yet)
    2. any plan UNKNOWN             -> UNKNOWN
    3. any plan still active        -> WORKING, or PARTIALLY_FILLED if
                                       anything has traded
    4. every plan terminal:
         something traded           -> COMPLETE
         every plan CANCELLED/EXPIRED -> CANCELLED
         otherwise                  -> FAILED

    **Nothing decides from this.** No cancel, no resubmission, no risk action
    reads the result; a caller records it. The exact treatment of mixed cases —
    one plan filled and another cancelled, a partial fill on a cancelled plan —
    is VALIDATION DEFERRED, and the conservative ordering above is a
    construction choice rather than a proven rule.
    """
    if not plans:
        return HedgeRequestStatus.PROPOSED

    if any(plan.status is ExecutionPlanStatus.UNKNOWN for plan in plans):
        return HedgeRequestStatus.UNKNOWN

    traded = _anything_traded(plans, orders)

    if any(plan.is_active for plan in plans):
        if any(plan.status is ExecutionPlanStatus.CANCEL_PENDING for plan in plans):
            return HedgeRequestStatus.CANCEL_PENDING
        return (
            HedgeRequestStatus.PARTIALLY_FILLED
            if traded
            else HedgeRequestStatus.WORKING
        )

    if traded:
        return HedgeRequestStatus.COMPLETE
    if all(
        plan.status in (ExecutionPlanStatus.CANCELLED, ExecutionPlanStatus.EXPIRED)
        for plan in plans
    ):
        return HedgeRequestStatus.CANCELLED
    return HedgeRequestStatus.FAILED


def _anything_traded(
    plans: list[ExecutionPlanRecord], orders: list[OrderSummary] | None
) -> bool:
    """Whether any quantity is known to have executed.

    Order summaries answer this directly when supplied. Without them the plan
    status is the only evidence available, and it is read narrowly: only
    PARTIALLY_FILLED and COMPLETE assert that something traded.
    """
    if orders:
        return any(order.filled_quantity > 0 for order in orders)
    return any(
        plan.status
        in (ExecutionPlanStatus.PARTIALLY_FILLED, ExecutionPlanStatus.COMPLETE)
        for plan in plans
    )


def filled_notional(orders: list[OrderSummary]) -> float:
    """Quote notional actually executed across a hedge's orders.

    Uses each order's own average price, which is what it traded at. An order
    with no average price contributes nothing rather than being valued at a
    mark: a hedge's fill value is what it paid, not what it would be worth now.
    """
    total = 0.0
    for order in orders:
        if order.average_price is None:
            continue
        total += order.filled_quantity * order.average_price
    return total


def order_is_unresolved(status: OrderStatus) -> bool:
    """Whether one order's venue-side truth is unknown.

    A thin restatement of the execution vocabulary, here so hedging code can
    ask the question without importing three enums to do it.
    """
    return status is OrderStatus.UNKNOWN


__all__ = [
    "derive_hedge_status",
    "filled_notional",
    "is_active",
    "is_outstanding",
    "needs_hedge",
    "order_is_unresolved",
    "side_for_residual",
]
