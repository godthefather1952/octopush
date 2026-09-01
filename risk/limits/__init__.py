"""Deterministic limit checks.

Each function is a pure predicate over state and configuration, returning a
:class:`GateCheck`.  No function here consults a model, reads the network, or
takes a probability.  RUNE-CORE simply runs them all.
"""

from __future__ import annotations

from collections.abc import Callable

from core.config import RiskLimits
from core.models.common import Millis
from core.models.opportunity import TradeIntent
from core.models.ops import HealthStatus, KillSwitchState, SystemHealth
from core.models.portfolio import PortfolioState
from core.models.risk import GateCheck, GateResult


def _check(
    name: str,
    ok: bool,
    *,
    observed: float | None = None,
    limit: float | None = None,
    detail: str = "",
    mandatory: bool = True,
) -> GateCheck:
    return GateCheck(
        name=name,
        result=GateResult.PASS if ok else GateResult.FAIL,
        mandatory=mandatory,
        detail=detail,
        observed=observed,
        limit=limit,
    )


def _unknown(name: str, detail: str, *, mandatory: bool = True) -> GateCheck:
    """An unevaluable gate. Mandatory gates treat UNKNOWN as blocking."""
    return GateCheck(name=name, result=GateResult.UNKNOWN, mandatory=mandatory, detail=detail)


# --------------------------------------------------------------------------
# Individual gates
# --------------------------------------------------------------------------


def gate_min_edge(intent: TradeIntent, limits: RiskLimits) -> GateCheck:
    return _check(
        "MIN_EXPECTED_EDGE",
        intent.expected_net_edge_bps >= limits.min_expected_edge_bps,
        observed=intent.expected_net_edge_bps,
        limit=limits.min_expected_edge_bps,
        detail="expected net edge after all modelled costs",
    )


def gate_order_notional(intent: TradeIntent, limits: RiskLimits) -> GateCheck:
    return _check(
        "MAX_ORDER_NOTIONAL",
        intent.notional <= limits.max_order_notional,
        observed=intent.notional,
        limit=limits.max_order_notional,
    )


def gate_data_age(intent: TradeIntent, limits: RiskLimits, now_ms: Millis) -> GateCheck:
    if intent.source_data_timestamp is None:
        return _unknown("MARKET_DATA_FRESH", "intent carries no source data timestamp")
    age = now_ms - intent.source_data_timestamp
    return _check(
        "MARKET_DATA_FRESH",
        age <= limits.max_data_age_ms,
        observed=float(age),
        limit=float(limits.max_data_age_ms),
        detail=f"market data age {age}ms",
    )


def gate_deadline(intent: TradeIntent, now_ms: Millis) -> GateCheck:
    return _check(
        "INTENT_NOT_EXPIRED",
        now_ms <= intent.deadline_ms,
        observed=float(now_ms),
        limit=float(intent.deadline_ms),
    )


def gate_position_notional(
    intent: TradeIntent, portfolio: PortfolioState, limits: RiskLimits
) -> GateCheck:
    """Largest post-trade single-position notional across the intent's legs."""
    worst = 0.0
    for leg in intent.legs:
        key = f"{leg.venue}:{leg.symbol}"
        position = portfolio.positions.get(key)
        current = position.notional if position else 0.0
        worst = max(worst, current + intent.notional)
    return _check(
        "MAX_POSITION_NOTIONAL",
        worst <= limits.max_position_notional,
        observed=worst,
        limit=limits.max_position_notional,
    )


def gate_gross_exposure(
    intent: TradeIntent, portfolio: PortfolioState, limits: RiskLimits
) -> GateCheck:
    projected = portfolio.gross_exposure + intent.notional * len(intent.legs)
    return _check(
        "MAX_GROSS_EXPOSURE",
        projected <= limits.max_gross_exposure,
        observed=projected,
        limit=limits.max_gross_exposure,
    )


def gate_net_exposure(
    intent: TradeIntent, portfolio: PortfolioState, limits: RiskLimits
) -> GateCheck:
    """Net exposure after the intent, assuming its legs offset as designed."""
    delta = sum(leg.side.sign * intent.notional for leg in intent.legs)
    projected = abs(portfolio.net_exposure + delta)
    return _check(
        "MAX_NET_EXPOSURE",
        projected <= limits.max_net_exposure,
        observed=projected,
        limit=limits.max_net_exposure,
    )


def gate_leverage(
    intent: TradeIntent, portfolio: PortfolioState, limits: RiskLimits
) -> GateCheck:
    equity = portfolio.equity
    if equity <= 0:
        return _check("MAX_LEVERAGE", False, observed=0.0, limit=limits.max_leverage,
                      detail="non-positive equity")
    projected = (portfolio.gross_exposure + intent.notional * len(intent.legs)) / equity
    return _check(
        "MAX_LEVERAGE",
        projected <= limits.max_leverage,
        observed=projected,
        limit=limits.max_leverage,
    )


def gate_venue_exposure(
    intent: TradeIntent, portfolio: PortfolioState, limits: RiskLimits
) -> GateCheck:
    exposure = portfolio.exposure_by_venue()
    worst = 0.0
    for leg in intent.legs:
        worst = max(worst, exposure.get(leg.venue, 0.0) + intent.notional)
    return _check(
        "MAX_VENUE_EXPOSURE",
        worst <= limits.max_venue_exposure,
        observed=worst,
        limit=limits.max_venue_exposure,
    )


def gate_strategy_exposure(
    intent: TradeIntent, strategy_exposure: float, limits: RiskLimits
) -> GateCheck:
    projected = strategy_exposure + intent.notional * len(intent.legs)
    return _check(
        "MAX_STRATEGY_EXPOSURE",
        projected <= limits.max_strategy_exposure,
        observed=projected,
        limit=limits.max_strategy_exposure,
    )


def gate_daily_loss(portfolio: PortfolioState, limits: RiskLimits) -> GateCheck:
    loss = max(0.0, -portfolio.day_realized_pnl)
    return _check(
        "MAX_DAILY_LOSS",
        loss < limits.max_daily_loss,
        observed=loss,
        limit=limits.max_daily_loss,
    )


def gate_drawdown(portfolio: PortfolioState, limits: RiskLimits) -> GateCheck:
    return _check(
        "MAX_DRAWDOWN",
        portfolio.drawdown < limits.max_drawdown,
        observed=portfolio.drawdown,
        limit=limits.max_drawdown,
    )


def gate_unhedged(unhedged_notional: float, limits: RiskLimits) -> GateCheck:
    return _check(
        "MAX_UNHEDGED_EXPOSURE",
        abs(unhedged_notional) <= limits.max_unhedged_notional,
        observed=abs(unhedged_notional),
        limit=limits.max_unhedged_notional,
    )


def gate_open_orders(open_orders: int, limits: RiskLimits) -> GateCheck:
    return _check(
        "MAX_OPEN_ORDERS",
        open_orders < limits.max_open_orders,
        observed=float(open_orders),
        limit=float(limits.max_open_orders),
    )


def gate_error_rate(error_rate: float, limits: RiskLimits) -> GateCheck:
    return _check(
        "MAX_ERROR_RATE",
        error_rate <= limits.max_error_rate,
        observed=error_rate,
        limit=limits.max_error_rate,
    )


def gate_kill_switch(kill: KillSwitchState) -> GateCheck:
    return _check(
        "KILL_SWITCH_CLEAR",
        kill.trading_allowed,
        detail=",".join(kill.triggered_by) if kill.triggered_by else "",
    )


def gate_system_health(health: SystemHealth | None, required: list[str]) -> GateCheck:
    if health is None:
        return _unknown("SYSTEM_HEALTHY", "no health snapshot available")
    ok, bad = health.required_ok(required)
    return _check(
        "SYSTEM_HEALTHY",
        ok,
        detail="unhealthy: " + ",".join(bad) if bad else "",
    )


def gate_execution_health(health: SystemHealth | None) -> GateCheck:
    if health is None:
        return _unknown("EXECUTION_HEALTHY", "no health snapshot available")
    veska = health.components.get("VESKA")
    if veska is None:
        return _unknown("EXECUTION_HEALTHY", "VESKA has never heartbeat")
    return _check(
        "EXECUTION_HEALTHY",
        veska.status is HealthStatus.HEALTHY,
        detail=veska.detail,
    )


def gate_consensus(
    intent: TradeIntent, threshold: float, *, complete: bool
) -> GateCheck:
    """Consensus as a gate.

    Consensus passing is necessary but never sufficient — and an incomplete
    consensus (a required agent missing) fails here regardless of its score.
    """
    if not complete:
        return _check(
            "CONSENSUS_COMPLETE",
            False,
            observed=intent.consensus_agreement,
            limit=threshold,
            detail="a required agent was missing, stale or unavailable",
        )
    return _check(
        "CONSENSUS_THRESHOLD",
        intent.consensus_agreement >= threshold,
        observed=intent.consensus_agreement,
        limit=threshold,
    )


def gate_liquidity(max_economical_notional: float | None, intent: TradeIntent) -> GateCheck:
    if max_economical_notional is None:
        return _unknown("LIQUIDITY_SUFFICIENT", "ZEPHR produced no sizing curve")
    return _check(
        "LIQUIDITY_SUFFICIENT",
        max_economical_notional >= intent.notional,
        observed=max_economical_notional,
        limit=intent.notional,
    )


def gate_hedge_available(hedge_available: bool) -> GateCheck:
    return _check(
        "HEDGE_AVAILABLE",
        hedge_available,
        detail="an offsetting venue must be quoting for a delta-neutral trade",
    )


Gate = Callable[..., GateCheck]

__all__ = [
    "Gate",
    "GateCheck",
    "GateResult",
    "gate_consensus",
    "gate_daily_loss",
    "gate_data_age",
    "gate_deadline",
    "gate_drawdown",
    "gate_error_rate",
    "gate_execution_health",
    "gate_gross_exposure",
    "gate_hedge_available",
    "gate_kill_switch",
    "gate_leverage",
    "gate_liquidity",
    "gate_min_edge",
    "gate_net_exposure",
    "gate_open_orders",
    "gate_order_notional",
    "gate_position_notional",
    "gate_strategy_exposure",
    "gate_system_health",
    "gate_unhedged",
    "gate_venue_exposure",
]
