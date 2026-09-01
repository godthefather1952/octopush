"""Cross-venue dislocation detection.

The first question the platform asks is deliberately narrow:

    Is the same asset temporarily mispriced across venues, *after* fees,
    spreads, slippage, liquidity and execution costs?

This module answers only the first half — where the raw dislocation is.  ZEPHR
answers the second half, and only the orchestrator combines the two.
"""

from __future__ import annotations

from dataclasses import dataclass

from core.clock import Clock
from core.config import Settings
from core.models.common import Side
from core.models.market import MarketState, VenueMarketState, safe_bps
from core.models.opportunity import Opportunity, OpportunityKind, OpportunityLeg

STRATEGY = "cross_venue"

#: Components this strategy cannot run without. If any is not HEALTHY the
#: orchestrator suspends the strategy rather than trading with a hole in it.
REQUIRED_COMPONENTS = ["TIDAL", "NORO", "ZEPHR", "RUNE", "VESKA", "MARIN"]


@dataclass(frozen=True)
class Dislocation:
    symbol: str
    buy_venue: str
    sell_venue: str
    buy_price: float
    sell_price: float
    reference_price: float
    #: Executable edge at the touch, before costs.
    gross_edge_bps: float


def find_dislocation(
    symbol: str, states: list[VenueMarketState], reference_price: float | None
) -> Dislocation | None:
    """Best executable cross-venue dislocation, at the touch.

    The edge is measured between the venue whose *ask* is cheapest and the
    venue whose *bid* is richest, because those are the prices actually
    available — comparing mids would overstate the edge by a full spread.
    """
    usable = [
        s
        for s in states
        if s.quality.is_usable
        and s.metrics.best_ask is not None
        and s.metrics.best_bid is not None
    ]
    if len(usable) < 2:
        return None

    buy_state = min(usable, key=lambda s: s.metrics.best_ask)
    sell_state = max(usable, key=lambda s: s.metrics.best_bid)
    if buy_state.venue == sell_state.venue:
        return None

    buy_price = buy_state.metrics.best_ask
    sell_price = sell_state.metrics.best_bid
    reference = reference_price or (buy_price + sell_price) / 2
    edge = safe_bps(sell_price - buy_price, reference)
    if edge is None:
        return None
    return Dislocation(
        symbol=symbol,
        buy_venue=buy_state.venue,
        sell_venue=sell_state.venue,
        buy_price=buy_price,
        sell_price=sell_price,
        reference_price=reference,
        gross_edge_bps=edge,
    )


class CrossVenueDetector:
    """Turns market state into candidate opportunities."""

    def __init__(self, settings: Settings, clock: Clock) -> None:
        self.settings = settings
        self.clock = clock
        #: Symbol -> id of the opportunity currently in flight, so a
        #: persistent dislocation is not re-detected every tick.
        self.active: dict[str, str] = {}

    def detect(self, market: MarketState) -> list[Opportunity]:
        now = self.clock.now_ms()
        out: list[Opportunity] = []
        for symbol in self.settings.symbols:
            if symbol in self.active:
                continue
            view = market.consolidated.get(symbol)
            dislocation = find_dislocation(
                symbol,
                market.states_for(symbol),
                view.reference_price if view else None,
            )
            if dislocation is None:
                continue
            if dislocation.gross_edge_bps < self.settings.min_dislocation_bps:
                continue
            opportunity = Opportunity(
                created_at=now,
                source_data_timestamp=market.source_data_timestamp,
                kind=OpportunityKind.CROSS_VENUE_DISLOCATION,
                strategy=STRATEGY,
                symbol=symbol,
                legs=[
                    OpportunityLeg(
                        venue=dislocation.buy_venue,
                        symbol=symbol,
                        side=Side.BUY,
                        reference_price=dislocation.buy_price,
                    ),
                    OpportunityLeg(
                        venue=dislocation.sell_venue,
                        symbol=symbol,
                        side=Side.SELL,
                        reference_price=dislocation.sell_price,
                    ),
                ],
                gross_edge_bps=dislocation.gross_edge_bps,
                expires_at=now + self.settings.risk.max_data_age_ms,
                reason_codes=["CROSS_VENUE_DISLOCATION"],
                detail={
                    "buy_venue": dislocation.buy_venue,
                    "sell_venue": dislocation.sell_venue,
                    "buy_price": dislocation.buy_price,
                    "sell_price": dislocation.sell_price,
                    "reference_price": dislocation.reference_price,
                },
            )
            opportunity.correlation_id = opportunity.opportunity_id
            self.active[symbol] = opportunity.opportunity_id
            out.append(opportunity)
        return out

    def release(self, symbol: str, opportunity_id: str) -> None:
        """Allow re-detection once an opportunity leaves the pipeline."""
        if self.active.get(symbol) == opportunity_id:
            del self.active[symbol]
