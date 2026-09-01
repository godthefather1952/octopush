"""Opportunities, trade intents and execution plans."""

from __future__ import annotations

from pydantic import Field

from core.models.common import (
    Base,
    Envelope,
    Millis,
    OrderType,
    Side,
    StrEnum,
    TimeInForce,
    new_id,
)


class OpportunityKind(StrEnum):
    CROSS_VENUE_DISLOCATION = "CROSS_VENUE_DISLOCATION"
    SPOT_PERP_BASIS = "SPOT_PERP_BASIS"
    RELATIVE_VALUE = "RELATIVE_VALUE"


class StrategyState(StrEnum):
    """Lifecycle of a single candidate opportunity."""

    IDLE = "IDLE"
    OPPORTUNITY_DETECTED = "OPPORTUNITY_DETECTED"
    AGENTS_EVALUATING = "AGENTS_EVALUATING"
    CONSENSUS_REACHED = "CONSENSUS_REACHED"
    RISK_CHECK = "RISK_CHECK"
    AUTHORIZED = "AUTHORIZED"
    EXECUTING = "EXECUTING"
    HEDGING = "HEDGING"
    RECONCILING = "RECONCILING"
    MONITORING = "MONITORING"
    EXITING = "EXITING"
    CLOSED = "CLOSED"
    REJECTED = "REJECTED"


#: Legal transitions for the strategy state machine. Every transition emits an
#: event; anything not listed here is a bug and raises.
STRATEGY_TRANSITIONS: dict[StrategyState, set[StrategyState]] = {
    StrategyState.IDLE: {StrategyState.OPPORTUNITY_DETECTED},
    StrategyState.OPPORTUNITY_DETECTED: {
        StrategyState.AGENTS_EVALUATING,
        StrategyState.REJECTED,
    },
    StrategyState.AGENTS_EVALUATING: {
        StrategyState.CONSENSUS_REACHED,
        StrategyState.REJECTED,
    },
    StrategyState.CONSENSUS_REACHED: {StrategyState.RISK_CHECK, StrategyState.REJECTED},
    StrategyState.RISK_CHECK: {StrategyState.AUTHORIZED, StrategyState.REJECTED},
    StrategyState.AUTHORIZED: {StrategyState.EXECUTING, StrategyState.REJECTED},
    StrategyState.EXECUTING: {
        StrategyState.HEDGING,
        StrategyState.RECONCILING,
        StrategyState.EXITING,
        StrategyState.CLOSED,
    },
    StrategyState.HEDGING: {StrategyState.RECONCILING, StrategyState.EXITING},
    StrategyState.RECONCILING: {StrategyState.MONITORING, StrategyState.EXITING},
    StrategyState.MONITORING: {StrategyState.EXITING, StrategyState.CLOSED},
    StrategyState.EXITING: {StrategyState.RECONCILING, StrategyState.CLOSED},
    StrategyState.CLOSED: set(),
    StrategyState.REJECTED: set(),
}


class OpportunityLeg(Base):
    """One side of a relative-value opportunity."""

    venue: str
    symbol: str
    side: Side
    #: Reference price observed at detection time.
    reference_price: float
    #: Explicit base-asset quantity for this leg. Entries leave this unset and
    #: are sized from the intent's notional; exits and hedges set it, because
    #: closing a position means closing *that quantity*, not a notional
    #: estimate that can leave a residual behind.
    quantity: float | None = None


class Opportunity(Envelope):
    """A candidate mispricing detected by a strategy."""

    opportunity_id: str = Field(default_factory=lambda: new_id("opp"))
    kind: OpportunityKind
    strategy: str
    symbol: str
    legs: list[OpportunityLeg]
    #: Price difference before any costs, in bps of the reference price.
    gross_edge_bps: float
    #: Direction the strategy would express, expressed on the first leg.
    expires_at: Millis
    reason_codes: list[str] = Field(default_factory=list)
    detail: dict[str, float | int | str | bool | None] = Field(default_factory=dict)

    def is_valid_at(self, now_ms: Millis) -> bool:
        return now_ms <= self.expires_at


class CostBreakdown(Base):
    """Every cost component between gross edge and expected net edge.

    All fields are in basis points of notional and are *positive costs*
    (i.e. they are subtracted from the gross edge).
    """

    fees_bps: float = 0.0
    spread_bps: float = 0.0
    slippage_bps: float = 0.0
    hedge_bps: float = 0.0
    funding_bps: float = 0.0
    latency_bps: float = 0.0
    other_bps: float = 0.0

    @property
    def total_bps(self) -> float:
        return (
            self.fees_bps
            + self.spread_bps
            + self.slippage_bps
            + self.hedge_bps
            + self.funding_bps
            + self.latency_bps
            + self.other_bps
        )

    def net_from(self, gross_edge_bps: float) -> float:
        return gross_edge_bps - self.total_bps


class TradeIntent(Envelope):
    """What the orchestrator wants to do, before risk approval."""

    intent_id: str = Field(default_factory=lambda: new_id("int"))
    opportunity_id: str
    strategy: str
    symbol: str
    legs: list[OpportunityLeg]
    #: Notional the orchestrator wants to deploy, in quote currency.
    notional: float = Field(gt=0)
    gross_edge_bps: float
    costs: CostBreakdown
    expected_net_edge_bps: float
    consensus_score: float
    consensus_agreement: float
    max_slippage_bps: float
    #: Absolute deadline after which the intent must not be executed.
    deadline_ms: Millis
    urgency: float = Field(default=0.5, ge=0.0, le=1.0)
    #: Set when the intent is an exit rather than an entry.
    is_exit: bool = False


class PlannedOrder(Base):
    """One order inside an execution plan."""

    client_order_id: str = Field(default_factory=lambda: new_id("ord"))
    venue: str
    symbol: str
    side: Side
    quantity: float = Field(gt=0)
    order_type: OrderType
    time_in_force: TimeInForce
    limit_price: float | None = None
    #: Price VESKA expects to achieve, including modelled slippage.
    expected_price: float
    expected_fee_bps: float
    #: Milliseconds after submission at which an unfilled order is cancelled.
    ttl_ms: int = 5_000


class ExecutionPlan(Envelope):
    """VESKA's concrete plan for an authorised intent."""

    plan_id: str = Field(default_factory=lambda: new_id("plan"))
    intent_id: str
    strategy: str
    symbol: str
    orders: list[PlannedOrder]
    deadline_ms: Millis
    max_slippage_bps: float
    notional: float

    @property
    def total_quantity(self) -> float:
        return sum(o.quantity for o in self.orders)
