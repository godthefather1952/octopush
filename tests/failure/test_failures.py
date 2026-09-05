"""Failure injection.

Everything in section 33 of the specification, plus the rule that binds them:
the platform must fail *safely*, and losing the intelligence layer must never
prevent it from stopping, closing, reconciling or preserving data.
"""

from __future__ import annotations

import pytest

from agents.lumen import Lumen, NewsItem, NullProvider, ScriptedProvider
from agents.tidal.book import BookDesyncError, LocalOrderBook
from core.bus import InMemoryEventBus
from core.events import Event, EventType
from core.models.common import AgentId, DataQuality, OrderType, Side, TimeInForce
from core.models.execution import OrderStatus
from core.models.opportunity import StrategyState
from core.models.ops import HealthStatus
from core.state import SystemState
from risk.kill_switch import KillSwitch, KillSwitchInputs
from tests.conftest import START_MS, make_book, run_platform
from venues.base.messages import BookDelta, VenueStatus, VenueStatusKind


def good_response():
    return {
        "sentiment": 0.1,
        "attention": 0.2,
        "information_shock": False,
        "direction": 0.0,
        "confidence": 0.6,
        "ttl_seconds": 60,
        "reason_codes": ["CALM"],
    }


class TestMarketDataFailures:
    async def test_disconnect_invalidates_that_venue_only(self, platform):
        await run_platform(platform, 30)
        adapter = platform.adapters["VENUE_A"]
        await adapter.simulate_disconnect()
        await platform.bus.drain()
        platform.clock.advance(100)
        await platform.orchestrator.tick()

        market = platform.state.market
        assert not market.venues["VENUE_A:BTC-USD"].quality.is_usable
        assert market.venues["VENUE_B:BTC-USD"].quality.is_usable

    async def test_a_reconnect_recovers_the_feed(self, platform):
        await run_platform(platform, 30)
        adapter = platform.adapters["VENUE_A"]
        await adapter.simulate_disconnect()
        await platform.bus.drain()
        platform.clock.advance(100)
        await platform.orchestrator.tick()
        assert not platform.state.market.venues["VENUE_A:BTC-USD"].quality.is_usable

        await adapter.simulate_reconnect()
        await platform.bus.drain()
        # The next snapshot resynchronises the book.
        for _ in range(60):
            platform.clock.advance(100)
            await platform.step_market(1)
            await platform.orchestrator.tick()
        assert platform.state.market.venues["VENUE_A:BTC-USD"].quality.is_usable

    async def test_losing_both_feeds_halts_trading(self, platform):
        await run_platform(platform, 30)
        for adapter in platform.adapters.values():
            await adapter.simulate_disconnect()
        await platform.bus.drain()
        for _ in range(3):
            platform.clock.advance(100)
            await platform.orchestrator.tick()
        assert "MARKET_DATA_OUTAGE" in platform.kill_switch.state.triggered_by
        assert not platform.state.kill_switch.trading_allowed

    async def test_stale_feed_degrades_then_goes_unusable(self, platform):
        await run_platform(platform, 30)
        adapter = platform.adapters["VENUE_A"]
        adapter.suspended = True  # feed silently stops without a disconnect
        limit = platform.settings.risk.max_data_age_ms

        platform.clock.advance(limit + 100)
        await platform.step_market(1)
        state = platform.tidal.venue_state("VENUE_A", "BTC-USD")
        assert state.quality is DataQuality.DEGRADED

        platform.clock.advance(limit * 3)
        await platform.step_market(1)
        state = platform.tidal.venue_state("VENUE_A", "BTC-USD")
        assert state.quality is DataQuality.STALE

    def test_sequence_gap_is_never_silently_absorbed(self):
        book = LocalOrderBook(venue="V", symbol="BTC-USD")
        book.apply_snapshot(make_book("V", "BTC-USD", 100.0, tick=0.01))
        book.sequence = 10
        with pytest.raises(BookDesyncError):
            book.apply_delta(
                BookDelta(
                    venue="V",
                    symbol="BTC-USD",
                    exchange_ts=START_MS,
                    received_ts=START_MS,
                    sequence=20,
                    prev_sequence=19,
                )
            )
        assert not book.usable and book.needs_resync

    async def test_out_of_order_and_duplicate_updates_are_dropped(self, platform):
        await run_platform(platform, 20)
        book = platform.tidal.books[("VENUE_A", "BTC-USD")]
        before = dict(book.bids)
        stale = BookDelta(
            venue="VENUE_A",
            symbol="BTC-USD",
            exchange_ts=START_MS,
            received_ts=START_MS,
            sequence=1,
            prev_sequence=0,
            bids=[],
        )
        await platform.tidal.on_delta(stale)
        assert book.bids == before

    async def test_a_crossed_book_trips_the_kill_switch(self, platform):
        await run_platform(platform, 30)
        book = platform.tidal.books[("VENUE_A", "BTC-USD")]
        # Corrupt the book so the best bid sits above the best ask.
        book.asks = {min(book.bids) - 1.0: 1.0}
        platform.clock.advance(100)
        await platform.orchestrator.tick()
        assert "BOOK_CORRUPTION" in platform.kill_switch.state.triggered_by


class TestExecutionFailures:
    async def test_duplicate_fill_delivery_is_idempotent(self, platform):
        await run_platform(platform, 400)
        assert platform.account.fill_log, "need a fill to duplicate"
        fill = platform.account.fill_log[0]
        cash_before = platform.account.cash
        quantity_before = platform.account.positions[f"{fill.venue}:{fill.symbol}"].quantity

        await platform.bus.publish(
            Event(
                type=EventType.PAPER_FILL,
                ts_ms=platform.clock.now_ms(),
                source="test",
                schema_name="FillEvent",
                payload=fill.to_json_dict(),
            )
        )
        await platform.bus.drain()
        assert platform.account.apply_fill(fill) is False
        assert platform.account.cash == pytest.approx(cash_before)
        assert platform.account.positions[
            f"{fill.venue}:{fill.symbol}"
        ].quantity == pytest.approx(quantity_before)
        assert platform.marin.reconcile().ok

    async def test_unknown_order_state_is_surfaced_not_assumed(self, platform):
        await run_platform(platform, 300)
        order = platform.oms.create(
            venue="VENUE_A",
            symbol="BTC-USD",
            side=Side.BUY,
            quantity=0.01,
            order_type=OrderType.LIMIT,
            time_in_force=TimeInForce.IOC,
            expected_price=100.0,
        )
        platform.oms.transition(order.client_order_id, OrderStatus.SUBMITTING)
        platform.oms.mark_unknown(order.client_order_id, "venue timed out")

        result = platform.marin.reconcile()
        assert any(
            m.key == order.client_order_id and "unknown" in m.detail for m in result.mismatches
        )
        # The order is not treated as failed, so nothing is retried blindly.
        assert not order.is_terminal

    async def test_reconciliation_mismatch_disables_execution(self, platform):
        await run_platform(platform, 300)
        # Corrupt the account behind the platform's back.
        platform.account.cash += 5_000.0
        for _ in range(platform.orchestrator.reconcile_every + 1):
            platform.clock.advance(100)
            await platform.step_market(1)
            await platform.orchestrator.tick()
        assert "RECONCILIATION_MISMATCH" in platform.kill_switch.state.triggered_by
        assert platform.executor.execution_disabled
        assert not platform.state.kill_switch.trading_allowed

    async def test_disabled_execution_refuses_new_orders(self, platform):
        await run_platform(platform, 300)
        platform.executor.execution_disabled = True
        before = len(platform.oms.orders)
        for _ in range(100):
            platform.clock.advance(100)
            await platform.step_market(1)
            await platform.orchestrator.tick()
        created = [
            order
            for order in list(platform.oms.orders.values())[before:]
            if order.status is not OrderStatus.REJECTED
        ]
        assert not created

    async def test_liquidity_vanishing_does_not_corrupt_state(self, settings, clock, store):
        from apps.orchestrator.wiring import build_platform
        from core.config import simulated_venues
        from simulation.market import default_market

        hostile = settings.model_copy(
            update={
                "venues": simulated_venues(),
                "execution": settings.execution.model_copy(
                    update={"liquidity_vanish_probability": 0.6}
                ),
            }
        )
        platform = build_platform(
            hostile,
            clock=clock,
            bus=InMemoryEventBus(raise_on_handler_error=True),
            store=store,
            market=default_market(start_ms=START_MS),
            raise_on_handler_error=True,
        )
        await run_platform(platform, 400)
        assert platform.marin.reconcile().ok


class TestRiskFailures:
    async def test_drawdown_breach_halts_and_flattens(self, platform):
        await run_platform(platform, 30)
        # Force a drawdown past the limit.
        platform.account.peak_equity = platform.account.equity + (
            platform.settings.risk.max_drawdown + 1_000
        )
        platform.clock.advance(100)
        await platform.orchestrator.tick()
        assert "MAX_DRAWDOWN_BREACHED" in platform.kill_switch.state.triggered_by
        assert not platform.state.kill_switch.trading_allowed

    async def test_daily_loss_breach_halts(self, platform):
        await run_platform(platform, 30)
        platform.account.day_realized_pnl = -(platform.settings.risk.max_daily_loss + 1)
        platform.clock.advance(100)
        await platform.orchestrator.tick()
        assert "MAX_DAILY_LOSS_BREACHED" in platform.kill_switch.state.triggered_by

    async def test_manual_kill_switch_stops_trading_and_winds_down(self, platform):
        await run_platform(platform, 300)
        await platform.kill_switch.engage("MANUAL", "operator pulled the switch")
        # Cancellation is not instantaneous: a cancel takes a modelled round
        # trip and can lose the race to a fill, and FLATTEN submits its own
        # exit orders. What must hold is that it converges.
        for _ in range(60):
            platform.clock.advance(100)
            await platform.step_market(1)
            await platform.orchestrator.tick()
        assert not platform.state.kill_switch.trading_allowed
        assert not platform.veska.open_orders()
        assert platform.marin.reconcile().ok

    async def test_kill_switch_does_not_clear_itself(self, platform, bus, clock, settings):
        switch = KillSwitch(bus, clock, settings)
        await switch.engage("MANUAL")
        # A subsequent clean evaluation must not un-engage it.
        await switch.evaluate(
            KillSwitchInputs(
                portfolio=platform.account.snapshot(),
                health=platform.health.snapshot(),
            )
        )
        assert switch.state.engaged
        await switch.clear("operator reset")
        assert not switch.state.engaged

    async def test_engaging_the_same_trigger_twice_is_idempotent(self, bus, clock, settings):
        switch = KillSwitch(bus, clock, settings)
        await switch.engage("MANUAL")
        await switch.engage("MANUAL")
        assert switch.state.triggered_by == ["MANUAL"]

    async def test_a_broken_trigger_does_not_crash_the_loop(self, bus, clock, settings, platform):
        from risk import kill_switch as ks

        switch = KillSwitch(bus, clock, settings)
        original = ks.TRIGGERS.copy()
        ks.TRIGGERS["EXPLODING"] = lambda inputs, cfg: 1 / 0
        try:
            fired = await switch.evaluate(
                KillSwitchInputs(
                    portfolio=platform.account.snapshot(),
                    health=platform.health.snapshot(),
                )
            )
            assert "EXPLODING" not in fired
        finally:
            ks.TRIGGERS.clear()
            ks.TRIGGERS.update(original)


class TestIntelligenceFailures:
    async def test_null_provider_publishes_no_opinion(self, bus, clock, settings, health):
        lumen = Lumen(bus, clock, settings, health, NullProvider())
        assert await lumen.evaluate("BTC-USD") is None
        # A missing agent is missing, not neutral.
        assert lumen.failures == 1

    async def test_repeated_failure_marks_lumen_offline(self, bus, clock, settings, health):
        lumen = Lumen(bus, clock, settings, health, NullProvider())
        for _ in range(settings.lumen.failure_threshold):
            await lumen.evaluate("BTC-USD")
        assert health.status_of("LUMEN") is HealthStatus.OFFLINE

    async def test_lumen_recovers_when_the_provider_returns(
        self, bus, clock, settings, health
    ):
        provider = ScriptedProvider([good_response()], fail_after=0)
        lumen = Lumen(bus, clock, settings, health, provider)
        await lumen.evaluate("BTC-USD")
        assert lumen.consecutive_failures == 1

        working = ScriptedProvider([good_response()])
        lumen.provider = working
        opinion = await lumen.evaluate("BTC-USD")
        assert opinion is not None
        assert lumen.consecutive_failures == 0
        assert health.status_of("LUMEN") is HealthStatus.HEALTHY

    async def test_malformed_response_produces_no_opinion(self, bus, clock, settings, health):
        lumen = Lumen(bus, clock, settings, health, ScriptedProvider([{"garbage": True}]))
        assert await lumen.evaluate("BTC-USD") is None

    async def test_information_shock_argues_against_relative_value(
        self, bus, clock, settings, health
    ):
        calm = Lumen(bus, clock, settings, health, ScriptedProvider([good_response()]))
        shocked = Lumen(
            bus,
            clock,
            settings,
            health,
            ScriptedProvider(
                [
                    {
                        "sentiment": -0.8,
                        "attention": 0.95,
                        "information_shock": True,
                        "direction": -0.7,
                        "confidence": 0.8,
                        "ttl_seconds": 60,
                        "reason_codes": [],
                    }
                ]
            ),
        )
        calm_opinion = await calm.evaluate("BTC-USD")
        shocked_opinion = await shocked.evaluate("BTC-USD")
        assert shocked_opinion.signal < calm_opinion.signal
        assert "NEGATIVE_INFORMATION_SHOCK" in shocked_opinion.reason_codes

    async def test_the_platform_trades_without_any_intelligence_layer(self, platform):
        """The central safety claim: Claude going away costs one input, not the system."""
        assert platform.lumen.provider.name == "null"
        await run_platform(platform, 600)
        assert platform.state.opportunities
        assert platform.account.fill_log
        assert platform.marin.reconcile().ok
        assert not platform.kill_switch.state.engaged

    async def test_lumen_failure_never_blocks_the_fast_loop(self, platform):
        await run_platform(platform, 200)
        # Drive the slow loop into repeated failure while the fast loop runs.
        for _ in range(5):
            await platform.lumen.run_once()
        assert platform.lumen.failures > 0
        for _ in range(100):
            platform.clock.advance(100)
            await platform.step_market(1)
            await platform.orchestrator.tick()
        assert platform.marin.reconcile().ok
        assert platform.orchestrator.warmed_up

    async def test_a_stopping_platform_can_still_close_and_reconcile(self, platform):
        await run_platform(platform, 400)
        await platform.kill_switch.engage("MANUAL", "shutdown drill")
        for _ in range(200):
            platform.clock.advance(100)
            await platform.step_market(1)
            await platform.orchestrator.tick()
        assert not platform.veska.open_orders()
        assert platform.marin.reconcile().ok


class TestAgentFailures:
    async def test_a_failing_subscriber_does_not_stop_the_bus(self, clock, settings, store):
        from apps.orchestrator.wiring import build_platform
        from core.config import simulated_venues
        from simulation.market import default_market

        bus = InMemoryEventBus(raise_on_handler_error=False)
        platform = build_platform(
            settings.model_copy(update={"venues": simulated_venues()}),
            clock=clock,
            bus=bus,
            store=store,
            market=default_market(start_ms=START_MS),
        )

        async def exploding(event):
            raise RuntimeError("agent blew up")

        subscription = bus.subscribe(exploding, name="exploding")
        await run_platform(platform, 100)
        assert subscription.errors > 0
        # Everything else kept working.
        assert platform.state.market is not None
        assert platform.marin.reconcile().ok

    async def test_a_missing_required_agent_stops_the_strategy(self, platform):
        """Losing a required agent must stop the strategy — not slow it down.

        TERMINAL vs PENDING (Phase 3+4 harness cleanup)
        ===============================================
        This test used to require EVERY opportunity detected during the
        failure window to be sitting in REJECTED/CONSENSUS_INCOMPLETE by the
        time the loop stopped. That silently assumed every one of them had
        already reached its response deadline.

        It has not. ``consensus.agent_response_timeout_ms`` is 1,000ms and a
        tick is 100ms, so the handful of opportunities detected in the last
        ten ticks are legitimately still in AGENTS_EVALUATING: they are
        WAITING for the agent that will never answer, which is the designed
        behaviour and is safe — a waiting opportunity has no intent, no risk
        decision and no orders. The assumption only held while opportunity
        turnover was broken; restoring turnover exposed it.

        So the records are partitioned rather than lumped together, and each
        half carries the assertion that actually applies to it. Neither half
        is allowed to have traded.
        """
        await run_platform(platform, 200)
        state: SystemState = platform.state
        seen_before = set(state.opportunities)

        # Watch what the platform actually executes from here on, by
        # correlation id. Raw counts of ``oms.orders`` / ``account.fill_log``
        # would not work: both are compacted during a long run (MARIN seals a
        # verified prefix of the ledger and the OMS drops terminal orders), so
        # a before/after count measures compaction as much as trading. The
        # event stream is never compacted, and every execution event carries
        # the opportunity's correlation id.
        executed: list[tuple[str, str | None]] = []

        async def _collect() -> None:
            return None

        platform.bus.subscribe(
            lambda e: executed.append((e.type.value, e.correlation_id)) or _collect(),
            types=[
                EventType.TRADE_INTENT,
                EventType.RISK_PASS,
                EventType.EXECUTION_PLAN,
                EventType.PAPER_ORDER_CREATED,
                EventType.PAPER_FILL,
            ],
            name="execution-watch",
        )

        # NORO goes away: it publishes no opinion at all -- as opposed to
        # publishing a neutral one, or an abstention.
        #
        # Suppressing ``evaluate`` is what actually removes it. Clearing
        # ``fair_values`` used to work because ``evaluate`` read that cache;
        # since v0.2 it evaluates straight from ``self.market`` via
        # ``build_contributors``, so emptying the cache leaves NORO answering
        # normally and the injection would silently stop injecting anything.
        platform.noro.evaluate = lambda _opportunity, _now_ms: None

        for _ in range(300):
            platform.clock.advance(100)
            await platform.step_market(1)
            await platform.orchestrator.tick()
        await platform.bus.drain()
        now = platform.clock.now_ms()

        new_records = [
            record
            for oid, record in state.opportunities.items()
            if oid not in seen_before
        ]
        assert new_records, "expected further opportunities to be detected"

        terminal = [r for r in new_records if r.state is StrategyState.REJECTED]
        pending = [r for r in new_records if r.state is StrategyState.AGENTS_EVALUATING]

        # There is no third category. Anything downstream of consensus --
        # AUTHORIZED, EXECUTING, HEDGING, RECONCILING, MONITORING, EXITING,
        # CLOSED -- would mean the platform proceeded past a missing required
        # agent, which is the failure this test exists to catch.
        assert len(terminal) + len(pending) == len(new_records), (
            "opportunities detected without NORO reached states other than "
            "rejected-or-waiting: "
            f"{sorted({r.state.value for r in new_records})}"
        )
        assert terminal, (
            "no opportunity was actually stopped; the injection may not have "
            "taken effect"
        )

        # Terminal records were stopped for the ONE right reason. Accepting
        # any other terminal reason (an expiry, say) would let the test pass
        # on a platform that never noticed NORO was gone.
        for record in terminal:
            assert record.rejected_reason == "CONSENSUS_INCOMPLETE", (
                f"{record.opportunity.opportunity_id} was stopped for "
                f"{record.rejected_reason!r}, not for the missing agent"
            )

        # Pending records are provably still inside the response window, so
        # they have not yet had a deadline to miss.
        timeout_ms = platform.settings.consensus.agent_response_timeout_ms
        for record in pending:
            waited = now - record.opportunity.created_at
            assert 0 <= waited < timeout_ms, (
                f"{record.opportunity.opportunity_id} has been waiting "
                f"{waited}ms on a {timeout_ms}ms deadline and should have "
                "been rejected"
            )

        # Neither half traded, or even got as far as asking to.
        for record in new_records:
            assert record.intent is None
            assert record.decision is None
            assert not record.order_ids
            assert record.filled_notional == 0.0
            assert record.realized_pnl == 0.0
            assert AgentId.NORO not in state.opinions_for(
                record.opportunity.opportunity_id
            )

        # The direct proof: nothing the platform executed during the failure
        # window belongs to any opportunity detected during it. Hedges and the
        # unwinding of positions opened BEFORE the injection are expected to
        # continue -- refusing to close an existing position because an
        # intelligence agent is down would be the opposite of failing safely --
        # so this is scoped by correlation id rather than by a global count.
        new_ids = {r.opportunity.opportunity_id for r in new_records}
        leaked = sorted({(kind, cid) for kind, cid in executed if cid in new_ids})
        assert not leaked, (
            f"a required agent was missing and the platform still executed: {leaked}"
        )

    async def test_opinions_do_not_accumulate_without_bound(self, platform):
        await run_platform(platform, 600)
        live = {r.opportunity.opportunity_id for r in platform.state.open_opportunities()}
        subjects = {subject for subject, _ in platform.state.opinions}
        assert subjects <= live | set(platform.settings.symbols)

    async def test_venue_status_events_are_typed_end_to_end(self, platform):
        await run_platform(platform, 20)
        status = VenueStatus(
            venue="VENUE_A",
            kind=VenueStatusKind.DISCONNECTED,
            received_ts=platform.clock.now_ms(),
            detail="test",
        )
        await platform.bus.publish(
            Event(
                type=EventType.VENUE_DISCONNECTED,
                ts_ms=platform.clock.now_ms(),
                source="VENUE_A",
                schema_name="VenueStatus",
                payload=status.model_dump(mode="json"),
            )
        )
        await platform.bus.drain()
        assert platform.tidal.connected["VENUE_A"] is False


class TestNewsInput:
    def test_headlines_are_bounded(self, bus, clock, settings, health):
        lumen = Lumen(bus, clock, settings, health, NullProvider())
        for i in range(80):
            lumen.add_headline(
                NewsItem(headline=f"h{i}", source="test", published_ms=clock.now_ms())
            )
        assert len(lumen.headlines) == 50

    def test_context_excludes_ancient_headlines(self, bus, clock, settings, health):
        lumen = Lumen(bus, clock, settings, health, NullProvider())
        lumen.add_headline(
            NewsItem(headline="old", source="t", published_ms=clock.now_ms() - 7_200_000)
        )
        lumen.add_headline(
            NewsItem(headline="new", source="t", published_ms=clock.now_ms())
        )
        context = lumen._context("BTC-USD")
        assert [h["headline"] for h in context["recent_headlines"]] == ["new"]


class TestDataIntegrity:
    async def test_events_are_still_recorded_after_a_kill_switch(self, platform):
        await run_platform(platform, 200)
        await platform.kill_switch.engage("MANUAL")
        for _ in range(20):
            platform.clock.advance(100)
            await platform.step_market(1)
            await platform.orchestrator.tick()
        await platform.recorder.flush()
        assert platform.recorder.events_recorded > 0
        assert await platform.store.count(platform.session_id) > 0

    async def test_fills_are_never_lost_between_oms_and_account(self, platform):
        await run_platform(platform, 500)
        oms_fills = {f.fill_id for f in platform.oms.all_fills()}
        account_fills = {f.fill_id for f in platform.account.fill_log}
        assert oms_fills == account_fills
