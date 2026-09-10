"""Phase 8 audit — tick lifecycle and coordination persistence."""

from __future__ import annotations

import pytest

from apps.orchestrator.coordination import CoordinationRegistry
from core.models.orchestration import (
    OrchestrationPhase,
    OrchestrationTickStatus,
)
from tests.audit.phase8_fixtures import T0, RecordingCoordinationStore


class TestTickLifecycle:
    def test_tick_uses_only_caller_supplied_logical_time(self):
        registry = CoordinationRegistry()
        tick = registry.begin_tick(T0, tick_number=7, market_timestamp=T0)
        registry.enter_phase(OrchestrationPhase.OBSERVE, T0)
        registry.complete_phase(OrchestrationPhase.OBSERVE, T0)
        registry.complete_tick(T0)

        assert tick.created_at == T0
        assert tick.updated_at == T0
        assert tick.completed_at == T0
        assert tick.phase_record(OrchestrationPhase.OBSERVE).started_at == T0

    def test_complete_tick_is_idempotent_for_lifetime_metrics(self):
        """H8-1: a repeated metadata close must not create a second tick."""
        registry = CoordinationRegistry()
        tick = registry.begin_tick(T0)

        registry.complete_tick(T0 + 1, tick_id=tick.tick_id)
        registry.complete_tick(T0 + 2, tick_id=tick.tick_id)

        assert registry.ticks_completed == 1
        assert tick.completed_at == T0 + 1

    def test_fail_tick_is_idempotent_for_lifetime_metrics(self):
        """H8-2: repeated failure recording must not invent failed ticks."""
        registry = CoordinationRegistry()
        tick = registry.begin_tick(T0)
        registry.enter_phase(OrchestrationPhase.SETTLE, T0)

        registry.fail_tick(T0 + 1, tick_id=tick.tick_id, error="first")
        registry.fail_tick(T0 + 2, tick_id=tick.tick_id, error="second")

        assert registry.ticks_failed == 1
        assert tick.completed_at == T0 + 1
        assert tick.error == "first"

    def test_complete_phase_cannot_rewrite_finished_history(self):
        """H8-3: an already-closed phase is historical truth."""
        registry = CoordinationRegistry()
        registry.begin_tick(T0)
        registry.enter_phase(OrchestrationPhase.MEASURE, T0)
        phase = registry.complete_phase(
            OrchestrationPhase.MEASURE, T0 + 1, ok=False, detail="original"
        )

        registry.complete_phase(
            OrchestrationPhase.MEASURE, T0 + 9, ok=True, detail="rewritten"
        )

        assert phase is not None
        assert phase.completed_at == T0 + 1
        assert phase.ok is False
        assert phase.detail == "original"

    def test_previous_running_tick_remains_visible_when_next_tick_begins(self):
        registry = CoordinationRegistry()
        first = registry.begin_tick(T0, tick_number=1)
        second = registry.begin_tick(T0 + 1, tick_number=2)

        assert first.status is OrchestrationTickStatus.RUNNING
        assert registry.get_tick(first.tick_id) is first
        assert registry.current_tick() is second
        assert [t.tick_id for t in registry.recent_ticks()] == [
            first.tick_id,
            second.tick_id,
        ]


class TestPersistenceBoundary:
    def test_optional_store_failure_cannot_break_coordination_call(self):
        """H8-4: observability persistence must not become trading control flow."""
        store = RecordingCoordinationStore(fail_on=1)
        registry = CoordinationRegistry(store=store)

        try:
            tick = registry.begin_tick(T0, tick_number=1)
        except RuntimeError as exc:  # pragma: no cover - failure evidence
            pytest.fail(f"coordination persistence escaped into caller: {exc}")

        assert tick.status is OrchestrationTickStatus.RUNNING
        assert registry.current_tick() is tick

    def test_note_tick_writes_through_when_store_is_configured(self):
        """H8-5: persisted history must include free-text tick metadata."""
        store = RecordingCoordinationStore()
        registry = CoordinationRegistry(store=store)
        tick = registry.begin_tick(T0)
        before = store.calls

        registry.note_tick("audit note", tick_id=tick.tick_id, now_ms=T0 + 1)

        assert store.calls == before + 1
        assert store.ticks[-1].notes == ["audit note"]

    def test_count_tick_writes_through_when_store_is_configured(self):
        """H8-6: persisted counters must match resident tick counters."""
        store = RecordingCoordinationStore()
        registry = CoordinationRegistry(store=store)
        tick = registry.begin_tick(T0)
        before = store.calls

        registry.count_tick(
            tick_id=tick.tick_id,
            opportunities_seen=2,
            consensus_requests=1,
            now_ms=T0 + 1,
        )

        assert store.calls == before + 1
        assert store.ticks[-1].opportunities_seen == 2
        assert store.ticks[-1].consensus_requests == 1

    def test_reentered_phase_metadata_is_persisted(self):
        """H8-7: the store may not lag a resident current-phase pointer."""
        store = RecordingCoordinationStore()
        registry = CoordinationRegistry(store=store)
        registry.begin_tick(T0)
        registry.enter_phase(OrchestrationPhase.SETTLE, T0 + 1)
        registry.enter_phase(OrchestrationPhase.MEASURE, T0 + 2)
        before = store.calls

        registry.enter_phase(OrchestrationPhase.SETTLE, T0 + 3)

        assert store.calls == before + 1
        assert store.ticks[-1].current_phase is OrchestrationPhase.SETTLE
        assert store.ticks[-1].updated_at == T0 + 3
