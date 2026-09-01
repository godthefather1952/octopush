"""The dashboard API surface."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from apps.api.app import create_app
from tests.conftest import run_platform


@pytest.fixture
async def client(platform):
    await run_platform(platform, 300)
    with TestClient(create_app(platform)) as test_client:
        yield test_client


class TestApi:
    def test_health_reports_every_component(self, client):
        body = client.get("/health").json()
        assert body["mode"] == "PAPER"
        assert body["warmed_up"] is True
        assert {"TIDAL", "NORO", "ZEPHR", "RUNE", "VESKA", "MARIN"} <= set(body["components"])

    def test_state_exposes_the_whole_floor(self, client):
        body = client.get("/api/state").json()
        assert body["mode"] == "PAPER"
        assert body["venues"] and body["consolidated"]
        assert body["portfolio"]["initial_balance"] == 100_000.0
        assert "kill_switch" in body and "risk" in body
        venue = body["venues"][0]
        assert {"bid", "ask", "spread_bps", "quality", "latency_ms"} <= set(venue)

    def test_consolidated_view_carries_noro_fair_value(self, client):
        body = client.get("/api/state").json()
        assert all(row["fair_value"] is not None for row in body["consolidated"])

    def test_agents_endpoint_reports_weights_and_scores(self, client):
        body = client.get("/api/agents").json()
        names = {row["agent"] for row in body["agents"]}
        assert {"TIDAL", "NORO", "ZEPHR", "LUMEN"} == names
        noro = next(row for row in body["agents"] if row["agent"] == "NORO")
        assert noro["required"] is True and noro["weight"] > 0
        lumen = next(row for row in body["agents"] if row["agent"] == "LUMEN")
        assert lumen["required"] is False
        assert body["intelligence"]["provider"] == "null"

    def test_attributions_endpoint_returns_closed_trades(self, client):
        body = client.get("/api/attributions?limit=5").json()
        for trade in body["trades"]:
            assert trade["symbol"]
            assert "contributions" in trade

    def test_prometheus_metrics_render(self, client):
        text = client.get("/metrics").text
        assert "# TYPE tf_events_processed_total counter" in text
        assert "tf_net_pnl" in text

    def test_metrics_json_is_flat(self, client):
        body = client.get("/api/metrics").json()
        assert body
        assert all(isinstance(v, (int, float)) for v in body.values())

    def test_dashboard_is_served_and_says_paper_mode(self, client):
        html = client.get("/").text
        assert "PAPER MODE" in html
        assert "Multi-Agent Trading Floor" in html

    def test_kill_switch_endpoint_only_stops_things(self, client, platform):
        assert platform.state.kill_switch.trading_allowed
        body = client.post("/api/kill-switch?trigger=MANUAL&detail=test").json()
        assert body["engaged"] is True
        assert "MANUAL" in body["triggered_by"]
        assert not platform.kill_switch.state.trading_allowed

    def test_there_is_no_endpoint_that_places_a_trade(self, client):
        paths = client.get("/openapi.json").json()["paths"]
        mutating = {
            path
            for path, methods in paths.items()
            if set(methods) - {"get", "head", "options"}
        }
        # The kill switch is the only way in, and it only halts.
        assert mutating == {"/api/kill-switch"}
