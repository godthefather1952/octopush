"""Hedging, reconciliation, health, kill-switch and generic system events."""

from __future__ import annotations

from pydantic import Field

from core.models.common import AgentId, Base, Envelope, Millis, Side, StrEnum, new_id

# --------------------------------------------------------------------------
# OKAPI — hedging
# --------------------------------------------------------------------------


class HedgeIntent(Envelope):
    """A simulated hedge OKAPI wants VESKA to work."""

    hedge_id: str = Field(default_factory=lambda: new_id("hdg"))
    symbol: str
    venue: str
    side: Side
    notional: float = Field(gt=0)
    #: Signed notional delta before hedging.
    current_delta: float
    #: Signed notional delta the strategy wants.
    target_delta: float
    reason_codes: list[str] = Field(default_factory=list)
    urgency: float = Field(default=0.5, ge=0.0, le=1.0)


class DeltaReport(Envelope):
    """OKAPI's view of exposure vs intent."""

    symbol: str
    desired_delta: float
    actual_delta: float
    unhedged_delta: float
    within_tolerance: bool
    tolerance: float


# --------------------------------------------------------------------------
# MARIN — reconciliation
# --------------------------------------------------------------------------


class MismatchKind(StrEnum):
    POSITION_MISMATCH = "POSITION_MISMATCH"
    CASH_MISMATCH = "CASH_MISMATCH"
    FEE_MISMATCH = "FEE_MISMATCH"
    PNL_MISMATCH = "PNL_MISMATCH"
    UNKNOWN_FILL = "UNKNOWN_FILL"
    MISSING_FILL = "MISSING_FILL"
    DUPLICATE_FILL = "DUPLICATE_FILL"
    ORDER_STATE_MISMATCH = "ORDER_STATE_MISMATCH"


class Severity(StrEnum):
    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


class Mismatch(Base):
    kind: MismatchKind
    severity: Severity
    key: str
    expected: float | str | None = None
    actual: float | str | None = None
    difference: float | None = None
    detail: str = ""


class ReconciliationResult(Envelope):
    """MARIN's comparison of platform belief against executor truth."""

    run_id: str = Field(default_factory=lambda: new_id("rec"))
    ok: bool
    mismatches: list[Mismatch] = Field(default_factory=list)
    orders_checked: int = 0
    fills_checked: int = 0
    positions_checked: int = 0

    @property
    def critical(self) -> list[Mismatch]:
        return [m for m in self.mismatches if m.severity is Severity.CRITICAL]

    @property
    def has_critical(self) -> bool:
        return bool(self.critical)


# --------------------------------------------------------------------------
# Health
# --------------------------------------------------------------------------


class HealthStatus(StrEnum):
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    OFFLINE = "OFFLINE"

    @property
    def rank(self) -> int:
        return {"HEALTHY": 0, "DEGRADED": 1, "OFFLINE": 2}[self.value]


class HealthState(Base):
    """A single component's heartbeat."""

    service: str
    status: HealthStatus = HealthStatus.OFFLINE
    last_event_age_ms: int | None = None
    last_heartbeat_ms: Millis | None = None
    queue_depth: int = 0
    error_count: int = 0
    version: str = "0.0.0"
    detail: str = ""


class SystemHealth(Envelope):
    components: dict[str, HealthState] = Field(default_factory=dict)

    @property
    def status(self) -> HealthStatus:
        if not self.components:
            return HealthStatus.OFFLINE
        return max((c.status for c in self.components.values()), key=lambda s: s.rank)

    def required_ok(self, required: list[str]) -> tuple[bool, list[str]]:
        """Return whether every required component is HEALTHY, plus the bad ones."""
        bad = [
            name
            for name in required
            if name not in self.components
            or self.components[name].status is not HealthStatus.HEALTHY
        ]
        return (not bad), bad


# --------------------------------------------------------------------------
# Kill switch
# --------------------------------------------------------------------------


class KillAction(StrEnum):
    HALT_NEW_TRADES = "HALT_NEW_TRADES"
    CANCEL_ALL = "CANCEL_ALL"
    FLATTEN = "FLATTEN"
    DISABLE_EXECUTION = "DISABLE_EXECUTION"


class KillSwitchState(Base):
    halt_new_trades: bool = False
    execution_disabled: bool = False
    flatten_requested: bool = False
    cancel_all_requested: bool = False
    triggered_by: list[str] = Field(default_factory=list)
    triggered_at: Millis | None = None

    @property
    def trading_allowed(self) -> bool:
        return not (self.halt_new_trades or self.execution_disabled)

    @property
    def engaged(self) -> bool:
        return (
            self.halt_new_trades
            or self.execution_disabled
            or self.flatten_requested
            or self.cancel_all_requested
        )


# --------------------------------------------------------------------------
# Generic system events & attribution
# --------------------------------------------------------------------------


class SystemEvent(Envelope):
    """Anything worth recording that is not a typed domain payload."""

    kind: str
    severity: Severity = Severity.INFO
    component: str
    message: str
    detail: dict[str, float | int | str | bool | None] = Field(default_factory=dict)


class TradeAttribution(Envelope):
    """Why a simulated trade happened, and what it produced."""

    trade_ref: str
    opportunity_id: str
    intent_id: str
    strategy: str
    symbol: str
    consensus_score: float
    consensus_agreement: float
    expected_net_edge_bps: float
    expected_costs_bps: float
    contributions: dict[AgentId, float] = Field(default_factory=dict)
    weights: dict[AgentId, float] = Field(default_factory=dict)
    signals: dict[AgentId, float] = Field(default_factory=dict)
    confidences: dict[AgentId, float] = Field(default_factory=dict)
    risk_verdict: str
    realized_pnl: float | None = None
    fees: float = 0.0
    slippage_bps: float | None = None
    filled_notional: float = 0.0
    closed_at: Millis | None = None


class AgentScore(Base):
    """Rolling predictive contribution for one agent."""

    agent_id: AgentId
    observations: int = 0
    #: Correlation-like score between the agent's signal and realised P&L.
    predictive_contribution: float = 0.0
    mean_signal: float = 0.0
    mean_confidence: float = 0.0
    hit_rate: float = 0.0
