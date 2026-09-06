"""RUNE-CORE: hard gates, headroom sizing, and the AI-cannot-override rule."""

from __future__ import annotations

import pytest

from agents.rune.ai import RiskCommentary, RuneAI
from agents.rune.core import RiskContext, RuneCore
from core.clock import ManualClock
from core.config import RiskLimits
from core.models.common import Side
from core.models.opportunity import CostBreakdown, OpportunityLeg, TradeIntent
from core.models.ops import HealthState, HealthStatus, KillSwitchState, SystemHealth
from core.models.portfolio import PortfolioState, PositionState
from core.models.risk import GateResult, RiskVerdict
from tests.conftest import START_MS

REQUIRED = ["TIDAL", "NORO", "ZEPHR", "RUNE", "VESKA", "MARIN"]


def healthy(*, veska=HealthStatus.HEALTHY) -> SystemHealth:
    components = {
        name: HealthState(
            service=name,
            status=veska if name == "VESKA" else HealthStatus.HEALTHY,
            last_heartbeat_ms=START_MS,
        )
        for name in REQUIRED
    }
    return SystemHealth(created_at=START_MS, components=components)


def portfolio(**overrides) -> PortfolioState:
    defaults = dict(
        created_at=START_MS,
        initial_balance=100_000.0,
        cash=100_000.0,
        peak_equity=100_000.0,
    )
    defaults.update(overrides)
    return PortfolioState(**defaults)


def intent(**overrides) -> TradeIntent:
    defaults = dict(
        created_at=START_MS,
        source_data_timestamp=START_MS - 50,
        opportunity_id="opp-1",
        strategy="cross_venue",
        symbol="BTC-USD",
        legs=[
            OpportunityLeg(venue="VENUE_A", symbol="BTC-USD", side=Side.BUY, reference_price=100.0),
            OpportunityLeg(venue="VENUE_B", symbol="BTC-USD", side=Side.SELL, reference_price=101.0),
        ],
        notional=5_000.0,
        gross_edge_bps=30.0,
        costs=CostBreakdown(fees_bps=10.0),
        expected_net_edge_bps=20.0,
        consensus_score=0.8,
        consensus_agreement=0.8,
        max_slippage_bps=10.0,
        deadline_ms=START_MS + 2_000,
    )
    defaults.update(overrides)
    return TradeIntent(**defaults)


def context(**overrides) -> RiskContext:
    defaults = dict(
        portfolio=portfolio(),
        kill_switch=KillSwitchState(),
        health=healthy(),
        consensus_threshold=0.6,
        consensus_complete=True,
        required_components=REQUIRED,
        open_orders=0,
        error_rate=0.0,
        unhedged_notional=0.0,
        strategy_exposure=0.0,
        max_economical_notional=25_000.0,
        hedge_available=True,
    )
    defaults.update(overrides)
    return RiskContext(**defaults)


@pytest.fixture
def rune(clock: ManualClock) -> RuneCore:
    return RuneCore(RiskLimits(), clock)


def gate(decision, name):
    return next(g for g in decision.gates if g.name == name)


class TestApproval:
    def test_a_clean_intent_is_approved(self, rune):
        decision = rune.evaluate(intent(), context())
        assert decision.verdict is RiskVerdict.APPROVED
        assert decision.approved_notional == pytest.approx(5_000.0)
        assert not decision.failed_gates

    def test_every_gate_is_recorded_even_when_passing(self, rune):
        decision = rune.evaluate(intent(), context())
        names = {g.name for g in decision.gates}
        assert {
            "KILL_SWITCH_CLEAR",
            "SYSTEM_HEALTHY",
            "EXECUTION_HEALTHY",
            "MARKET_DATA_FRESH",
            "MIN_EXPECTED_EDGE",
            "LIQUIDITY_SUFFICIENT",
            "HEDGE_AVAILABLE",
            "MAX_DRAWDOWN",
        } <= names


class TestHardGates:
    def test_kill_switch_blocks(self, rune):
        decision = rune.evaluate(
            intent(),
            context(kill_switch=KillSwitchState(halt_new_trades=True, triggered_by=["MANUAL"])),
        )
        assert decision.verdict is RiskVerdict.REJECTED
        assert "KILL_SWITCH_CLEAR" in decision.reason_codes

    def test_stale_market_data_blocks(self, rune, clock):
        clock.advance(10_000)
        decision = rune.evaluate(intent(), context())
        assert decision.verdict is RiskVerdict.REJECTED
        assert "MARKET_DATA_FRESH" in decision.reason_codes

    def test_missing_source_timestamp_is_unknown_and_blocks(self, rune):
        decision = rune.evaluate(intent(source_data_timestamp=None), context())
        assert gate(decision, "MARKET_DATA_FRESH").result is GateResult.UNKNOWN
        assert decision.verdict is RiskVerdict.REJECTED

    def test_thin_edge_blocks(self, rune):
        decision = rune.evaluate(intent(expected_net_edge_bps=0.5), context())
        assert "MIN_EXPECTED_EDGE" in decision.reason_codes

    def test_negligible_liquidity_blocks(self, rune):
        # Below the minimum viable size there is nothing worth reducing to.
        decision = rune.evaluate(intent(), context(max_economical_notional=100.0))
        assert decision.verdict is RiskVerdict.REJECTED
        assert "MIN_TRADE_NOTIONAL" in decision.reason_codes

    def test_zero_liquidity_blocks(self, rune):
        decision = rune.evaluate(intent(), context(max_economical_notional=0.0))
        assert decision.verdict is RiskVerdict.REJECTED

    def test_unavailable_hedge_blocks(self, rune):
        decision = rune.evaluate(intent(), context(hedge_available=False))
        assert "HEDGE_AVAILABLE" in decision.reason_codes

    def test_unhealthy_execution_blocks(self, rune):
        decision = rune.evaluate(
            intent(), context(health=healthy(veska=HealthStatus.DEGRADED))
        )
        assert "EXECUTION_HEALTHY" in decision.reason_codes

    def test_missing_health_snapshot_blocks(self, rune):
        decision = rune.evaluate(intent(), context(health=None))
        assert gate(decision, "SYSTEM_HEALTHY").result is GateResult.UNKNOWN
        assert decision.verdict is RiskVerdict.REJECTED

    def test_incomplete_consensus_blocks_regardless_of_score(self, rune):
        decision = rune.evaluate(
            intent(consensus_agreement=0.99), context(consensus_complete=False)
        )
        assert "CONSENSUS_COMPLETE" in decision.reason_codes

    def test_consensus_below_threshold_blocks(self, rune):
        decision = rune.evaluate(intent(consensus_agreement=0.1), context())
        assert "CONSENSUS_THRESHOLD" in decision.reason_codes

    def test_expired_intent_blocks(self, rune, clock):
        clock.advance(3_000)
        decision = rune.evaluate(intent(source_data_timestamp=clock.now_ms()), context())
        assert "INTENT_NOT_EXPIRED" in decision.reason_codes

    def test_daily_loss_blocks(self, rune):
        decision = rune.evaluate(
            intent(), context(portfolio=portfolio(day_realized_pnl=-3_000.0))
        )
        assert "MAX_DAILY_LOSS" in decision.reason_codes

    def test_drawdown_blocks(self, rune):
        decision = rune.evaluate(
            intent(), context(portfolio=portfolio(cash=90_000.0, peak_equity=100_000.0))
        )
        assert "MAX_DRAWDOWN" in decision.reason_codes

    def test_unhedged_exposure_blocks(self, rune):
        """No trade is authorised while the residual is already past its limit.

        The rejection now arrives through MIN_TRADE_NOTIONAL rather than
        naming the gate: MAX_UNHEDGED_EXPOSURE gained a headroom solver in
        Phase 5 Remediation D (P5-18), so a state no reduction can rescue
        leaves zero headroom and ``evaluate`` short-circuits — the same shape
        every other size-sensitive limit already had.
        """
        decision = rune.evaluate(intent(), context(unhedged_notional=50_000.0))
        assert decision.verdict is RiskVerdict.REJECTED
        assert decision.approved_notional == 0.0

    def test_oversized_order_is_cut_to_the_tightest_limit(self, rune):
        """Under the shipped defaults the unhedged budget binds before the
        order limit does.

        ``max_order_notional`` is 25,000 and ``max_unhedged_notional`` is
        10,000, and a two-leg delta-neutral trade holds one whole leg of
        one-sided exposure between its first fill and its second — so the
        largest safe per-leg size is the unhedged budget divided by the
        slippage allowance, not the order limit (P5-18). Both gates pass on
        the sized intent, which is the property that matters.
        """
        decision = rune.evaluate(intent(notional=999_999.0), context())
        assert decision.verdict is RiskVerdict.APPROVED_REDUCED
        assert decision.approved_notional < rune.limits.max_order_notional
        assert decision.approved_notional == pytest.approx(
            rune.limits.max_unhedged_notional / 1.001
        )
        assert gate(decision, "MAX_ORDER_NOTIONAL").result is GateResult.PASS
        assert gate(decision, "MAX_UNHEDGED_EXPOSURE").result is GateResult.PASS

    def test_the_order_limit_still_binds_when_it_is_the_tightest(self, clock):
        """The control for the test above: with an ample unhedged budget the
        cut is back to ``max_order_notional``."""
        rune = RuneCore(RiskLimits(max_unhedged_notional=1_000_000.0), clock)
        decision = rune.evaluate(
            intent(notional=999_999.0), context(max_economical_notional=1_000_000.0)
        )
        assert decision.verdict is RiskVerdict.APPROVED_REDUCED
        assert decision.approved_notional == pytest.approx(
            rune.limits.max_order_notional
        )
        assert gate(decision, "MAX_ORDER_NOTIONAL").result is GateResult.PASS

    def test_venue_exposure_caps_the_size(self, clock):
        # Only the venue limit binds here: everything else has ample room.
        rune = RuneCore(
            RiskLimits(
                max_venue_exposure=41_000.0,
                max_position_notional=100_000.0,
                max_gross_exposure=1_000_000.0,
                # Coherence (P5-15): an order cap above the venue cap is not a
                # configuration. Held equal to the venue limit, which leaves it
                # non-binding here — 1,000 of venue room is what decides this.
                max_order_notional=41_000.0,
                max_net_exposure=100_000.0,
            ),
            clock,
        )
        crowded = portfolio(
            positions={
                "VENUE_A:BTC-USD": PositionState(
                    venue="VENUE_A",
                    symbol="BTC-USD",
                    quantity=1.0,
                    average_entry_price=40_000.0,
                    mark_price=40_000.0,
                )
            }
        )
        decision = rune.evaluate(intent(), context(portfolio=crowded))
        assert decision.verdict is RiskVerdict.APPROVED_REDUCED
        # 41,000 venue limit less 40,000 already deployed.
        assert decision.approved_notional == pytest.approx(1_000.0)

    def test_position_already_over_the_limit_leaves_no_headroom(self, rune):
        over = portfolio(
            positions={
                "VENUE_A:BTC-USD": PositionState(
                    venue="VENUE_A",
                    symbol="BTC-USD",
                    quantity=1.0,
                    average_entry_price=74_000.0,
                    mark_price=74_000.0,
                )
            }
        )
        decision = rune.evaluate(intent(), context(portfolio=over))
        assert decision.verdict is RiskVerdict.REJECTED
        assert decision.approved_notional == 0.0

    def test_too_many_open_orders_blocks(self, rune):
        decision = rune.evaluate(intent(), context(open_orders=999))
        assert "MAX_OPEN_ORDERS" in decision.reason_codes

    def test_high_error_rate_blocks(self, rune):
        decision = rune.evaluate(intent(), context(error_rate=0.9))
        assert "MAX_ERROR_RATE" in decision.reason_codes

    def test_one_failing_gate_is_enough(self, rune):
        """No amount of consensus outvotes a hard gate."""
        decision = rune.evaluate(
            intent(consensus_agreement=1.0, expected_net_edge_bps=1_000.0),
            context(hedge_available=False),
        )
        assert decision.verdict is RiskVerdict.REJECTED


class TestHeadroom:
    def test_size_is_reduced_to_fit_the_limit(self, clock):
        rune = RuneCore(RiskLimits(max_order_notional=3_000.0, max_position_notional=3_000.0), clock)
        decision = rune.evaluate(intent(notional=2_500.0), context(max_economical_notional=1_200.0))
        assert decision.verdict is RiskVerdict.APPROVED_REDUCED
        assert decision.approved_notional == pytest.approx(1_200.0)
        assert "SIZE_REDUCED_BY_HEADROOM" in decision.reason_codes

    def test_approved_notional_never_exceeds_the_request(self, rune):
        decision = rune.evaluate(intent(notional=1_000.0), context())
        assert decision.approved_notional == pytest.approx(1_000.0)


class TestRuneAI:
    async def test_ai_commentary_cannot_flip_a_rejection(self, rune, bus, clock, settings, health):
        from agents.lumen.provider import ScriptedProvider
        from agents.rune.agent import Rune

        ai = RuneAI(
            ScriptedProvider(
                [{"concern_level": 0.0, "regime": "CALM", "commentary": "all clear", "reason_codes": []}]
            )
        )
        await ai.assess({})
        agent = Rune(bus, clock, settings, health, ai=ai)
        decision = await agent.evaluate(intent(), context(hedge_available=False))
        # The AI is relaxed; the deterministic gate still rejects.
        assert decision.verdict is RiskVerdict.REJECTED
        assert decision.ai_concern_level == pytest.approx(0.0)

    async def test_ai_commentary_cannot_block_an_approval(self, bus, clock, settings, health):
        from agents.lumen.provider import ScriptedProvider
        from agents.rune.agent import Rune

        ai = RuneAI(
            ScriptedProvider(
                [
                    {
                        "concern_level": 1.0,
                        "regime": "DISLOCATED",
                        "commentary": "extremely dangerous",
                        "reason_codes": ["PANIC"],
                    }
                ]
            )
        )
        await ai.assess({})
        agent = Rune(bus, clock, settings, health, ai=ai)
        decision = await agent.evaluate(intent(), context())
        assert decision.approved
        assert decision.ai_concern_level == pytest.approx(1.0)
        assert "AI:PANIC" in decision.reason_codes

    async def test_provider_failure_leaves_the_verdict_intact(self, bus, clock, settings, health):
        from agents.lumen.provider import NullProvider
        from agents.rune.agent import Rune

        ai = RuneAI(NullProvider())
        commentary = await ai.assess({})
        assert isinstance(commentary, RiskCommentary) and not commentary.ok
        agent = Rune(bus, clock, settings, health, ai=ai)
        decision = await agent.evaluate(intent(), context())
        assert decision.approved
        assert decision.ai_commentary is None
