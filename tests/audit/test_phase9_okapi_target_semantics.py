"""Phase 9 audit: desired-target and mirror semantics."""

from __future__ import annotations

from agents.okapi.targets import HedgeTargetRegistry
from core.models.hedging import HedgeTargetSource
from tests.audit.phase9_fixtures import build_okapi, make_portfolio
from tests.conftest import START_MS


def test_unrecorded_target_defaults_to_zero_only_in_economic_authority(
    bus, clock, settings, health
):
    okapi = build_okapi(bus=bus, clock=clock, settings=settings, health=health)

    assert okapi.target("BTC-USD") == 0.0
    assert okapi.target_registry.get_target("BTC-USD") is None


def test_mirror_uses_caller_logical_time(bus, clock, settings, health):
    okapi = build_okapi(bus=bus, clock=clock, settings=settings, health=health)
    okapi.set_desired_delta("BTC-USD", 0.0)
    clock.set(START_MS + 999_999)

    targets = okapi.mirror_targets(START_MS)

    assert len(targets) == 1
    assert targets[0].created_at == START_MS
    assert targets[0].updated_at == START_MS


def test_mirror_does_not_invent_retirement_for_missing_symbol():
    registry = HedgeTargetRegistry()
    registry.mirror({"BTC-USD": 0.0, "ETH-USD": 0.0}, START_MS)
    registry.mirror({"BTC-USD": 0.0}, START_MS + 1)

    eth = registry.get_target("ETH-USD")
    assert eth is not None
    assert eth.active is True


def test_target_source_defaults_to_strategy_only():
    registry = HedgeTargetRegistry()
    target = registry.set_target("BTC-USD", 0.0, START_MS)

    assert target.source is HedgeTargetSource.STRATEGY


def test_delta_report_uses_desired_dict_even_when_mirror_disagrees(
    bus, clock, settings, health
):
    okapi = build_okapi(bus=bus, clock=clock, settings=settings, health=health)
    okapi.set_desired_delta("BTC-USD", 250.0)
    okapi.target_registry.set_target("BTC-USD", -999.0, START_MS)

    [report] = okapi.delta_reports(make_portfolio(), START_MS)

    assert report.desired_delta == 250.0
    assert report.unhedged_delta == -250.0
