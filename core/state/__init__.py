"""Shared system state.

The orchestrator owns one instance of :class:`SystemState`; every other
component reads from it through typed accessors.  Freshness is enforced at the
read boundary: an expired opinion is never returned as if it were current.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from core.clock import Clock
from core.models.agent import AgentOpinion
from core.models.common import AgentId, DataQuality, Millis
from core.models.execution import FillEvent, PaperOrder
from core.models.market import MarketState
from core.models.opportunity import Opportunity, StrategyState, TradeIntent
from core.models.ops import KillSwitchState, SystemHealth
from core.models.portfolio import PortfolioState
from core.models.risk import RiskDecision, RiskUtilization


@dataclass
class OpinionSlot:
    """Latest opinion from one agent for one symbol, plus its quality."""

    opinion: AgentOpinion
    quality: DataQuality


@dataclass
class OpportunityRecord:
    """An opportunity and its position in the strategy state machine."""

    opportunity: Opportunity
    state: StrategyState = StrategyState.OPPORTUNITY_DETECTED
    intent: TradeIntent | None = None
    decision: RiskDecision | None = None
    order_ids: list[str] = field(default_factory=list)
    entry_agreement: float | None = None
    last_agreement: float | None = None
    rejected_reason: str | None = None
    #: How many times an exit has been submitted for this opportunity. An exit
    #: leg can expire unfilled, and closing the record while a position is
    #: still open would orphan it.
    exit_attempts: int = 0
    updated_at: Millis = 0
    #: Realised P&L attributed to this opportunity.
    realized_pnl: float = 0.0
    fees: float = 0.0
    filled_notional: float = 0.0

    @property
    def is_open(self) -> bool:
        return self.state not in (StrategyState.CLOSED, StrategyState.REJECTED)


@dataclass
class SystemState:
    """Global mutable state, read by everything, written by the orchestrator."""

    clock: Clock
    market: MarketState | None = None
    portfolio: PortfolioState | None = None
    health: SystemHealth | None = None
    kill_switch: KillSwitchState = field(default_factory=KillSwitchState)
    risk_utilization: RiskUtilization = field(default_factory=RiskUtilization)

    #: (subject, agent) -> latest opinion, where subject is an opportunity id
    #: for per-opportunity agents and a symbol for symbol-scoped ones.
    opinions: dict[tuple[str, AgentId], AgentOpinion] = field(default_factory=dict)
    opportunities: dict[str, OpportunityRecord] = field(default_factory=dict)
    orders: dict[str, PaperOrder] = field(default_factory=dict)
    fills: list[FillEvent] = field(default_factory=list)
    #: Recent rejections, newest last, bounded for the dashboard.
    rejections: list[RiskDecision] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    max_history: int = 200

    # -- opinions ----------------------------------------------------------

    def put_opinion(self, opinion: AgentOpinion) -> None:
        key = (opinion.subject, opinion.agent_id)
        current = self.opinions.get(key)
        # Out-of-order delivery must not resurrect an older opinion.
        if current is not None and current.created_at > opinion.created_at:
            return
        self.opinions[key] = opinion

    def opinion(
        self, subject: str, agent: AgentId, *, degraded_grace_ms: int = 0
    ) -> OpinionSlot | None:
        """Return the agent's opinion with its quality, or ``None`` if absent.

        A missing agent returns ``None`` — callers must handle absence
        explicitly rather than substituting a zero signal.
        """
        opinion = self.opinions.get((subject, agent))
        if opinion is None:
            return None
        quality = opinion.quality_at(self.clock.now_ms(), degraded_grace_ms)
        return OpinionSlot(opinion=opinion, quality=quality)

    def opinions_for(
        self, *subjects: str, degraded_grace_ms: int = 0
    ) -> dict[AgentId, OpinionSlot]:
        """Collect opinions across subjects, earlier subjects winning.

        Callers pass the opportunity id first and the symbol second, so a
        per-opportunity opinion takes precedence over a symbol-scoped one.
        """
        out: dict[AgentId, OpinionSlot] = {}
        now = self.clock.now_ms()
        for subject in subjects:
            for (subj, agent), opinion in self.opinions.items():
                if subj != subject or agent in out:
                    continue
                out[agent] = OpinionSlot(
                    opinion=opinion,
                    quality=opinion.quality_at(now, degraded_grace_ms),
                )
        return out

    def prune_opinions(self, keep_subjects: set[str]) -> None:
        """Drop opinions whose subject is no longer live."""
        for key in [k for k in self.opinions if k[0] not in keep_subjects]:
            del self.opinions[key]

    # -- opportunities -----------------------------------------------------

    def add_opportunity(self, opportunity: Opportunity) -> OpportunityRecord:
        record = OpportunityRecord(
            opportunity=opportunity, updated_at=self.clock.now_ms()
        )
        self.opportunities[opportunity.opportunity_id] = record
        self._trim_opportunities()
        return record

    def open_opportunities(self) -> list[OpportunityRecord]:
        return [r for r in self.opportunities.values() if r.is_open]

    def _trim_opportunities(self) -> None:
        if len(self.opportunities) <= self.max_history:
            return
        closed = [
            (r.updated_at, oid)
            for oid, r in self.opportunities.items()
            if not r.is_open
        ]
        closed.sort()
        for _, oid in closed[: len(self.opportunities) - self.max_history]:
            self.opportunities.pop(oid, None)

    # -- orders & fills ----------------------------------------------------

    def put_order(self, order: PaperOrder) -> None:
        self.orders[order.client_order_id] = order

    def open_orders(self) -> list[PaperOrder]:
        return [o for o in self.orders.values() if o.is_live]

    def add_fill(self, fill: FillEvent) -> bool:
        """Record a fill. Returns False if it was a duplicate."""
        if any(f.fill_id == fill.fill_id for f in self.fills[-500:]):
            return False
        self.fills.append(fill)
        if len(self.fills) > 5_000:
            del self.fills[:1_000]
        return True

    # -- misc --------------------------------------------------------------

    def record_rejection(self, decision: RiskDecision) -> None:
        self.rejections.append(decision)
        if len(self.rejections) > self.max_history:
            del self.rejections[: len(self.rejections) - self.max_history]

    def record_error(self, message: str) -> None:
        self.errors.append(f"{self.clock.now_ms()} {message}")
        if len(self.errors) > self.max_history:
            del self.errors[: len(self.errors) - self.max_history]


__all__ = ["OpinionSlot", "OpportunityRecord", "SystemState"]
