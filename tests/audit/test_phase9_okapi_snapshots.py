"""Phase 9 audit: target authority and point-in-time snapshot integrity."""

from __future__ import annotations

from core.models.common import DataQuality, Side
from tests.audit.phase9_fixtures import build_okapi, make_market, make_portfolio
from tests.conftest import START_MS


def test_target_registry_is_mirror_not_economic_authority(bus, clock, settings, health):
    okapi = build_okapi(bus=bus, clock=clock, settings=settings, health=health)
    okapi.set_desired_delta("BTC-USD", 125.0)
    okapi.mirror_targets(START_MS)

    okapi.target_registry.set_target("BTC-USD", 999_999.0, START_MS + 1)
    [report] = okapi.delta_reports(make_portfolio(), START_MS + 1)

    assert okapi.target("BTC-USD") == 125.0
    assert report.desired_delta == 125.0
    assert report.unhedged_delta == -125.0


def test_delta_snapshot_is_detached_from_later_target_mutation(
    bus, clock, settings, health
):
    okapi = build_okapi(bus=bus, clock=clock, settings=settings, health=health)
    okapi.set_desired_delta("BTC-USD", 0.0)

    snapshot = okapi.delta_snapshot(make_portfolio(), START_MS)
    assert snapshot.targets[0].target_notional == 0.0
    okapi.target_registry.set_target("BTC-USD", 321.0, START_MS + 1)

    assert snapshot.created_at == START_MS
    assert snapshot.targets[0].target_notional == 0.0


def test_okapi_snapshot_is_detached_from_later_target_mutation(
    bus, clock, settings, health
):
    okapi = build_okapi(bus=bus, clock=clock, settings=settings, health=health)
    okapi.set_desired_delta("BTC-USD", 0.0)
    market = make_market(
        ("VENUE_A", "BTC-USD", 100.0, START_MS, DataQuality.FRESH),
        ("VENUE_B", "BTC-USD", 101.0, START_MS, DataQuality.FRESH),
    )

    snapshot = okapi.okapi_snapshot(make_portfolio(), market, START_MS)
    okapi.target_registry.set_target("BTC-USD", 456.0, START_MS + 1)

    assert snapshot.targets[0].target_notional == 0.0
    assert snapshot.readiness.created_at == START_MS


def test_mutating_snapshot_target_does_not_corrupt_target_registry(
    bus, clock, settings, health
):
    okapi = build_okapi(bus=bus, clock=clock, settings=settings, health=health)
    okapi.set_desired_delta("BTC-USD", 0.0)

    snapshot = okapi.delta_snapshot(make_portfolio(), START_MS)
    snapshot.targets[0].target_notional = 777.0

    held = okapi.target_registry.get_target("BTC-USD")
    assert held is not None
    assert held.target_notional == 0.0


def test_route_snapshot_copies_existing_selector(bus, clock, settings, health):
    okapi = build_okapi(bus=bus, clock=clock, settings=settings, health=health)
    market = make_market(
        ("VENUE_A", "BTC-USD", 100.0, START_MS, DataQuality.FRESH),
        ("VENUE_B", "BTC-USD", 102.0, START_MS, DataQuality.FRESH),
    )

    buy = okapi.route_snapshot("BTC-USD", Side.BUY, market, START_MS)
    sell = okapi.route_snapshot("BTC-USD", Side.SELL, market, START_MS)

    assert buy.selected_venue == okapi._hedge_venue("BTC-USD", Side.BUY, market)
    assert sell.selected_venue == okapi._hedge_venue("BTC-USD", Side.SELL, market)
    assert buy.selected_venue == "VENUE_A"
    assert sell.selected_venue == "VENUE_B"


def test_snapshot_total_unhedged_uses_same_reports_once(bus, clock, settings, health):
    okapi = build_okapi(bus=bus, clock=clock, settings=settings, health=health)
    okapi.set_desired_delta("BTC-USD", 0.0)
    okapi.set_desired_delta("ETH-USD", 0.0)
    portfolio = make_portfolio(
        ("VENUE_A", "BTC-USD", 5.0, 100.0),
        ("VENUE_B", "ETH-USD", -2.0, 200.0),
    )

    snapshot = okapi.delta_snapshot(portfolio, START_MS)

    assert snapshot.total_unhedged == sum(abs(r.unhedged_delta) for r in snapshot.reports)
    assert snapshot.total_unhedged == 900.0
