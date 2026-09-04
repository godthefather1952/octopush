"""Order and fill schemas plus the paper order state machine."""

from __future__ import annotations

from pydantic import Field

from core.models.common import (
    Envelope,
    Liquidity,
    Millis,
    OrderType,
    Side,
    StrEnum,
    TimeInForce,
    new_id,
)


class OrderStatus(StrEnum):
    CREATED = "CREATED"
    SUBMITTING = "SUBMITTING"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    OPEN = "OPEN"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCEL_PENDING = "CANCEL_PENDING"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    #: A real state, not an error. Reached when an operation times out and the
    #: true venue-side state is not known. Never assumed to mean "failed".
    UNKNOWN = "UNKNOWN"


TERMINAL_STATUSES: frozenset[OrderStatus] = frozenset(
    {
        OrderStatus.FILLED,
        OrderStatus.CANCELLED,
        OrderStatus.REJECTED,
        OrderStatus.EXPIRED,
    }
)

#: Legal order state transitions. ``UNKNOWN`` is reachable from any live state
#: and can resolve back into any live or terminal state once truth is learned.
ORDER_TRANSITIONS: dict[OrderStatus, set[OrderStatus]] = {
    OrderStatus.CREATED: {OrderStatus.SUBMITTING, OrderStatus.REJECTED},
    OrderStatus.SUBMITTING: {
        OrderStatus.ACKNOWLEDGED,
        OrderStatus.REJECTED,
        OrderStatus.UNKNOWN,
    },
    OrderStatus.ACKNOWLEDGED: {
        OrderStatus.OPEN,
        OrderStatus.PARTIALLY_FILLED,
        OrderStatus.FILLED,
        OrderStatus.REJECTED,
        OrderStatus.CANCEL_PENDING,
        OrderStatus.UNKNOWN,
    },
    OrderStatus.OPEN: {
        OrderStatus.PARTIALLY_FILLED,
        OrderStatus.FILLED,
        OrderStatus.CANCEL_PENDING,
        OrderStatus.CANCELLED,
        OrderStatus.EXPIRED,
        OrderStatus.UNKNOWN,
    },
    OrderStatus.PARTIALLY_FILLED: {
        OrderStatus.PARTIALLY_FILLED,
        OrderStatus.FILLED,
        OrderStatus.CANCEL_PENDING,
        OrderStatus.CANCELLED,
        OrderStatus.EXPIRED,
        OrderStatus.UNKNOWN,
    },
    OrderStatus.CANCEL_PENDING: {
        OrderStatus.CANCELLED,
        OrderStatus.FILLED,
        OrderStatus.PARTIALLY_FILLED,
        OrderStatus.UNKNOWN,
    },
    OrderStatus.UNKNOWN: {
        OrderStatus.OPEN,
        OrderStatus.PARTIALLY_FILLED,
        OrderStatus.FILLED,
        OrderStatus.CANCELLED,
        OrderStatus.REJECTED,
        OrderStatus.EXPIRED,
    },
    OrderStatus.FILLED: set(),
    OrderStatus.CANCELLED: set(),
    OrderStatus.REJECTED: set(),
    OrderStatus.EXPIRED: set(),
}


class IllegalTransition(RuntimeError):
    def __init__(self, current: OrderStatus, requested: OrderStatus) -> None:
        super().__init__(f"illegal order transition {current} -> {requested}")
        self.current = current
        self.requested = requested


class FillEvent(Envelope):
    """A simulated fill. Immutable once emitted."""

    fill_id: str = Field(default_factory=lambda: new_id("fill"))
    client_order_id: str
    venue: str
    symbol: str
    side: Side
    quantity: float = Field(gt=0)
    price: float = Field(gt=0)
    fee: float = 0.0
    liquidity: Liquidity = Liquidity.TAKER
    #: Slippage against the price VESKA expected, in bps (signed: positive is
    #: worse than expected).
    slippage_bps: float = 0.0
    strategy: str | None = None
    #: This fill's own realized-P&L contribution (pre-fee), captured by
    #: ``PaperAccount.apply_fill`` at the exact moment it applies this fill to
    #: the position -- not re-derived later from live account state, which
    #: can already reflect fills applied after this one but dispatched
    #: before it (PAPER_FILL is queued, not delivered, at publish time; the
    #: account can accumulate several fills before any of their handlers
    #: run). Set once, before this event is ever published, so it stays
    #: within "immutable once emitted".
    realized_pnl_delta: float = 0.0

    @property
    def notional(self) -> float:
        return self.price * self.quantity

    @property
    def signed_quantity(self) -> float:
        return self.quantity * self.side.sign

    @property
    def cash_delta(self) -> float:
        """Change in quote-currency cash caused by this fill, fees included."""
        return -self.signed_quantity * self.price - self.fee


class PaperOrder(Envelope):
    """A simulated order, with its full state history."""

    client_order_id: str = Field(default_factory=lambda: new_id("ord"))
    venue_order_id: str | None = None
    plan_id: str | None = None
    intent_id: str | None = None
    strategy: str | None = None
    venue: str
    symbol: str
    side: Side
    order_type: OrderType
    time_in_force: TimeInForce
    quantity: float = Field(gt=0)
    limit_price: float | None = None
    expected_price: float
    status: OrderStatus = OrderStatus.CREATED
    filled_quantity: float = 0.0
    #: Quantity-weighted average fill price.
    average_price: float | None = None
    fees_paid: float = 0.0
    submitted_at: Millis | None = None
    acknowledged_at: Millis | None = None
    terminal_at: Millis | None = None
    expires_at: Millis | None = None
    reject_reason: str | None = None
    fills: list[FillEvent] = Field(default_factory=list)
    #: Every (timestamp, status) the order has been through.
    history: list[tuple[Millis, OrderStatus]] = Field(default_factory=list)

    @property
    def remaining_quantity(self) -> float:
        return max(0.0, self.quantity - self.filled_quantity)

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    @property
    def is_live(self) -> bool:
        return not self.is_terminal and self.status is not OrderStatus.UNKNOWN

    def can_transition_to(self, status: OrderStatus) -> bool:
        return status in ORDER_TRANSITIONS[self.status]

    def transition(self, status: OrderStatus, now_ms: Millis) -> None:
        """Move to ``status``, raising :class:`IllegalTransition` if invalid."""
        if not self.can_transition_to(status):
            raise IllegalTransition(self.status, status)
        self.status = status
        self.history = [*self.history, (now_ms, status)]
        if status in TERMINAL_STATUSES:
            self.terminal_at = now_ms

    def apply_fill(self, fill: FillEvent) -> None:
        """Record a fill and roll the average price forward."""
        if fill.client_order_id != self.client_order_id:
            raise ValueError("fill does not belong to this order")
        if any(f.fill_id == fill.fill_id for f in self.fills):
            # Duplicate fill delivery is expected on unreliable transports and
            # must be idempotent.
            return
        prior_notional = (self.average_price or 0.0) * self.filled_quantity
        self.fills = [*self.fills, fill]
        self.filled_quantity += fill.quantity
        self.fees_paid += fill.fee
        self.average_price = (prior_notional + fill.price * fill.quantity) / self.filled_quantity


class ExecutionReport(Envelope):
    """What VESKA reports back after working a plan."""

    plan_id: str
    intent_id: str
    orders: list[PaperOrder] = Field(default_factory=list)
    fills: list[FillEvent] = Field(default_factory=list)
    complete: bool = False
    notes: list[str] = Field(default_factory=list)

    @property
    def filled_notional(self) -> float:
        return sum(f.notional for f in self.fills)
