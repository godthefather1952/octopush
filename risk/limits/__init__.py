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
# Leg grouping
# --------------------------------------------------------------------------
#
# CANONICAL NOTIONAL UNIT
# =======================
# ``TradeIntent.notional`` and ``RiskDecision.approved_notional`` are the
# *per-leg* quote notional. VESKA sizes every leg as
# ``notional / expected_price``, so an N-leg intent puts ``notional`` on each
# of N venues and contributes ``notional * N`` of gross exposure. Every
# projection below is written in that unit.
#
# A consequence that used to be missed: if two legs route to the SAME venue,
# that venue receives ``2 * notional``, not ``notional``. Taking ``max`` over
# legs answered "what is the largest single leg's effect", which is not the
# question a venue limit asks. Grouping first makes the projection describe
# what the venue would actually hold (P5-11).


def legs_per_venue(intent: TradeIntent) -> dict[str, int]:
    """How many of the intent's legs route to each venue."""
    counts: dict[str, int] = {}
    for leg in intent.legs:
        counts[leg.venue] = counts.get(leg.venue, 0) + 1
    return counts


def legs_per_position(intent: TradeIntent) -> dict[str, int]:
    """How many of the intent's legs land on each ``venue:symbol`` position."""
    counts: dict[str, int] = {}
    for leg in intent.legs:
        key = f"{leg.venue}:{leg.symbol}"
        counts[key] = counts.get(key, 0) + 1
    return counts


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
    """Largest post-trade single-position notional across the intent's legs.

    Legs are grouped by ``venue:symbol`` first, so two legs landing on one
    position add twice (P5-11). Taking ``max`` over ungrouped legs counted such
    a pair once and understated the position the trade would actually build.

    Deliberately conservative with opposing legs: a BUY and a SELL on the same
    position are added rather than netted. RUNE has no guaranteed fill sequence
    or per-leg quantity for a generic multi-leg intent, so it cannot know the
    two would offset — and overstating a position blocks a safe trade, while
    understating one authorises an unsafe one.
    """
    per_position = legs_per_position(intent)
    worst = 0.0
    for key, leg_count in per_position.items():
        position = portfolio.positions.get(key)
        current = position.notional if position else 0.0
        worst = max(worst, current + intent.notional * leg_count)
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


def net_exposure_coefficient(intent: TradeIntent) -> float:
    """How much the intent's net delta moves per unit of per-leg notional.

    ``sum(side.sign)`` over the legs: +1 per BUY, -1 per SELL. A balanced
    two-leg cross-venue trade has coefficient 0 — it adds no net delta at any
    size, which is exactly why net exposure cannot be reduced by shrinking it.
    """
    return float(sum(leg.side.sign for leg in intent.legs))


def gate_net_exposure(
    intent: TradeIntent, portfolio: PortfolioState, limits: RiskLimits
) -> GateCheck:
    """Net exposure after the intent, assuming its legs offset as designed."""
    delta = net_exposure_coefficient(intent) * intent.notional
    projected = abs(portfolio.net_exposure + delta)
    return _check(
        "MAX_NET_EXPOSURE",
        projected <= limits.max_net_exposure,
        observed=projected,
        limit=limits.max_net_exposure,
    )


def net_exposure_headroom(
    intent: TradeIntent, portfolio: PortfolioState, limits: RiskLimits
) -> float:
    """Largest per-leg notional keeping projected net exposure inside its limit.

    ``gate_net_exposure`` computes ``abs(current + coefficient * n) <= limit``.
    That is linear in ``n``, so the feasible set is an interval and its upper
    bound can be solved for directly rather than searched.

    Direction matters, which is why the naive ``limit - abs(current)`` is
    wrong: a trade whose legs point AWAY from an existing position reduces net
    exposure, and one pointing into it increases it. With ``current = +20,000``
    and ``limit = 25,000``, a SELL-heavy intent has far more room than a
    BUY-heavy one, and the naive form gives both the same 5,000.

    * ``coefficient == 0`` — a balanced intent adds no net delta at any size,
      so the constraint does not bind on ``n`` at all. Unbounded here; the gate
      still fails the trade if ``abs(current)`` alone already breaches, and no
      reduction could have helped.
    * Otherwise the roots ``(±limit - current) / coefficient`` bracket the
      feasible interval. Intersected with ``[0, inf)`` its upper bound is the
      answer, and an empty intersection means zero.

    Never negative, and never used to enlarge a request: the caller takes a
    ``min`` over every candidate including the requested notional. If the
    portfolio is already outside the permitted band and only a LARGER
    risk-reducing trade would re-enter it, this still returns a bound at or
    below the request — RUNE reduces, never enlarges — and the gate rejects.
    """
    current = portfolio.net_exposure
    limit = limits.max_net_exposure
    coefficient = net_exposure_coefficient(intent)

    if coefficient == 0:
        return float("inf")

    roots = sorted(
        ((-limit - current) / coefficient, (limit - current) / coefficient)
    )
    upper = roots[1]
    # The interval is [roots[0], roots[1]]; intersecting with [0, inf) is
    # empty when its upper bound is below zero.
    return max(0.0, upper)


def leverage_headroom(
    intent: TradeIntent, portfolio: PortfolioState, limits: RiskLimits
) -> float:
    """Largest per-leg notional keeping projected leverage inside its limit.

    ``gate_leverage`` computes ``(gross + n * legs) / equity <= max_leverage``.
    Every term but ``n`` is fixed at decision time, so this rearranges to
    ``n <= (max_leverage * equity - gross) / legs``.

    Non-positive equity yields zero: there is no size at which the gate can
    pass, and the gate itself remains the fail-closed authority.
    """
    equity = portfolio.equity
    if equity <= 0:
        return 0.0
    legs = max(1, len(intent.legs))
    allowed_gross = limits.max_leverage * equity - portfolio.gross_exposure
    return max(0.0, allowed_gross / legs)


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
    """Largest post-trade exposure on any one venue.

    Legs are grouped by venue first, so an intent routing two legs to one venue
    projects ``2 * notional`` onto it rather than ``notional`` (P5-11).
    """
    exposure = portfolio.exposure_by_venue()
    worst = 0.0
    for venue, leg_count in legs_per_venue(intent).items():
        worst = max(worst, exposure.get(venue, 0.0) + intent.notional * leg_count)
    return _check(
        "MAX_VENUE_EXPOSURE",
        worst <= limits.max_venue_exposure,
        observed=worst,
        limit=limits.max_venue_exposure,
    )


def gate_strategy_exposure(
    intent: TradeIntent, strategy_exposure: float, limits: RiskLimits
) -> GateCheck:
    """Gross quote exposure this strategy would hold across all its legs.

    ``strategy_exposure`` MUST already be gross strategy exposure — the sum of
    ``per-leg notional * leg count`` over every trade the strategy currently has
    working — not a sum of per-leg notionals. ``max_strategy_exposure`` is a
    gross budget, and the incoming intent is projected at ``notional * legs``,
    so a caller supplying per-leg sums would be comparing two different units
    and would authorise roughly ``leg count`` times the configured budget
    (P5-2). ``Orchestrator._current_strategy_exposure`` is the one place that
    computes it.
    """
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


def gate_open_orders(
    open_orders: int, incoming_orders: int, limits: RiskLimits
) -> GateCheck:
    """Order capacity after this intent is planned.

    The question a capacity limit asks is not "is there room for one more?" but
    "will the platform still be within its limit once this trade's orders
    exist?". ``Veska.build_plan`` emits one order per leg, so an intent adds
    ``len(intent.legs)`` orders, and the old ``open_orders < max_open_orders``
    let 19 live orders admit a two-leg trade and reach 21 (P5-4).

    ``observed`` is the PROJECTED count rather than the current one, so a
    rejection reads as "21 against a limit of 20" instead of "19 against a
    limit of 20", which said nothing about why it failed.

    Not size-reducible: shrinking the notional does not change how many orders
    an intent creates, so this gate has no headroom candidate.
    """
    projected = open_orders + incoming_orders
    return _check(
        "MAX_OPEN_ORDERS",
        projected <= limits.max_open_orders,
        observed=float(projected),
        limit=float(limits.max_open_orders),
        detail=(
            f"{open_orders} live + {incoming_orders} incoming = {projected}"
        ),
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
    "legs_per_position",
    "legs_per_venue",
    "leverage_headroom",
    "net_exposure_coefficient",
    "net_exposure_headroom",
]
