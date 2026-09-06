"""RUNE-CORE — deterministic risk.

Ordinary code, no probability, no model, no network.  Every mandatory gate must
return PASS for a trade to proceed; an UNKNOWN mandatory gate blocks, because
a gate that could not be evaluated has not been satisfied.

Nothing in this module can be overridden by RUNE-AI, by consensus, or by any
other component.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from core.clock import Clock
from core.config import RiskLimits
from core.models.common import Millis, new_id
from core.models.opportunity import TradeIntent
from core.models.ops import KillSwitchState, SystemHealth
from core.models.portfolio import PortfolioState
from core.models.risk import (
    GateCheck,
    GateResult,
    RiskDecision,
    RiskUtilization,
    RiskVerdict,
)
from risk import limits as gates

VERSION = "rune-core-0.1"


@dataclass
class RiskContext:
    """Everything RUNE-CORE needs, gathered by the orchestrator.

    CANONICAL NOTIONAL UNIT
    =======================
    ``TradeIntent.notional`` and ``RiskDecision.approved_notional`` are the
    *per-leg* quote notional: VESKA sizes every leg as
    ``notional / expected_price``, so an N-leg trade puts ``notional`` on each
    of N venues. Its gross contribution — to the portfolio, to a venue, to a
    strategy budget — is therefore ``notional * N``.
    """

    portfolio: PortfolioState
    kill_switch: KillSwitchState
    health: SystemHealth | None
    consensus_threshold: float
    consensus_complete: bool
    required_components: list[str] = field(default_factory=list)
    open_orders: int = 0
    error_rate: float = 0.0
    unhedged_notional: float = 0.0
    #: GROSS quote exposure this strategy already has working, summed across
    #: every leg of every trade — NOT a sum of per-leg notionals. The incoming
    #: intent is projected at ``notional * legs``, so a per-leg sum here would
    #: compare two different units and authorise roughly ``leg count`` times
    #: the configured budget (P5-2). Built by
    #: ``Orchestrator._current_strategy_exposure``.
    strategy_exposure: float = 0.0
    max_economical_notional: float | None = None
    hedge_available: bool = True


class RuneCore:
    """Runs every deterministic gate and issues the RiskDecision."""

    def __init__(self, limits: RiskLimits, clock: Clock) -> None:
        self.limits = limits
        self.clock = clock
        self.evaluations = 0
        self.rejections = 0

    def evaluate(
        self, intent: TradeIntent, ctx: RiskContext, now_ms: Millis | None = None
    ) -> RiskDecision:
        """Size the intent to fit every limit, then gate the sized intent.

        Order matters.  Sizing first means a trade that is merely *too large*
        is cut down rather than thrown away, while the gates still run — and
        still have the final word — against the size actually proposed.  A
        size-based gate can therefore only fail if the reduction could not
        make it pass, which is exactly when rejection is the right answer.
        """
        # Risk gating is economic control flow: the data-age and deadline
        # gates below decide whether the trade happens at all. The tick
        # supplies its canonical time so the answer depends only on the
        # logical instant the tick is deciding at, never on how far a
        # concurrent feed happened to advance the clock first (Phase 2
        # Batch 1.4).
        now: Millis = self.clock.now_ms() if now_ms is None else now_ms

        headroom = self._headroom(intent, ctx)
        sized = (
            intent
            if headroom >= intent.notional - 1e-9
            else intent.model_copy(update={"notional": headroom})
        )
        reduced = sized.notional < intent.notional - 1e-9

        if headroom < self.limits.min_trade_notional:
            self.evaluations += 1
            self.rejections += 1
            check = GateCheck(
                name="MIN_TRADE_NOTIONAL",
                result=GateResult.FAIL,
                mandatory=True,
                observed=headroom,
                limit=self.limits.min_trade_notional,
                detail="remaining headroom is too small to be worth trading",
            )
            return RiskDecision(
                created_at=now,
                source_data_timestamp=intent.source_data_timestamp,
                correlation_id=intent.correlation_id,
                decision_id=new_id("risk"),
                intent_id=intent.intent_id,
                strategy=intent.strategy,
                symbol=intent.symbol,
                verdict=RiskVerdict.REJECTED,
                approved_notional=0.0,
                requested_notional=intent.notional,
                gates=[check],
                reason_codes=[check.name],
            )

        return self._gate(intent, sized, ctx, now, reduced=reduced)

    def _gate(
        self,
        original: TradeIntent,
        intent: TradeIntent,
        ctx: RiskContext,
        now: Millis,
        *,
        reduced: bool,
    ) -> RiskDecision:
        checks: list[GateCheck] = [
            gates.gate_kill_switch(ctx.kill_switch),
            gates.gate_system_health(ctx.health, ctx.required_components),
            gates.gate_execution_health(ctx.health),
            gates.gate_consensus(
                intent, ctx.consensus_threshold, complete=ctx.consensus_complete
            ),
            gates.gate_data_age(intent, self.limits, now),
            gates.gate_deadline(intent, now),
            gates.gate_min_edge(intent, self.limits),
            gates.gate_liquidity(ctx.max_economical_notional, intent),
            gates.gate_hedge_available(ctx.hedge_available),
            gates.gate_order_notional(intent, self.limits),
            gates.gate_position_notional(intent, ctx.portfolio, self.limits),
            gates.gate_gross_exposure(intent, ctx.portfolio, self.limits),
            gates.gate_net_exposure(intent, ctx.portfolio, self.limits),
            gates.gate_leverage(intent, ctx.portfolio, self.limits),
            gates.gate_venue_exposure(intent, ctx.portfolio, self.limits),
            gates.gate_strategy_exposure(intent, ctx.strategy_exposure, self.limits),
            gates.gate_daily_loss(ctx.portfolio, self.limits),
            gates.gate_drawdown(ctx.portfolio, self.limits),
            gates.gate_unhedged(ctx.unhedged_notional, self.limits),
            # The SIZED intent's leg count, which is also the original's:
            # reducing a notional never changes how many orders VESKA plans.
            gates.gate_open_orders(ctx.open_orders, len(intent.legs), self.limits),
            gates.gate_error_rate(ctx.error_rate, self.limits),
        ]

        self.evaluations += 1
        blocking = [check for check in checks if check.blocking]
        if blocking:
            self.rejections += 1
            return RiskDecision(
                created_at=now,
                source_data_timestamp=intent.source_data_timestamp,
                correlation_id=intent.correlation_id,
                decision_id=new_id("risk"),
                intent_id=original.intent_id,
                strategy=intent.strategy,
                symbol=intent.symbol,
                verdict=RiskVerdict.REJECTED,
                approved_notional=0.0,
                requested_notional=original.notional,
                gates=checks,
                reason_codes=[check.name for check in blocking],
            )

        verdict = RiskVerdict.APPROVED_REDUCED if reduced else RiskVerdict.APPROVED
        return RiskDecision(
            created_at=now,
            source_data_timestamp=intent.source_data_timestamp,
            correlation_id=intent.correlation_id,
            decision_id=new_id("risk"),
            intent_id=original.intent_id,
            strategy=intent.strategy,
            symbol=intent.symbol,
            verdict=verdict,
            approved_notional=intent.notional,
            requested_notional=original.notional,
            gates=checks,
            reason_codes=["ALL_GATES_PASSED"]
            + (["SIZE_REDUCED_BY_HEADROOM"] if reduced else []),
        )

    def _headroom(self, intent: TradeIntent, ctx: RiskContext) -> float:
        """Largest per-leg notional that still satisfies every size-based limit.

        Gates have already passed at the requested size, so this only ever
        reduces; it exists so that a trade close to a limit is cut down rather
        than rejected outright.

        EVERY SIZE-REDUCIBLE GATE MUST APPEAR HERE
        ==========================================
        :meth:`evaluate` promises that a size-based gate "can only fail if the
        reduction could not make it pass". That is only true if this function
        mirrors every gate whose outcome depends on ``notional``. Two were
        missing — MAX_NET_EXPOSURE and MAX_LEVERAGE — so a trade breaching
        either was rejected outright where a smaller one would have been
        authorised (P5-7). Both are now solved for directly, next to the gates
        they mirror, in :mod:`risk.limits`.

        The one deliberate exclusion is MAX_OPEN_ORDERS: an intent creates one
        order per leg regardless of its notional, so no reduction can make that
        gate pass and rejection is the only correct answer.

        Grouping matters as much as inclusion. Venue and position candidates
        are computed per *group* rather than per leg, using the same helpers
        the gates use, so two legs sharing a venue consume that venue's
        headroom twice (P5-11). Appending one full remaining amount per leg
        would have described a different model from the gate that judges the
        result.
        """
        legs = max(1, len(intent.legs))
        candidates = [
            intent.notional,
            self.limits.max_order_notional,
            (self.limits.max_gross_exposure - ctx.portfolio.gross_exposure) / legs,
            # ``ctx.strategy_exposure`` is gross strategy exposure across all
            # legs of every working trade, the same unit the gate projects into.
            (self.limits.max_strategy_exposure - ctx.strategy_exposure) / legs,
            gates.net_exposure_headroom(intent, ctx.portfolio, self.limits),
            gates.leverage_headroom(intent, ctx.portfolio, self.limits),
        ]

        exposure = ctx.portfolio.exposure_by_venue()
        for venue, leg_count in gates.legs_per_venue(intent).items():
            remaining = self.limits.max_venue_exposure - exposure.get(venue, 0.0)
            candidates.append(remaining / leg_count)

        for key, leg_count in gates.legs_per_position(intent).items():
            position = ctx.portfolio.positions.get(key)
            current = position.notional if position else 0.0
            remaining = self.limits.max_position_notional - current
            candidates.append(remaining / leg_count)

        if ctx.max_economical_notional is not None:
            candidates.append(ctx.max_economical_notional)
        return max(0.0, min(candidates))

    def utilization(
        self,
        portfolio: PortfolioState,
        unhedged_notional: float,
        strategy_exposure: dict[str, float],
    ) -> RiskUtilization:
        """The one canonical risk-utilization snapshot.

        Takes exactly the inputs it actually uses -- ``portfolio`` and
        ``unhedged_notional`` -- rather than a whole :class:`RiskContext`
        (which also carries kill-switch/health/consensus fields this
        calculation never reads). That makes it callable on every tick from
        current state alone, not only from inside a new-trade risk
        evaluation: the orchestrator's per-tick refresh
        (``Orchestrator._refresh_risk_utilization``) and ``_risk_check``'s
        own call (via ``ctx.unhedged_notional``) both go through this same
        method, so there is exactly one risk-utilization formula regardless
        of which caller asks for it.
        """
        return RiskUtilization(
            gross_exposure=portfolio.gross_exposure,
            max_gross_exposure=self.limits.max_gross_exposure,
            net_exposure=portfolio.net_exposure,
            max_net_exposure=self.limits.max_net_exposure,
            day_loss=max(0.0, -portfolio.day_realized_pnl),
            max_day_loss=self.limits.max_daily_loss,
            drawdown=portfolio.drawdown,
            max_drawdown=self.limits.max_drawdown,
            unhedged_notional=abs(unhedged_notional),
            max_unhedged_notional=self.limits.max_unhedged_notional,
            venue_exposure=portfolio.exposure_by_venue(),
            max_venue_exposure=self.limits.max_venue_exposure,
            strategy_exposure=strategy_exposure,
            max_strategy_exposure=self.limits.max_strategy_exposure,
        )
