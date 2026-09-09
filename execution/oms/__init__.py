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
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass, field

from core.clock import Clock
from core.models.common import QTY_EPSILON, Millis, OrderType, Side, TimeInForce
from core.models.execution import (
    FillEvent,
    IllegalTransition,
    OrderStatus,
    PaperOrder,
)
from core.models.opportunity import PlannedOrder

log = logging.getLogger(__name__)


#: Fill ids kept for duplicate detection. Matches the paper account's window
#: so both layers reject the same re-deliveries.
DEFAULT_DEDUPE_FILLS = 10_000


@dataclass
class ArchivedOrders:
    """Totals for orders compacted out of memory.

    Reconciliation checks these as aggregates. The orders themselves are not
    lost — every one was published and persisted by the recorder — they are
    simply no longer resident, which is what keeps a long session's footprint
    flat instead of linear in orders placed.
    """

    count: int = 0
    fills: int = 0
    filled_quantity: float = 0.0
    fees_paid: float = 0.0
    by_status: dict[str, int] = field(default_factory=dict)

    def absorb(self, order: PaperOrder) -> None:
        self.count += 1
        self.fills += len(order.fills)
        self.filled_quantity += order.filled_quantity
        self.fees_paid += order.fees_paid
        self.by_status[order.status.value] = self.by_status.get(order.status.value, 0) + 1


@dataclass
class OrderManager:
    """In-memory order book of record.

    Holds *active* orders. Terminal orders are compacted into
    :class:`ArchivedOrders` once reconciliation has verified them, so the
    resident set is bounded by concurrent activity rather than by session
    length.
    """

    clock: Clock
    orders: dict[str, PaperOrder] = field(default_factory=dict)
    #: Fill ids already applied, for idempotent duplicate handling. Bounded to
    #: the most recent ``dedupe_fills`` ids; a re-delivery arrives close behind
    #: its original, and a repeat beyond that horizon surfaces as a
    #: reconciliation mismatch rather than being silently absorbed.
    _applied_fills: set[str] = field(default_factory=set)
    _dedupe_order: deque[str] = field(default_factory=deque)
    dedupe_fills: int = DEFAULT_DEDUPE_FILLS
    archived: ArchivedOrders = field(default_factory=ArchivedOrders)
    #: Lifetime counters, unaffected by compaction.
    orders_created: int = 0
    fills_applied: int = 0
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
        now_ms: Millis | None = None,
    ) -> PaperOrder:
        if client_order_id is not None and client_order_id in self.orders:
            raise ValueError(
                f"order {client_order_id} already exists; client_order_id is immutable"
            )

        now = self.clock.now_ms() if now_ms is None else now_ms
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
        if order.client_order_id in self.orders:
            raise ValueError(
                f"order {order.client_order_id} already exists; client_order_id is immutable"
            )
        order.history = [(now, OrderStatus.CREATED)]
        self.orders[order.client_order_id] = order
        self.orders_created += 1
        return order

    def from_plan(
        self,
        planned: PlannedOrder,
        *,
        plan_id: str,
        intent_id: str,
        strategy: str,
        now_ms: Millis | None = None,
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
            now_ms=now_ms,
        )

    # -- transitions -------------------------------------------------------

    def transition(
        self,
        client_order_id: str,
        status: OrderStatus,
        *,
        now_ms: Millis | None = None,
    ) -> PaperOrder:
        order = self.orders[client_order_id]
        stamp = self.clock.now_ms() if now_ms is None else now_ms
        try:
            order.transition(status, stamp)
        except IllegalTransition:
            self.illegal_transitions += 1
            raise
        return order

    def mark_unknown(
        self,
        client_order_id: str,
        reason: str = "",
        *,
        now_ms: Millis | None = None,
    ) -> PaperOrder:
        """Move an order to UNKNOWN after a timeout.

        The order is neither filled nor cancelled as far as the platform is
        concerned; reconciliation is responsible for resolving it.
        """
        order = self.orders[client_order_id]
        if order.is_terminal:
            return order
        stamp = self.clock.now_ms() if now_ms is None else now_ms
        order.transition(OrderStatus.UNKNOWN, stamp)
        order.reject_reason = reason or "operation timed out"
        return order

    def resolve_unknown(
        self,
        client_order_id: str,
        status: OrderStatus,
        *,
        now_ms: Millis | None = None,
    ) -> PaperOrder:
        order = self.orders[client_order_id]
        if order.status is not OrderStatus.UNKNOWN:
            raise ValueError(f"order {client_order_id} is not UNKNOWN")
        stamp = self.clock.now_ms() if now_ms is None else now_ms
        order.transition(status, stamp)
        return order

    def reject(
        self,
        client_order_id: str,
        reason: str,
        *,
        now_ms: Millis | None = None,
    ) -> PaperOrder:
        order = self.orders[client_order_id]
        order.reject_reason = reason
        stamp = self.clock.now_ms() if now_ms is None else now_ms
        order.transition(OrderStatus.REJECTED, stamp)
        return order

    # -- fills -------------------------------------------------------------

    def apply_fill(
        self, fill: FillEvent, *, now_ms: Millis | None = None
    ) -> bool:
        """Apply a fill idempotently. Returns False for a duplicate."""
        if fill.fill_id in self._applied_fills:
            self.duplicate_fills += 1
            return False
        order = self.orders.get(fill.client_order_id)
        if order is None:
            raise KeyError(f"fill for unknown order {fill.client_order_id}")
        if fill.quantity > order.remaining_quantity + QTY_EPSILON:
            raise ValueError(
                f"overfill on {order.client_order_id}: "
                f"{fill.quantity} > {order.remaining_quantity} remaining"
            )
        self._applied_fills.add(fill.fill_id)
        self._dedupe_order.append(fill.fill_id)
        self._trim_dedupe()
        self.fills_applied += 1
        order.apply_fill(fill)
        target = (
            OrderStatus.FILLED
            if order.remaining_quantity <= QTY_EPSILON
            else OrderStatus.PARTIALLY_FILLED
        )
        if order.status is not target or target is OrderStatus.PARTIALLY_FILLED:
            stamp = self.clock.now_ms() if now_ms is None else now_ms
            order.transition(target, stamp)
        return True

    # -- queries -----------------------------------------------------------

    def get(self, client_order_id: str) -> PaperOrder | None:
        return self.orders.get(client_order_id)

    def all_orders(self) -> list[PaperOrder]:
        """Every resident order, whatever its state.

        Resident, not historical: an order compacted into :attr:`archived` is
        gone from here by design, and its totals live there instead.
        """
        return list(self.orders.values())

    def live_orders(self) -> list[PaperOrder]:
        """Orders known to be working. Excludes UNKNOWN, which is not known."""
        return [o for o in self.orders.values() if o.is_live]

    def outstanding_orders(self) -> list[PaperOrder]:
        """Orders whose final venue truth is not yet known.

        Everything :meth:`live_orders` returns, plus the UNKNOWN ones. The two
        differ only there, and the difference is the point: a caller asking
        "what is still working?" wants the first, and a caller asking "what
        might still turn out to have traded?" wants this.
        """
        return [o for o in self.orders.values() if o.is_outstanding]

    def terminal_orders(self) -> list[PaperOrder]:
        """Orders that have stopped moving. Never includes UNKNOWN."""
        return [o for o in self.orders.values() if o.is_terminal]

    def unknown_orders(self) -> list[PaperOrder]:
        return [o for o in self.orders.values() if o.status is OrderStatus.UNKNOWN]

    def orders_for_plan(self, plan_id: str) -> list[PaperOrder]:
        return [o for o in self.orders.values() if o.plan_id == plan_id]

    def counts_by_status(self) -> dict[str, int]:
        """Resident order counts, keyed by status value."""
        counts: dict[str, int] = {}
        for order in self.orders.values():
            counts[order.status.value] = counts.get(order.status.value, 0) + 1
        return counts

    def all_fills(self) -> list[FillEvent]:
        return [fill for order in self.orders.values() for fill in order.fills]

    def expired(self, now_ms: Millis) -> Iterable[PaperOrder]:
        for order in self.orders.values():
            if order.is_live and order.expires_at is not None and now_ms >= order.expires_at:
                yield order

    # -- compaction --------------------------------------------------------

    def _trim_dedupe(self) -> None:
        excess = len(self._dedupe_order) - self.dedupe_fills
        for _ in range(max(0, excess)):
            self._applied_fills.discard(self._dedupe_order.popleft())

    def compactable_order_ids(self, unsealed_fills: set[str]) -> list[str]:
        """Order ids safe to release from both OMS and executor bookkeeping."""
        return [
            order.client_order_id
            for order in self.orders.values()
            if order.is_terminal
            and not any(fill.fill_id in unsealed_fills for fill in order.fills)
        ]

    def compact(self, unsealed_fills: set[str]) -> int:
        """Archive terminal orders the ledger has finished with.

        An order leaves memory only when it is terminal *and* none of its
        fills are still awaiting reconciliation — ``unsealed_fills`` is the
        set the paper account has yet to seal. That condition is what keeps
        the two views in step: neither side ever drops a fill the other still
        holds, so a genuine missing-fill mismatch stays detectable instead of
        being manufactured by compaction.

        Returns the number of orders archived.
        """
        doomed = [
            self.orders[client_order_id]
            for client_order_id in self.compactable_order_ids(unsealed_fills)
        ]
        for order in doomed:
            self.archived.absorb(order)
            del self.orders[order.client_order_id]
        return len(doomed)

    @property
    def resident_fills(self) -> int:
        return sum(len(order.fills) for order in self.orders.values())


__all__ = ["ArchivedOrders", "OrderManager", "OrderStatus", "PaperOrder"]
