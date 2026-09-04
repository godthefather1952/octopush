"""Trade attribution and agent scorecards.

Every simulated trade records *why* it happened: which agents argued for it,
how strongly, what edge was expected, what it actually cost, and what it
produced.  Without this, an eventual decision to reweight agents would be
guesswork.

Weights are deliberately **not** adapted here.  Early development collects
evidence; performance-aware weighting is a later change with its own
safeguards against overfitting.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from core.models.agent import ConsensusResult
from core.models.common import AgentId, Millis
from core.models.ops import AgentScore, TradeAttribution
from core.models.risk import RiskDecision


@dataclass
class AttributionBuilder:
    """Assembles a :class:`TradeAttribution` as a trade progresses."""

    trade_ref: str
    opportunity_id: str
    intent_id: str
    strategy: str
    symbol: str
    created_at: Millis
    consensus: ConsensusResult
    expected_net_edge_bps: float
    expected_costs_bps: float
    decision: RiskDecision
    #: Pre-fee realized P&L, accumulated one fill at a time via
    #: :meth:`add_realized` -- never read from a position's lifetime-cumulative
    #: counter, which would mix in every other opportunity ever traded on the
    #: same venue:symbol (see ``Orchestrator._track_realized_delta``).
    realized_pnl_gross: float = 0.0
    fees: float = 0.0
    filled_notional: float = 0.0
    slippage_samples: list[float] = field(default_factory=list)
    closed_at: Millis | None = None

    def add_fill(self, notional: float, fee: float, slippage_bps: float) -> None:
        self.filled_notional += notional
        self.fees += fee
        self.slippage_samples.append(slippage_bps)

    def add_realized(self, delta: float) -> None:
        """Add one fill's own contribution to this trade's realized P&L.

        ``delta`` must already be isolated to this fill alone -- the caller is
        responsible for not handing this method a cumulative or shared value.
        """
        self.realized_pnl_gross += delta

    def build(self) -> TradeAttribution:
        return TradeAttribution(
            created_at=self.created_at,
            correlation_id=self.opportunity_id,
            trade_ref=self.trade_ref,
            opportunity_id=self.opportunity_id,
            intent_id=self.intent_id,
            strategy=self.strategy,
            symbol=self.symbol,
            consensus_score=self.consensus.score,
            consensus_agreement=self.consensus.agreement,
            expected_net_edge_bps=self.expected_net_edge_bps,
            expected_costs_bps=self.expected_costs_bps,
            contributions={
                c.agent_id: c.contribution_share for c in self.consensus.contributions
            },
            weights={c.agent_id: c.weight for c in self.consensus.contributions},
            signals={c.agent_id: c.signal for c in self.consensus.contributions},
            confidences={c.agent_id: c.confidence for c in self.consensus.contributions},
            risk_verdict=self.decision.verdict.value,
            realized_pnl=self.realized_pnl_gross - self.fees,
            fees=self.fees,
            slippage_bps=(
                sum(self.slippage_samples) / len(self.slippage_samples)
                if self.slippage_samples
                else None
            ),
            filled_notional=self.filled_notional,
            closed_at=self.closed_at,
        )


@dataclass
class _AgentSamples:
    signals: list[float] = field(default_factory=list)
    confidences: list[float] = field(default_factory=list)
    outcomes: list[float] = field(default_factory=list)


class Scorecard:
    """Rolling predictive contribution per agent.

    ``predictive_contribution`` is the Pearson correlation between an agent's
    signal and the realised P&L of the trades it weighed in on.  It is a
    diagnostic, not a control input: nothing reads it back into the weights.
    """

    def __init__(self, min_observations: int = 20) -> None:
        self.min_observations = min_observations
        self._samples: dict[AgentId, _AgentSamples] = {}
        self.trades: list[TradeAttribution] = []

    def record(self, attribution: TradeAttribution) -> None:
        self.trades.append(attribution)
        if len(self.trades) > 5_000:
            del self.trades[:1_000]
        outcome = attribution.realized_pnl
        if outcome is None:
            return
        for agent, signal in attribution.signals.items():
            samples = self._samples.setdefault(agent, _AgentSamples())
            samples.signals.append(signal)
            samples.confidences.append(attribution.confidences.get(agent, 0.0))
            samples.outcomes.append(outcome)

    @staticmethod
    def _correlation(xs: list[float], ys: list[float]) -> float:
        n = len(xs)
        if n < 2:
            return 0.0
        mean_x = sum(xs) / n
        mean_y = sum(ys) / n
        cov = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys, strict=True))
        var_x = sum((x - mean_x) ** 2 for x in xs)
        var_y = sum((y - mean_y) ** 2 for y in ys)
        if var_x <= 0 or var_y <= 0:
            return 0.0
        value = cov / math.sqrt(var_x * var_y)
        return max(-1.0, min(1.0, value))

    def score(self, agent: AgentId) -> AgentScore:
        samples = self._samples.get(agent)
        if samples is None or not samples.signals:
            return AgentScore(agent_id=agent)
        n = len(samples.signals)
        hits = sum(
            1
            for signal, outcome in zip(samples.signals, samples.outcomes, strict=True)
            if (signal > 0 and outcome > 0) or (signal < 0 and outcome < 0)
        )
        return AgentScore(
            agent_id=agent,
            observations=n,
            predictive_contribution=(
                self._correlation(samples.signals, samples.outcomes)
                if n >= self.min_observations
                else 0.0
            ),
            mean_signal=sum(samples.signals) / n,
            mean_confidence=sum(samples.confidences) / n,
            hit_rate=hits / n,
        )

    def scores(self) -> dict[AgentId, AgentScore]:
        return {agent: self.score(agent) for agent in sorted(self._samples, key=str)}

    @property
    def sufficient_evidence(self) -> bool:
        """Whether enough trades exist to say anything at all."""
        return all(
            score.observations >= self.min_observations for score in self.scores().values()
        ) and bool(self._samples)
