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
    """Whether final execution truth for this hedge is unknown."""
    return status not in (
        HedgeRequestStatus.COMPLETE,
        HedgeRequestStatus.CANCELLED,
        HedgeRequestStatus.FAILED,
    )


def is_active(status: HedgeRequestStatus) -> bool:
    """Whether the hedge is currently known to be working."""
    return status in (
        HedgeRequestStatus.SUBMITTING,
        HedgeRequestStatus.WORKING,
        HedgeRequestStatus.PARTIALLY_FILLED,
        HedgeRequestStatus.CANCEL_PENDING,
    )


def needs_hedge(report: DeltaReport) -> bool:
    """The condition ``build_hedges`` already applies, written down."""
    return not report.within_tolerance and abs(report.unhedged_delta) > 0


def side_for_residual(residual: float) -> Side | None:
    """Which side closes a residual: long too much sells, short too much buys."""
    if residual > 0:
        return Side.SELL
    if residual < 0:
        return Side.BUY
    return None


def derive_hedge_status(
    plans: list[ExecutionPlanRecord],
    orders: list[OrderSummary] | None = None,
) -> HedgeRequestStatus:
    """Derive an observational hedge status from Phase 6 execution truth.

    UNKNOWN always dominates resolved states. Active plans remain outstanding.
    For terminal plans, evidence that *something* traded is not sufficient to
    declare the hedge complete: when order summaries are supplied, every order
    must be fully filled before COMPLETE is returned. A cancelled or failed
    plan with only a partial fill therefore keeps its terminal cancellation or
    failure semantics instead of silently claiming the requested hedge closed.
    """
    if not plans:
        return HedgeRequestStatus.PROPOSED

    if any(plan.status is ExecutionPlanStatus.UNKNOWN for plan in plans):
        return HedgeRequestStatus.UNKNOWN
    if orders and any(order.status is OrderStatus.UNKNOWN for order in orders):
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

    if orders:
        fully_filled = bool(orders) and all(
            order.quantity > 0 and order.filled_quantity >= order.quantity
            for order in orders
        )
        if fully_filled:
            return HedgeRequestStatus.COMPLETE
    elif all(plan.status is ExecutionPlanStatus.COMPLETE for plan in plans):
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
    """Whether any quantity is known to have executed."""
    if orders:
        return any(order.filled_quantity > 0 for order in orders)
    return any(
        plan.status
        in (ExecutionPlanStatus.PARTIALLY_FILLED, ExecutionPlanStatus.COMPLETE)
        for plan in plans
    )


def filled_notional(orders: list[OrderSummary]) -> float:
    """Quote notional actually executed across a hedge's orders."""
    total = 0.0
    for order in orders:
        if order.average_price is None:
            continue
        total += order.filled_quantity * order.average_price
    return total


def order_is_unresolved(status: OrderStatus) -> bool:
    """Whether one order's venue-side truth is unknown."""
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
