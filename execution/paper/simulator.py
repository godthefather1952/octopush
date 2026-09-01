"""Paper fill simulation.

The point of this module is to be *pessimistic in the right places*.  A paper
engine that fills every order at the touch produces a strategy that only works
on paper, so the simulator models:

* depth — a large order walks the book and pays for it;
* latency — the price drifts adversely between decision and arrival;
* queue position — a passive order sits behind existing size and often does
  not trade;
* vanishing liquidity — resting size sometimes disappears before it is hit;
* partial fills — an order is not all-or-nothing;
* cancel races — a cancel in flight can lose to a fill.

All randomness comes from a seeded generator, so a replay of the same session
with the same configuration produces the same fills.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from core.config import ExecutionConfig, FeeSchedule
from core.models.common import Liquidity, Millis, OrderType, Side, TimeInForce
from core.models.execution import FillEvent, PaperOrder
from core.models.market import PriceLevel
from execution.costs import walk_book


@dataclass(frozen=True)
class SimulatedFill:
    quantity: float
    price: float
    fee: float
    liquidity: Liquidity
    slippage_bps: float


@dataclass
class BookView:
    """The levels an order would consume, plus the touch on its own side."""

    #: Levels on the opposite side — what a marketable order consumes.
    opposing: list[PriceLevel]
    #: Best price on the order's own side, used for passive queue modelling.
    own_touch: float | None = None
    #: Notional traded through the order's price since it was placed.
    traded_through: float = 0.0


class FillSimulator:
    """Turns an order plus a book view into (possibly partial) fills."""

    def __init__(self, config: ExecutionConfig, *, seed: int | None = None) -> None:
        self.config = config
        self.rng = random.Random(seed if seed is not None else config.seed)

    # -- helpers -----------------------------------------------------------

    def latency_adjusted_levels(
        self, levels: list[PriceLevel], side: Side, latency_ms: float
    ) -> list[PriceLevel]:
        """Move the book adversely to reflect the time an order spends in flight.

        A buyer arriving late finds higher asks; a seller finds lower bids.
        """
        if latency_ms <= 0 or not levels:
            return levels
        drift_bps = self.config.latency_drift_bps_per_100ms * (latency_ms / 100.0)
        factor = 1 + drift_bps / 10_000 if side is Side.BUY else 1 - drift_bps / 10_000
        return [PriceLevel(price=level.price * factor, size=level.size) for level in levels]

    def apply_vanishing_liquidity(self, levels: list[PriceLevel]) -> list[PriceLevel]:
        """Randomly remove resting size that got away before we arrived."""
        probability = self.config.liquidity_vanish_probability
        if probability <= 0:
            return levels
        out: list[PriceLevel] = []
        for level in levels:
            if self.rng.random() < probability:
                continue
            out.append(level)
        return out

    def _fee(self, notional: float, fees: FeeSchedule, liquidity: Liquidity) -> float:
        return notional * fees.fee_bps(liquidity is Liquidity.MAKER) / 10_000

    # -- marketable orders -------------------------------------------------

    def fill_marketable(
        self,
        order: PaperOrder,
        view: BookView,
        fees: FeeSchedule,
        latency_ms: float,
    ) -> SimulatedFill | None:
        """Fill a market or crossing-limit order by walking the book."""
        levels = self.apply_vanishing_liquidity(
            self.latency_adjusted_levels(view.opposing, order.side, latency_ms)
        )
        if order.order_type is OrderType.LIMIT and order.limit_price is not None:
            # A limit order only consumes levels at or better than its price.
            levels = [
                level
                for level in levels
                if (order.side is Side.BUY and level.price <= order.limit_price)
                or (order.side is Side.SELL and level.price >= order.limit_price)
            ]
        if not levels:
            return None

        wanted_quantity = order.remaining_quantity
        cap = self.config.max_partial_fraction
        if cap < 1.0:
            wanted_quantity *= cap
        notional = wanted_quantity * levels[0].price
        walk = walk_book(levels, notional, order.side)
        if walk.filled_quantity <= 0:
            return None

        quantity = min(wanted_quantity, walk.filled_quantity)
        price = walk.average_price
        fee = self._fee(quantity * price, fees, Liquidity.TAKER)
        slippage_bps = (
            (price - order.expected_price) / order.expected_price * 10_000 * order.side.sign
            if order.expected_price > 0
            else 0.0
        )
        return SimulatedFill(
            quantity=quantity,
            price=price,
            fee=fee,
            liquidity=Liquidity.TAKER,
            slippage_bps=slippage_bps,
        )

    # -- passive orders ----------------------------------------------------

    def fill_passive(
        self,
        order: PaperOrder,
        view: BookView,
        fees: FeeSchedule,
    ) -> SimulatedFill | None:
        """Decide whether a resting order trades.

        Modelled as: some fraction of the size at our price is ahead of us in
        the queue, so we only trade once enough volume has traded through, and
        even then only probabilistically.
        """
        if order.limit_price is None:
            return None
        opposing_touch = view.opposing[0].price if view.opposing else None
        if opposing_touch is None:
            return None

        crossed = (
            order.side is Side.BUY and opposing_touch <= order.limit_price
        ) or (order.side is Side.SELL and opposing_touch >= order.limit_price)

        if not crossed:
            # The market has not come to us; only queue-jumping flow can fill,
            # which we model as the base probability scaled by traded volume.
            if view.traded_through <= 0:
                return None
            probability = self.config.maker_fill_probability * min(
                1.0, view.traded_through / max(1e-9, order.remaining_quantity * order.limit_price)
            )
        else:
            probability = self.config.maker_fill_probability

        if self.rng.random() > probability:
            return None

        # Only the part of our order beyond the queue ahead of us trades.
        share = max(0.0, 1.0 - self.config.queue_ahead_fraction * self.rng.random())
        quantity = order.remaining_quantity * share
        if quantity <= 1e-9:
            return None
        price = order.limit_price
        fee = self._fee(quantity * price, fees, Liquidity.MAKER)
        slippage_bps = (
            (price - order.expected_price) / order.expected_price * 10_000 * order.side.sign
            if order.expected_price > 0
            else 0.0
        )
        return SimulatedFill(
            quantity=quantity,
            price=price,
            fee=fee,
            liquidity=Liquidity.MAKER,
            slippage_bps=slippage_bps,
        )

    # -- cancels -----------------------------------------------------------

    def cancel_wins_race(self, order: PaperOrder, view: BookView) -> bool:
        """Whether a cancel arrives before the order would have traded.

        A cancel on an order the market has already reached is not guaranteed
        to win — assuming otherwise is how a paper engine flatters itself.
        """
        if order.order_type is OrderType.MARKET:
            return False
        if order.limit_price is None or not view.opposing:
            return True
        touch = view.opposing[0].price
        marketable = (order.side is Side.BUY and touch <= order.limit_price) or (
            order.side is Side.SELL and touch >= order.limit_price
        )
        if not marketable:
            return True
        return self.rng.random() > self.config.maker_fill_probability

    def build_fill(
        self,
        order: PaperOrder,
        simulated: SimulatedFill,
        now_ms: Millis,
        source_ts: Millis | None = None,
    ) -> FillEvent:
        return FillEvent(
            created_at=now_ms,
            source_data_timestamp=source_ts,
            correlation_id=order.correlation_id,
            client_order_id=order.client_order_id,
            venue=order.venue,
            symbol=order.symbol,
            side=order.side,
            quantity=simulated.quantity,
            price=simulated.price,
            fee=simulated.fee,
            liquidity=simulated.liquidity,
            slippage_bps=simulated.slippage_bps,
            strategy=order.strategy,
        )


def is_marketable(order: PaperOrder) -> bool:
    """Whether an order should be treated as taking liquidity."""
    if order.order_type is OrderType.MARKET:
        return True
    return order.time_in_force in (TimeInForce.IOC, TimeInForce.FOK)
