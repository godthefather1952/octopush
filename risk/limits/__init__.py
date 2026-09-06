"""Deterministic limit checks.

Each function is a pure predicate over state and configuration, returning a
:class:`GateCheck`.  No function here consults a model, reads the network, or
takes a probability.  RUNE-CORE simply runs them all.
"""

from __future__ import annotations

from collections.abc import Callable

from core.config import RiskLimits
from core.models.common import Millis, Side
from core.models.opportunity import TradeIntent
from core.models.ops import HealthStatus, KillSwitchState, SystemHealth
from core.models.portfolio import PortfolioState
from core.models.risk import CommittedExposure, GateCheck, GateResult


def _resolved(committed: CommittedExposure | None) -> CommittedExposure:
    """A zero snapshot when the caller supplies none.

    Every committed-exposure argument below is keyword-only and defaults to
    ``None``, so a caller written before committed exposure existed gets
    exactly its old behaviour: an all-zero snapshot adds nothing to any base.
    """
    return CommittedExposure() if committed is None else committed


def _basis(filled: float, committed: float) -> str:
    """A gate ``detail`` explaining a base that is not just the filled book.

    Empty when nothing is committed, so gates read the same as before in the
    ordinary case and explain themselves in the case that used to be invisible.
    """
    if committed == 0.0:
        return ""
    return f"{filled:,.2f} filled + {committed:,.2f} committed"


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
    """Freshness of the market data an intent was built from.

    The permitted interval is two-sided:
    ``-max_clock_skew_ms <= age <= max_data_age_ms``, where
    ``age = now_ms - intent.source_data_timestamp``.

    The upper bound is the obvious one — data too old to act on. The lower
    bound is the one this gate used to be missing (P5-9): a timestamp in the
    *future* produces a negative age, and ``age <= max_data_age_ms`` is
    satisfied by every negative number, however large. Data stamped a day
    ahead therefore read as maximally fresh, and the further wrong the
    timestamp was, the more freshness the gate credited it with. That is
    exactly backwards, and it is reachable without an adversary: a venue whose
    clock has jumped, or a unit mix-up that multiplies a timestamp.

    A small negative age is normal and stays permitted. Two independently
    synced machines disagree by a few milliseconds, so requiring
    ``age >= 0`` would reject honest data. ``max_clock_skew_ms`` is the
    tolerance already configured for precisely that question — how far an
    exchange timestamp may lead local time before it is a clock problem
    rather than drift — so it is reused rather than duplicated by a second
    number that could drift away from it.

    This gate reads no clock: ``now_ms`` is supplied by the caller, which is
    what lets a replay evaluate the same intent at the same logical instant
    and reach the same verdict. TIDAL's own upstream skew handling is
    unchanged and still runs first; this is the risk boundary's independent
    check, not a replacement for it.

    Not size-reducible: a smaller trade is not built on fresher data, so this
    gate has no headroom candidate.
    """
    if intent.source_data_timestamp is None:
        return _unknown("MARKET_DATA_FRESH", "intent carries no source data timestamp")
    age = now_ms - intent.source_data_timestamp
    lower = -limits.max_clock_skew_ms
    return _check(
        "MARKET_DATA_FRESH",
        lower <= age <= limits.max_data_age_ms,
        observed=float(age),
        limit=float(limits.max_data_age_ms),
        detail=(
            f"market data age {age}ms; "
            f"permitted [{lower}, {limits.max_data_age_ms}]ms"
        ),
    )


def gate_deadline(intent: TradeIntent, now_ms: Millis) -> GateCheck:
    return _check(
        "INTENT_NOT_EXPIRED",
        now_ms <= intent.deadline_ms,
        observed=float(now_ms),
        limit=float(intent.deadline_ms),
    )


def gate_position_notional(
    intent: TradeIntent,
    portfolio: PortfolioState,
    limits: RiskLimits,
    *,
    committed: CommittedExposure | None = None,
) -> GateCheck:
    """Largest post-trade single-position notional across the intent's legs.

    Legs are grouped by ``venue:symbol`` first, so two legs landing on one
    position add twice (P5-11). Taking ``max`` over ungrouped legs counted such
    a pair once and understated the position the trade would actually build.

    The base is the position already held PLUS whatever is already working on
    that same ``venue:symbol`` but has not filled yet (P5-1). Two trades
    authorised into one position inside a single tick both read an empty
    position without it.

    Deliberately conservative with opposing legs: a BUY and a SELL on the same
    position are added rather than netted. RUNE has no guaranteed fill sequence
    or per-leg quantity for a generic multi-leg intent, so it cannot know the
    two would offset — and overstating a position blocks a safe trade, while
    understating one authorises an unsafe one. Committed notional is unsigned
    for the same reason.
    """
    reserved = _resolved(committed).position_exposure
    per_position = legs_per_position(intent)
    worst = 0.0
    detail = ""
    for key, leg_count in per_position.items():
        position = portfolio.positions.get(key)
        held = position.notional if position else 0.0
        pending = reserved.get(key, 0.0)
        projected = held + pending + intent.notional * leg_count
        if projected > worst:
            worst = projected
            detail = _basis(held, pending)
    return _check(
        "MAX_POSITION_NOTIONAL",
        worst <= limits.max_position_notional,
        observed=worst,
        limit=limits.max_position_notional,
        detail=detail,
    )


def gate_gross_exposure(
    intent: TradeIntent,
    portfolio: PortfolioState,
    limits: RiskLimits,
    *,
    committed: CommittedExposure | None = None,
) -> GateCheck:
    """Gross exposure after this intent, counting what is already working.

    The base is filled gross PLUS committed gross. Without the second term the
    orchestrator's ``_seek`` pass could authorise several trades in one tick,
    each reading the same portfolio, because no fill had landed to make the
    earlier ones visible (P5-1).
    """
    pending = _resolved(committed).gross_exposure
    base = portfolio.gross_exposure + pending
    projected = base + intent.notional * len(intent.legs)
    return _check(
        "MAX_GROSS_EXPOSURE",
        projected <= limits.max_gross_exposure,
        observed=projected,
        limit=limits.max_gross_exposure,
        detail=_basis(portfolio.gross_exposure, pending),
    )


def net_exposure_coefficient(intent: TradeIntent) -> float:
    """How much the intent's net delta moves per unit of per-leg notional.

    ``sum(side.sign)`` over the legs: +1 per BUY, -1 per SELL. A balanced
    two-leg cross-venue trade has coefficient 0 — it adds no net delta at any
    size, which is exactly why net exposure cannot be reduced by shrinking it.
    """
    return float(sum(leg.side.sign for leg in intent.legs))


def gate_net_exposure(
    intent: TradeIntent,
    portfolio: PortfolioState,
    limits: RiskLimits,
    *,
    committed: CommittedExposure | None = None,
) -> GateCheck:
    """Net exposure after the intent, assuming its legs offset as designed.

    ``committed.net_exposure`` is SIGNED, so a working BUY and a working SELL
    of the same size cancel here exactly as two filled positions would. That is
    the whole point of tracking net separately from gross: a balanced pair
    in flight adds no net delta and must not be made to look as though it does.
    """
    pending = _resolved(committed).net_exposure
    base = portfolio.net_exposure + pending
    delta = net_exposure_coefficient(intent) * intent.notional
    projected = abs(base + delta)
    return _check(
        "MAX_NET_EXPOSURE",
        projected <= limits.max_net_exposure,
        observed=projected,
        limit=limits.max_net_exposure,
        detail=_basis(portfolio.net_exposure, pending),
    )


def net_exposure_headroom(
    intent: TradeIntent,
    portfolio: PortfolioState,
    limits: RiskLimits,
    *,
    committed: CommittedExposure | None = None,
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

    ``current`` is filled net PLUS committed net, matching ``gate_net_exposure``
    term for term. If the two disagreed, a trade could be sized against a base
    the gate does not use and then be rejected by that gate at its own reduced
    size, which is exactly the promise :meth:`RuneCore.evaluate` makes.
    """
    current = portfolio.net_exposure + _resolved(committed).net_exposure
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
    intent: TradeIntent,
    portfolio: PortfolioState,
    limits: RiskLimits,
    *,
    committed: CommittedExposure | None = None,
) -> float:
    """Largest per-leg notional keeping projected leverage inside its limit.

    ``gate_leverage`` computes ``(gross + n * legs) / equity <= max_leverage``.
    Every term but ``n`` is fixed at decision time, so this rearranges to
    ``n <= (max_leverage * equity - gross) / legs``.

    ``gross`` here is filled gross PLUS committed gross, matching the gate.
    Equity deliberately does NOT move: an unfilled order has not paid a fee or
    taken a mark, so committed exposure belongs in the numerator only. Leaving
    it out of the numerator would let leverage be re-levered inside one tick.

    Non-positive equity yields zero: there is no size at which the gate can
    pass, and the gate itself remains the fail-closed authority.
    """
    equity = portfolio.equity
    if equity <= 0:
        return 0.0
    legs = max(1, len(intent.legs))
    gross = portfolio.gross_exposure + _resolved(committed).gross_exposure
    allowed_gross = limits.max_leverage * equity - gross
    return max(0.0, allowed_gross / legs)


def gate_leverage(
    intent: TradeIntent,
    portfolio: PortfolioState,
    limits: RiskLimits,
    *,
    committed: CommittedExposure | None = None,
) -> GateCheck:
    equity = portfolio.equity
    if equity <= 0:
        return _check("MAX_LEVERAGE", False, observed=0.0, limit=limits.max_leverage,
                      detail="non-positive equity")
    pending = _resolved(committed).gross_exposure
    gross = portfolio.gross_exposure + pending
    projected = (gross + intent.notional * len(intent.legs)) / equity
    return _check(
        "MAX_LEVERAGE",
        projected <= limits.max_leverage,
        observed=projected,
        limit=limits.max_leverage,
        detail=_basis(portfolio.gross_exposure, pending),
    )


def gate_venue_exposure(
    intent: TradeIntent,
    portfolio: PortfolioState,
    limits: RiskLimits,
    *,
    committed: CommittedExposure | None = None,
) -> GateCheck:
    """Largest post-trade exposure on any one venue.

    Legs are grouped by venue first, so an intent routing two legs to one venue
    projects ``2 * notional`` onto it rather than ``notional`` (P5-11). Each
    venue's base is what is held there PLUS what is already working there and
    unfilled (P5-1) — venue concentration is where concurrent authorisation
    bites hardest, because every leg of every cross-venue trade lands on one of
    a small number of venues.
    """
    reserved = _resolved(committed).venue_exposure
    exposure = portfolio.exposure_by_venue()
    worst = 0.0
    detail = ""
    for venue, leg_count in legs_per_venue(intent).items():
        held = exposure.get(venue, 0.0)
        pending = reserved.get(venue, 0.0)
        projected = held + pending + intent.notional * leg_count
        if projected > worst:
            worst = projected
            detail = _basis(held, pending)
    return _check(
        "MAX_VENUE_EXPOSURE",
        worst <= limits.max_venue_exposure,
        observed=worst,
        limit=limits.max_venue_exposure,
        detail=detail,
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

    DELIBERATELY TAKES NO COMMITTED-EXPOSURE ARGUMENT
    ================================================
    ``strategy_exposure`` is already reserved at authorisation time, from
    ``Orchestrator.working_notional``, and is held until the opportunity
    reaches CLOSED or REJECTED. It therefore ALREADY covers the
    authorised-but-unfilled window that :class:`CommittedExposure` exists to
    close for the other gates. Adding committed exposure here would count the
    same trade twice and roughly halve the effective strategy budget, which
    would silently undo the P5-2 fix rather than extend it.
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


def unhedged_fill_factor(intent: TradeIntent) -> int:
    """How many per-leg notionals of residual the intent can transiently hold.

    An intent's FINAL delta may be zero and its worst INTERMEDIATE delta still
    be a full leg: a cross-venue BUY/SELL pair is delta-neutral only once both
    legs have filled, and between the first fill and the second the book holds
    one whole leg of one-sided exposure.

    Legs are grouped by symbol, and each symbol contributes
    ``max(buy_legs, sell_legs)``: if every BUY on that symbol fills before any
    SELL, the transient residual is the BUY side; if the SELLs go first, it is
    the SELL side; the worst magnitude is the larger of the two. Symbols are
    then SUMMED, because each can independently become one-sided and OKAPI's
    ``total_unhedged`` is itself ``sum(abs(residual per symbol))`` — the same
    aggregation, so the bound is stated in the units the limit is measured in.

    * 1 BUY BTC                                  -> 1
    * BUY BTC + SELL BTC                         -> 1
    * 2 BUY BTC + 1 SELL BTC                     -> 2
    * BUY BTC + SELL ETH                         -> 2
    * BUY BTC + SELL BTC + BUY ETH + SELL ETH    -> 2
    """
    per_symbol: dict[str, list[int]] = {}
    for leg in intent.legs:
        counts = per_symbol.setdefault(leg.symbol, [0, 0])
        counts[0 if leg.side is Side.BUY else 1] += 1
    return sum(max(buys, sells) for buys, sells in per_symbol.values())


def execution_multiplier(intent: TradeIntent) -> float:
    """Conservative allowance for filling worse than the expected price.

    VESKA sizes each leg as ``notional / routing.expected_price``, but a
    marketable order may fill above that price — up to the slippage budget the
    intent itself carries. Taking the worst-case filled quote exposure as
    exactly ``notional`` therefore still permits a trade that lands slightly
    over a hard limit.

    Uses the intent's existing ``max_slippage_bps`` rather than a new
    configurable buffer: that number is already the platform's own statement of
    how far a fill may stray, and inventing a second one would give the same
    question two answers.
    """
    return 1.0 + max(0.0, intent.max_slippage_bps) / 10_000.0


def gate_unhedged(
    intent: TradeIntent,
    unhedged_notional: float,
    limits: RiskLimits,
    *,
    committed: CommittedExposure | None = None,
    recovery_reserve: float = 0.0,
) -> GateCheck:
    """Worst-case unhedged residual once this intent is working.

    MAX_UNHEDGED_EXPOSURE IS SIZE-SENSITIVE
    =======================================
    It used to compare ``abs(ctx.unhedged_notional)`` — the residual already
    present in the FILLED book — against the limit, and nothing else. That
    treated the gate as a pure current-state check, and the default
    configuration then let RUNE authorise 25,000 per leg against a 10,000 hard
    unhedged ceiling: an ordinary two-leg delta-neutral trade whose final delta
    is zero, but whose known worst intermediate state is one full leg of
    one-sided exposure. The post-fill backstop caught it afterwards, correctly
    and every time; the pre-trade projection was the incomplete half (P5-18).

    Four terms, all in quote notional:

    * ``actual``   — the residual OKAPI measures right now;
    * ``pending``  — :attr:`CommittedExposure.unhedged_fill_risk`, the worst
      case from entry orders already working and not yet filled;
    * ``incoming`` — this intent, at ``notional * fill factor * execution
      multiplier``;
    * ``recovery reserve`` — see below.

    THE RECOVERY RESERVE
    ====================
    Sizing the temporary leg exactly TO the hard ceiling is not the same as
    sizing it safely. External validation measured the difference: an entry
    authorised at 9,980.97 of notional was MARKED at 10,022.93 while it was
    still one-sided, 42 bps of ordinary movement, and the emergency backstop
    engaged — with the exit already CANCEL_PENDING and OKAPI's hedge already
    SUBMITTING. Nothing had gone wrong; recovery was underway and simply had
    no room between it and the ceiling.

    So an entry reserves a slice of the budget it may not consume, leaving the
    recovery path somewhere to move. The reserve is not a new invented
    percentage: the caller passes ``Settings.hedge_tolerance_notional``, the
    operator-configured delta OKAPI already tolerates before it hedges — which
    ties entry sizing to the mechanism that has to unwind the exposure.

    This is a PRE-TRADE budget only. The hard limit is unchanged, and the kill
    switch still fires at ``max_unhedged_notional`` on the residual that
    actually exists. If the market moves further than the reserve and crosses
    the real ceiling, the backstop is supposed to engage; the reserve only
    stops normal recovery from starting with zero headroom to it.

    ``observed`` is the STRESSED projection — all four terms — so the gate's
    ``observed <= limit`` contract still reads against the true hard limit
    rather than against a second, quieter one. ``detail`` names every term,
    including a zero reserve, so nothing is hidden.

    WHY ADDING THEM IS DELIBERATELY CONSERVATIVE
    ============================================
    ``unhedged_notional`` is an unsigned aggregate: it does not retain the
    signed direction of each per-symbol residual, so the sum cannot know that
    an incoming first fill might offset an existing residual instead of adding
    to it. The result is an upper bound that can overstate, never understate.
    For a hard safety limit that is the correct direction to be wrong in, and
    it is not offered as an exact post-fill predictor.
    """
    reserved = _resolved(committed)
    actual = abs(unhedged_notional)
    pending = reserved.unhedged_fill_risk
    incoming = (
        intent.notional * unhedged_fill_factor(intent) * execution_multiplier(intent)
    )
    reserve = max(0.0, recovery_reserve)
    projected = actual + pending + incoming + reserve
    return _check(
        "MAX_UNHEDGED_EXPOSURE",
        projected <= limits.max_unhedged_notional,
        observed=projected,
        limit=limits.max_unhedged_notional,
        detail=(
            f"{actual:,.2f} actual + {pending:,.2f} pending + "
            f"{incoming:,.2f} incoming + {reserve:,.2f} recovery reserve "
            f"= {projected:,.2f} stressed"
        ),
    )


def unhedged_headroom(
    intent: TradeIntent,
    unhedged_notional: float,
    limits: RiskLimits,
    *,
    committed: CommittedExposure | None = None,
    recovery_reserve: float = 0.0,
) -> float:
    """Largest per-leg notional keeping worst-case unhedged inside its limit.

    Mirrors :func:`gate_unhedged` term for term, so a trade is sized against
    exactly what it is then judged against. ``gate_unhedged`` computes
    ``actual + pending + n * factor * multiplier + reserve <= limit``, which is
    linear in ``n`` and rearranges to
    ``n <= (limit - reserve - actual - pending) / (factor * multiplier)``.

    The reserve comes off the top, in addition to the residual that already
    exists — with a 10,000 limit, a 500 reserve and 2,000 of actual residual,
    7,500 remains for pending plus incoming, not 8,000.

    Zero when the base alone already breaches: no reduction can help, and the
    gate remains the fail-closed authority. A non-finite ``unhedged_notional``
    also lands on zero, because every comparison against it is false and
    ``max(0.0, nan)`` is ``0.0`` — an unknown residual is not an acceptable one.
    """
    reserved = _resolved(committed)
    remaining = (
        limits.max_unhedged_notional
        - max(0.0, recovery_reserve)
        - abs(unhedged_notional)
        - reserved.unhedged_fill_risk
    )
    factor = unhedged_fill_factor(intent)
    if factor <= 0:
        # A legless intent creates no residual, so this limit does not bind.
        return float("inf")
    return max(0.0, remaining / (factor * execution_multiplier(intent)))


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
    "CommittedExposure",
    "Gate",
    "GateCheck",
    "GateResult",
    "execution_multiplier",
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
    "unhedged_fill_factor",
    "unhedged_headroom",
]
