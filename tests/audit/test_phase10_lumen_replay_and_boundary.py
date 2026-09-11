"""Phase 10 audit: replay, optional-agent, slow-loop and safety boundaries."""

from __future__ import annotations

import inspect

from agents.lumen.provider import ClaudeProvider, NullProvider
from agents.lumen.source import IntelligenceSource, LocalHeadlineSource
from agents.rune.agent import Rune
from apps.orchestrator.wiring import _build_agent_directory, build_platform
from core.bus import InMemoryEventBus
from core.clock import ManualClock
from core.config import simulated_venues
from core.models.common import AgentId
from core.models.intelligence import IntelligenceControlKind
from core.models.orchestration import AgentCadence
from replay.engine import (
    EXTERNAL_INTELLIGENCE_SOURCES,
    ReplaySession,
    config_digest,
)
from simulation.market import default_market
from storage import InMemoryEventStore
from tests.audit.phase10_fixtures import ExplodingProvider, build_lumen
from tests.conftest import START_MS
from tests.replay.test_replay_external_intelligence import record_with_intelligence


def test_lumen_remains_optional_in_consensus(settings):
    assert AgentId.LUMEN not in settings.consensus.required_agents
    assert settings.consensus.weights.get(AgentId.LUMEN, 0) > 0


def test_agent_directory_describes_lumen_as_slow_and_optional(settings):
    descriptor = _build_agent_directory(settings).get(AgentId.LUMEN)

    assert descriptor is not None
    assert descriptor.cadence is AgentCadence.SLOW
    assert descriptor.required_by_default is False


async def test_platform_start_does_not_require_or_call_lumen_provider(settings):
    provider = ExplodingProvider()
    local_settings = settings.model_copy(update={"venues": simulated_venues()})
    platform = build_platform(
        local_settings,
        clock=ManualClock(START_MS),
        bus=InMemoryEventBus(raise_on_handler_error=True),
        store=InMemoryEventStore(),
        market=default_market(start_ms=START_MS),
        intelligence_provider=provider,
        raise_on_handler_error=True,
    )

    await platform.start(record=False, feeds=False)
    try:
        assert provider.calls == 0
        readiness = platform.operational_readiness(START_MS)
        assert readiness.required_agents_ready in (True, False)
        assert all("LUMEN" not in reason for reason in readiness.reason_codes)
    finally:
        await platform.stop()


def test_lumen_readiness_is_explicitly_optional_even_when_unready(settings):
    lumen = build_lumen(settings, provider=NullProvider())

    readiness = lumen.readiness(START_MS)

    assert readiness.ready is False
    assert readiness.optional is True
    assert "NO_PROVIDER_CONFIGURED" in readiness.reason_codes


def test_rune_fast_decision_path_never_calls_the_shared_provider():
    source = inspect.getsource(Rune.evaluate)

    assert ".assess(" not in source
    assert "provider.analyze" not in source


def test_replay_policy_declares_only_lumen_as_external_intelligence():
    assert frozenset({"LUMEN"}) == EXTERNAL_INTELLIGENCE_SOURCES


async def test_replay_with_an_exploding_provider_never_reinvokes_it(settings):
    original, store, session_id = await record_with_intelligence(settings)
    provider = ExplodingProvider()
    replay_settings = settings.model_copy(update={"venues": simulated_venues()})
    replayed = build_platform(
        replay_settings,
        clock=ManualClock(START_MS),
        bus=InMemoryEventBus(raise_on_handler_error=True),
        store=InMemoryEventStore(),
        market=default_market(start_ms=START_MS),
        intelligence_provider=provider,
        raise_on_handler_error=True,
    )
    await replayed.start(record=False, feeds=False)
    session = ReplaySession(
        store=store,
        bus=replayed.bus,
        clock=replayed.clock,
        session_id=session_id,
        current_config_hash=config_digest(replay_settings.model_dump()),
    )
    try:
        async with session:
            while (event := await session.step()) is not None:
                if event.type.value == "ORCHESTRATOR_TICK":
                    await replayed.orchestrator.tick()
        assert provider.calls == 0
        assert AgentId.LUMEN in replayed.state.opinions_for(settings.symbols[0])
    finally:
        await replayed.stop()
        await original.stop()


def test_no_external_information_source_is_implemented():
    subclasses = set(IntelligenceSource.__subclasses__())

    assert subclasses == {LocalHeadlineSource}
    assert all(source.is_external is False for source in subclasses)


def test_intelligence_control_vocabulary_has_no_agent_dispatcher(settings):
    lumen = build_lumen(settings)
    assert {kind.value for kind in IntelligenceControlKind} == {
        "PAUSE",
        "RESUME",
        "RUN_NOW",
        "CLEAR_EVIDENCE",
    }
    for name in ("pause", "resume", "run_now", "clear_evidence", "dispatch_control"):
        assert not hasattr(lumen, name)


def test_lumen_snapshot_never_serializes_claude_credentials(settings):
    provider = ClaudeProvider(api_key="ULTRA-SECRET-PHASE10")
    lumen = build_lumen(settings, provider=provider)

    payload = lumen.lumen_snapshot(START_MS).model_dump_json()

    assert "ULTRA-SECRET-PHASE10" not in payload
    assert "_api_key" not in payload
    assert "secret" not in payload.lower()


def test_provider_directory_is_metadata_not_a_failover_selector(settings):
    lumen = build_lumen(settings)
    directory = lumen.provider_directory

    assert not hasattr(directory, "select")
    assert not hasattr(directory, "failover")
    assert not hasattr(directory, "retry")
    assert lumen.provider is not None
