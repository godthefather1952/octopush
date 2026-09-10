"""Phase 7 audit — MARIN publication and orchestrator integration."""

from __future__ import annotations

import inspect

import pytest

from apps.orchestrator.orchestrator import Orchestrator
from tests.audit.marin_fixtures import T0, build_marin, matched_trade
from tests.audit.veska_fixtures import ExplodingBus


class TestPublicationFailure:
    async def test_critical_result_remains_available_when_event_publish_fails(
        self, clock, health
    ):
        bus = ExplodingBus(fail_on_publish=1)
        marin = build_marin(bus=bus, clock=clock, health=health)
        matched_trade(marin)
        marin.account.cash += 500.0

        with pytest.raises(RuntimeError, match="audit-injected"):
            await marin.run(T0)

        assert marin.last_result is not None
        assert marin.last_result.ok is False
        assert marin.registry.open_critical()

    async def test_clean_run_does_not_commit_registry_before_event_acceptance(
        self, clock, health
    ):
        """H7-49: workflow truth should not outrun its public event."""
        bus = ExplodingBus(fail_on_publish=1)
        marin = build_marin(bus=bus, clock=clock, health=health)

        with pytest.raises(RuntimeError, match="audit-injected"):
            await marin.run(T0)

        assert marin.registry.all_runs() == [], (
            "the workflow registry advanced even though the reconciliation "
            "completion event was rejected"
        )


class TestOrchestratorContract:
    def test_first_or_periodic_protect_tick_runs_marin(self):
        source = inspect.getsource(Orchestrator._protect)
        assert "self.marin.last_result is None" in source
        assert "self.ticks % self.reconcile_every == 0" in source
        assert "await self.marin.run(self.tick_time)" in source

    def test_between_runs_safety_uses_last_reconciliation_answer(self):
        source = inspect.getsource(Orchestrator._protect)
        assert "reconciliation_ok = self.marin.last_result.ok" in source

    def test_reconciliation_answer_reaches_kill_switch_input(self):
        source = inspect.getsource(Orchestrator._protect)
        assert "reconciliation_ok=reconciliation_ok" in source


class TestPaperOnlyBoundary:
    def test_marin_source_module_has_no_network_or_credentials(self):
        import agents.marin.source as source_module

        source = inspect.getsource(source_module)
        forbidden = (
            "requests.",
            "httpx.",
            "aiohttp.",
            "websockets.",
            "api_key",
            "secret_key",
            "private_key",
        )
        assert not any(token in source for token in forbidden)

    def test_venue_source_remains_abstract(self):
        from agents.marin.source import VenueReconciliationSource

        assert inspect.isabstract(VenueReconciliationSource)
