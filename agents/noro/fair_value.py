"""NORO's fair-value calculation.

V1 is deterministic quantitative code, not a model.  Fair value is the
liquidity-weighted price across venues:

    FV = sum(price_v * usable_liquidity_v) / sum(usable_liquidity_v)

where ``price_v`` blends the mid and the microprice (the microprice leans
towards the side about to be consumed, so it leads the mid slightly), and
``usable_liquidity_v`` is the notional resting within a configured distance of
that venue's mid — quoting a tight price on no size should not move the global
fair value.
"""

from __future__ import annotations

from dataclasses import dataclass

from core.config import NoroConfig
from core.models.market import VenueMarketState, safe_bps


@dataclass(frozen=True)
class VenueValuation:
    venue: str
    price: float
    liquidity: float
    weight: float
    deviation_bps: float


@dataclass(frozen=True)
class FairValue:
    symbol: str
    fair_value: float
    total_liquidity: float
    venues: tuple[VenueValuation, ...]

    def deviation(self, venue: str) -> float | None:
        for valuation in self.venues:
            if valuation.venue == venue:
                return valuation.deviation_bps
        return None

    @property
    def widest_deviation_bps(self) -> float:
        if not self.venues:
            return 0.0
        return max(abs(v.deviation_bps) for v in self.venues)

    @property
    def cheapest(self) -> VenueValuation | None:
        return min(self.venues, key=lambda v: v.deviation_bps) if self.venues else None

    @property
    def richest(self) -> VenueValuation | None:
        return max(self.venues, key=lambda v: v.deviation_bps) if self.venues else None


def venue_price(state: VenueMarketState, microprice_weight: float) -> float | None:
    """Blend of mid and microprice for one venue."""
    mid = state.metrics.mid
    if mid is None:
        return None
    micro = state.metrics.microprice
    if micro is None:
        return mid
    w = min(1.0, max(0.0, microprice_weight))
    return mid * (1 - w) + micro * w


def usable_liquidity(state: VenueMarketState, window_bps: float) -> float:
    """Notional resting within ``window_bps`` of the venue's mid, both sides."""
    key = f"{window_bps:g}"
    bid = state.metrics.bid_depth_by_bps.get(key)
    ask = state.metrics.ask_depth_by_bps.get(key)
    if bid is None or ask is None:
        # No bucket at that exact distance was measured; fall back to full
        # book depth rather than silently reporting zero liquidity.
        bid = state.metrics.bid_depth_notional
        ask = state.metrics.ask_depth_notional
    return min(bid, ask)


def compute_fair_value(
    symbol: str, states: list[VenueMarketState], config: NoroConfig
) -> FairValue | None:
    """Fair value across usable venues, or ``None`` if none are usable."""
    usable = [
        s
        for s in states
        if s.quality.is_usable and venue_price(s, config.microprice_weight) is not None
    ]
    if not usable:
        return None

    priced: list[tuple[VenueMarketState, float, float]] = []
    for state in usable:
        price = venue_price(state, config.microprice_weight)
        liquidity = usable_liquidity(state, config.liquidity_window_bps)
        if price is None or liquidity <= 0:
            continue
        priced.append((state, price, liquidity))
    if not priced:
        return None

    total_liquidity = sum(liquidity for _, _, liquidity in priced)
    fair = sum(price * liquidity for _, price, liquidity in priced) / total_liquidity

    valuations = tuple(
        VenueValuation(
            venue=state.venue,
            price=price,
            liquidity=liquidity,
            weight=liquidity / total_liquidity,
            deviation_bps=safe_bps(price - fair, fair) or 0.0,
        )
        for state, price, liquidity in priced
    )
    return FairValue(
        symbol=symbol,
        fair_value=fair,
        total_liquidity=total_liquidity,
        venues=valuations,
    )
