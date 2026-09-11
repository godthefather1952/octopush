"""Phase 12 SHADOW remediation invariants.

These tests are deliberately CI-discovered under tests/unit.  SHADOW may add
observability, never economic authority.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from apps.api.app import create_app
from apps.orchestrator.wiring import build_platform
from apps.shadow.observer import ShadowObserver
from apps.shadow.registry import ShadowRegistry
from core.bus import InMemoryEventBus
from core.clock import ManualClock
from core.config import load_settings, simulated_venues
from core.events import Event, EventType
from core.ids import deterministic_ids
from core.models.common import TradingMode
from core.models.runtime import OperationalProfile
from core.models.shadow import (
    MarketDataProvenance,
    ShadowDecisionStatus,
    ShadowMarketCheckpoint,
    ShadowOrderSummary,
)
from simulation.market import DislocationSpec, default_market
from storage import InMemoryEventStore
from tests.audit.rune_fixtures import context, core, intent
from tests.conftest import START_MS


async def _rate(*, healthy: int, observer: bool) -> float:
    bus = InMemoryEventBus()

    async def boom(_event: Event) -> None:
        raise RuntimeError("authoritative handler failed")

    async def fine(_event: Event) -> None:
        return None

    bus.subscribe(boom, name="boom")
    for i in range(healthy):
        bus.subscribe(fine, name=f"healthy-{i}")
    if observer:
        bus.subscribe(fine, name="shadow-like-observer", health_relevant=False)

    for i in range(16):
        await bus.publish(
            Event(type=EventType.SYSTEM_EVENT, ts_ms=START_MS + i, source="test")
        )
    await bus.drain()
    return bus.recent_error_rate


class TestShadowCannotChangeRiskHealth:
    @pytest.mark.parametrize(
        ("healthy", "expected"),
        [(3, 0.25), (2, 1.0 / 3.0)],
    )
    async def test_observer_presence_does_not_change_risk_error_rate(
        self, healthy, expected
    ):
        paper_rate = await _rate(healthy=healthy, observer=False)
        shadow_rate = await _rate(healthy=healthy, observer=True)
        assert paper_rate == pytest.approx(expected)
        assert shadow_rate == pytest.approx(paper_rate)

        paper = core().evaluate(intent(), context(error_rate=paper_rate), START_MS)
        shadow = core().evaluate(intent(), context(error_rate=shadow_rate), START_MS)
        assert shadow.verdict == paper.verdict
        assert shadow.approved_notional == paper.approved_notional
        assert [g.result for g in shadow.gates] == [g.result for g in paper.gates]


async def _observe(observer: ShadowObserver, event_type: EventType, payload: dict, ts: int):
    await observer._on_event(
        Event(
            type=event_type,
            ts_ms=ts,
            source="test",
            correlation_id="opp-1",
            payload=payload,
        )
    )


class TestShadowFillIntegrity:
    async def test_fill_is_duplicate_safe_and_bound_to_its_order_plan(self):
        registry = ShadowRegistry(market_data=MarketDataProvenance.SIMULATED)
        observer = ShadowObserver(InMemoryEventBus(), registry, enabled=True)

        await _observe(
            observer,
            EventType.OPPORTUNITY_DETECTED,
            {"opportunity_id": "opp-1", "strategy": "cross_venue", "symbol": "BTC-USD"},
            1,
        )
        await _observe(
            observer,
            EventType.EXECUTION_PLAN,
            {"plan_id": "plan-a", "notional": 100.0},
            2,
        )
        await _observe(
            observer,
            EventType.PAPER_ORDER_CREATED,
            {
                "client_order_id": "order-a",
                "plan_id": "plan-a",
                "venue": "VENUE_A",
                "symbol": "BTC-USD",
                "status": "SUBMITTING",
                "quantity": 1.0,
            },
            3,
        )
        await _observe(
            observer,
            EventType.EXECUTION_PLAN,
            {"plan_id": "plan-b", "notional": 200.0},
            4,
        )
        await _observe(
            observer,
            EventType.PAPER_ORDER_CREATED,
            {
                "client_order_id": "order-b",
                "plan_id": "plan-b",
                "venue": "VENUE_B",
                "symbol": "BTC-USD",
                "status": "SUBMITTING",
                "quantity": 2.0,
            },
            5,
        )

        fill = {
            "fill_id": "fill-a",
            "client_order_id": "order-a",
            "quantity": 1.0,
            "price": 101.0,
            "fee": 0.25,
            "slippage_bps": 1.5,
        }
        await _observe(observer, EventType.PAPER_FILL, fill, 6)
        first = registry.execution_for_plan("plan-a")
        other = registry.execution_for_plan("plan-b")
        assert first is not None and other is not None
        assert first.paper_fill_ids == ["fill-a"]
        assert first.paper_filled_notional == pytest.approx(101.0)
        assert first.paper_fees == pytest.approx(0.25)
        assert other.paper_fill_ids == []

        await _observe(observer, EventType.PAPER_FILL, fill, 7)
        duplicate = registry.execution_for_plan("plan-a")
        assert duplicate is not None
        assert duplicate.paper_fill_ids == ["fill-a"]
        assert duplicate.paper_filled_notional == pytest.approx(101.0)
        assert duplicate.paper_fees == pytest.approx(0.25)
        assert duplicate.updated_at == first.updated_at
        assert registry.fills_recorded == 1

    async def test_unattributable_fill_is_visible_and_not_guessed(self):
        registry = ShadowRegistry()
        observer = ShadowObserver(InMemoryEventBus(), registry, enabled=True)
        await _observe(
            observer,
            EventType.OPPORTUNITY_DETECTED,
            {"opportunity_id": "opp-1"},
            1,
        )
        await _observe(
            observer,
            EventType.PAPER_FILL,
            {
                "fill_id": "fill-x",
                "client_order_id": "missing-order",
                "quantity": 1.0,
                "price": 100.0,
            },
            2,
        )
        assert observer.events_unattributable == 1
        assert registry.fills_recorded == 0


class TestShadowLifecycle:
    def test_terminal_decision_cannot_be_resurrected(self):
        registry = ShadowRegistry()
        decision = registry.register_decision("opp-1", 1)
        registry.set_status(
            decision.shadow_decision_id, ShadowDecisionStatus.CONSENSUS_RECORDED, 2
        )
        registry.set_status(
            decision.shadow_decision_id,
            ShadowDecisionStatus.REJECTED,
            3,
            reason="CONSENSUS_BELOW_THRESHOLD",
        )
        terminal = registry.get(decision.shadow_decision_id)
        assert terminal is not None
        registry.set_status(
            decision.shadow_decision_id, ShadowDecisionStatus.AUTHORIZED, 4
        )
        after = registry.get(decision.shadow_decision_id)
        assert after is not None
        assert after.status is ShadowDecisionStatus.REJECTED
        assert after.terminal_at == terminal.terminal_at
        assert after.reason_codes == ["CONSENSUS_BELOW_THRESHOLD"]

    def test_terminal_execution_cannot_be_resurrected(self):
        registry = ShadowRegistry()
        decision = registry.register_decision("opp-1", 1)
        execution = registry.register_execution(
            decision.shadow_decision_id, 2, plan_id="plan-1"
        )
        assert execution is not None
        registry.set_execution_status(
            execution.shadow_execution_id, ShadowDecisionStatus.PAPER_WORKING, 3
        )
        registry.set_execution_status(
            execution.shadow_execution_id, ShadowDecisionStatus.PAPER_COMPLETE, 4
        )
        registry.set_execution_status(
            execution.shadow_execution_id, ShadowDecisionStatus.PAPER_WORKING, 5
        )
        after = registry.execution_for_plan("plan-1")
        assert after is not None
        assert after.status is ShadowDecisionStatus.PAPER_COMPLETE
        assert after.terminal_at == 4

    async def test_approved_reduced_is_still_approved(self):
        registry = ShadowRegistry()
        observer = ShadowObserver(InMemoryEventBus(), registry, enabled=True)
        await _observe(
            observer,
            EventType.OPPORTUNITY_DETECTED,
            {"opportunity_id": "opp-1"},
            1,
        )
        await _observe(
            observer,
            EventType.RISK_PASS,
            {
                "decision_id": "risk-1",
                "verdict": "APPROVED_REDUCED",
                "requested_notional": 1000.0,
                "approved_notional": 500.0,
            },
            2,
        )
        decision = registry.for_opportunity("opp-1")
        assert decision is not None
        assert decision.approved is True
        assert decision.approved_notional == pytest.approx(500.0)

    async def test_generic_rejection_is_not_mislabeled_as_risk_rejection(self):
        registry = ShadowRegistry()
        observer = ShadowObserver(InMemoryEventBus(), registry, enabled=True)
        await _observe(
            observer,
            EventType.OPPORTUNITY_DETECTED,
            {"opportunity_id": "opp-1"},
            1,
        )
        await _observe(
            observer,
            EventType.STRATEGY_STATE_CHANGED,
            {
                "detail": {
                    "to": "REJECTED",
                    "reason": "CONSENSUS_INCOMPLETE",
                }
            },
            2,
        )
        decision = registry.for_opportunity("opp-1")
        assert decision is not None
        assert decision.status is ShadowDecisionStatus.REJECTED
        assert decision.reason_codes == ["CONSENSUS_INCOMPLETE"]


class TestRegistryOwnershipAndCounters:
    def test_query_results_are_detached(self):
        registry = ShadowRegistry()
        decision = registry.register_decision("opp-1", 1)
        queried = registry.get(decision.shadow_decision_id)
        assert queried is not None
        queried.reason_codes.append("MUTATED")
        queried.status = ShadowDecisionStatus.FAILED
        again = registry.get(decision.shadow_decision_id)
        assert again is not None
        assert again.reason_codes == []
        assert again.status is ShadowDecisionStatus.OBSERVED

    def test_order_and_checkpoint_inputs_are_detached(self):
        registry = ShadowRegistry()
        decision = registry.register_decision("opp-1", 1)
        execution = registry.register_execution(
            decision.shadow_decision_id, 2, plan_id="plan-1"
        )
        assert execution is not None
        order = ShadowOrderSummary(client_order_id="order-1", quantity=1.0)
        registry.record_orders(execution.shadow_execution_id, [order], 3)
        order.quantity = 999.0
        stored = registry.execution_for_order("order-1")
        assert stored is not None
        assert stored.orders[0].quantity == pytest.approx(1.0)

        checkpoint = ShadowMarketCheckpoint(
            created_at=4,
            decision_id=decision.shadow_decision_id,
            venue_touches={"A:BTC-USD": [99.0, 101.0]},
        )
        registry.add_market_checkpoint(checkpoint)
        checkpoint.venue_touches["A:BTC-USD"][0] = 0.0
        read = registry.checkpoints_for(decision.shadow_decision_id)
        assert read[0].venue_touches["A:BTC-USD"][0] == pytest.approx(99.0)
        read[0].venue_touches["A:BTC-USD"][0] = -1.0
        assert (
            registry.checkpoints_for(decision.shadow_decision_id)[0]
            .venue_touches["A:BTC-USD"][0]
            == pytest.approx(99.0)
        )

    def test_snapshot_distinguishes_lifetime_from_resident_after_compaction(self):
        registry = ShadowRegistry()
        first = registry.register_decision("opp-1", 1)
        registry.set_status(first.shadow_decision_id, ShadowDecisionStatus.REJECTED, 2)
        registry.register_decision("opp-2", 3)
        before = registry.snapshot(4)
        assert before.decisions_total == 2
        assert before.resident_decisions == 2
        assert before.rejected == 1
        assert registry.compact(keep_all=False) == 1
        after = registry.snapshot(5)
        assert after.decisions_total == 2
        assert after.resident_decisions == 1
        assert after.rejected == 1


class TestObserverFailureVisibility:
    async def test_failure_is_isolated_but_counted_separately(self):
        observer = ShadowObserver(InMemoryEventBus(), ShadowRegistry(), enabled=True)

        def broken(_event: Event) -> None:
            raise RuntimeError("bookkeeping broke")

        observer._dispatch = broken  # type: ignore[method-assign]
        await observer._on_event(
            Event(type=EventType.SYSTEM_EVENT, ts_ms=1, source="test")
        )
        assert observer.handler_failures == 1
        assert observer.events_intentionally_ignored == 0


def _shadow_platform():
    settings = load_settings().model_copy(
        update={
            "venues": simulated_venues(),
            "operational_profile": OperationalProfile.SHADOW,
        }
    )
    clock = ManualClock(START_MS)
    return build_platform(
        settings,
        clock=clock,
        bus=InMemoryEventBus(raise_on_handler_error=True),
        store=InMemoryEventStore(),
        market=default_market(start_ms=START_MS),
        raise_on_handler_error=True,
    )


class TestShadowReadSurface:
    def test_snapshot_and_api_are_same_time_pure(self):
        platform = _shadow_platform()
        now = platform.clock.now_ms()
        before = platform.shadow.snapshot(now).model_dump(mode="json")
        first = platform.shadow_snapshot(now).model_dump(mode="json")
        second = platform.shadow_snapshot(now).model_dump(mode="json")
        after = platform.shadow.snapshot(now).model_dump(mode="json")
        assert first == second
        assert before == after

        with TestClient(create_app(platform)) as client:
            api1 = client.get("/api/shadow").json()
            api2 = client.get("/api/shadow").json()
        assert api1 == api2

    def test_readiness_cannot_be_ready_when_execution_is_unavailable(self):
        platform = _shadow_platform()
        platform.state.kill_switch = platform.state.kill_switch.model_copy(
            update={"execution_disabled": True}
        )
        readiness = platform.shadow_readiness(platform.clock.now_ms())
        assert readiness.paper_execution_available is False
        assert readiness.ready is False
        assert "PAPER_EXECUTION_UNAVAILABLE" in readiness.reason_codes

    def test_observer_gaps_are_exposed_without_becoming_trade_authority(self):
        platform = _shadow_platform()
        assert platform.shadow_observer is not None
        platform.shadow_observer.handler_failures = 2
        snap = platform.shadow_snapshot(platform.clock.now_ms())
        assert snap.observer_failures == 2
        assert snap.readiness is not None
        assert snap.readiness.observer_healthy is False
        assert "OBSERVER_FAILURES:2" in snap.readiness.reason_codes

    def test_shadow_boundary_is_still_paper_only(self):
        platform = _shadow_platform()
        assert platform.settings.mode is TradingMode.PAPER
        assert platform.executor.is_paper is True
        assert all(
            not adapter.capabilities.authenticated
            and not adapter.capabilities.order_submission
            for adapter in platform.adapters.values()
        )
        with TestClient(create_app(platform)) as client:
            paths = client.get("/openapi.json").json()["paths"]
        assert "/api/shadow" in paths
        assert set(paths["/api/shadow"]) == {"get"}
        assert not any("promote" in path for path in paths)
        live_named = {path: methods for path, methods in paths.items() if "live" in path}
        assert live_named == {"/api/pre-live": paths["/api/pre-live"]}
        assert set(paths["/api/pre-live"]) == {"get"}


async def _economic_run(profile: OperationalProfile) -> dict:
    settings = load_settings().model_copy(
        update={
            "venues": simulated_venues(),
            "operational_profile": profile,
        }
    )
    clock = ManualClock(START_MS)
    market = default_market(
        start_ms=START_MS,
        dislocations=[
            DislocationSpec(
                start_step=5,
                duration_steps=140,
                venue="VENUE_B",
                symbol="BTC-USD",
                magnitude_bps=45.0,
            )
        ],
    )
    with deterministic_ids("phase12-economic-equivalence"):
        platform = build_platform(
            settings,
            clock=clock,
            bus=InMemoryEventBus(raise_on_handler_error=True),
            store=InMemoryEventStore(),
            market=market,
            raise_on_handler_error=True,
        )
        await platform.start(record=False)
        for _ in range(220):
            clock.advance(100)
            await platform.step_market(1)
            await platform.orchestrator.tick()

        portfolio = platform.account.snapshot()
        summary = {
            "opportunities": sorted(
                (
                    oid,
                    record.state.value,
                    record.rejected_reason,
                    record.filled_notional,
                    record.fees,
                    record.realized_pnl,
                )
                for oid, record in platform.state.opportunities.items()
            ),
            "risk": [d.model_dump(mode="json") for d in platform.rune.decisions],
            "orders": [
                order.to_json_dict()
                for order in sorted(
                    platform.oms.all_orders(), key=lambda item: item.client_order_id
                )
            ],
            "fills": [fill.to_json_dict() for fill in platform.account.fill_log],
            "portfolio": portfolio.model_dump(mode="json"),
            "hedges_requested": platform.okapi.hedges_requested,
            "hedges": [
                record.model_dump(mode="json")
                for record in platform.okapi.hedge_registry.all()
            ],
            "marin": (
                platform.marin.last_result.model_dump(mode="json")
                if platform.marin.last_result is not None
                else None
            ),
            "kill_switch": platform.kill_switch.state.model_dump(mode="json"),
        }
        await platform.stop()
        return summary


class TestPaperShadowEconomicEquivalence:
    async def test_profile_adds_observation_only(self):
        paper = await _economic_run(OperationalProfile.PAPER)
        shadow = await _economic_run(OperationalProfile.SHADOW)
        assert paper["risk"], "equivalence run must exercise RUNE"
        assert paper == shadow
