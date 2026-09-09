"""Plan preflight — the canonical fail-closed submission gate.

A plan is the final artefact between an authorised intent and venue-side
execution. Batch B wires this pure checker into `Veska.execute` before any new
OMS order is created.

The checker owns only validated submission boundaries: structural plan shape,
executor capability claims, duplicate order identity, configured/enabled venue
reachability, and the absolute submission deadline when logical time is
supplied. It deliberately does not invent economic conservation rules that the
frozen Phase 6 audit did not establish.

The result remains data rather than an exception: VESKA records a blocked plan
as FAILED, preserves reason codes in the submission report, and creates no
economic exposure.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from core.models.common import Millis
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
    allowed_venues: frozenset[str] | None,
    findings: _Findings,
) -> None:
    where = f"order[{index}]"
    if not order.client_order_id:
        findings.add("MISSING_CLIENT_ORDER_ID", f"{where} has no client_order_id")
    if not order.venue:
        findings.add("MISSING_VENUE", f"{where} names no venue")
    if not order.symbol:
        findings.add("MISSING_SYMBOL", f"{where} names no symbol")
    if allowed_venues is not None and order.venue not in allowed_venues:
        findings.add(
            "UNKNOWN_VENUE",
            f"{where} names venue {order.venue!r}, which is not configured and enabled",
        )

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
    now_ms: Millis | None = None,
    allowed_venues: frozenset[str] | None = None,
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
    if now_ms is not None and now_ms > plan.deadline_ms:
        findings.add(
            "EXPIRED_DEADLINE",
            f"plan deadline {plan.deadline_ms} expired before submission at {now_ms}",
        )

    seen: set[str] = set()
    for index, order in enumerate(plan.orders):
        _check_order(
            order,
            index,
            capabilities,
            allowed_venues,
            findings,
        )
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
