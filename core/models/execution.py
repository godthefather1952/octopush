"""Order and fill schemas, the paper order state machine, and the execution
framework's plan-level vocabulary.

TWO LIFECYCLES, NOT ONE
=======================
:class:`OrderStatus` is the lifecycle of a single order at a venue.
:class:`ExecutionPlanStatus` is the lifecycle of the *plan* those orders were
created to work. They are deliberately separate: a two-leg plan whose first leg
has filled and whose second is still resting is not describable by any single
order's status, and a plan is complete only when every order it owns has
stopped moving.

Nothing in this module reads a clock. Every timestamp is supplied by the
caller, so a record built during replay carries the instant the original run
recorded rather than whatever the replaying process's clock happens to read.
"""

from __future__ import annotations

from pydantic import Field

from core.models.common import (
    Base,
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


class ExecutionPlanStatus(StrEnum):
    """Lifecycle of an execution PLAN, distinct from any one order's status.

    A plan is the unit the orchestrator authorised and the unit an operator
    cancels; its orders are the unit a venue works. ``UNKNOWN`` here means the
    same thing it means for an order — some part of this plan's venue-side
    truth is not known — and, like the order state, it is never assumed to mean
    failure.
    """

    CREATED = "CREATED"
    SUBMITTING = "SUBMITTING"
    #: At least one order is working and nothing has been reported filled.
    WORKING = "WORKING"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    CANCEL_PENDING = "CANCEL_PENDING"
    #: Every order reached a terminal state and something traded.
    COMPLETE = "COMPLETE"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"
    #: Some order's venue-side state is unresolved. Not terminal.
    UNKNOWN = "UNKNOWN"
    #: The plan could not be worked at all — rejected at submission, or a
    #: submission that did not complete.
    FAILED = "FAILED"


#: Plan states from which nothing further happens on its own. ``UNKNOWN`` is
#: deliberately absent, exactly as it is from :data:`TERMINAL_STATUSES`.
PLAN_TERMINAL_STATUSES: frozenset[ExecutionPlanStatus] = frozenset(
    {
        ExecutionPlanStatus.COMPLETE,
        ExecutionPlanStatus.CANCELLED,
        ExecutionPlanStatus.EXPIRED,
        ExecutionPlanStatus.FAILED,
    }
)


class ExecutionRole(StrEnum):
    """Why an order exists, in risk terms.

    The platform already distinguishes risk-increasing from risk-reducing
    activity — exits and hedges bypass entry edge and consensus gates, and they
    carry an explicit leg quantity because closing a position means closing
    *that quantity*. Until now that distinction was inferred from whether
    ``OpportunityLeg.quantity`` happened to be set. This states it.

    Construction-phase note: this field is metadata. It is populated at every
    construction site and carried through planning, but no sizing, routing or
    fill decision reads it yet — see ``docs/phase6-veska-framework.md``,
    "validation deferred".
    """

    #: Risk-increasing. Sized from the notional RUNE authorised.
    ENTRY = "ENTRY"
    #: Risk-reducing. Closes a position the platform already holds.
    EXIT = "EXIT"
    #: Risk-reducing. Neutralises a residual delta rather than closing a trade.
    HEDGE = "HEDGE"
    #: Risk-reducing, under an engaged safety condition.
    FLATTEN = "FLATTEN"

    @property
    def is_risk_reducing(self) -> bool:
        return self is not ExecutionRole.ENTRY


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

    @property
    def is_outstanding(self) -> bool:
        """Whether this order's final venue-side truth is still unknown.

        LIVE AND OUTSTANDING ARE DIFFERENT QUESTIONS
        ============================================
        :attr:`is_live` asks "is this order known to be working?" and answers
        False for an UNKNOWN order, which is correct for that question: nobody
        knows whether it is working.

        This asks the other question — "could this order still turn out to have
        traded?" — and answers True for UNKNOWN, because it could. An order
        whose submission timed out may be resting on the book right now.

        The two differ only for UNKNOWN, and that is the whole point. A caller
        deciding whether to *wait* wants :attr:`is_live`; a caller deciding
        whether it is safe to *act as though this order is finished* wants
        this. Both exist so the choice is explicit at each call site rather
        than inherited from whichever predicate came to hand.

        Construction-phase note: no existing call site was migrated in this
        pass. Which ones must move is a validation question, not a
        construction one.
        """
        return not self.is_terminal

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


# ======================================================================
# plan lifecycle
# ======================================================================


class ExecutionPlanRecord(Base):
    """VESKA's record of one plan's lifecycle.

    Deliberately *not* a copy of the plan. The plan says what was intended;
    this says what became of it. Keeping them apart means the record can be
    updated as orders move without ever rewriting the authorised intent.

    NO CLOCK, NO DECISIONS
    ======================
    Every timestamp is supplied by the caller, and nothing here changes a fill,
    a size or a status on its own — :func:`derive_plan_status` computes the
    status and a caller applies it. A record built during replay therefore
    carries the instants the original run recorded.
    """

    plan_id: str
    intent_id: str
    correlation_id: str | None = None
    strategy: str
    symbol: str
    execution_role: ExecutionRole = ExecutionRole.ENTRY

    status: ExecutionPlanStatus = ExecutionPlanStatus.CREATED

    created_at: Millis
    #: Instant of the most recent change to this record.
    updated_at: Millis
    #: The intent's absolute execution deadline, carried for observability.
    #: Construction phase: nothing enforces it here — see the framework doc.
    deadline_ms: Millis | None = None
    terminal_at: Millis | None = None

    #: Orders this plan created, in the order the executor accepted them.
    order_ids: list[str] = Field(default_factory=list)

    #: What the orchestrator asked for, and what RUNE authorised. Both per-leg,
    #: matching ``ExecutionPlan.notional``'s existing meaning.
    requested_notional: float = 0.0
    approved_notional: float = 0.0

    notes: list[str] = Field(default_factory=list)

    @property
    def is_terminal(self) -> bool:
        return self.status in PLAN_TERMINAL_STATUSES

    @property
    def is_unresolved(self) -> bool:
        """Neither finished nor known to be working."""
        return self.status is ExecutionPlanStatus.UNKNOWN

    @property
    def is_active(self) -> bool:
        """Still expected to change without anyone intervening."""
        return not self.is_terminal and not self.is_unresolved


# ======================================================================
# executor capability, identity and command vocabulary
# ======================================================================


class ExecutorCapabilities(Base):
    """What an executor *claims* to implement.

    Framework metadata, not a proof. An executor advertising ``supports_ioc``
    is stating an intent to honour immediate-or-cancel semantics; whether its
    implementation actually does is a validation question, and this pass does
    not answer it. The value of declaring it is that a future live executor,
    a preflight check and a venue gateway can all ask one question instead of
    inferring from behaviour.
    """

    #: The one capability the composition root enforces structurally.
    is_paper: bool = True

    supports_market: bool = False
    supports_limit: bool = True

    supports_ioc: bool = True
    supports_fok: bool = False
    supports_post_only: bool = True
    supports_gtc: bool = True

    supports_cancel: bool = True
    supports_cancel_all: bool = True

    supports_order_lookup: bool = True
    supports_unknown_resolution: bool = True
    supports_execution_snapshot: bool = True

    def supports_order_type(self, order_type: OrderType) -> bool:
        return {
            OrderType.MARKET: self.supports_market,
            OrderType.LIMIT: self.supports_limit,
        }[order_type]

    def supports_time_in_force(self, tif: TimeInForce) -> bool:
        return {
            TimeInForce.GTC: self.supports_gtc,
            TimeInForce.IOC: self.supports_ioc,
            TimeInForce.FOK: self.supports_fok,
            TimeInForce.POST_ONLY: self.supports_post_only,
        }[tif]


class ExecutionCommandResult(Base):
    """The normalized answer to a command aimed at one order or plan.

    Used by the cancel and unknown-resolution surfaces so a caller gets a
    structured answer rather than ``None`` and a guess. Existing methods that
    already return something else are left alone in this pass; this is the
    shape new commands take.
    """

    accepted: bool
    #: Whichever the command addressed. Both may be present.
    client_order_id: str | None = None
    plan_id: str | None = None
    #: The order's status after the command, where one applies.
    status: OrderStatus | None = None
    plan_status: ExecutionPlanStatus | None = None
    reason: str = ""
    #: Supplied by the caller, never read from a clock.
    at_ms: Millis | None = None


# ======================================================================
# execution snapshot — the surface future reconciliation consumes
# ======================================================================


class OrderSummary(Base):
    """One order, compacted to what a reconciler needs.

    A snapshot carrying whole ``PaperOrder`` objects would carry every fill and
    every history entry with it, which makes the snapshot's size a function of
    trading history rather than of open state.
    """

    client_order_id: str
    plan_id: str | None = None
    intent_id: str | None = None
    venue: str
    symbol: str
    side: Side
    order_type: OrderType
    time_in_force: TimeInForce
    status: OrderStatus
    quantity: float
    filled_quantity: float
    average_price: float | None = None
    fees_paid: float = 0.0
    submitted_at: Millis | None = None
    terminal_at: Millis | None = None

    @property
    def remaining_quantity(self) -> float:
        return max(0.0, self.quantity - self.filled_quantity)

    @classmethod
    def of(cls, order: PaperOrder) -> OrderSummary:
        return cls(
            client_order_id=order.client_order_id,
            plan_id=order.plan_id,
            intent_id=order.intent_id,
            venue=order.venue,
            symbol=order.symbol,
            side=order.side,
            order_type=order.order_type,
            time_in_force=order.time_in_force,
            status=order.status,
            quantity=order.quantity,
            filled_quantity=order.filled_quantity,
            average_price=order.average_price,
            fees_paid=order.fees_paid,
            submitted_at=order.submitted_at,
            terminal_at=order.terminal_at,
        )


class ExecutionMetrics(Base):
    """Counters describing what execution has done. No thresholds, no alerts.

    Plain totals so an operator, the dashboard, or a later reconciliation pass
    can see the shape of a session without any of them agreeing in advance on
    what a healthy number looks like.
    """

    plans_created: int = 0
    plans_submitted: int = 0
    plans_completed: int = 0
    plans_cancelled: int = 0
    plans_failed: int = 0

    orders_created: int = 0
    orders_outstanding: int = 0
    orders_unknown: int = 0

    fills: int = 0
    partial_fills: int = 0
    duplicate_fills: int = 0
    illegal_transitions: int = 0

    cancel_requests: int = 0
    rejected_submissions: int = 0


class ExecutionSnapshot(Envelope):
    """One canonical view of what execution believes at a logical instant.

    This is the surface future reconciliation (MARIN, Phase 7) consumes. It
    exists so that a reconciler asks the execution layer a question rather than
    reaching into ``OrderManager.orders``, ``PaperExecutor._pending`` or
    ``Veska.plans`` and building its own idea of the truth out of three private
    dictionaries.

    ``created_at`` is supplied by the caller, never read from a clock, so a
    replay can ask "what did execution believe at logical time T?" and get a
    deterministic answer.

    It is NOT a reconciliation result. Nothing here compares execution's view
    against anything else; that comparison is Phase 7's work.
    """

    #: Every order the execution layer currently holds, compacted.
    orders: list[OrderSummary] = Field(default_factory=list)

    #: Known to be working right now.
    open_order_ids: list[str] = Field(default_factory=list)
    #: Final venue truth not yet known — a superset of the open ids.
    outstanding_order_ids: list[str] = Field(default_factory=list)
    #: The subset of outstanding ids whose state is explicitly UNKNOWN.
    unknown_order_ids: list[str] = Field(default_factory=list)

    #: Plans still expected to move, and plans awaiting resolution.
    active_plan_ids: list[str] = Field(default_factory=list)
    unresolved_plan_ids: list[str] = Field(default_factory=list)

    #: Resident order counts by status, for a quick shape check.
    counts_by_status: dict[str, int] = Field(default_factory=dict)

    metrics: ExecutionMetrics = Field(default_factory=ExecutionMetrics)

    #: Lifetime totals from the OMS, unaffected by compaction.
    fills_applied: int = 0
    orders_created: int = 0
    duplicate_fills: int = 0
    illegal_transitions: int = 0
    archived_orders: int = 0

    @property
    def outstanding_count(self) -> int:
        return len(self.outstanding_order_ids)

    @property
    def unknown_count(self) -> int:
        return len(self.unknown_order_ids)
