"""Phase 7 audit — MARIN publication and orchestrator integration."""

from __future__ import annotations

import inspect

import pytest

from apps.orchestrator.orchestrator import Orchestrator
from tests.audit.marin_fixtures import T0, build_marin, matched_trade
from tests.audit.veska_fixtures import ExplodingBus


class TestPublicationFailure:
    async def test_critical_result_remains_safety_visible_when_event_publish_fails(
        self, clock, health
    ):
        """The legacy safety answer survives even when workflow commit does not."""
        bus = ExplodingBus(fail_on_publish=1)
        marin = build_marin(bus=bus, clock=clock, health=health)
        matched_trade(marin)
        marin.account.cash += 500.0

        with pytest.raises(RuntimeError, match="audit-injected"):
            await marin.run(T0)

        assert marin.last_result is not None
        assert marin.last_result.ok is False
        assert marin.last_result.critical
        assert marin.registry.all_runs() == []

    async def test_clean_run_does_not_commit_registry_or_compaction_before_event(
        self, clock, health
    ):
        """H7-49: workflow/retention truth may advance only after event acceptance."""
        bus = ExplodingBus(fail_on_publish=1)
        marin = build_marin(bus=bus, clock=clock, health=health)
        order, fill = matched_trade(marin)
        marin.account.retained_fills = 0
        before_log = [item.fill_id for item in marin.account.fill_log]

        with pytest.raises(RuntimeError, match="audit-injected"):
            await marin.run(T0)

        assert marin.last_result is not None
        assert marin.last_result.ok is True
        assert marin.registry.all_runs() == []
        assert [item.fill_id for item in marin.account.fill_log] == before_log
        assert fill.fill_id in before_log
        assert marin.oms.get(order.client_order_id) is not None
        assert marin.fills_sealed == 0
        assert marin.orders_archived == 0


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
