"""Phase 10 audit: LUMEN reading semantics remain single-source and bounded."""

from __future__ import annotations

import pytest

from agents.lumen.agent import NewsItem
from tests.audit.phase10_fixtures import VALID_RESPONSE, build_lumen, market_state


def test_turbulence_formula_and_signal_sign_are_unchanged(settings):
    lumen = build_lumen(settings)
    symbol = settings.symbols[0]

    opinion = lumen._to_opinion(symbol, dict(VALID_RESPONSE))

    assert opinion is not None
    expected = 0.6 * 0.5 + 0.4 * abs(-0.4)
    assert opinion.detail["turbulence"] == pytest.approx(expected)
    assert opinion.signal == pytest.approx(-expected)


def test_information_shock_adds_the_existing_penalty_and_caps_at_one(settings):
    lumen = build_lumen(settings)
    data = dict(VALID_RESPONSE)
    data.update(attention=0.8, direction=-0.9, information_shock=True)

    opinion = lumen._to_opinion(settings.symbols[0], data)

    assert opinion is not None
    assert opinion.detail["turbulence"] == pytest.approx(1.0)
    assert opinion.signal == pytest.approx(-1.0)


def test_confidence_is_clamped_to_zero_one(settings):
    lumen = build_lumen(settings)
    high = dict(VALID_RESPONSE, confidence=4.0)
    low = dict(VALID_RESPONSE, confidence=-2.0)

    high_opinion = lumen._to_opinion(settings.symbols[0], high)
    low_opinion = lumen._to_opinion(settings.symbols[0], low)

    assert high_opinion is not None and high_opinion.confidence == 1.0
    assert low_opinion is not None and low_opinion.confidence == 0.0


def test_ttl_has_the_existing_lower_bound(settings):
    lumen = build_lumen(settings)
    data = dict(VALID_RESPONSE, ttl_seconds=0)

    opinion = lumen._to_opinion(settings.symbols[0], data)

    assert opinion is not None
    assert opinion.expires_at - opinion.created_at == 5_000


def test_ttl_has_the_existing_upper_bound(settings):
    lumen = build_lumen(settings)
    data = dict(VALID_RESPONSE, ttl_seconds=10_000)

    opinion = lumen._to_opinion(settings.symbols[0], data)

    assert opinion is not None
    assert opinion.expires_at - opinion.created_at == 900_000


def test_negative_shock_reason_code_is_added_once(settings):
    lumen = build_lumen(settings)
    data = dict(
        VALID_RESPONSE,
        information_shock=True,
        direction=-0.8,
        reason_codes=["SOURCE"],
    )

    opinion = lumen._to_opinion(settings.symbols[0], data)

    assert opinion is not None
    assert opinion.reason_codes.count("NEGATIVE_INFORMATION_SHOCK") == 1


def test_quiet_environment_reason_code_is_preserved(settings):
    lumen = build_lumen(settings)
    data = dict(
        VALID_RESPONSE,
        information_shock=False,
        attention=0.1,
        reason_codes=[],
    )

    opinion = lumen._to_opinion(settings.symbols[0], data)

    assert opinion is not None
    assert "QUIET_INFORMATION_ENVIRONMENT" in opinion.reason_codes


def test_provider_reason_codes_are_bounded(settings):
    lumen = build_lumen(settings)
    data = dict(VALID_RESPONSE, reason_codes=[f"R{i}" for i in range(20)])

    opinion = lumen._to_opinion(settings.symbols[0], data)

    assert opinion is not None
    # Eight provider codes plus at most one locally-added quiet/shock code.
    assert opinion.reason_codes[:8] == [f"R{i}" for i in range(8)]
    assert len(opinion.reason_codes) <= 9


def test_market_summary_is_a_copy_of_the_existing_coarse_view(settings):
    lumen = build_lumen(settings)
    symbol = settings.symbols[0]
    market = market_state(symbol=symbol)
    lumen.on_market_state(market)

    payload = lumen._context(symbol)

    assert payload["market_summary"] == {
        "reference_price": 100.5,
        "venues_quoting": 2,
        "max_cross_venue_deviation_bps": 50.0,
        "short_vol_bps": 7.0,
    }


def test_evidence_freshness_is_not_reinterpreted(settings):
    lumen = build_lumen(settings)
    symbol = settings.symbols[0]
    now = lumen.clock.now_ms()
    lumen.add_headline(NewsItem("headline", "wire", now - 10))

    payload = lumen._context(symbol)
    evidence = lumen._evidence_from_payload(symbol, payload, now)

    assert len(evidence) == 1
    assert evidence[0].freshness.value == "UNKNOWN"


def test_response_record_does_not_rederive_the_published_signal(settings):
    lumen = build_lumen(settings)
    data = dict(VALID_RESPONSE, attention=0.9, direction=-0.9, information_shock=True)
    record = lumen.intel_registry.begin_analysis(
        settings.symbols[0],
        lumen.clock.now_ms(),
        provider="recording",
    )

    response = lumen.intel_registry.attach_response(
        record.analysis_id,
        lumen.clock.now_ms(),
        ok=True,
        provider="recording",
        data=data,
    )
    held = lumen.intel_registry.get_analysis(record.analysis_id)

    assert response is not None and held is not None
    assert not hasattr(held, "signal")
    assert held.attention == pytest.approx(0.9)
    assert held.direction == pytest.approx(-0.9)
