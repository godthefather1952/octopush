"""Phase 8 audit — decision trace integrity and retention."""

from __future__ import annotations

from apps.orchestrator.coordination import CoordinationRegistry
from core.models.opportunity import StrategyState
from core.models.orchestration import ConsensusPurpose
from tests.audit.phase8_fixtures import T0, consensus_result


class TestDecisionTrace:
    def test_one_trace_per_opportunity(self):
        registry = CoordinationRegistry()
        first = registry.trace_for_opportunity("opp-1", T0, create=True)
        second = registry.trace_for_opportunity("opp-1", T0 + 1, create=True)
        assert second is first
        assert registry.traces_created == 1

    def test_linking_plan_and_orders_is_idempotent(self):
        registry = CoordinationRegistry()
        trace = registry.trace_for_opportunity("opp-1", T0, create=True)
        registry.link_execution_plan(
            "opp-1", "plan-1", T0 + 1, order_ids=["o-1", "o-2"]
        )
        registry.link_execution_plan(
            "opp-1", "plan-1", T0 + 2, order_ids=["o-1", "o-2"]
        )

        assert trace is not None
        assert trace.execution_plan_ids == ["plan-1"]
        assert trace.order_ids == ["o-1", "o-2"]

    def test_closing_trace_counts_once(self):
        registry = CoordinationRegistry()
        trace = registry.trace_for_opportunity("opp-1", T0, create=True)
        registry.update_trace_state("opp-1", StrategyState.CLOSED, T0 + 1)
        registry.update_trace_state("opp-1", StrategyState.CLOSED, T0 + 2)
        assert trace is not None and trace.is_closed
        assert registry.traces_closed == 1

    def test_reading_trace_never_creates_one(self):
        registry = CoordinationRegistry()
        assert registry.trace_for_opportunity("missing") is None
        assert registry.traces_created == 0


class TestRetention:
    def test_current_or_nonterminal_tick_is_never_compacted(self):
        registry = CoordinationRegistry()
        current = registry.begin_tick(T0, tick_number=1)
        released = registry.compact(keep_ticks=0)
        assert released == 0
        assert registry.get_tick(current.tick_id) is current

    def test_terminal_tick_compaction_preserves_lifetime_metrics(self):
        registry = CoordinationRegistry()
        tick = registry.begin_tick(T0, tick_number=1)
        registry.complete_tick(T0 + 1)
        before = registry.metrics()

        released = registry.compact(keep_ticks=0)
        after = registry.metrics()

        assert released == 1
        assert registry.get_tick(tick.tick_id) is None
        assert after.ticks_started == before.ticks_started
        assert after.ticks_completed == before.ticks_completed

    def test_closed_trace_can_compact_even_without_terminal_ticks(self):
        """H8-12: trace retention policy should not depend on tick eviction."""
        registry = CoordinationRegistry()
        trace = registry.trace_for_opportunity("opp-1", T0, create=True)
        registry.update_trace_state("opp-1", StrategyState.CLOSED, T0 + 1)

        registry.compact(keep_ticks=0, keep_open_traces=False)

        assert trace is not None
        assert registry.get_trace(trace.trace_id) is None
        assert registry.trace_for_opportunity("opp-1") is None

    def test_open_trace_survives_aggressive_compaction(self):
        registry = CoordinationRegistry()
        trace = registry.trace_for_opportunity("opp-1", T0, create=True)
        tick = registry.begin_tick(T0)
        registry.complete_tick(T0 + 1)

        registry.compact(keep_ticks=0, keep_open_traces=False)

        assert trace is not None
        assert registry.get_trace(trace.trace_id) is trace
        assert registry.get_tick(tick.tick_id) is None

    def test_closed_trace_does_not_orphan_newer_same_correlation_round(self):
        registry = CoordinationRegistry()
        trace = registry.trace_for_opportunity("opp-1", T0, create=True)
        first = registry.register_consensus_request(
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
        newer = registry.register_consensus_request(
            "opp-1", T0 + 2, purpose=ConsensusPurpose.CONTINUATION
        )
        registry.update_trace_state("opp-1", StrategyState.CLOSED, T0 + 3)
        registry.begin_tick(T0 + 3)
        registry.complete_tick(T0 + 3)

        registry.compact(keep_ticks=0, keep_open_traces=False)

        assert trace is not None
        assert registry.get_consensus_request(first.request_id) is None
        assert registry.get_consensus_evaluation(evaluation.evaluation_id) is None
        assert registry.request_for_correlation("opp-1") is newer
        assert registry.get_consensus_request(newer.request_id) is newer
