"""Venue routing.

Decides where and how each leg of an intent is worked.  Kept separate from
VESKA so that smarter routing (splitting a leg across venues, choosing a
passive leg on the wide side) is a change here and nowhere else.
"""

from __future__ import annotations

from dataclasses import dataclass

from core.config import Settings
from core.models.common import OrderType, Side, TimeInForce
from core.models.market import MarketState, PriceLevel
from core.models.opportunity import OpportunityLeg


@dataclass(frozen=True)
class RoutingDecision:
    venue: str
    order_type: OrderType
    time_in_force: TimeInForce
    limit_price: float | None
    expected_price: float
    #: True when the leg is expected to earn the maker fee.
    is_maker: bool
    reason: str


class VenueRouter:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @staticmethod
    def _levels(market: MarketState, leg: OpportunityLeg) -> list[PriceLevel]:
        state = market.venue_state(leg.venue, leg.symbol)
        if state is None or state.book is None:
            return []
        return state.book.asks if leg.side is Side.BUY else state.book.bids

    def route(
        self,
        leg: OpportunityLeg,
        market: MarketState,
        *,
        urgency: float,
        max_slippage_bps: float,
    ) -> RoutingDecision | None:
        """Choose an order type and limit for one leg.

        Urgency drives the maker/taker choice.  A relative-value trade whose
        edge decays in seconds cannot afford to sit passively, so anything
        above the passive threshold crosses — but always with a limit, so the
        realised price can never be worse than the modelled one by more than
        the configured slippage budget.
        """
        levels = self._levels(market, leg)
        if not levels:
            return None
        touch = levels[0].price

        if urgency < 0.35:
            # Patient: post inside the spread on our own side and earn the
            # maker fee if the market comes to us.
            state = market.venue_state(leg.venue, leg.symbol)
            own_touch = (
                state.metrics.best_bid if leg.side is Side.BUY else state.metrics.best_ask
            ) if state else None
            if own_touch is None:
                return None
            return RoutingDecision(
                venue=leg.venue,
                order_type=OrderType.LIMIT,
                time_in_force=TimeInForce.POST_ONLY,
                limit_price=own_touch,
                expected_price=own_touch,
                is_maker=True,
                reason="LOW_URGENCY_PASSIVE",
            )

        budget = max_slippage_bps / 10_000
        limit = touch * (1 + budget) if leg.side is Side.BUY else touch * (1 - budget)
        return RoutingDecision(
            venue=leg.venue,
            order_type=OrderType.LIMIT,
            time_in_force=TimeInForce.IOC,
            limit_price=limit,
            expected_price=touch,
            is_maker=False,
            reason="CROSS_WITH_SLIPPAGE_LIMIT",
        )
