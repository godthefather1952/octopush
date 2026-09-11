"""Phase 10 audit: provider contract, schema and context fidelity."""

from __future__ import annotations

from agents.lumen.agent import RESPONSE_SCHEMA, SYSTEM_PROMPT, NewsItem
from agents.lumen.provider import ClaudeProvider, NullProvider, ScriptedProvider
from agents.lumen.providers import describe_provider
from core.models.intelligence import IntelligenceRunStatus
from tests.audit.phase10_fixtures import (
    VALID_RESPONSE,
    AdvancingClock,
    RecordingProvider,
    build_lumen,
    market_state,
)


async def test_evaluate_calls_provider_exactly_once(settings):
    provider = RecordingProvider()
    lumen = build_lumen(settings, provider=provider)

    opinion = await lumen.evaluate(settings.symbols[0])

    assert opinion is not None
    assert provider.calls == 1
    assert len(provider.requests) == 1


async def test_provider_receives_the_authoritative_prompt_and_schema(settings):
    provider = RecordingProvider()
    lumen = build_lumen(settings, provider=provider)

    await lumen.evaluate(settings.symbols[0])
    request = provider.requests[0]

    assert request.system == SYSTEM_PROMPT
    assert request.response_schema == RESPONSE_SCHEMA
    assert request.task == "information_environment"


async def test_recorded_evidence_is_built_from_the_exact_payload_sent(settings):
    provider = RecordingProvider()
    lumen = build_lumen(settings, provider=provider)
    symbol = settings.symbols[0]
    now = lumen.clock.now_ms()
    lumen.add_headline(NewsItem("one", "wire", now - 100, "body one"))
    lumen.add_headline(NewsItem("two", "wire", now - 50, "body two"))

    await lumen.evaluate(symbol)

    request = provider.requests[0]
    analysis = lumen.latest_analysis(symbol)
    assert analysis is not None
    bundle = lumen.intel_registry.get_bundle(analysis.evidence_bundle_id)
    assert bundle is not None
    assert [item.title for item in bundle.evidence] == [
        item["headline"] for item in request.payload["recent_headlines"]
    ]
    assert [item.body_excerpt for item in bundle.evidence] == [
        item["body"] for item in request.payload["recent_headlines"]
    ]


def test_context_keeps_only_the_existing_narrow_information_surface(settings):
    provider = RecordingProvider()
    lumen = build_lumen(settings, provider=provider)
    symbol = settings.symbols[0]
    lumen.on_market_state(market_state(symbol=symbol))

    payload = lumen._context(symbol)

    assert set(payload) == {"symbol", "as_of_ms", "recent_headlines", "market_summary"}
    assert set(payload["market_summary"]) == {
        "reference_price",
        "venues_quoting",
        "max_cross_venue_deviation_bps",
        "short_vol_bps",
    }
    serialized = repr(payload).lower()
    for forbidden in (
        "balance",
        "position",
        "open_order",
        "risk_limit",
        "wallet",
        "api_key",
        "secret",
        "private_key",
    ):
        assert forbidden not in serialized


def test_context_preserves_last_ten_headline_order_and_one_hour_window(settings):
    lumen = build_lumen(settings)
    symbol = settings.symbols[0]
    now = lumen.clock.now_ms()
    lumen.add_headline(NewsItem("old", "wire", now - 3_600_001))
    for i in range(12):
        lumen.add_headline(NewsItem(f"h{i}", "wire", now - 100 + i))

    payload = lumen._context(symbol)

    assert [item["headline"] for item in payload["recent_headlines"]] == [
        f"h{i}" for i in range(2, 12)
    ]


def test_news_body_is_bounded_before_it_reaches_the_provider(settings):
    lumen = build_lumen(settings)
    now = lumen.clock.now_ms()
    lumen.add_headline(NewsItem("bounded", "wire", now, "x" * 5000))

    payload = lumen._context(settings.symbols[0])

    assert len(payload["recent_headlines"][0]["body"]) == 1000


def test_claude_descriptor_never_exposes_the_api_key():
    provider = ClaudeProvider(api_key="TOP-SECRET-KEY")
    descriptor = describe_provider(provider)

    dumped = descriptor.model_dump_json()

    assert "TOP-SECRET-KEY" not in dumped
    assert "_api_key" not in dumped
    assert descriptor.name == "claude"


async def test_null_provider_is_unavailable_without_fabricating_an_opinion(settings):
    lumen = build_lumen(settings, provider=NullProvider())
    symbol = settings.symbols[0]

    opinion = await lumen.evaluate(symbol)
    analysis = lumen.latest_analysis(symbol)

    assert opinion is None
    assert analysis is not None
    assert analysis.status is IntelligenceRunStatus.UNAVAILABLE
    assert analysis.published_opinion_ref is None


async def test_scripted_provider_is_deterministic_for_the_same_queued_reading(settings):
    provider = ScriptedProvider([VALID_RESPONSE])
    lumen = build_lumen(settings, provider=provider)
    symbol = settings.symbols[0]

    first = await lumen.evaluate(symbol)
    second = await lumen.evaluate(symbol)

    assert first is not None and second is not None
    assert first.signal == second.signal
    assert first.confidence == second.confidence


async def test_schema_rejects_string_boolean_instead_of_reinterpreting_it(settings):
    data = dict(VALID_RESPONSE)
    data["information_shock"] = "false"
    provider = RecordingProvider(data)
    lumen = build_lumen(settings, provider=provider)

    opinion = await lumen.evaluate(settings.symbols[0])

    assert opinion is None, "response schema requires a boolean, not truthy text"


async def test_schema_rejects_out_of_range_readings(settings):
    data = dict(VALID_RESPONSE)
    data["attention"] = 2.0
    data["direction"] = -4.0
    provider = RecordingProvider(data)
    lumen = build_lumen(settings, provider=provider)

    opinion = await lumen.evaluate(settings.symbols[0])

    assert opinion is None, "provider readings outside RESPONSE_SCHEMA must not publish"


async def test_schema_rejects_a_missing_required_reason_codes_field(settings):
    data = dict(VALID_RESPONSE)
    data.pop("reason_codes")
    provider = RecordingProvider(data)
    lumen = build_lumen(settings, provider=provider)

    opinion = await lumen.evaluate(settings.symbols[0])

    assert opinion is None, "required schema fields cannot be silently defaulted"


async def test_analysis_timestamp_matches_the_context_instant_sent_to_provider(settings):
    clock = AdvancingClock(10_000)
    provider = RecordingProvider()
    lumen = build_lumen(settings, provider=provider, clock=clock)
    symbol = settings.symbols[0]

    await lumen.evaluate(symbol)

    request = provider.requests[0]
    analysis = lumen.latest_analysis(symbol)
    assert analysis is not None
    assert analysis.created_at == request.payload["as_of_ms"]


async def test_headline_window_is_measured_against_the_payload_as_of_instant(settings):
    clock = AdvancingClock(3_600_001)
    provider = RecordingProvider()
    lumen = build_lumen(settings, provider=provider, clock=clock)
    symbol = settings.symbols[0]
    # At the request's as_of_ms this is exactly one hour old and therefore in scope.
    lumen.add_headline(NewsItem("boundary", "wire", 1))

    await lumen.evaluate(symbol)
    request = provider.requests[0]

    assert request.payload["as_of_ms"] == 3_600_001
    assert [item["headline"] for item in request.payload["recent_headlines"]] == [
        "boundary"
    ]
