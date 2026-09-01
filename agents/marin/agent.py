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
"""

from __future__ import annotations

from dataclasses import dataclass

from core.bus import EventBus
from core.clock import Clock
from core.events import Event, EventType
from core.health import HealthRegistry
from core.models.common import MONEY_EPSILON, QTY_EPSILON
from core.models.execution import OrderStatus
from core.models.ops import (
    HealthStatus,
    Mismatch,
    MismatchKind,
    ReconciliationResult,
    Severity,
)
from execution.oms import OrderManager
from execution.paper.account import PaperAccount

SERVICE = "MARIN"
VERSION = "marin-0.1"

#: Cash tolerance. Prices and sizes are float64, so two arithmetically
#: identical paths can differ in the last bits; anything larger is a real bug.
CASH_TOLERANCE = 1e-6
QTY_TOLERANCE = 1e-9


@dataclass
class Marin:
    bus: EventBus
    clock: Clock
    health: HealthRegistry
    oms: OrderManager
    account: PaperAccount
    runs: int = 0
    last_result: ReconciliationResult | None = None

    def __post_init__(self) -> None:
        self.health.register(SERVICE, VERSION)

    def reconcile(self) -> ReconciliationResult:
        now = self.clock.now_ms()
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

        # 4. Fees.
        expected_fees = sum(fill.fee for fill in account_fills.values())
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

    async def run(self) -> ReconciliationResult:
        result = self.reconcile()
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


__all__ = ["CASH_TOLERANCE", "MONEY_EPSILON", "QTY_EPSILON", "SERVICE", "VERSION", "Marin"]
