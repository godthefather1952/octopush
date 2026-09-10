"""Phase 8 audit — consensus request/evaluation semantics."""

from __future__ import annotations

from apps.orchestrator.coordination import CoordinationRegistry
from core.models.common import AgentId
from core.models.orchestration import (
    ConsensusPurpose,
    ConsensusRequestStatus,
    consensus_evaluation_from_result,
)
from tests.audit.phase8_fixtures import T0, consensus_result, opinion_reference


class TestRequestLifecycle:
    def test_open_reregistration_reuses_same_round(self):
        registry = CoordinationRegistry()
        first = registry.register_consensus_request(
            "opp-1", T0, purpose=ConsensusPurpose.ENTRY, required_agents={AgentId.TIDAL}
        )
        second = registry.register_consensus_request(
            "opp-1", T0 + 1, purpose=ConsensusPurpose.ENTRY, required_agents={AgentId.TIDAL}
        )

        assert second is first
        assert registry.requests_registered == 1

    def test_terminal_round_allows_new_round_for_same_correlation(self):
        registry = CoordinationRegistry()
        first = registry.register_consensus_request(
            "opp-1", T0, purpose=ConsensusPurpose.ENTRY, required_agents={AgentId.TIDAL}
        )
        registry.record_barrier_result(
            "opp-1",
            T0 + 1,
            responded={AgentId.TIDAL},
            required={AgentId.TIDAL},
            missing=set(),
            complete=True,
        )
        registry.record_consensus(
            consensus_result(),
            T0 + 1,
            purpose=ConsensusPurpose.ENTRY,
            correlation_id="opp-1",
            allowed=True,
        )

        second = registry.register_consensus_request(
            "opp-1", T0 + 2, purpose=ConsensusPurpose.ENTRY, required_agents={AgentId.TIDAL}
        )

        assert second.request_id != first.request_id
        assert registry.request_for_correlation("opp-1") is second
        assert registry.get_consensus_request(first.request_id) is first

    def test_entry_and_continuation_are_distinct_rounds(self):
        registry = CoordinationRegistry()
        entry = registry.register_consensus_request(
            "opp-1", T0, purpose=ConsensusPurpose.ENTRY
        )
        continuation = registry.register_consensus_request(
            "opp-1", T0 + 1, purpose=ConsensusPurpose.CONTINUATION
        )

        assert continuation.request_id != entry.request_id
        assert entry.purpose is ConsensusPurpose.ENTRY
        assert continuation.purpose is ConsensusPurpose.CONTINUATION

    def test_timeout_counter_is_idempotent_for_same_barrier_outcome(self):
        """H8-8: replaying the same timeout observation must not add timeouts."""
        registry = CoordinationRegistry()
        registry.register_consensus_request(
            "opp-1", T0, required_agents={AgentId.TIDAL, AgentId.NORO}
        )

        for now in (T0 + 10, T0 + 11):
            registry.record_barrier_result(
                "opp-1",
                now,
                responded={AgentId.TIDAL},
                required={AgentId.TIDAL, AgentId.NORO},
                missing={AgentId.NORO},
                complete=False,
                timed_out=True,
                waited_ms=10,
            )

        request = registry.request_for_correlation("opp-1")
        assert request is not None
        assert request.status is ConsensusRequestStatus.TIMED_OUT
        assert registry.requests_timed_out == 1

    def test_barrier_response_counter_counts_only_new_responders(self):
        registry = CoordinationRegistry()
        registry.register_consensus_request("opp-1", T0)
        registry.record_barrier_result(
            "opp-1", T0 + 1, responded={AgentId.TIDAL}, complete=False
        )
        registry.record_barrier_result(
            "opp-1", T0 + 2, responded={AgentId.TIDAL}, complete=False
        )
        assert registry.agent_responses == 1


class TestEvaluationTruth:
    def test_evaluation_copies_engine_answer_without_redeciding(self):
        result = consensus_result(score=0.1, agreement=0.1)
        evaluation = consensus_evaluation_from_result(
            result,
            purpose=ConsensusPurpose.ENTRY,
            now_ms=T0,
            entry_threshold=0.99,
            allowed=True,
        )

        assert evaluation.score == 0.1
        assert evaluation.agreement == 0.1
        assert evaluation.entry_threshold == 0.99
        assert evaluation.allowed is True

    def test_incomplete_evaluation_can_preserve_allowed_none(self):
        result = consensus_result(complete=False)
        evaluation = consensus_evaluation_from_result(
            result,
            purpose=ConsensusPurpose.ENTRY,
            now_ms=T0,
            allowed=None,
        )
        assert evaluation.complete is False
        assert evaluation.allowed is None

    def test_contribution_snapshot_is_detached_from_consensus_result(self):
        """H8-9: history must not alias a mutable attribution object."""
        result = consensus_result()
        evaluation = consensus_evaluation_from_result(
            result, purpose=ConsensusPurpose.ENTRY, now_ms=T0
        )

        original = evaluation.contributions[0].signal
        result.contributions[0].signal = -0.75

        assert evaluation.contributions[0].signal == original

    def test_opinion_reference_snapshot_is_detached_from_caller(self):
        """H8-10: a recorded input reference must describe the instant used."""
        ref = opinion_reference(signal=0.8)
        evaluation = consensus_evaluation_from_result(
            consensus_result(),
            purpose=ConsensusPurpose.ENTRY,
            now_ms=T0,
            opinion_refs=[ref],
        )

        ref.signal = -0.9

        assert evaluation.opinion_refs[0].signal == 0.8

    def test_record_consensus_links_request_and_trace(self):
        registry = CoordinationRegistry()
        trace = registry.trace_for_opportunity(
            "opp-1", T0, create=True, correlation_id="opp-1"
        )
        request = registry.register_consensus_request(
            "opp-1", T0, purpose=ConsensusPurpose.ENTRY
        )

        evaluation = registry.record_consensus(
            consensus_result(),
            T0 + 1,
            purpose=ConsensusPurpose.ENTRY,
            correlation_id="opp-1",
            allowed=True,
            opportunity_id="opp-1",
        )

        assert trace is not None
        assert request.status is ConsensusRequestStatus.COMPLETED
        assert request.consensus_evaluation_id == evaluation.evaluation_id
        assert evaluation.evaluation_id in trace.consensus_evaluation_ids
        assert request.request_id in trace.consensus_request_ids
