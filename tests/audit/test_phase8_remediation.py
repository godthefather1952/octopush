"""Phase 8 remediation regressions — P8-1 through P8-7.

One test per remediated defect (and per distinguishable consequence of one),
written against the behaviour the frozen inventory demanded rather than
against the implementation that satisfies it. Audit-only; production never
imports this module.
"""

from __future__ import annotations

import pytest

from apps.orchestrator.coordination import CoordinationRegistry
from core.models.common import AgentId
from core.models.orchestration import (
    ConsensusPurpose,
    ConsensusRequestStatus,
    OrchestrationPhase,
    OrchestrationTickStatus,
)
from tests.audit.phase8_fixtures import (
    T0,
    RecordingCoordinationStore,
    consensus_result,
)


class TestP8_2PersistenceIsNeverControlFlow:
    """A backend nobody must configure is a backend nobody must succeed at."""

    def test_every_persistence_seam_survives_a_store_that_always_raises(self):
        store = RecordingCoordinationStore(fail_always=True)
        registry = CoordinationRegistry(store=store)

        tick = registry.begin_tick(T0, tick_number=1)
        registry.enter_phase(OrchestrationPhase.SEEK, T0)
        registry.complete_phase(OrchestrationPhase.SEEK, T0 + 1)
        request = registry.register_consensus_request("opp-1", T0)
        trace = registry.trace_for_opportunity("opp-1", T0, create=True)
        evaluation = registry.record_consensus(
            consensus_result(),
            T0 + 2,
            purpose=ConsensusPurpose.ENTRY,
            correlation_id="opp-1",
            allowed=True,
            opportunity_id="opp-1",
        )
        registry.complete_tick(T0 + 3, tick_id=tick.tick_id)

        # Every one of the four put_* seams was exercised and none escaped.
        assert store.calls > 4
        assert registry.get_tick(tick.tick_id) is tick
        assert registry.get_consensus_request(request.request_id) is request
        assert registry.get_consensus_evaluation(evaluation.evaluation_id) is evaluation
        assert trace is not None
        assert registry.get_trace(trace.trace_id) is trace
        assert tick.status is OrchestrationTickStatus.COMPLETE

    def test_resident_truth_is_complete_even_when_nothing_was_persisted(self):
        store = RecordingCoordinationStore(fail_always=True)
        registry = CoordinationRegistry(store=store)
        tick = registry.begin_tick(T0)
        registry.note_tick("still recorded", tick_id=tick.tick_id, now_ms=T0 + 1)
        registry.count_tick(tick_id=tick.tick_id, opportunities_seen=3, now_ms=T0 + 2)

        assert store.ticks == []
        assert tick.notes == ["still recorded"]
        assert tick.opportunities_seen == 3

    def test_failing_store_logs_rather_than_silently_dropping(self, caplog):
        store = RecordingCoordinationStore(fail_always=True)
        registry = CoordinationRegistry(store=store)
        with caplog.at_level("ERROR", logger="apps.orchestrator.coordination"):
            registry.begin_tick(T0)
        assert caplog.records
        assert any(record.exc_info for record in caplog.records)


class TestP8_2OriginalExceptionSurvives:
    """The one place a swallowed store error matters most."""

    def test_registry_failure_recording_does_not_replace_the_tick_exception(self):
        store = RecordingCoordinationStore(fail_always=True)
        registry = CoordinationRegistry(store=store)
        tick = registry.begin_tick(T0)
        registry.enter_phase(OrchestrationPhase.SETTLE, T0)
        marker = RuntimeError("phase8 remediation marker")

        # The shape of the orchestrator's except block, in miniature.
        with pytest.raises(RuntimeError) as caught:
            try:
                raise marker
            except RuntimeError as exc:
                registry.fail_tick(T0 + 1, error=f"{type(exc).__name__}: {exc}")
                raise

        assert caught.value is marker
        assert tick.status is OrchestrationTickStatus.FAILED

    async def test_orchestrator_tick_propagates_its_own_exception_when_the_store_is_down(
        self, platform, monkeypatch
    ):
        """P8-2 end to end: a real tick failure, recorded through a dead store."""
        await platform.start(record=False)
        platform.clock.advance(100)
        await platform.step_market(1)
        platform.orchestrator.coordination.store = RecordingCoordinationStore(
            fail_always=True
        )
        marker = RuntimeError("phase8 remediation tick failure")

        async def explode(_now):
            raise marker

        monkeypatch.setattr(platform.orchestrator, "_settle", explode)
        try:
            with pytest.raises(RuntimeError) as caught:
                await platform.orchestrator.tick()
            assert caught.value is marker
            assert "coordination" not in str(caught.value)
            tick = platform.orchestrator.coordination.recent_ticks()[-1]
            assert tick.status is OrchestrationTickStatus.FAILED
        finally:
            await platform.stop()


class TestP8_3WriteThrough:
    def test_note_and_count_each_write_through_exactly_once(self):
        store = RecordingCoordinationStore()
        registry = CoordinationRegistry(store=store)
        tick = registry.begin_tick(T0)

        before = store.calls
        registry.note_tick("one", tick_id=tick.tick_id, now_ms=T0 + 1)
        registry.count_tick(tick_id=tick.tick_id, risk_evaluations=1, now_ms=T0 + 2)
        assert store.calls == before + 2

        assert store.ticks[-2].notes == ["one"]
        assert store.ticks[-1].risk_evaluations == 1

    def test_note_on_a_missing_tick_persists_nothing(self):
        store = RecordingCoordinationStore()
        registry = CoordinationRegistry(store=store)
        registry.note_tick("no open tick")
        registry.count_tick(opportunities_seen=1)
        assert store.calls == 0

    def test_empty_note_is_not_a_mutation_and_is_not_persisted(self):
        store = RecordingCoordinationStore()
        registry = CoordinationRegistry(store=store)
        tick = registry.begin_tick(T0)
        before = store.calls
        registry.note_tick("", tick_id=tick.tick_id, now_ms=T0 + 1)
        assert store.calls == before
        assert tick.notes == []

    def test_persisted_phase_pointer_never_lags_the_resident_one(self):
        store = RecordingCoordinationStore()
        registry = CoordinationRegistry(store=store)
        registry.begin_tick(T0)
        for offset, phase in enumerate(
            (
                OrchestrationPhase.OBSERVE,
                OrchestrationPhase.SETTLE,
                OrchestrationPhase.MEASURE,
                OrchestrationPhase.OBSERVE,
                OrchestrationPhase.SETTLE,
            ),
            start=1,
        ):
            registry.enter_phase(phase, T0 + offset)
            assert store.ticks[-1].current_phase is phase
            assert store.ticks[-1].updated_at == T0 + offset


class TestP8_1TerminalMetadataIsIdempotent:
    def test_repeated_complete_keeps_the_first_terminal_instant(self):
        registry = CoordinationRegistry()
        tick = registry.begin_tick(T0)

        first = registry.complete_tick(T0 + 1, tick_id=tick.tick_id)
        second = registry.complete_tick(T0 + 2, tick_id=tick.tick_id)
        third = registry.complete_tick(T0 + 3, tick_id=tick.tick_id)

        assert first is second is third is tick
        assert registry.ticks_completed == 1
        assert tick.completed_at == T0 + 1
        assert tick.updated_at == T0 + 1

    def test_repeated_failure_keeps_the_first_error(self):
        registry = CoordinationRegistry()
        tick = registry.begin_tick(T0)
        registry.enter_phase(OrchestrationPhase.SETTLE, T0)

        registry.fail_tick(T0 + 1, tick_id=tick.tick_id, error="first")
        registry.fail_tick(T0 + 2, tick_id=tick.tick_id, error="second")

        assert registry.ticks_failed == 1
        assert tick.completed_at == T0 + 1
        assert tick.error == "first"
        settle = tick.phase_record(OrchestrationPhase.SETTLE)
        assert settle is not None
        assert settle.completed_at == T0 + 1
        assert settle.detail == "first"

    def test_a_recorded_failure_is_never_upgraded_to_success(self):
        registry = CoordinationRegistry()
        tick = registry.begin_tick(T0)
        registry.fail_tick(T0 + 1, tick_id=tick.tick_id, error="real")

        registry.complete_tick(T0 + 2, tick_id=tick.tick_id)

        assert tick.status is OrchestrationTickStatus.FAILED
        assert tick.error == "real"
        assert registry.ticks_completed == 0

    def test_a_recorded_success_is_never_downgraded_to_failure(self):
        """Historical immutability, chosen deliberately over last-write-wins."""
        registry = CoordinationRegistry()
        tick = registry.begin_tick(T0)
        registry.complete_tick(T0 + 1, tick_id=tick.tick_id)

        registry.fail_tick(T0 + 2, tick_id=tick.tick_id, error="late")

        assert tick.status is OrchestrationTickStatus.COMPLETE
        assert tick.completed_at == T0 + 1
        assert tick.error == ""
        assert registry.ticks_failed == 0

    def test_terminal_idempotency_does_not_re_persist(self):
        store = RecordingCoordinationStore()
        registry = CoordinationRegistry(store=store)
        tick = registry.begin_tick(T0)
        registry.complete_tick(T0 + 1, tick_id=tick.tick_id)
        before = store.calls

        registry.complete_tick(T0 + 2, tick_id=tick.tick_id)
        registry.fail_tick(T0 + 3, tick_id=tick.tick_id, error="late")

        assert store.calls == before

    def test_a_closed_phase_keeps_its_verdict_instant_and_detail(self):
        registry = CoordinationRegistry()
        registry.begin_tick(T0)
        registry.enter_phase(OrchestrationPhase.MEASURE, T0)
        phase = registry.complete_phase(
            OrchestrationPhase.MEASURE, T0 + 1, ok=False, detail="original"
        )

        again = registry.complete_phase(
            OrchestrationPhase.MEASURE, T0 + 9, ok=True, detail="rewritten"
        )

        assert phase is not None
        assert again is phase
        assert phase.completed_at == T0 + 1
        assert phase.ok is False
        assert phase.detail == "original"

    def test_reopening_a_phase_does_not_reopen_its_verdict(self):
        registry = CoordinationRegistry()
        registry.begin_tick(T0)
        registry.enter_phase(OrchestrationPhase.SETTLE, T0)
        registry.complete_phase(OrchestrationPhase.SETTLE, T0 + 1, ok=True)
        registry.enter_phase(OrchestrationPhase.MEASURE, T0 + 2)
        registry.enter_phase(OrchestrationPhase.SETTLE, T0 + 3)

        registry.complete_phase(OrchestrationPhase.SETTLE, T0 + 4, ok=False)

        settle = registry.current_tick().phase_record(OrchestrationPhase.SETTLE)
        assert settle is not None
        assert settle.completed_at == T0 + 1
        assert settle.ok is True

    def test_failing_a_tick_does_not_rewrite_an_already_closed_phase(self):
        registry = CoordinationRegistry()
        tick = registry.begin_tick(T0)
        registry.enter_phase(OrchestrationPhase.OBSERVE, T0)
        registry.complete_phase(OrchestrationPhase.OBSERVE, T0 + 1, ok=True)
        registry.enter_phase(OrchestrationPhase.SETTLE, T0 + 2)

        registry.fail_tick(T0 + 3, tick_id=tick.tick_id, error="boom")

        observe = tick.phase_record(OrchestrationPhase.OBSERVE)
        assert observe is not None
        assert observe.ok is True
        assert observe.completed_at == T0 + 1


class TestP8_4TimeoutCounterCountsTransitions:
    def test_repeating_the_same_timeout_counts_once(self):
        registry = CoordinationRegistry()
        registry.register_consensus_request("opp-1", T0, required_agents=[AgentId.TIDAL])

        for offset in (1, 2, 3):
            registry.record_barrier_result(
                "opp-1",
                T0 + offset,
                responded=[],
                missing=[AgentId.TIDAL],
                complete=False,
                timed_out=True,
            )

        assert registry.requests_timed_out == 1
        assert registry.metrics().consensus_timeouts == 1

    def test_recovering_then_timing_out_again_counts_the_new_transition(self):
        registry = CoordinationRegistry()
        request = registry.register_consensus_request(
            "opp-1", T0, required_agents=[AgentId.TIDAL]
        )

        registry.record_barrier_result(
            "opp-1", T0 + 1, responded=[], complete=False, timed_out=True
        )
        registry.record_barrier_result(
            "opp-1", T0 + 2, responded=[AgentId.TIDAL], complete=True, timed_out=False
        )
        registry.record_barrier_result(
            "opp-1", T0 + 3, responded=[], complete=False, timed_out=True
        )

        assert request.status is ConsensusRequestStatus.TIMED_OUT
        assert registry.requests_timed_out == 2

    def test_barrier_completeness_is_still_copied_down_verbatim(self):
        registry = CoordinationRegistry()
        registry.register_consensus_request(
            "opp-1", T0, required_agents=[AgentId.TIDAL, AgentId.NORO]
        )

        record = registry.record_barrier_result(
            "opp-1",
            T0 + 1,
            responded=[AgentId.TIDAL],
            missing=[AgentId.NORO],
            complete=False,
            timed_out=True,
            waited_ms=250,
        )

        assert record is not None
        assert record.responded_agents == [AgentId.TIDAL]
        assert record.missing_agents == [AgentId.NORO]
        assert record.timed_out is True
        assert record.waited_ms == 250
        assert record.status is ConsensusRequestStatus.TIMED_OUT

    def test_repeating_the_same_completion_counts_once(self):
        registry = CoordinationRegistry()
        registry.register_consensus_request("opp-1", T0)

        for offset in (1, 2):
            registry.record_consensus(
                consensus_result(),
                T0 + offset,
                purpose=ConsensusPurpose.ENTRY,
                correlation_id="opp-1",
                allowed=True,
            )

        assert registry.requests_completed == 1
