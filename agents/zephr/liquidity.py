"""ZEPHR's executability model.

NORO says whether theoretical edge exists.  ZEPHR says whether it survives
contact with the book.  The core output is a *sizing curve*: expected net edge
as a function of order size, from which the maximum economically justified
size falls out.
"""

from __future__ import annotations

from dataclasses import dataclass

from core.config import FeeSchedule, ZephrConfig
from core.models.common import Side
from core.models.market import PriceLevel, VenueMarketState
from execution.costs import latency_cost_bps, market_impact_bps, walk_book


@dataclass(frozen=True)
class LegQuote:
    """One leg of a trade, priced at a given size."""

    venue: str
    side: Side
    notional: float
    expected_price: float
    touch_price: float
    slippage_bps: float
    impact_bps: float
    fee_bps: float
    latency_bps: float
    #: Half the venue's quoted spread, the cost of crossing.
    spread_bps: float
    exhausted: bool
    usable_liquidity: float

    @property
    def total_cost_bps(self) -> float:
        return (
            self.slippage_bps
            + self.impact_bps
            + self.fee_bps
            + self.latency_bps
            + self.spread_bps
        )


@dataclass(frozen=True)
class SizePoint:
    notional: float
    gross_edge_bps: float
    net_edge_bps: float
    total_cost_bps: float
    legs: tuple[LegQuote, ...]
    feasible: bool

    @property
    def expected_profit(self) -> float:
        return self.notional * self.net_edge_bps / 10_000


@dataclass(frozen=True)
class SizingCurve:
    """Net edge across the size ladder, plus the chosen maximum size."""

    symbol: str
    points: tuple[SizePoint, ...]
    max_economical_notional: float
    best: SizePoint | None

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
) -> LegQuote:
    """Price one leg at ``notional`` against the real book."""
    walk = walk_book(levels, notional, side)
    touch = levels[0].price if levels else 0.0
    depth = sum(level.price * level.size for level in levels)
    impact = market_impact_bps(notional, depth, config)
    latency = latency_cost_bps(
        state.latency_ms or 0.0, state.metrics.short_vol_bps, config
    )
    half_spread_bps = (state.metrics.spread_bps or 0.0) / 2.0
    return LegQuote(
        venue=state.venue,
        side=side,
        notional=notional,
        expected_price=walk.average_price or touch,
        touch_price=touch,
        slippage_bps=walk.slippage_bps,
        impact_bps=impact,
        fee_bps=fees.fee_bps(is_maker),
        latency_bps=latency,
        spread_bps=0.0 if is_maker else half_spread_bps,
        exhausted=walk.exhausted,
        usable_liquidity=depth,
    )


def build_sizing_curve(
    symbol: str,
    gross_edge_bps: float,
    leg_inputs: list[tuple[VenueMarketState, Side, list[PriceLevel], FeeSchedule]],
    config: ZephrConfig,
    *,
    hedge_bps: float | None = None,
    max_notional: float | None = None,
) -> SizingCurve:
    """Evaluate the size ladder and pick the largest economical size.

    "Economical" means net edge is still above ``min_net_edge_bps`` *and* the
    book could actually supply the size on every leg.
    """
    hedge = config.hedge_cost_bps if hedge_bps is None else hedge_bps
    ladder = [n for n in config.size_ladder if max_notional is None or n <= max_notional]
    if max_notional is not None and max_notional > 0 and max_notional not in ladder:
        ladder = sorted({*ladder, max_notional})

    points: list[SizePoint] = []
    for notional in ladder:
        legs = tuple(
            quote_leg(state, side, notional, levels, fees, config)
            for state, side, levels, fees in leg_inputs
        )
        # Costs are additive across legs: each leg pays its own crossing.
        total_cost = sum(leg.total_cost_bps for leg in legs) + hedge
        net = gross_edge_bps - total_cost
        feasible = (
            bool(legs)
            and not any(leg.exhausted for leg in legs)
            and net >= config.min_net_edge_bps
        )
        points.append(
            SizePoint(
                notional=notional,
                gross_edge_bps=gross_edge_bps,
                net_edge_bps=net,
                total_cost_bps=total_cost,
                legs=legs,
                feasible=feasible,
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
    )
