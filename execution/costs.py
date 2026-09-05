"""Transaction-cost model.

A trade is never judged by its gross price difference.  Every component that
sits between the quoted edge and the money that actually lands is modelled
here, in one place, so the orchestrator, ZEPHR and the paper executor all
agree on what a trade costs.

    gross edge (in a stated reference frame -- see EdgeFrame)
      - spread        (crossing the touch, ONLY when the frame is mid-to-mid)
      - slippage      (walking the book beyond the touch)
      - impact        (pressure beyond the visible book)
      - fees          (venue schedule, maker or taker)
      - latency       (adverse drift between decision and fill)
      - hedge         (residual delta the priced legs do not cancel)
      - funding       (perpetual carry, where applicable)
      = expected net edge

The reference frame is not a detail. Every cost above is defined as the gap
between some *reference* price and the price actually paid, so a cost is only
a cost relative to a stated reference. Subtracting a component that the
reference already accounts for charges it twice and understates the edge; the
platform's own cross-venue detector measures its gross edge touch to touch
(cheapest ask against richest bid), which already prices both crossings, so
the spread term does not apply to it. :class:`EdgeFrame` makes that choice
explicit at every call site instead of leaving it to be assumed.
"""

from __future__ import annotations

from dataclasses import dataclass

from core.config import FeeSchedule, ZephrConfig
from core.models.common import QTY_EPSILON, Side, StrEnum
from core.models.market import PriceLevel
from core.models.opportunity import CostBreakdown


class EdgeFrame(StrEnum):
    """Which prices a gross edge was measured between.

    :attr:`TOUCH_TO_TOUCH` is an edge measured between the prices actually
    available to an aggressor -- the buy side's best ask against the sell
    side's best bid. Crossing the touch is already paid for inside such a
    number, so a taker leg adds no further spread cost; what remains is what
    happens *beyond* the touch (slippage, impact) plus fees, latency and the
    residual hedge. This is what
    :func:`strategies.cross_venue.detector.find_dislocation` produces, and it
    is the frame every opportunity in this platform is quoted in.

    :attr:`MID_TO_MID` is an edge measured between mid prices. That number
    ignores the spread entirely, so an aggressive leg must be charged the half
    spread it crosses on top of everything else.
    """

    TOUCH_TO_TOUCH = "TOUCH_TO_TOUCH"
    MID_TO_MID = "MID_TO_MID"


@dataclass(frozen=True)
class WalkResult:
    """Outcome of consuming a book with a market order."""

    filled_notional: float
    filled_quantity: float
    average_price: float
    #: Cost of walking past the touch, in bps of the touch price.
    slippage_bps: float
    #: True when the book could not supply the requested notional.
    exhausted: bool
    levels_consumed: int


def walk_book(levels: list[PriceLevel], notional: float, side: Side) -> WalkResult:
    """Consume ``levels`` until ``notional`` is filled.

    ``side`` is the side of the *book* being consumed, i.e. a buyer walks the
    asks.  Slippage is measured against the touch price, signed so that a
    positive number is always a cost.
    """
    if not levels or notional <= 0:
        return WalkResult(0.0, 0.0, 0.0, 0.0, notional > 0, 0)

    touch = levels[0].price
    remaining = notional
    spent = 0.0
    quantity = 0.0
    consumed = 0
    for level in levels:
        available = level.price * level.size
        take = min(remaining, available)
        if take <= 0:
            break
        spent += take
        quantity += take / level.price
        remaining -= take
        consumed += 1
        if remaining <= QTY_EPSILON:
            break

    if quantity <= 0:
        return WalkResult(0.0, 0.0, 0.0, 0.0, True, 0)

    average = spent / quantity
    # Buying: paying above the touch is a cost. Selling: receiving below is.
    raw = (average - touch) if side is Side.BUY else (touch - average)
    slippage_bps = raw / touch * 10_000 if touch > 0 else 0.0
    return WalkResult(
        filled_notional=spent,
        filled_quantity=quantity,
        average_price=average,
        slippage_bps=slippage_bps,
        exhausted=remaining > 1e-6,
        levels_consumed=consumed,
    )


def market_impact_bps(
    notional: float, available_depth: float, config: ZephrConfig
) -> float:
    """Impact beyond the visible book.

    ``k * (size / depth) ** exponent``.  Superlinear, because consuming a
    large fraction of the visible book also moves the hidden book and invites
    other participants to step away.
    """
    if available_depth <= 0:
        return float("inf")
    ratio = notional / available_depth
    return config.impact_coefficient * (ratio**config.impact_exponent)


def latency_cost_bps(latency_ms: float, volatility_bps: float, config: ZephrConfig) -> float:
    """Adverse drift expected between deciding and filling.

    Scales with the square root of elapsed time (a diffusive price) and with
    the venue's observed short-horizon volatility, with a configured floor so
    a quiet market still carries some penalty.
    """
    if latency_ms <= 0:
        return config.latency_penalty_bps
    periods = latency_ms / 100.0
    drift = volatility_bps * (periods**0.5) * 0.5
    return config.latency_penalty_bps + drift


def fee_bps(fees: FeeSchedule, is_maker: bool) -> float:
    return fees.fee_bps(is_maker)


def build_cost_breakdown(
    *,
    fees_bps: float,
    spread_bps: float,
    slippage_bps: float,
    hedge_bps: float = 0.0,
    funding_bps: float = 0.0,
    latency_bps: float = 0.0,
    other_bps: float = 0.0,
) -> CostBreakdown:
    return CostBreakdown(
        fees_bps=fees_bps,
        spread_bps=spread_bps,
        slippage_bps=slippage_bps,
        hedge_bps=hedge_bps,
        funding_bps=funding_bps,
        latency_bps=latency_bps,
        other_bps=other_bps,
    )
