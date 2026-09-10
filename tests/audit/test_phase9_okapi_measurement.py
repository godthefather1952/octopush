"""Phase 9 audit: exposure measurement, routing, freshness and readiness."""

from __future__ import annotations

import math

import pytest

from core.models.common import DataQuality, Side
from core.models.hedging import HedgeRequestStatus
from tests.audit.phase9_fixtures import build_okapi, make_intent, make_market, make_portfolio
from tests.conftest import START_MS


def test_multi_venue_delta_is_signed_sum_and_tolerance_equality_is_safe(
    bus, clock, settings, health
):
    cfg = settings.model_copy(update={"hedge_tolerance_notional": 500.0})
    okapi = build_okapi(bus=bus, clock=clock, settings=cfg, health=health)
    okapi.set_desired_delta("BTC-USD", 100.0)
    portfolio = make_portfolio(
        ("VENUE_A", "BTC-USD", 10.0, 100.0),
        ("VENUE_B", "BTC-USD", -4.0, 100.0),
    )

    [report] = okapi.delta_reports(portfolio, START_MS)

    assert report.actual_delta == 600.0
    assert report.desired_delta == 100.0
    assert report.unhedged_delta == 500.0
    assert report.within_tolerance is True
    assert report.tolerance == 500.0


def test_positive_residual_sells_and_negative_residual_buys(
    bus, clock, settings, health
):
    cfg = settings.model_copy(update={"hedge_tolerance_notional": 100.0})
    okapi = build_okapi(bus=bus, clock=clock, settings=cfg, health=health)
    okapi.set_desired_delta("BTC-USD", 0.0)
    market = make_market(
        ("VENUE_A", "BTC-USD", 100.0, START_MS, DataQuality.FRESH),
        ("VENUE_B", "BTC-USD", 101.0, START_MS, DataQuality.FRESH),
    )

    [sell] = okapi.build_hedges(
        make_portfolio(("VENUE_A", "BTC-USD", 10.0, 100.0)), market, START_MS
    )
    [buy] = okapi.build_hedges(
        make_portfolio(("VENUE_A", "BTC-USD", -10.0, 100.0)), market, START_MS
    )

    assert sell.side is Side.SELL
    assert sell.notional == 1_000.0
    assert sell.venue == "VENUE_B"
    assert buy.side is Side.BUY
    assert buy.notional == 1_000.0
    assert buy.venue == "VENUE_A"


def test_unusable_best_price_is_not_selected(bus, clock, settings, health):
    cfg = settings.model_copy(update={"hedge_tolerance_notional": 100.0})
    okapi = build_okapi(bus=bus, clock=clock, settings=cfg, health=health)
    okapi.set_desired_delta("BTC-USD", 0.0)
    market = make_market(
        ("VENUE_A", "BTC-USD", 90.0, START_MS, DataQuality.STALE),
        ("VENUE_B", "BTC-USD", 100.0, START_MS, DataQuality.FRESH),
    )

    [hedge] = okapi.build_hedges(
        make_portfolio(("VENUE_A", "BTC-USD", -10.0, 100.0)), market, START_MS
    )

    assert hedge.side is Side.BUY
    assert hedge.venue == "VENUE_B"


def test_hedge_intent_uses_selected_venue_exchange_timestamp(
    bus, clock, settings, health
):
    """P9 provenance: a fresh unrelated book must not launder a stale hedge leg."""
    cfg = settings.model_copy(update={"hedge_tolerance_notional": 100.0})
    okapi = build_okapi(bus=bus, clock=clock, settings=cfg, health=health)
    okapi.set_desired_delta("BTC-USD", 0.0)
    selected_ts = START_MS - 1_500
    unrelated_fresh_ts = START_MS - 20
    market = make_market(
        ("VENUE_A", "BTC-USD", 99.0, selected_ts, DataQuality.FRESH),
        ("VENUE_B", "BTC-USD", 101.0, unrelated_fresh_ts, DataQuality.FRESH),
        now_ms=START_MS,
        source_data_timestamp=unrelated_fresh_ts,
    )

    [hedge] = okapi.build_hedges(
        make_portfolio(("VENUE_A", "BTC-USD", -10.0, 100.0)), market, START_MS
    )

    assert hedge.venue == "VENUE_A"
    assert hedge.source_data_timestamp == selected_ts


def test_non_finite_desired_delta_is_rejected_at_authority_boundary(
    bus, clock, settings, health
):
    okapi = build_okapi(bus=bus, clock=clock, settings=settings, health=health)
    for bad in (math.nan, math.inf, -math.inf):
        with pytest.raises((TypeError, ValueError)):
            okapi.set_desired_delta("BTC-USD", bad)


def test_readiness_fails_closed_without_targets(bus, clock, settings, health):
    okapi = build_okapi(bus=bus, clock=clock, settings=settings, health=health)
    market = make_market(
        ("VENUE_A", "BTC-USD", 100.0, START_MS, DataQuality.FRESH),
        ("VENUE_B", "BTC-USD", 101.0, START_MS, DataQuality.FRESH),
    )

    readiness = okapi.readiness(make_portfolio(), market, START_MS)

    assert readiness.ready is False
    assert "NO_TARGETS_ESTABLISHED" in readiness.reason_codes


def test_unknown_hedge_blocks_readiness(bus, clock, settings, health):
    okapi = build_okapi(bus=bus, clock=clock, settings=settings, health=health)
    okapi.set_desired_delta("BTC-USD", 0.0)
    market = make_market(
        ("VENUE_A", "BTC-USD", 100.0, START_MS, DataQuality.FRESH),
        ("VENUE_B", "BTC-USD", 101.0, START_MS, DataQuality.FRESH),
    )
    record = okapi.hedge_registry.register_request(make_intent(), START_MS)
    okapi.hedge_registry.set_status(
        record.hedge_id, HedgeRequestStatus.UNKNOWN, START_MS + 1
    )

    readiness = okapi.readiness(make_portfolio(), market, START_MS + 2)

    assert readiness.ready is False
    assert readiness.unknown_hedges == 1
    assert "UNKNOWN_HEDGES:1" in readiness.reason_codes
