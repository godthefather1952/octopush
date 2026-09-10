"""Deterministic instruments for the Phase 8 orchestration validation audit.

Audit-only. Production never imports this module.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from apps.orchestrator.coordination import CoordinationStore
from core.models.agent import AgentContribution, ConsensusResult
from core.models.common import AgentId, DataQuality, Millis
from core.models.orchestration import (
    ConsensusEvaluationRecord,
    ConsensusRequestRecord,
    DecisionTrace,
    OpinionReference,
    OrchestrationTickRecord,
)

T0: Millis = 1_700_200_000_000


@dataclass
class RecordingCoordinationStore(CoordinationStore):
    """Write-through seam that can fail on one exact persistence call."""

    fail_on: int | None = None
    #: Raise on every write, not just one. Models a backend that is down for
    #: the whole of a tick rather than flaking once.
    fail_always: bool = False
    calls: int = 0
    ticks: list[OrchestrationTickRecord] = field(default_factory=list)
    requests: list[ConsensusRequestRecord] = field(default_factory=list)
    evaluations: list[ConsensusEvaluationRecord] = field(default_factory=list)
    traces: list[DecisionTrace] = field(default_factory=list)

    def _before_write(self) -> None:
        self.calls += 1
        if self.fail_always or (self.fail_on is not None and self.calls == self.fail_on):
            raise RuntimeError("audit coordination-store failure")

    def put_tick(self, record: OrchestrationTickRecord) -> None:
        self._before_write()
        self.ticks.append(record.model_copy(deep=True))

    def put_consensus_request(self, record: ConsensusRequestRecord) -> None:
        self._before_write()
        self.requests.append(record.model_copy(deep=True))

    def put_consensus_evaluation(self, record: ConsensusEvaluationRecord) -> None:
        self._before_write()
        self.evaluations.append(record.model_copy(deep=True))

    def put_trace(self, trace: DecisionTrace) -> None:
        self._before_write()
        self.traces.append(trace.model_copy(deep=True))


def contribution(
    *,
    agent_id: AgentId = AgentId.TIDAL,
    signal: float = 0.8,
    confidence: float = 0.9,
) -> AgentContribution:
    weight = 1.4
    return AgentContribution(
        agent_id=agent_id,
        signal=signal,
        confidence=confidence,
        weight=weight,
        weighted_signal=weight * confidence * signal,
        contribution_share=1.0,
        quality=DataQuality.FRESH,
    )


def consensus_result(
    *,
    correlation_id: str = "opp-1",
    score: float = 0.8,
    agreement: float = 0.9,
    complete: bool = True,
) -> ConsensusResult:
    return ConsensusResult(
        created_at=T0,
        correlation_id=correlation_id,
        symbol="BTC-USD",
        strategy="cross_venue",
        score=score,
        agreement=agreement,
        contributions=[contribution()],
        complete=complete,
    )


def opinion_reference(
    *, agent_id: AgentId = AgentId.TIDAL, signal: float = 0.8
) -> OpinionReference:
    return OpinionReference(
        agent_id=agent_id,
        subject="opp-1",
        created_at=T0,
        expires_at=T0 + 1_000,
        quality=DataQuality.FRESH,
        signal=signal,
        confidence=0.9,
        model_version="audit-v1",
    )
