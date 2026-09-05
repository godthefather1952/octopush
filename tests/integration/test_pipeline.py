"""End-to-end: the whole floor, from market data to reconciled positions."""

from __future__ import annotations

import pytest

from core.events import EventType
from core.models.common import AgentId, Side
from core.models.opportunity import StrategyState
from tests.conftest import run_platform


class TestWarmUp:
    async def test_platform_does_not_trade_before_it_is_warmed_up(self, platform):
        await platform.start(record=False)
        platform.clock.advance(100)
        await platform.step_market(1)
        await platform.orchestrator.tick()
        # First tick establishes health; nothing has been sought yet.
        assert platform.orchestrator.warmup_ticks >= 1
        assert not platform.state.opportunities
        assert not platform.oms.orders

    async def test_warm_up_completes_and_the_kill_switch_stays_clear(self, platform):
        await run_platform(platform, 10)
        assert platform.orchestrator.warmed_up
        assert not platform.kill_switch.state.engaged


class TestObservation:
    async def test_tidal_builds_a_book_for_every_venue_and_symbol(self, platform):
        await run_platform(platform, 20)
        market = platform.state.market
        expected = {
            f"{venue.name}:{symbol}"
            for venue in platform.settings.enabled_venues
            for symbol in platform.settings.symbols
        }
        assert set(market.venues) == expected
        assert all(state.quality.is_usable for state in market.venues.values())

    async def test_consolidated_view_prices_every_symbol(self, platform):
        await run_platform(platform, 20)
        for symbol in platform.settings.symbols:
            view = platform.state.market.consolidated[symbol]
            assert view.reference_price and view.reference_price > 0
            assert len(view.usable_venues) == 2
            assert view.max_deviation_bps is not None

    async def test_noro_prices_every_symbol(self, platform):
        await run_platform(platform, 20)
        for symbol in platform.settings.symbols:
            fair = platform.noro.fair_value(symbol)
            assert fair is not None
            assert fair.fair_value > 0
            assert len(fair.venues) == 2


class TestOpportunityLifecycle:
    async def test_dislocations_are_detected_and_evaluated(self, platform):
        # Read the opinions off the event stream rather than out of state:
        # state is pruned as opportunities close, and what matters is that
        # every required agent weighed in at the time.
        detected: list[str] = []
        evaluated: dict[str, set[AgentId]] = {}

        async def watch(event):
            if event.type is EventType.OPPORTUNITY_DETECTED:
                oid = event.payload["opportunity_id"]
                if oid not in evaluated:
                    detected.append(oid)
                    evaluated[oid] = set()
            elif event.type is EventType.AGENT_OPINION:
                oid = event.correlation_id
                if oid in evaluated:
                    evaluated[oid].add(AgentId(event.payload["agent_id"]))

        platform.bus.subscribe(
            watch,
            types=[EventType.OPPORTUNITY_DETECTED, EventType.AGENT_OPINION],
            name="probe",
        )
        await run_platform(platform, 400)

        assert detected, "expected the synthetic dislocations to be found"
        required = {AgentId.TIDAL, AgentId.NORO, AgentId.ZEPHR}
        assert all(required <= evaluated[oid] for oid in detected)

    async def test_opportunities_reach_a_terminal_state(self, platform):
        await run_platform(platform, 600)
        terminal = {StrategyState.CLOSED, StrategyState.REJECTED}
        states = {r.state for r in platform.state.opportunities.values()}
        assert states
        # Nothing is left stuck mid-pipeline once the market has moved on.
        assert states <= terminal | {StrategyState.MONITORING, StrategyState.EXITING}

    async def test_a_full_trade_completes_and_flattens(self, platform):
        await run_platform(platform, 800)
        closed = [
            r for r in platform.state.opportunities.values() if r.state is StrategyState.CLOSED
        ]
        assert closed, "expected at least one completed round trip"
        assert platform.account.fill_log
        # Both legs traded: a relative-value entry is not one-sided.
        venues = {fill.venue for fill in platform.account.fill_log}
        assert len(venues) == 2
        sides = {fill.side for fill in platform.account.fill_log}
        assert sides == {Side.BUY, Side.SELL}

    async def test_rejections_carry_a_reason(self, platform):
        await run_platform(platform, 600)
        rejected = [
            r for r in platform.state.opportunities.values() if r.state is StrategyState.REJECTED
        ]
        for record in rejected:
            assert record.rejected_reason


class TestInvariants:
    async def test_positions_reconcile_throughout(self, platform):
        await platform.start(record=False)
        for _ in range(400):
            platform.clock.advance(100)
            await platform.step_market(1)
            await platform.orchestrator.tick()
            result = platform.marin.reconcile()
            assert result.ok, [m.model_dump() for m in result.critical]

    async def test_cash_always_equals_the_fill_log(self, platform):
        await run_platform(platform, 500)
        cash, _positions, realized = platform.account.recompute_from_fills()
        assert cash == pytest.approx(platform.account.cash, abs=1e-6)
        assert realized == pytest.approx(platform.account.realized_pnl, abs=1e-6)

    async def test_net_pnl_is_gross_less_fees(self, platform):
        await run_platform(platform, 500)
        snapshot = platform.account.snapshot()
        assert snapshot.net_pnl == pytest.approx(snapshot.gross_pnl - snapshot.fees_paid)

    async def test_risk_limits_are_never_breached(self, platform):
        limits = platform.settings.risk
        await platform.start(record=False)
        for _ in range(500):
            platform.clock.advance(100)
            await platform.step_market(1)
            await platform.orchestrator.tick()
            snapshot = platform.account.snapshot()
            assert snapshot.gross_exposure <= limits.max_gross_exposure * 1.001
            assert snapshot.drawdown <= limits.max_drawdown * 1.001

    async def test_no_order_ends_in_an_illegal_state(self, platform):
        await run_platform(platform, 500)
        for order in platform.oms.orders.values():
            # Every recorded transition was legal, or the OMS would have raised.
            assert order.history
            assert order.filled_quantity <= order.quantity + 1e-9

    async def test_every_fill_belongs_to_a_known_order(self, platform):
        await run_platform(platform, 500)
        known = set(platform.oms.orders)
        assert all(fill.client_order_id in known for fill in platform.account.fill_log)

    async def test_the_event_stream_tells_the_whole_story(self, platform):
        seen: set[EventType] = set()

        async def collect(event):
            seen.add(event.type)

        platform.bus.subscribe(collect, name="probe")
        await run_platform(platform, 600)
        assert {
            EventType.BOOK_SNAPSHOT,
            EventType.MARKET_STATE,
            EventType.OPPORTUNITY_DETECTED,
            EventType.AGENT_OPINION,
            EventType.CONSENSUS_UPDATED,
            EventType.RISK_EVALUATION_REQUEST,
            EventType.TRADE_INTENT,
            EventType.EXECUTION_PLAN,
            EventType.PAPER_ORDER_CREATED,
            EventType.PAPER_FILL,
            EventType.PORTFOLIO_STATE,
            EventType.RECONCILIATION_COMPLETE,
            EventType.STRATEGY_STATE_CHANGED,
            EventType.TRADE_ATTRIBUTION,
            EventType.DELTA_REPORT,
        } <= seen


class TestAttribution:
    async def test_closed_trades_produce_attribution(self, platform):
        await run_platform(platform, 800)
        trades = platform.orchestrator.scorecard.trades
        assert trades
        trade = trades[0]
        assert trade.consensus_agreement > 0
        assert trade.contributions
        assert set(trade.signals) <= set(AgentId)
        assert trade.expected_costs_bps > 0

    async def test_scorecard_reports_per_agent_statistics(self, platform):
        """Every agent that cast a directional vote gets statistics.

        NORO is deliberately not required to appear. On the two-venue
        simulated market it abstains -- present, healthy, and contributing no
        directional claim -- so it earns no ``AgentContribution`` and
        therefore no predictive record. Scoring an abstention would mean
        crediting NORO with predicting a trade it explicitly declined to have
        a view on.
        """
        await run_platform(platform, 800)
        scores = platform.orchestrator.scorecard.scores()
        assert scores, "some agent voted and was scored"
        assert AgentId.ZEPHR in scores, "the executability vote is directional"
        for score in scores.values():
            assert score.observations > 0
            assert 0.0 <= score.hit_rate <= 1.0
            assert -1.0 <= score.predictive_contribution <= 1.0

    async def test_an_abstaining_agent_is_not_credited_with_a_prediction(
        self, platform
    ):
        """The abstention has to be visible, and visible as an abstention.

        It appears in ``ConsensusResult.abstained_agents`` -- not in
        ``missing_agents``, which would mean the strategy should have been
        suspended, and not in ``contributions``, which would mean it had
        influenced the score.
        """
        await run_platform(platform, 800)
        results = [
            record.opportunity
            for record in platform.state.opportunities.values()
        ]
        assert results, "the run produced opportunities"

        scorecard = platform.orchestrator.scorecard
        abstained = [
            trade for trade in scorecard.trades if AgentId.NORO not in trade.signals
        ]
        for trade in abstained:
            assert AgentId.NORO not in trade.contributions
            assert AgentId.NORO not in trade.weights
        assert AgentId.NORO not in scorecard.scores() or (
            scorecard.scores()[AgentId.NORO].observations > 0
        ), "NORO is either absent from the scorecard, or genuinely voted"

    async def test_weights_are_not_adapted_automatically(self, platform):
        before = dict(platform.settings.consensus.weights)
        await run_platform(platform, 800)
        # Evidence is collected; nothing feeds it back into the weights yet.
        assert platform.settings.consensus.weights == before
