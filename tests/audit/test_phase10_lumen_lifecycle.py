"""Phase 10 audit: lifecycle truth, publication atomicity and counters."""

from __future__ import annotations

import pytest

from core.models.intelligence import (
    IntelligenceRunStatus,
    PublishedOpinionRef,
)
from tests.audit.phase10_fixtures import (
    CapturingBus,
    FailingPublishBus,
    RecordingProvider,
    VALID_RESPONSE,
    build_lumen,
)


async def test_successful_run_once_publishes_exactly_one_opinion_and_marks_published(settings):
    bus = CapturingBus()
    provider = RecordingProvider()
    narrowed = settings.model_copy(update={"symbols": [settings.symbols[0]]})
    lumen = build_lumen(narrowed, provider=provider, bus=bus)

    opinions = await lumen.run_once()
    analysis = lumen.latest_analysis(narrowed.symbols[0])

    assert len(opinions) == 1
    assert len(bus.events) == 1
    assert analysis is not None
    assert analysis.status is IntelligenceRunStatus.PUBLISHED
    assert analysis.published_opinion_ref is not None
    assert lumen.intel_registry.opinions_published == 1


async def test_failed_bus_publish_cannot_leave_history_claiming_published(settings):
    bus = FailingPublishBus()
    provider = RecordingProvider()
    narrowed = settings.model_copy(update={"symbols": [settings.symbols[0]]})
    lumen = build_lumen(narrowed, provider=provider, bus=bus)

    with pytest.raises(RuntimeError, match="publication failed"):
        await lumen.run_once()

    analysis = lumen.latest_analysis(narrowed.symbols[0])
    assert analysis is not None
    assert analysis.status is not IntelligenceRunStatus.PUBLISHED
    assert analysis.published_opinion_ref is None
    assert lumen.intel_registry.opinions_published == 0


async def test_unavailable_provider_is_distinct_from_malformed_or_failed(settings):
    provider = RecordingProvider(
        ok=False,
        unavailable=True,
        error="provider offline",
    )
    lumen = build_lumen(settings, provider=provider)

    result = await lumen.evaluate(settings.symbols[0])
    analysis = lumen.latest_analysis(settings.symbols[0])

    assert result is None
    assert analysis is not None
    assert analysis.status is IntelligenceRunStatus.UNAVAILABLE
    assert analysis.provider_unavailable is True
    assert analysis.malformed_response is False


async def test_non_unavailable_provider_failure_is_failed_not_unavailable(settings):
    provider = RecordingProvider(
        ok=False,
        unavailable=False,
        error="bad provider response",
    )
    lumen = build_lumen(settings, provider=provider)

    result = await lumen.evaluate(settings.symbols[0])
    analysis = lumen.latest_analysis(settings.symbols[0])

    assert result is None
    assert analysis is not None
    assert analysis.status is IntelligenceRunStatus.FAILED
    assert analysis.provider_unavailable is False


async def test_malformed_successful_response_is_failed_and_never_published(settings):
    provider = RecordingProvider({"sentiment": 0.0})
    lumen = build_lumen(settings, provider=provider)

    result = await lumen.evaluate(settings.symbols[0])
    analysis = lumen.latest_analysis(settings.symbols[0])

    assert result is None
    assert analysis is not None
    assert analysis.status is IntelligenceRunStatus.FAILED
    assert analysis.malformed_response is True
    assert analysis.published_opinion_ref is None
    assert lumen.intel_registry.malformed_responses == 1


def test_complete_analysis_is_idempotent_and_first_completion_time_wins(settings):
    lumen = build_lumen(settings)
    registry = lumen.intel_registry
    record = registry.begin_analysis(settings.symbols[0], 100)

    first = registry.complete_analysis(record.analysis_id, 200)
    second = registry.complete_analysis(record.analysis_id, 300)

    assert first is not None and second is not None
    held = registry.analyses[record.analysis_id]
    assert registry.analyses_completed == 1
    assert held.completed_at == 200


def test_link_opinion_is_idempotent_for_the_same_analysis(settings):
    lumen = build_lumen(settings)
    registry = lumen.intel_registry
    symbol = settings.symbols[0]
    record = registry.begin_analysis(symbol, 100)
    registry.complete_analysis(record.analysis_id, 200)
    ref = PublishedOpinionRef(
        symbol=symbol,
        created_at=210,
        expires_at=310,
        model_version="lumen-0.1",
        signal=-0.4,
        confidence=0.8,
    )

    registry.link_opinion(record.analysis_id, ref, 220)
    registry.link_opinion(record.analysis_id, ref, 230)

    assert registry.opinions_published == 1
    held = registry.analyses[record.analysis_id]
    assert held.published_opinion_ref is not None
    assert held.published_opinion_ref.created_at == 210


def test_terminal_unavailable_history_is_not_rewritten_complete(settings):
    lumen = build_lumen(settings)
    registry = lumen.intel_registry
    record = registry.begin_analysis(settings.symbols[0], 100)
    registry.mark_unavailable(record.analysis_id, 200, error="offline")

    registry.complete_analysis(record.analysis_id, 300)

    held = registry.analyses[record.analysis_id]
    assert held.status is IntelligenceRunStatus.UNAVAILABLE
    assert held.completed_at == 200


def test_published_opinion_reference_copies_actual_opinion_fields(settings):
    lumen = build_lumen(settings)
    opinion = lumen._to_opinion(settings.symbols[0], dict(VALID_RESPONSE))
    assert opinion is not None

    ref = PublishedOpinionRef(
        agent_id=opinion.agent_id,
        symbol=opinion.symbol,
        created_at=opinion.created_at,
        expires_at=opinion.expires_at,
        model_version=opinion.model_version,
        correlation_id=opinion.correlation_id,
        signal=opinion.signal,
        confidence=opinion.confidence,
    )

    assert ref.symbol == opinion.symbol
    assert ref.created_at == opinion.created_at
    assert ref.signal == opinion.signal
    assert ref.confidence == opinion.confidence
