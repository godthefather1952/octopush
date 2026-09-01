"""Order management.

Owns every order's identity, state and fills.  The state machine is enforced
here, once, so that no executor can invent a transition.

``UNKNOWN`` is a first-class state.  When an operation times out the order goes
to UNKNOWN and stays there until something authoritative resolves it; it is
never assumed to have failed, because assuming a timed-out order failed is how
a platform ends up with a position it does not know about.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field

from core.clock import Clock
from core.models.common import Millis, OrderType, Side, TimeInForce
from core.models.execution import (
    FillEvent,
    IllegalTransition,
    OrderStatus,
    PaperOrder,
)
from core.models.opportunity import PlannedOrder

log = logging.getLogger(__name__)


@dataclass
class OrderManager:
    """In-memory order book of record."""

    clock: Clock
    orders: dict[str, PaperOrder] = field(default_factory=dict)
    #: Fill ids already applied, for idempotent duplicate handling.
    _applied_fills: set[str] = field(default_factory=set)
    duplicate_fills: int = 0
    illegal_transitions: int = 0

    # -- creation ----------------------------------------------------------

    def create(
        self,
        *,
        venue: str,
        symbol: str,
        side: Side,
        quantity: float,
        order_type: OrderType,
        time_in_force: TimeInForce,
        expected_price: float,
        limit_price: float | None = None,
        plan_id: str | None = None,
        intent_id: str | None = None,
        strategy: str | None = None,
        client_order_id: str | None = None,
        ttl_ms: int | None = None,
    ) -> PaperOrder:
        now = self.clock.now_ms()
        order = PaperOrder(
            created_at=now,
            venue=venue,
            symbol=symbol,
            side=side,
            quantity=quantity,
            order_type=order_type,
            time_in_force=time_in_force,
            expected_price=expected_price,
            limit_price=limit_price,
            plan_id=plan_id,
            intent_id=intent_id,
            strategy=strategy,
            expires_at=now + ttl_ms if ttl_ms else None,
        )
        if client_order_id:
            order.client_order_id = client_order_id
        order.history = [(now, OrderStatus.CREATED)]
        self.orders[order.client_order_id] = order
        return order

    def from_plan(
        self, planned: PlannedOrder, *, plan_id: str, intent_id: str, strategy: str
    ) -> PaperOrder:
        return self.create(
            venue=planned.venue,
            symbol=planned.symbol,
            side=planned.side,
            quantity=planned.quantity,
            order_type=planned.order_type,
            time_in_force=planned.time_in_force,
            expected_price=planned.expected_price,
            limit_price=planned.limit_price,
            plan_id=plan_id,
            intent_id=intent_id,
            strategy=strategy,
            client_order_id=planned.client_order_id,
            ttl_ms=planned.ttl_ms,
        )

    # -- transitions -------------------------------------------------------

    def transition(self, client_order_id: str, status: OrderStatus) -> PaperOrder:
        order = self.orders[client_order_id]
        try:
            order.transition(status, self.clock.now_ms())
        except IllegalTransition:
            self.illegal_transitions += 1
            raise
        return order

    def mark_unknown(self, client_order_id: str, reason: str = "") -> PaperOrder:
        """Move an order to UNKNOWN after a timeout.

        The order is neither filled nor cancelled as far as the platform is
        concerned; reconciliation is responsible for resolving it.
        """
        order = self.orders[client_order_id]
        if order.is_terminal:
            return order
        order.transition(OrderStatus.UNKNOWN, self.clock.now_ms())
        order.reject_reason = reason or "operation timed out"
        return order

    def resolve_unknown(self, client_order_id: str, status: OrderStatus) -> PaperOrder:
        order = self.orders[client_order_id]
        if order.status is not OrderStatus.UNKNOWN:
            raise ValueError(f"order {client_order_id} is not UNKNOWN")
        order.transition(status, self.clock.now_ms())
        return order

    def reject(self, client_order_id: str, reason: str) -> PaperOrder:
        order = self.orders[client_order_id]
        order.reject_reason = reason
        order.transition(OrderStatus.REJECTED, self.clock.now_ms())
        return order

    # -- fills -------------------------------------------------------------

    def apply_fill(self, fill: FillEvent) -> bool:
        """Apply a fill idempotently. Returns False for a duplicate."""
        if fill.fill_id in self._applied_fills:
            self.duplicate_fills += 1
            return False
        order = self.orders.get(fill.client_order_id)
        if order is None:
            raise KeyError(f"fill for unknown order {fill.client_order_id}")
        if fill.quantity > order.remaining_quantity + 1e-9:
            raise ValueError(
                f"overfill on {order.client_order_id}: "
                f"{fill.quantity} > {order.remaining_quantity} remaining"
            )
        self._applied_fills.add(fill.fill_id)
        order.apply_fill(fill)
        target = (
            OrderStatus.FILLED
            if order.remaining_quantity <= 1e-9
            else OrderStatus.PARTIALLY_FILLED
        )
        if order.status is not target or target is OrderStatus.PARTIALLY_FILLED:
            order.transition(target, self.clock.now_ms())
        return True

    # -- queries -----------------------------------------------------------

    def get(self, client_order_id: str) -> PaperOrder | None:
        return self.orders.get(client_order_id)

    def live_orders(self) -> list[PaperOrder]:
        return [o for o in self.orders.values() if o.is_live]

    def unknown_orders(self) -> list[PaperOrder]:
        return [o for o in self.orders.values() if o.status is OrderStatus.UNKNOWN]

    def orders_for_plan(self, plan_id: str) -> list[PaperOrder]:
        return [o for o in self.orders.values() if o.plan_id == plan_id]

    def all_fills(self) -> list[FillEvent]:
        return [fill for order in self.orders.values() for fill in order.fills]

    def expired(self, now_ms: Millis) -> Iterable[PaperOrder]:
        for order in self.orders.values():
            if order.is_live and order.expires_at is not None and now_ms >= order.expires_at:
                yield order


__all__ = ["OrderManager", "OrderStatus", "PaperOrder"]
