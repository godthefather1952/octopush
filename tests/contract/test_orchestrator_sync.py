"""The orchestrator must not depend on drain() meaning "agents answered".

The audit found that on a networked bus, `publish(opportunity); await drain()`
returns before any agent has replied, so consensus was computed with zero
opinions and every opportunity was rejected as CONSENSUS_INCOMPLETE.

These tests model that transport directly: a bus whose drain() is honest about
providing no completion guarantee for asynchronous responders. Before the
ResponseBarrier was introduced these tests fail — the orchestrator decides
immediately on an empty opinion set.
"""

from __future__ import annotations

import asyncio

from core.bus import InMemoryEventBus
from core.bus.barrier import ResponseBarrier
from core.clock import ManualClock
from core.events import Event, EventType
from core.models.common import AgentId
from core.models.opportunity import StrategyState
from tests.conftest import START_MS, run_platform


class DeferredDeliveryBus(InMemoryEventBus):
    """A bus whose drain() does not settle a nominated event type.

    This is what a networked transport looks like from the orchestrator's
    side: publishing returns, drain() returns, and the response arrives some
    time later. Nothing here is artificial — it is the Redis behaviour the
    audit measured, reduced to its essentials so it can be tested in-process.
    """

    def __init__(self, deferred: EventType, **kwargs) -> None:
        super().__init__(**kwargs)
        self._deferred_type = deferred
        self._held: list[Event] = []

    async def publish(self, event: Event) -> None:
        if event.type is self._deferred_type:
            # Held back exactly as a remote responder's reply would be.
            if event.sequence is None:
                event.sequence = 10_000 + len(self._held)
            self._held.append(event)
            return
        await super().publish(event)

    async def release(self) -> None:
        """Let the deferred responses through, as the network eventually would."""
        held, self._held = self._held, []
        for event in held:
            await super().publish(event)
        await super().drain()

    @property
    def held(self) -> int:
        return len(self._held)


class TestResponseBarrier:
    async def test_completes_when_every_required_agent_responds(self, clock):
        barrier = ResponseBarrier(clock)
        required = {AgentId.TIDAL, AgentId.NORO, AgentId.ZEPHR}
        barrier.expect("opp-1", required)
        for agent in required:
            barrier.record("opp-1", agent)
        result = await barrier.wait("opp-1", timeout_ms=1_000)
        assert result.complete and not result.timed_out
        assert result.missing == set()

    async def test_reports_exactly_which_agents_are_missing(self, clock):
        barrier = ResponseBarrier(clock)
        barrier.expect("opp-2", {AgentId.TIDAL, AgentId.NORO, AgentId.ZEPHR})
        barrier.record("opp-2", AgentId.TIDAL)

        async def advance():
            await asyncio.sleep(0)
            clock.advance(2_000)

        task = asyncio.create_task(advance())
        result = await barrier.wait("opp-2", timeout_ms=1_000)
        await task
        assert not result.complete
        assert result.missing == {AgentId.NORO, AgentId.ZEPHR}
        assert result.timed_out

    async def test_a_response_arriving_before_the_wait_is_not_missed(self, clock):
        barrier = ResponseBarrier(clock)
        barrier.expect("opp-3", {AgentId.NORO})
        barrier.record("opp-3", AgentId.NORO)  # arrives before wait() is called
        result = await barrier.wait("opp-3", timeout_ms=1_000)
        assert result.complete

    async def test_unknown_correlation_ids_are_counted_not_crashed(self, clock):
        barrier = ResponseBarrier(clock)
        barrier.record("never-registered", AgentId.NORO)
        assert barrier.late_responses == 1

    async def test_waiting_on_an_unregistered_id_returns_immediately(self, clock):
        barrier = ResponseBarrier(clock)
        result = await barrier.wait("nothing", timeout_ms=5_000)
        assert result.complete and result.waited_ms == 0

    async def test_forget_releases_tracking(self, clock):
        barrier = ResponseBarrier(clock)
        barrier.expect("opp-4", {AgentId.NORO})
        assert barrier.outstanding == 1
        barrier.forget("opp-4")
        assert barrier.outstanding == 0


class TestOrchestratorSurvivesADeferredBus:
    """The end-to-end regression for P0-C1."""

    async def _platform(self, settings, store, deferred: EventType):
        from apps.orchestrator.wiring import build_platform
        from core.config import simulated_venues
        from simulation.market import default_market

        clock = ManualClock(START_MS)
        bus = DeferredDeliveryBus(deferred, raise_on_handler_error=True)
        return build_platform(
            settings.model_copy(update={"venues": simulated_venues()}),
            clock=clock,
            bus=bus,
            store=store,
            market=default_market(start_ms=START_MS),
            raise_on_handler_error=True,
        )

    async def test_opinions_delayed_past_drain_do_not_cause_blanket_rejection(
        self, settings, store
    ):
        """Before the fix this rejected every opportunity as CONSENSUS_INCOMPLETE."""
        platform = await self._platform(settings, store, EventType.AGENT_OPINION)
        bus: DeferredDeliveryBus = platform.bus
        await platform.start(record=False)

        for _ in range(400):
            platform.clock.advance(100)
            await platform.step_market(1)
            await platform.orchestrator.tick()
            # The "network" delivers the held opinions between ticks — after
            # drain() has already returned, exactly as Redis behaves.
            await bus.release()

        records = list(platform.state.opportunities.values())
        assert records, "expected opportunities to be detected"
        incomplete = [r for r in records if r.rejected_reason == "CONSENSUS_INCOMPLETE"]
        assert len(incomplete) < len(records), (
            "every opportunity was rejected for missing agents — the orchestrator "
            "is still inferring completion from the transport"
        )
        # And the pipeline genuinely progressed.
        assert any(r.state is not StrategyState.REJECTED for r in records) or any(
            r.rejected_reason != "CONSENSUS_INCOMPLETE" for r in records
        )

    async def test_agents_still_evaluate_when_responses_are_deferred(
        self, settings, store
    ):
        platform = await self._platform(settings, store, EventType.AGENT_OPINION)
        bus: DeferredDeliveryBus = platform.bus
        seen: dict[str, set[AgentId]] = {}

        async def watch(event):
            if event.type is EventType.AGENT_OPINION and event.correlation_id:
                seen.setdefault(event.correlation_id, set()).add(
                    AgentId(event.payload["agent_id"])
                )

        bus.subscribe(watch, types=[EventType.AGENT_OPINION], name="probe")
        await platform.start(record=False)
        for _ in range(300):
            platform.clock.advance(100)
            await platform.step_market(1)
            await platform.orchestrator.tick()
            await bus.release()

        assert seen, "no agent opinions were published at all"
        required = {AgentId.TIDAL, AgentId.NORO, AgentId.ZEPHR}
        assert any(required <= agents for agents in seen.values())

    async def test_a_permanently_silent_agent_is_reported_not_waited_on_forever(
        self, settings, store
    ):
        """Deferred responses that never arrive must not hang the tick loop."""
        platform = await self._platform(settings, store, EventType.AGENT_OPINION)
        await platform.start(record=False)

        # Never release: the responses are lost in the network for good.
        for _ in range(200):
            platform.clock.advance(100)
            await platform.step_market(1)
            await asyncio.wait_for(platform.orchestrator.tick(), timeout=5.0)

        records = list(platform.state.opportunities.values())
        assert records
        # Every one resolved — none left stuck in AGENTS_EVALUATING forever.
        stuck = [
            r
            for r in records
            if r.state is StrategyState.AGENTS_EVALUATING
            and platform.clock.now_ms() - r.updated_at
            > settings.consensus.agent_response_timeout_ms * 4
        ]
        assert not stuck, f"{len(stuck)} opportunities stuck waiting on agents"
        assert all(
            r.rejected_reason == "CONSENSUS_INCOMPLETE"
            for r in records
            if r.rejected_reason
        )

    async def test_the_tick_loop_never_blocks_on_the_barrier(self, settings, store):
        """A tick must complete promptly even with responses outstanding."""
        platform = await self._platform(settings, store, EventType.AGENT_OPINION)
        await platform.start(record=False)
        for _ in range(60):
            platform.clock.advance(100)
            await platform.step_market(1)
            # 2s of real time is enormous for a tick that does no I/O; a
            # blocking barrier wait would exceed it.
            await asyncio.wait_for(platform.orchestrator.tick(), timeout=2.0)


class TestBarrierIntegration:
    async def test_barrier_is_cleared_as_opportunities_resolve(self, platform):
        """Outstanding tracking must not grow without bound."""
        await run_platform(platform, 500)
        live = {r.opportunity.opportunity_id for r in platform.state.open_opportunities()}
        assert platform.orchestrator.barrier.outstanding <= len(live) + 2

    async def test_normal_in_process_operation_completes_without_timeouts(self, platform):
        """With local agents the barrier is satisfied by drain() every time."""
        from monitoring import metrics as M

        await run_platform(platform, 400)
        timeouts = sum(
            v
            for k, v in platform.metrics.snapshot().items()
            if k.startswith(M.AGENT_RESPONSE_TIMEOUT)
        )
        assert timeouts == 0, "in-process agents should never hit the response deadline"
