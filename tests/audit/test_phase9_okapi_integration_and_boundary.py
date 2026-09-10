"""Phase 9 audit: orchestrator linkage, layering and PAPER-only boundaries."""

from __future__ import annotations

import ast
import inspect
import pathlib
import types

from core.models.hedging import HedgeRequestStatus
from tests.audit.phase9_fixtures import make_intent
from tests.conftest import START_MS


ROOT = pathlib.Path(__file__).resolve().parents[2]


def _imports(path: pathlib.Path) -> set[str]:
    tree = ast.parse(path.read_text())
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    return found


def test_okapi_runtime_contains_no_network_or_exchange_client_imports():
    forbidden = {
        "aiohttp",
        "httpx",
        "requests",
        "websocket",
        "websockets",
        "ccxt",
        "binance",
        "coinbase",
        "kraken",
    }
    for path in (ROOT / "agents" / "okapi").glob("*.py"):
        roots = {name.split(".")[0] for name in _imports(path)}
        assert roots.isdisjoint(forbidden), (
            f"{path} imports network/exchange client: {roots & forbidden}"
        )


def test_core_hedging_models_do_not_import_outward_layers():
    imports = _imports(ROOT / "core" / "models" / "hedging.py")
    forbidden_prefixes = ("apps.", "agents.", "execution.", "venues.", "risk.")
    offending = sorted(name for name in imports if name.startswith(forbidden_prefixes))
    assert offending == []


def test_orchestrator_hedge_decision_does_not_read_hedge_registry():
    from apps.orchestrator.orchestrator import Orchestrator

    source = inspect.getsource(Orchestrator._hedge)
    assert "hedge_registry" not in source
    assert "hedge_for_id" not in source
    assert "active_hedges" not in source
    assert "outstanding_hedges" not in source


def test_orchestrator_linkage_records_only_downstream_identifiers(platform):
    hedge = make_intent(hedge_id="hdg-linked")
    record = platform.okapi.hedge_registry.register_request(hedge, START_MS)
    trade_intent = types.SimpleNamespace(intent_id="trade-intent-1")
    plan = types.SimpleNamespace(plan_id="plan-1")
    report = types.SimpleNamespace(
        orders=[
            types.SimpleNamespace(client_order_id="order-1"),
            types.SimpleNamespace(client_order_id="order-2"),
        ]
    )

    platform.orchestrator._link_hedge(
        hedge, trade_intent, plan, report, START_MS + 1
    )

    held = platform.okapi.hedge_registry.for_intent(hedge.hedge_id)
    assert held is not None
    assert held.hedge_id == record.hedge_id
    assert held.trade_intent_id == "trade-intent-1"
    assert held.execution_plan_ids == ["plan-1"]
    assert held.order_ids == ["order-1", "order-2"]
    assert held.status is HedgeRequestStatus.SUBMITTING


def test_missing_hedge_registry_record_cannot_break_linkage(platform):
    hedge = make_intent(hedge_id="hdg-not-registered")
    trade_intent = types.SimpleNamespace(intent_id="trade-intent-1")
    plan = types.SimpleNamespace(plan_id="plan-1")
    report = types.SimpleNamespace(
        orders=[types.SimpleNamespace(client_order_id="order-1")]
    )

    platform.orchestrator._link_hedge(
        hedge, trade_intent, plan, report, START_MS + 1
    )

    assert platform.okapi.hedge_registry.for_intent(hedge.hedge_id) is None


def test_phase9_files_do_not_define_live_execution_mode_or_credentials():
    paths = [
        ROOT / "agents" / "okapi" / "agent.py",
        ROOT / "agents" / "okapi" / "policy.py",
        ROOT / "agents" / "okapi" / "registry.py",
        ROOT / "agents" / "okapi" / "targets.py",
        ROOT / "core" / "models" / "hedging.py",
    ]
    forbidden_tokens = (
        "place_order",
        "submit_live",
        "live_executor",
        "api_secret",
        "api_key",
        "private_key",
    )
    for path in paths:
        text = path.read_text().lower()
        for token in forbidden_tokens:
            assert token not in text, (
                f"{path} contains forbidden Phase 9 live token {token}"
            )
