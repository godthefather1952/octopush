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
    #: The agent answered, and deliberately casts no directional vote.
    #:
    #: There are three distinct states an agent can be in, and conflating any
    #: two of them corrupts the consensus:
    #:
    #: * **Missing** -- no usable opinion at all (offline, stale, unable to
    #:   read its inputs). A required agent in this state suspends trading.
    #: * **Informative** (``abstain=False``, the default) -- the agent has
    #:   evidence and is voting on it. A signal of exactly ``0.0`` here is a
    #:   real claim: *the evidence supports neutrality*.
    #: * **Abstaining** (``abstain=True``) -- the agent evaluated the question
    #:   successfully but lacks the independent information a directional
    #:   claim would require. It is present, healthy and complete, and it
    #:   supplies no scoring mass at all: neither numerator nor denominator.
    #:
    #: The distinction is not decorative. A weighted mean divides by the
    #: weights it summed, so an agent that contributes ``0`` to the numerator
    #: while contributing its weight to the denominator does not abstain --
    #: it votes *against* whatever the other agents concluded, in proportion
    #: to its own weight. That is how NORO's honest "I have no independent
    #: valuation evidence" came to suppress consensus on every two-venue
    #: market.
    #:
    #: Confidence is a different dimension and is not a substitute: an
    #: informative model can legitimately report low or zero confidence, and
    #: an abstention is a statement about participation, not certainty.
    #:
    #: Defaults to ``False`` so that agents which never abstain need no
    #: change, and so that opinions serialised before this field existed still
    #: validate.
    abstain: bool = False
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
    #: Agents that were present and usable but cast no directional vote —
    #: :attr:`AgentOpinion.abstain`.
    #:
    #: Deliberately separate from :attr:`missing_agents`. An abstaining agent
    #: answered the question and is not a hole in the data, so overloading
    #: "missing" with it would suspend the strategy on markets where the agent
    #: is working exactly as designed. It carries no
    #: :class:`AgentContribution`, because it influenced nothing and a
    #: zero-weight row would only invite something downstream to re-add its
    #: weight. Defaults to ``[]`` so results recorded before this field
    #: existed still validate.
    abstained_agents: list[AgentId] = Field(default_factory=list)
    #: False when a required agent was not usable; the orchestrator must not
    #: treat an unusable agent as a neutral vote.
    complete: bool = True

    @property
    def consensus_pct(self) -> float:
        return self.agreement * 100.0
