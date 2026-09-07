"""Plan preflight — the construction-time check seam.

WHERE THE HARD CHECKS WILL GO
=============================
A plan is the last artefact between an authorised intent and orders at a venue.
This module is the place a later pass puts the checks that must pass before any
of it is submitted.

This pass deliberately implements only what is *unquestionably* required: a
plan must have orders, every order must name a venue, a symbol and an id, and
the executor must claim to support the order type and time-in-force it is being
asked for. Every one of those is structurally necessary for the plan to be
workable at all — none is a judgement about whether it *should* be worked.

Anything that requires a decision — deadline enforcement, per-role size rules,
notional conservation, venue reachability — is deliberately absent. Adding a
speculative rule here would create a safety boundary nobody has validated, and
a boundary that has not been measured is a boundary nobody can trust.

The result is returned, not raised. A structurally impossible plan is a fact
about the plan, and the caller decides what to do with it; the check itself has
no business ending a tick.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from core.models.execution import ExecutorCapabilities
from core.models.opportunity import ExecutionPlan, PlannedOrder


@dataclass(frozen=True)
class PlanPreflight:
    """The outcome of checking a plan before it is submitted.

    ``reason_codes`` is the machine-readable answer and ``details`` the human
    one; both are empty when ``ok``.
    """

    ok: bool
    reason_codes: tuple[str, ...] = ()
    details: tuple[str, ...] = ()

    @property
    def blocked(self) -> bool:
        return not self.ok

    def __bool__(self) -> bool:
        return self.ok


@dataclass
class _Findings:
    codes: list[str] = field(default_factory=list)
    details: list[str] = field(default_factory=list)

    def add(self, code: str, detail: str) -> None:
        if code not in self.codes:
            self.codes.append(code)
        self.details.append(detail)


def _check_order(
    order: PlannedOrder,
    index: int,
    capabilities: ExecutorCapabilities | None,
    findings: _Findings,
) -> None:
    where = f"order[{index}]"
    if not order.client_order_id:
        findings.add("MISSING_CLIENT_ORDER_ID", f"{where} has no client_order_id")
    if not order.venue:
        findings.add("MISSING_VENUE", f"{where} names no venue")
    if not order.symbol:
        findings.add("MISSING_SYMBOL", f"{where} names no symbol")

    # Quantity and price positivity are enforced by ``PlannedOrder``'s own
    # field constraints, so reaching here means they already hold. Restated as
    # a check anyway: a plan rebuilt from a persisted payload by some future
    # path that bypasses validation must not be assumed well-formed.
    if not order.quantity > 0:
        findings.add("NON_POSITIVE_QUANTITY", f"{where} quantity is {order.quantity}")
    if not order.expected_price > 0:
        findings.add(
            "NON_POSITIVE_EXPECTED_PRICE",
            f"{where} expected_price is {order.expected_price}",
        )

    if capabilities is None:
        return
    if not capabilities.supports_order_type(order.order_type):
        findings.add(
            "UNSUPPORTED_ORDER_TYPE",
            f"{where} asks for {order.order_type.value}, which the executor "
            "does not claim to support",
        )
    if not capabilities.supports_time_in_force(order.time_in_force):
        findings.add(
            "UNSUPPORTED_TIME_IN_FORCE",
            f"{where} asks for {order.time_in_force.value}, which the executor "
            "does not claim to support",
        )


def preflight_plan(
    plan: ExecutionPlan,
    *,
    capabilities: ExecutorCapabilities | None = None,
) -> PlanPreflight:
    """Check a plan's structural workability.

    ``capabilities`` is optional so the function can be used against a plan
    with no executor in hand; when supplied, the plan is additionally checked
    against what that executor claims to implement.

    Pure: no clock, no state, no side effects.
    """
    findings = _Findings()

    if not plan.orders:
        findings.add("EMPTY_PLAN", "the plan carries no orders")

    seen: set[str] = set()
    for index, order in enumerate(plan.orders):
        _check_order(order, index, capabilities, findings)
        if order.client_order_id in seen:
            findings.add(
                "DUPLICATE_CLIENT_ORDER_ID",
                f"client_order_id {order.client_order_id!r} appears twice in "
                "one plan",
            )
        seen.add(order.client_order_id)

    if findings.codes:
        return PlanPreflight(
            ok=False,
            reason_codes=tuple(findings.codes),
            details=tuple(findings.details),
        )
    return PlanPreflight(ok=True)


__all__ = ["PlanPreflight", "preflight_plan"]
