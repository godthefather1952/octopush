"""Phase 8 audit — orchestrator integration, layering and paper boundary."""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from apps.orchestrator.orchestrator import Orchestrator
from core.models.orchestration import (
    AgentEndpointDescriptor,
    OrchestrationPhase,
    OrchestrationTickStatus,
)
from core.models.venue_execution import VenueBalanceSnapshot as CoreBalance
from core.models.venue_execution import VenueFillSnapshot as CoreFill
from core.models.venue_execution import VenueOrderSnapshot as CoreOrder
from execution.gateway import VenueBalanceSnapshot as GatewayBalance
from execution.gateway import VenueFillSnapshot as GatewayFill
from execution.gateway import VenueOrderSnapshot as GatewayOrder


class TestTickIntegration:
    def test_source_preserves_canonical_phase_order(self):
        source = inspect.getsource(Orchestrator._tick_body)
        positions = [
            source.index(f"enter_phase(OrchestrationPhase.{phase})")
            for phase in ("OBSERVE", "SETTLE", "MEASURE", "PROTECT", "MANAGE", "SEEK")
        ]
        assert positions == sorted(positions)

    def test_seek_recording_remains_inside_trading_allowed_guard(self):
        source = inspect.getsource(Orchestrator._tick_body)
        guard = source.index("if self.state.kill_switch.trading_allowed:")
        seek = source.index("enter_phase(OrchestrationPhase.SEEK")
        assert seek > guard

    async def test_tick_failure_is_recorded_and_same_exception_propagates(
        self, platform, monkeypatch
    ):
        await platform.start(record=False)
        platform.clock.advance(100)
        await platform.step_market(1)
        marker = RuntimeError("phase8 audit tick failure")

        async def explode(_now):
            raise marker

        monkeypatch.setattr(platform.orchestrator, "_settle", explode)
        try:
            with pytest.raises(RuntimeError) as caught:
                await platform.orchestrator.tick()
            assert caught.value is marker
            tick = platform.orchestrator.coordination.recent_ticks()[-1]
            assert tick.status is OrchestrationTickStatus.FAILED
            settle = tick.phase_record(OrchestrationPhase.SETTLE)
            assert settle is not None
            assert settle.ok is False
            assert settle.completed_at == tick.completed_at
            assert tick.phase_record(OrchestrationPhase.MEASURE) is None
        finally:
            await platform.stop()

    async def test_warmup_tick_does_not_invent_later_phases(self, platform):
        await platform.start(record=False)
        platform.clock.advance(100)
        await platform.step_market(1)
        try:
            await platform.orchestrator.tick()
            tick = platform.orchestrator.coordination.recent_ticks()[-1]
            phases = [phase.phase for phase in tick.phases]
            assert phases[:3] == [
                OrchestrationPhase.OBSERVE,
                OrchestrationPhase.SETTLE,
                OrchestrationPhase.MEASURE,
            ]
            if tick.warming_up and not platform.orchestrator.warmed_up:
                assert OrchestrationPhase.PROTECT not in phases
                assert OrchestrationPhase.MANAGE not in phases
                assert OrchestrationPhase.SEEK not in phases
            assert tick.status is OrchestrationTickStatus.COMPLETE
        finally:
            await platform.stop()


class TestLayering:
    def test_core_has_no_runtime_import_from_execution(self):
        violations: list[str] = []
        for path in Path("core").rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            for line in text.splitlines():
                stripped = line.strip()
                if stripped.startswith("from execution") or stripped.startswith(
                    "import execution"
                ):
                    violations.append(f"{path}:{stripped}")
        assert violations == []

    def test_gateway_reexports_core_venue_value_types(self):
        assert GatewayOrder is CoreOrder
        assert GatewayFill is CoreFill
        assert GatewayBalance is CoreBalance


class TestObservabilityOnlyBoundary:
    def test_coordination_registry_is_not_used_in_a_decision_condition(self):
        source = inspect.getsource(Orchestrator)
        forbidden = (
            "if self.coordination",
            "if not self.coordination",
            "while self.coordination",
            "return self.coordination",
        )
        assert not any(token in source for token in forbidden)

    def test_phase8_files_add_no_private_execution_or_credentials(self):
        paths = (
            Path("apps/orchestrator/coordination.py"),
            Path("apps/orchestrator/agent_directory.py"),
            Path("core/models/orchestration.py"),
        )
        forbidden = (
            "api_key",
            "secret_key",
            "private_key",
            "place_order(",
            "submit_order(",
            "cancel_order(",
            "requests.",
            "httpx.",
            "aiohttp.",
        )
        joined = "\n".join(path.read_text(encoding="utf-8") for path in paths)
        assert not any(token in joined for token in forbidden)

    def test_agent_endpoint_descriptor_is_not_a_transport(self):
        fields = set(AgentEndpointDescriptor.model_fields)
        assert fields == {"agent_id", "transport", "location", "detail"}
        assert not fields.intersection({"url", "credential", "secret", "client"})
