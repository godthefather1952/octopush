"""ZEPHR's executability model.

NORO says whether theoretical edge exists.  ZEPHR says whether it survives
contact with the book.  The core output is a *sizing curve*: expected net edge
as a function of order size, from which the maximum economically justified
size falls out.

Every number here is defined relative to the reference frame the gross edge
was measured in (:class:`~execution.costs.EdgeFrame`).  That is the whole
discipline of this module: a cost is the gap between a reference price and the
price actually paid, so charging a component the reference already contains
charges it twice.  The platform's cross-venue detector quotes touch to touch,
which already prices both crossings, so crossing the touch is free *in that
frame* and only what happens beyond the touch is charged.
"""

from __future__ import annotations

from dataclasses import dataclass

from core.config import FeeSchedule, ZephrConfig
from core.models.common import Liquidity, Side
from core.models.market import PriceLevel, VenueMarketState
from core.models.opportunity import CostBreakdown
from execution.costs import EdgeFrame, latency_cost_bps, market_impact_bps, walk_book

#: Why a rung of the ladder cannot be traded. Exactly one applies to any
#: infeasible point, and they are checked physical -> permitted -> economic,
#: so the reported reason is the *first* thing that rules the size out rather
#: than whichever check happened to run last.
NO_LEGS = "NO_LEGS"
INSUFFICIENT_DEPTH = "INSUFFICIENT_DEPTH"
BELOW_MIN_TRADE_NOTIONAL = "BELOW_MIN_TRADE_NOTIONAL"
NET_EDGE_BELOW_FLOOR = "NET_EDGE_BELOW_FLOOR"


@dataclass(frozen=True)
class LegQuote:
    """One leg of a trade, priced at a given size.

    The fields split cleanly in two, and the split is load-bearing:

    *Charged* -- ``slippage_bps``, ``impact_bps``, ``fee_bps``,
    ``latency_bps`` and ``spread_cost_bps`` are the components
    :attr:`total_cost_bps` subtracts from the gross edge. Each is defined as
    incremental to the frame the gross edge was measured in.

    *Informational* -- ``quoted_half_spread_bps`` and ``crossed_spread_bps``
    describe the venue's spread and what this leg's execution style does with
    it. They are reported so the spread stays observable, and they are
    deliberately **not** summed into the cost: in the touch-to-touch frame the
    gross edge already paid for the crossing.
    """

    venue: str
    side: Side
    #: Execution style, which decides the fee tier and whether this leg
    #: crosses the spread at all. ZEPHR's own pricing path always uses
    #: :attr:`~core.models.common.Liquidity.TAKER` -- see :func:`quote_leg`.
    liquidity: Liquidity
    notional: float
    expected_price: float
    touch_price: float
    #: CHARGED. Cost of walking the book *past* the touch, from
    #: :func:`~execution.costs.walk_book`. Zero at the touch by construction,
    #: so it never overlaps the spread.
    slippage_bps: float
    #: CHARGED. Modelled pressure *beyond the visible book* -- the hidden book
    #: moving and other participants stepping away. Distinct from
    #: ``slippage_bps``, which is what the visible levels actually cost.
    #: ``inf`` when there is no depth to price against.
    impact_bps: float
    #: CHARGED. Venue fee for this leg's execution style.
    fee_bps: float
    #: CHARGED. Adverse drift expected between deciding and filling.
    latency_bps: float
    #: CHARGED. Crossing cost *incremental to the gross edge's frame*: zero in
    #: the touch-to-touch frame (already paid for), and zero for a passive leg
    #: in any frame (it does not cross).
    spread_cost_bps: float
    #: INFORMATIONAL. Half this venue's quoted spread, whatever the execution
    #: style. Reported so the spread the trade sits inside stays visible.
    quoted_half_spread_bps: float
    #: INFORMATIONAL. The half spread this leg's style actually crosses --
    #: ``quoted_half_spread_bps`` for a taker, zero for a maker. What that
    #: crossing *costs* is ``spread_cost_bps``, which also depends on the
    #: frame.
    crossed_spread_bps: float
    exhausted: bool
    usable_liquidity: float

    @property
    def spread_bps(self) -> float:
        """Deprecated alias for :attr:`crossed_spread_bps`.

        Retained so existing callers keep working. New code should read
        ``crossed_spread_bps`` (what is crossed) or ``spread_cost_bps`` (what
        is charged) and say which it means -- the two were one ambiguous field
        named ``spread_bps``, and the ambiguity is what let the spread be
        charged on top of a gross edge that already contained it.
        """
        return self.crossed_spread_bps

    @property
    def total_cost_bps(self) -> float:
        return (
            self.slippage_bps
            + self.impact_bps
            + self.fee_bps
            + self.latency_bps
            + self.spread_cost_bps
        )


@dataclass(frozen=True)
class SizePoint:
    notional: float
    gross_edge_bps: float
    net_edge_bps: float
    total_cost_bps: float
    legs: tuple[LegQuote, ...]
    feasible: bool
    #: The residual hedging allowance folded into ``total_cost_bps``. Charged
    #: once for the whole trade, not once per leg.
    hedge_bps: float = 0.0
    #: Why this size is not tradeable, or ``None`` when it is. One of the
    #: module-level reason constants.
    infeasible_reason: str | None = None

    @property
    def expected_profit(self) -> float:
        return self.notional * self.net_edge_bps / 10_000

    def cost_breakdown(self) -> CostBreakdown:
        """The canonical :class:`CostBreakdown` for this size.

        This is the *only* place the per-leg quotes are folded into a
        breakdown. The orchestrator used to re-derive the same sum by hand
        while sizing the intent, which meant two independent implementations
        of one calculation had to be kept in agreement by hand -- and the
        moment they drifted, ZEPHR's ``net_edge_bps`` and the intent's
        ``expected_net_edge_bps`` would describe the same trade with different
        numbers, with RUNE gating on one of them.

        ``total_bps`` here equals :attr:`total_cost_bps` exactly, so
        ``cost_breakdown().net_from(gross_edge_bps)`` equals
        :attr:`net_edge_bps` exactly.

        Slippage and impact stay in separate fields. They are separate models:
        ``slippage_bps`` is measured off the levels actually quoted, while
        ``other_bps`` carries the modelled pressure beyond them. Summing them
        into one number hides which half of an expensive trade was observed
        and which was assumed.
        """
        return CostBreakdown(
            fees_bps=sum(leg.fee_bps for leg in self.legs),
            spread_bps=sum(leg.spread_cost_bps for leg in self.legs),
            slippage_bps=sum(leg.slippage_bps for leg in self.legs),
            hedge_bps=self.hedge_bps,
            latency_bps=sum(leg.latency_bps for leg in self.legs),
            other_bps=sum(leg.impact_bps for leg in self.legs),
        )


@dataclass(frozen=True)
class SizingCurve:
    """Net edge across the size ladder, plus the chosen maximum size.

    :attr:`best` and :attr:`max_economical_notional` are deliberately
    different things and must not be collapsed into one:

    * :attr:`best` is the size to *trade* -- the feasible rung with the
      greatest expected profit. Net edge decays with size while notional
      grows, so the most profitable rung is often not the largest one.
    * :attr:`max_economical_notional` is the *ceiling* -- the largest rung
      that is feasible at all. RUNE gates on it (``LIQUIDITY_SUFFICIENT``) and
      caps its own sizing by it, which is a headroom question, not a choice of
      trade size.

    Replacing the ceiling with the chosen size would make the liquidity gate
    tautological (it would compare the intent's notional against itself);
    replacing the chosen size with the ceiling would trade the largest size
    that clears the floor rather than the most profitable one.
    """

    symbol: str
    points: tuple[SizePoint, ...]
    max_economical_notional: float
    best: SizePoint | None
    #: The frame the gross edge was measured in, carried so a consumer can see
    #: which costs were in scope.
    frame: EdgeFrame = EdgeFrame.TOUCH_TO_TOUCH
    #: The net-edge floor actually applied.
    min_net_edge_bps: float = 0.0
    #: The smallest size that could be authorised at all.
    min_notional: float = 0.0
    #: The residual hedging allowance charged once on every point.
    hedge_bps: float = 0.0

    @property
    def feasible(self) -> bool:
        return self.best is not None and self.max_economical_notional > 0


def quote_leg(
    state: VenueMarketState,
    side: Side,
    notional: float,
    levels: list[PriceLevel],
    fees: FeeSchedule,
    config: ZephrConfig,
    *,
    is_maker: bool = False,
    frame: EdgeFrame = EdgeFrame.TOUCH_TO_TOUCH,
) -> LegQuote:
    """Price one leg at ``notional`` against the real book.

    ``is_maker`` selects the execution style, and it is the single knob that
    decides both the fee tier and whether the leg crosses at all -- the two
    always move together, and letting them be set independently is how a leg
    ends up paying a maker fee for a taker's crossing.

    ZEPHR's own pricing path never passes ``is_maker=True``, and
    :func:`build_sizing_curve` prices every leg as a taker. That is deliberate
    rather than an oversight: a resting order's economics are dominated by
    whether it fills at all, and this module models neither queue position nor
    fill probability. Pricing a passive leg as though it were certain to fill
    would credit the strategy with a spread it has not earned. The maker path
    exists for callers that model fill risk themselves.
    """
    walk = walk_book(levels, notional, side)
    touch = levels[0].price if levels else 0.0
    depth = sum(level.price * level.size for level in levels)
    impact = market_impact_bps(notional, depth, config)
    latency = latency_cost_bps(
        state.latency_ms or 0.0, state.metrics.short_vol_bps, config
    )
    liquidity = Liquidity.MAKER if is_maker else Liquidity.TAKER
    quoted_half_spread = (state.metrics.spread_bps or 0.0) / 2.0
    # A passive leg rests rather than crossing, so it crosses nothing.
    crossed = 0.0 if is_maker else quoted_half_spread
    # ...and what crossing *costs* depends on whether the gross edge already
    # paid for it. Touch-to-touch edges already did.
    spread_cost = 0.0 if frame is EdgeFrame.TOUCH_TO_TOUCH else crossed
    return LegQuote(
        venue=state.venue,
        side=side,
        liquidity=liquidity,
        notional=notional,
        expected_price=walk.average_price or touch,
        touch_price=touch,
        slippage_bps=walk.slippage_bps,
        impact_bps=impact,
        fee_bps=fees.fee_bps(is_maker),
        latency_bps=latency,
        spread_cost_bps=spread_cost,
        quoted_half_spread_bps=quoted_half_spread,
        crossed_spread_bps=crossed,
        exhausted=walk.exhausted,
        usable_liquidity=depth,
    )


def _ladder(config: ZephrConfig, max_notional: float | None) -> list[float]:
    """The rungs to price, ascending and unique.

    ``ZephrConfig`` already guarantees its ladder is non-empty, positive and
    strictly ascending, so the only thing that can disturb the ordering is the
    injected ``max_notional`` rung.
    """
    rungs = [n for n in config.size_ladder if max_notional is None or n <= max_notional]
    if max_notional is not None and max_notional > 0 and max_notional not in rungs:
        rungs = sorted({*rungs, max_notional})
    return rungs


def build_sizing_curve(
    symbol: str,
    gross_edge_bps: float,
    leg_inputs: list[tuple[VenueMarketState, Side, list[PriceLevel], FeeSchedule]],
    config: ZephrConfig,
    *,
    frame: EdgeFrame = EdgeFrame.TOUCH_TO_TOUCH,
    hedge_bps: float | None = None,
    min_net_edge_bps: float | None = None,
    min_notional: float = 0.0,
    max_notional: float | None = None,
) -> SizingCurve:
    """Evaluate the size ladder and pick the largest economical size.

    A rung is *economical* when the book can actually supply it on every leg,
    it is large enough to be authorised at all, and the net edge left after
    every modelled cost still clears the floor. Each of those is checked in
    that order -- physical, then permitted, then economic -- and the first one
    that fails is recorded on the point as its ``infeasible_reason``, so an
    unexecutable opportunity says *why* rather than merely that it is
    unexecutable.

    Infeasible rungs are kept on the curve rather than dropped: the shape of
    the curve above and below the cutoff is the diagnostic.

    ``min_net_edge_bps`` overrides ``config.min_net_edge_bps``. ZEPHR passes
    :attr:`Settings.zephr_min_net_edge_bps` so its floor is never looser than
    the risk limit that will judge the resulting intent.

    ``min_notional`` is the floor below which a size could not be authorised
    (``RiskLimits.min_trade_notional``). Rungs under it are marked infeasible.
    No rung is *added* at that boundary: injecting a smaller tradeable size
    than the operator configured would make ZEPHR find executable trades where
    the configured ladder found none, and this pass does not widen what the
    platform will trade.
    """
    hedge = config.hedge_cost_bps if hedge_bps is None else hedge_bps
    floor = config.min_net_edge_bps if min_net_edge_bps is None else min_net_edge_bps

    points: list[SizePoint] = []
    for notional in _ladder(config, max_notional):
        legs = tuple(
            quote_leg(state, side, notional, levels, fees, config, frame=frame)
            for state, side, levels, fees in leg_inputs
        )
        # Per-leg costs are additive across legs: each leg pays its own
        # crossing. The hedging allowance is not -- it covers the residual
        # delta the legs leave behind, so it is charged once for the trade.
        total_cost = sum(leg.total_cost_bps for leg in legs) + hedge
        net = gross_edge_bps - total_cost

        reason: str | None = None
        if not legs:
            reason = NO_LEGS
        elif any(leg.exhausted for leg in legs):
            reason = INSUFFICIENT_DEPTH
        elif notional < min_notional:
            reason = BELOW_MIN_TRADE_NOTIONAL
        elif not (net >= floor):
            # Negated ``>=`` rather than ``<`` so that a non-comparable net
            # edge is infeasible: every ordinary comparison against NaN is
            # false, and ``net < floor`` would quietly wave one through.
            reason = NET_EDGE_BELOW_FLOOR

        points.append(
            SizePoint(
                notional=notional,
                gross_edge_bps=gross_edge_bps,
                net_edge_bps=net,
                total_cost_bps=total_cost,
                legs=legs,
                feasible=reason is None,
                hedge_bps=hedge,
                infeasible_reason=reason,
            )
        )

    feasible_points = [p for p in points if p.feasible]
    best = max(feasible_points, key=lambda p: p.expected_profit) if feasible_points else None
    max_size = max((p.notional for p in feasible_points), default=0.0)
    return SizingCurve(
        symbol=symbol,
        points=tuple(points),
        max_economical_notional=max_size,
        best=best,
        frame=frame,
        min_net_edge_bps=floor,
        min_notional=min_notional,
        hedge_bps=hedge,
    )
