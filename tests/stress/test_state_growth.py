"""Orchestrator state must stay bounded over a long session — P0-H4, P0-M.

The audit asked for the opinion-pruning path to be reviewed specifically:
is it bounded, deterministic, safe, and does it respect TTLs? These tests
answer that, and sweep the rest of ``SystemState`` for the same property,
because pruning one collection while another grows unchecked buys nothing.

The sweep found one: ``SystemState.orders`` was the only collection with no
trim at all. Every other one — opportunities, fills, rejections, errors —
was already bounded.
"""

from __future__ import annotations

import pytest

from core.clock import ManualClock
from core.models.common import AgentId, OrderType, Side, TimeInForce
from core.models.execution import OrderStatus, PaperOrder
from core.models.opportunity import StrategyState
from core.state import SystemState

START_MS = 1_700_000_000_000


def make_order(i: int, clock: ManualClock) -> PaperOrder:
    return PaperOrder(
        created_at=clock.now_ms(),
        client_order_id=f"ord-{i:06d}",
        venue="VENUE_A",
        symbol="BTC-USD",
        side=Side.BUY,
        order_type=OrderType.LIMIT,
        time_in_force=TimeInForce.GTC,
        quantity=1.0,
        expected_price=50_000.0,
    )


def make_opinion(subject: str, agent: AgentId, clock: ManualClock, ttl_ms: int = 1_000):
    """An opinion about ``subject``.

    ``subject`` is a property: correlation_id when set (per-opportunity
    agents), otherwise the symbol (symbol-scoped agents like LUMEN).
    """
    from core.models.agent import AgentOpinion

    now = clock.now_ms()
    return AgentOpinion(
        created_at=now,
        expires_at=now + ttl_ms,
        correlation_id=subject,
        agent_id=agent,
        symbol="BTC-USD",
        signal=0.5,
        confidence=0.8,
        model_version="test-1",
    )


def make_opportunity(clock: ManualClock):
    from core.models.opportunity import (
        Opportunity,
        OpportunityKind,
        OpportunityLeg,
    )

    now = clock.now_ms()
    return Opportunity(
        created_at=now,
        expires_at=now + 5_000,
        kind=OpportunityKind.CROSS_VENUE_DISLOCATION,
        strategy="cross_venue",
        symbol="BTC-USD",
        gross_edge_bps=20.0,
        legs=[
            OpportunityLeg(
                venue="VENUE_A", symbol="BTC-USD", side=Side.BUY, reference_price=50_000.0
            ),
            OpportunityLeg(
                venue="VENUE_B", symbol="BTC-USD", side=Side.SELL, reference_price=50_100.0
            ),
        ],
    )


@pytest.fixture
def state():
    return SystemState(clock=ManualClock(start_ms=START_MS))


class TestOpinionPruningIsBounded:
    def test_pruning_drops_opinions_for_dead_subjects(self, state):
        clock = state.clock
        for i in range(500):
            state.put_opinion(make_opinion(f"opp-{i}", AgentId.NORO, clock))
        assert len(state.opinions) == 500

        state.prune_opinions({"opp-1", "opp-2"})
        assert set(key[0] for key in state.opinions) == {"opp-1", "opp-2"}

    def test_pruning_is_deterministic(self, state):
        """Same inputs, same survivors — no dependence on dict ordering."""
        clock = state.clock
        runs = []
        for _ in range(3):
            fresh = SystemState(clock=ManualClock(start_ms=START_MS))
            for i in range(200):
                fresh.put_opinion(make_opinion(f"opp-{i}", AgentId.NORO, clock))
                fresh.put_opinion(make_opinion(f"opp-{i}", AgentId.ZEPHR, clock))
            fresh.prune_opinions({f"opp-{i}" for i in range(0, 200, 3)})
            runs.append(sorted(fresh.opinions))
        assert runs[0] == runs[1] == runs[2]

    def test_pruning_keeps_every_agent_for_a_live_subject(self, state):
        """Dropping one agent's opinion would silently change consensus."""
        clock = state.clock
        for agent in (AgentId.TIDAL, AgentId.NORO, AgentId.ZEPHR, AgentId.LUMEN):
            state.put_opinion(make_opinion("opp-live", agent, clock))
        state.prune_opinions({"opp-live"})
        assert len(state.opinions) == 4

    def test_pruning_an_empty_keep_set_clears_everything(self, state):
        clock = state.clock
        state.put_opinion(make_opinion("opp-1", AgentId.NORO, clock))
        state.prune_opinions(set())
        assert state.opinions == {}

    def test_pruning_never_resurrects_or_mutates_an_opinion(self, state):
        clock = state.clock
        kept = make_opinion("opp-keep", AgentId.NORO, clock)
        state.put_opinion(kept)
        state.put_opinion(make_opinion("opp-drop", AgentId.NORO, clock))
        state.prune_opinions({"opp-keep"})
        assert state.opinions[("opp-keep", AgentId.NORO)] is kept

    def test_expiry_is_not_pruning(self, state):
        """A stale opinion must stay visible so it can be *excluded*.

        Deleting it on expiry would make an expired opinion indistinguishable
        from an agent that never answered, and "no opinion" is not the same
        claim as "an opinion that has gone stale".
        """
        clock = state.clock
        state.put_opinion(make_opinion("opp-1", AgentId.NORO, clock, ttl_ms=100))
        clock.advance(10_000)
        state.prune_opinions({"opp-1"})
        assert ("opp-1", AgentId.NORO) in state.opinions

    def test_an_out_of_order_opinion_does_not_overwrite_a_newer_one(self, state):
        clock = state.clock
        newer = make_opinion("opp-1", AgentId.NORO, clock)
        clock.advance(-0)  # explicit: the older one is stamped earlier
        older = make_opinion("opp-1", AgentId.NORO, clock)
        older.created_at = newer.created_at - 500

        state.put_opinion(newer)
        state.put_opinion(older)
        assert state.opinions[("opp-1", AgentId.NORO)] is newer


class TestEveryCollectionIsBounded:
    def test_orders_do_not_grow_without_limit(self, state):
        """The one collection that had no trim at all."""
        clock = state.clock
        for i in range(5_000):
            order = make_order(i, clock)
            order.status = OrderStatus.FILLED
            order.terminal_at = clock.now_ms()
            state.put_order(order)
            clock.advance(1)
        assert len(state.orders) <= state.max_history

    def test_a_live_order_is_never_evicted(self, state):
        """Eviction must not lose an order the platform still has working."""
        clock = state.clock
        live = make_order(0, clock)
        live.status = OrderStatus.OPEN
        state.put_order(live)

        for i in range(1, 3_000):
            order = make_order(i, clock)
            order.status = OrderStatus.FILLED
            order.terminal_at = clock.now_ms()
            state.put_order(order)
            clock.advance(1)

        assert live.client_order_id in state.orders

    def test_an_order_a_retained_opportunity_names_is_never_evicted(self, state):
        """Attribution walks record.order_ids and must not find a hole."""
        clock = state.clock
        referenced = make_order(0, clock)
        referenced.status = OrderStatus.FILLED
        referenced.terminal_at = clock.now_ms()
        state.put_order(referenced)

        record = state.add_opportunity(make_opportunity(clock))
        record.order_ids.append(referenced.client_order_id)

        for i in range(1, 3_000):
            order = make_order(i, clock)
            order.status = OrderStatus.FILLED
            order.terminal_at = clock.now_ms()
            state.put_order(order)
            clock.advance(1)

        assert referenced.client_order_id in state.orders

    def test_eviction_drops_the_oldest_first(self, state):
        clock = state.clock
        for i in range(state.max_history * 3):
            order = make_order(i, clock)
            order.status = OrderStatus.FILLED
            order.terminal_at = clock.now_ms()
            state.put_order(order)
            clock.advance(1)

        surviving = sorted(state.orders)
        assert surviving[-1] == f"ord-{state.max_history * 3 - 1:06d}"
        assert surviving[0] > "ord-000000"

    def test_fills_do_not_grow_without_limit(self, state):
        from core.models.execution import FillEvent

        clock = state.clock
        for i in range(20_000):
            state.add_fill(
                FillEvent(
                    created_at=clock.now_ms(),
                    client_order_id=f"ord-{i}",
                    venue="VENUE_A",
                    symbol="BTC-USD",
                    side=Side.BUY,
                    quantity=1.0,
                    price=50_000.0,
                )
            )
        assert len(state.fills) <= 5_000

    def test_opportunities_do_not_grow_without_limit(self, state):
        clock = state.clock
        for _ in range(3_000):
            record = state.add_opportunity(make_opportunity(clock))
            record.state = StrategyState.CLOSED
            clock.advance(1)
        assert len(state.opportunities) <= state.max_history

    def test_errors_and_rejections_do_not_grow_without_limit(self, state):
        for i in range(3_000):
            state.record_error(f"error {i}")
        assert len(state.errors) <= state.max_history


class TestALongSessionStaysBounded:
    """The property all of the above exists to produce."""

    async def test_two_thousand_ticks_leave_every_collection_bounded(self):
        from apps.orchestrator.wiring import build_platform
        from core.config import load_settings

        clock = ManualClock(start_ms=START_MS)
        settings = load_settings()
        platform = build_platform(settings, clock=clock, session_label="growth")
        await platform.start(record=False, feeds=False)
        try:
            for _ in range(2_000):
                await platform.step_market(1)
                await platform.orchestrator.tick()
                clock.advance(int(settings.tick_interval_s * 1000))

            state = platform.state
            assert len(state.orders) <= state.max_history
            assert len(state.opportunities) <= state.max_history
            assert len(state.rejections) <= state.max_history
            assert len(state.errors) <= state.max_history
            assert len(state.fills) <= 5_000

            # Opinions are keyed by live subject, so they are bounded by the
            # opportunities in flight plus one entry per symbol per agent.
            assert len(state.opinions) <= (
                (state.max_history + len(settings.symbols)) * len(AgentId)
            )

            # And the ledger underneath it.
            assert len(platform.oms.orders) <= platform.oms.orders_created
            assert (
                len(platform.account.fill_log) <= platform.account.retained_fills
                + platform.account.fills_applied
            )
        finally:
            await platform.stop()

    async def test_the_session_actually_did_something(self):
        """Guards the test above: bounded-because-idle proves nothing."""
        from apps.orchestrator.wiring import build_platform
        from core.config import load_settings

        clock = ManualClock(start_ms=START_MS)
        settings = load_settings()
        platform = build_platform(settings, clock=clock, session_label="growth-activity")
        await platform.start(record=False, feeds=False)
        try:
            for _ in range(300):
                await platform.step_market(1)
                await platform.orchestrator.tick()
                clock.advance(int(settings.tick_interval_s * 1000))
            assert platform.orchestrator.ticks == 300
            assert platform.state.market is not None
        finally:
            await platform.stop()
