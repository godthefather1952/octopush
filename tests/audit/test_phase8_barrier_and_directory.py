"""Phase 8 audit — read-only barrier views and agent directory metadata."""

from __future__ import annotations

from apps.orchestrator.agent_directory import AgentDirectory
from core.bus.barrier import ResponseBarrier
from core.models.common import AgentId
from core.models.orchestration import AgentCadence, AgentSubjectScope
from tests.audit.phase8_fixtures import T0


class TestBarrierObservation:
    def test_snapshot_is_read_only(self, clock):
        barrier = ResponseBarrier(clock)
        barrier.expect("opp-1", {AgentId.TIDAL, AgentId.NORO})
        barrier.record("opp-1", AgentId.TIDAL)
        before_outstanding = barrier.outstanding
        before_responded = barrier.responded("opp-1")

        snapshot = barrier.snapshot("opp-1", T0)
        all_snapshots = barrier.all_pending_snapshots(T0)

        assert snapshot is not None
        assert snapshot.responded == [AgentId.TIDAL]
        assert snapshot.missing == [AgentId.NORO]
        assert barrier.outstanding == before_outstanding
        assert barrier.responded("opp-1") == before_responded
        assert [item.correlation_id for item in all_snapshots] == ["opp-1"]

    def test_unknown_and_forgotten_ids_have_no_snapshot(self, clock):
        barrier = ResponseBarrier(clock)
        assert barrier.snapshot("missing", T0) is None
        barrier.expect("opp-1", {AgentId.TIDAL})
        barrier.forget("opp-1")
        assert barrier.snapshot("opp-1", T0) is None

    def test_snapshot_uses_supplied_logical_time(self, clock):
        barrier = ResponseBarrier(clock)
        barrier.expect("opp-1", {AgentId.TIDAL})
        clock.advance(10_000)
        snapshot = barrier.snapshot("opp-1", T0)
        assert snapshot is not None
        assert snapshot.captured_at == T0

    def test_observation_does_not_change_late_response_counter(self, clock):
        barrier = ResponseBarrier(clock)
        barrier.expect("opp-1", {AgentId.TIDAL})
        before = barrier.late_responses
        barrier.snapshot("opp-1", T0)
        barrier.pending_ids()
        barrier.all_pending_snapshots(T0)
        assert barrier.late_responses == before


class TestAgentDirectory:
    def test_reregistration_replaces_in_place(self):
        directory = AgentDirectory()
        directory.register(AgentId.TIDAL, service="old")
        directory.register(AgentId.TIDAL, service="new")
        assert len(directory) == 1
        assert directory.get(AgentId.TIDAL).service == "new"

    def test_authoritative_required_list_is_copied_verbatim(self):
        directory = AgentDirectory()
        directory.register(
            AgentId.TIDAL,
            required_by_default=False,
            scope=AgentSubjectScope.OPPORTUNITY,
            cadence=AgentCadence.FAST,
        )

        snapshot = directory.snapshot(
            T0, required_agents=[AgentId.NORO, AgentId.TIDAL]
        )

        assert snapshot.required_agents == [AgentId.NORO, AgentId.TIDAL]
        assert directory.required() == []

    def test_directory_snapshot_is_detached_from_live_descriptor(self):
        """H8-11: a captured directory must not mutate retroactively."""
        directory = AgentDirectory()
        descriptor = directory.register(AgentId.TIDAL, service="TIDAL", weight=1.4)
        snapshot = directory.snapshot(T0)

        descriptor.weight = 99.0
        descriptor.service = "MUTATED"

        assert snapshot.agents[0].weight == 1.4
        assert snapshot.agents[0].service == "TIDAL"

    def test_forgetting_metadata_does_not_mutate_existing_snapshot(self):
        directory = AgentDirectory()
        directory.register(AgentId.TIDAL, service="TIDAL")
        snapshot = directory.snapshot(T0)
        directory.forget(AgentId.TIDAL)

        assert directory.contains(AgentId.TIDAL) is False
        assert [d.agent_id for d in snapshot.agents] == [AgentId.TIDAL]
