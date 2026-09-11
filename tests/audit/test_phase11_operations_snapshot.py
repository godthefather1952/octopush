"""Phase 11 operational snapshot and API invariants."""

from fastapi.testclient import TestClient

from apps.api.app import create_app
from tests.conftest import run_platform


async def test_operational_snapshot_is_serializable_complete_and_observational(platform) -> None:
    await run_platform(platform, 20)
    try:
        now = platform.clock.now_ms()

        targets_before = [
            target.model_dump(mode="json")
            for target in platform.okapi.target_registry.all_targets()
        ]
        hedge_metrics_before = platform.okapi.hedge_registry.metrics().model_dump(
            mode="json"
        )

        first = platform.operational_snapshot(now)
        second = platform.operational_snapshot(now)

        targets_after = [
            target.model_dump(mode="json")
            for target in platform.okapi.target_registry.all_targets()
        ]
        hedge_metrics_after = platform.okapi.hedge_registry.metrics().model_dump(
            mode="json"
        )

        assert first.model_dump(mode="json") == second.model_dump(mode="json")
        assert targets_after == targets_before
        assert hedge_metrics_after == hedge_metrics_before

        metrics = first.metrics
        assert metrics.ticks == platform.orchestrator.ticks
        assert metrics.events_recorded == platform.recorder.events_recorded
        assert metrics.paper_orders == platform.oms.orders_created
        assert metrics.paper_fills == platform.oms.fills_applied
        assert (
            metrics.opportunities
            == platform.orchestrator.coordination.opportunities_created_total
        )
        assert (
            metrics.risk_rejections
            == platform.orchestrator.coordination.risk_rejections_total
        )
        assert metrics.hedges == platform.okapi.hedges_requested
        assert metrics.reconciliations == platform.marin.metrics().runs_completed
        assert (
            metrics.intelligence_analyses
            == platform.lumen.intel_registry.analyses_completed
        )
        assert first.recording["requested"] is True
        assert first.recording["active"] is True
    finally:
        await platform.stop()


async def test_session_summary_matches_authoritative_session_counters(platform) -> None:
    await run_platform(platform, 40)
    try:
        summary = platform.session_summary(platform.clock.now_ms())
        portfolio = platform.state.portfolio
        assert portfolio is not None

        assert (
            summary.opportunities
            == platform.orchestrator.coordination.opportunities_created_total
        )
        assert summary.orders == platform.oms.orders_created
        assert summary.fills == platform.oms.fills_applied
        assert (
            summary.rejections
            == platform.orchestrator.coordination.risk_rejections_total
        )
        assert summary.hedges == platform.okapi.hedges_requested
        assert summary.reconciliations == platform.marin.metrics().runs_completed
        assert summary.gross_pnl == portfolio.gross_pnl
        assert summary.net_pnl == portfolio.net_pnl
        assert summary.fees == portfolio.fees_paid
    finally:
        await platform.stop()


async def test_api_operations_returns_the_canonical_snapshot(platform) -> None:
    await run_platform(platform, 20)
    try:
        with TestClient(create_app(platform)) as client:
            response = client.get("/api/operations")

        assert response.status_code == 200
        body = response.json()
        assert body["session"]["session_id"] == platform.session_id
        assert body["recording"]["requested"] is True
        assert body["metrics"]["ticks"] == platform.orchestrator.ticks
        assert body["metrics"]["paper_orders"] == platform.oms.orders_created
        assert body["readiness"]["paper_mode_confirmed"] is True
    finally:
        await platform.stop()
