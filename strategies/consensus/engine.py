"""Weighted consensus.

Consensus is a *soft* mechanism.  It decides whether a trade is worth putting
in front of RUNE; it never decides whether a trade is allowed.  A hard gate
failure is not outvoted, no matter how strong the agreement.

    consensus = sum(weight * confidence * signal) / sum(weight * confidence)

Agents that are missing, stale or unavailable are not folded in as zeros — a
zero is an opinion, and an absent agent has none.  A missing *required* agent
makes the result incomplete, which the orchestrator treats as a stop.
"""

from __future__ import annotations

from core.clock import Clock
from core.config import ConsensusConfig
from core.models.agent import AgentContribution, ConsensusResult
from core.models.common import AgentId, DataQuality, Millis
from core.state import OpinionSlot


def _effective_weight(base: float, quality: DataQuality, degraded_factor: float) -> float:
    if quality is DataQuality.FRESH:
        return base
    if quality is DataQuality.DEGRADED:
        return base * degraded_factor
    return 0.0


class ConsensusEngine:
    def __init__(self, config: ConsensusConfig, clock: Clock) -> None:
        self.config = config
        self.clock = clock

    def combine(
        self,
        *,
        symbol: str,
        strategy: str,
        opinions: dict[AgentId, OpinionSlot],
        correlation_id: str | None = None,
        required: list[AgentId] | None = None,
        now_ms: Millis | None = None,
    ) -> ConsensusResult:
        # A tick supplies its own canonical time so the result it computes is
        # stamped with the instant it decided at, not a later clock read
        # (Phase 2 Batch 1.4).
        now = self.clock.now_ms() if now_ms is None else now_ms
        required_agents = required if required is not None else self.config.required_agents

        contributions: list[AgentContribution] = []
        missing: list[AgentId] = []
        degraded: list[AgentId] = []
        weighted_signal_sum = 0.0
        weight_sum = 0.0

        for agent, base_weight in self.config.weights.items():
            slot = opinions.get(agent)
            if slot is None:
                missing.append(agent)
                continue
            weight = _effective_weight(
                base_weight, slot.quality, self.config.degraded_weight_factor
            )
            if slot.quality is DataQuality.DEGRADED:
                degraded.append(agent)
            if weight <= 0:
                # STALE or UNAVAILABLE: excluded entirely, and reported as
                # missing so the caller can see the hole.
                missing.append(agent)
                continue
            effective = weight * slot.opinion.confidence
            weighted = effective * slot.opinion.signal
            weighted_signal_sum += weighted
            weight_sum += effective
            contributions.append(
                AgentContribution(
                    agent_id=agent,
                    signal=slot.opinion.signal,
                    confidence=slot.opinion.confidence,
                    weight=weight,
                    weighted_signal=weighted,
                    contribution_share=0.0,
                    quality=slot.quality,
                )
            )

        score = weighted_signal_sum / weight_sum if weight_sum > 0 else 0.0

        # Attribution shares are computed on absolute weighted mass, so an
        # agent that argued strongly against the trade still shows up as
        # having influenced it.
        total_mass = sum(abs(c.weighted_signal) for c in contributions)
        for contribution in contributions:
            contribution.contribution_share = (
                abs(contribution.weighted_signal) / total_mass if total_mass > 0 else 0.0
            )

        missing_required = [a for a in required_agents if a in missing]
        return ConsensusResult(
            created_at=now,
            correlation_id=correlation_id,
            symbol=symbol,
            strategy=strategy,
            score=max(-1.0, min(1.0, score)),
            agreement=max(0.0, min(1.0, score)),
            contributions=contributions,
            missing_agents=missing,
            degraded_agents=degraded,
            complete=not missing_required,
        )

    def entry_allowed(self, result: ConsensusResult) -> bool:
        return result.complete and result.agreement >= self.config.entry_threshold

    def continuation_allowed(self, result: ConsensusResult) -> bool:
        """Whether an open position should be maintained.

        Uses a lower threshold than entry so that a position is not churned
        out on the first tick of noise.
        """
        return result.complete and result.agreement >= self.config.exit_threshold
