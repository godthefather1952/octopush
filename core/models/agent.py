"""Analytical outputs: agent opinions and consensus."""

from __future__ import annotations

from pydantic import Field, model_validator

from core.models.common import AgentId, Base, DataQuality, Envelope, Millis


class AgentOpinion(Envelope):
    """The single output type every analytical agent publishes.

    ``signal`` is a directional score in [-1, 1] where positive means "the
    proposed trade direction is favourable".  ``confidence`` in [0, 1] is how
    strongly the agent stands behind that number.  Both are meaningless once
    ``expires_at`` has passed — see :meth:`quality_at`.
    """

    agent_id: AgentId
    symbol: str
    signal: float = Field(ge=-1.0, le=1.0)
    confidence: float = Field(ge=0.0, le=1.0)
    expires_at: Millis
    reason_codes: list[str] = Field(default_factory=list)
    model_version: str
    #: Agent-specific structured detail. Never parsed for control flow by
    #: other components; used for attribution and the dashboard.
    detail: dict[str, float | int | str | bool | None] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _cannot_expire_before_it_exists(self) -> AgentOpinion:
        if self.expires_at < self.created_at:
            raise ValueError(
                f"expires_at ({self.expires_at}) precedes created_at "
                f"({self.created_at}): an opinion cannot be born expired. "
                "quality_at() would report STALE, which is indistinguishable "
                "from an agent that has simply gone quiet."
            )
        return self

    @property
    def subject(self) -> str:
        """What this opinion is about.

        Per-opportunity agents (TIDAL/NORO/ZEPHR) set ``correlation_id`` to the
        opportunity id and are keyed by it.  Symbol-scoped agents (LUMEN, which
        runs on the slow intelligence loop and knows nothing about individual
        opportunities) leave it unset and are keyed by symbol.
        """
        return self.correlation_id or self.symbol

    def quality_at(self, now_ms: Millis, degraded_grace_ms: int = 0) -> DataQuality:
        """Classify this opinion's usability at ``now_ms``.

        An expired opinion never becomes a neutral opinion — it becomes
        ``DEGRADED`` inside the grace window and ``STALE`` beyond it.
        """
        if now_ms <= self.expires_at:
            return DataQuality.FRESH
        if now_ms <= self.expires_at + degraded_grace_ms:
            return DataQuality.DEGRADED
        return DataQuality.STALE

    def is_valid_at(self, now_ms: Millis) -> bool:
        return now_ms <= self.expires_at

    @property
    def ttl_ms(self) -> int:
        return self.expires_at - self.created_at


class AgentContribution(Base):
    """One agent's share of a consensus score, for attribution."""

    agent_id: AgentId
    signal: float
    confidence: float
    weight: float
    #: weight * confidence * signal
    weighted_signal: float
    #: Share of the absolute weighted mass, in [0, 1].
    contribution_share: float
    quality: DataQuality


class ConsensusResult(Envelope):
    """Weighted, freshness-aware combination of agent opinions."""

    symbol: str
    strategy: str
    #: Weighted mean signal in [-1, 1].
    score: float
    #: Confidence-weighted agreement, in [0, 1]. Used as the "consensus %".
    agreement: float
    contributions: list[AgentContribution] = Field(default_factory=list)
    #: Agents the strategy requires that were missing, stale or unavailable.
    missing_agents: list[AgentId] = Field(default_factory=list)
    degraded_agents: list[AgentId] = Field(default_factory=list)
    #: False when a required agent was not usable; the orchestrator must not
    #: treat an unusable agent as a neutral vote.
    complete: bool = True

    @property
    def consensus_pct(self) -> float:
        return self.agreement * 100.0
