"""Position and portfolio schemas."""

from __future__ import annotations

from pydantic import Field

from core.models.common import FLAT_EPSILON, Base, Envelope, Millis, Side


class PositionState(Base):
    """Net position in one symbol on one venue.

    Realised P&L uses average-cost accounting: closing quantity realises the
    difference between the fill price and the running average entry price.
    """

    venue: str
    symbol: str
    #: Signed base-asset quantity. Positive is long.
    quantity: float = 0.0
    average_entry_price: float = 0.0
    realized_pnl: float = 0.0
    fees_paid: float = 0.0
    #: Last mark used for unrealised P&L.
    mark_price: float | None = None
    updated_at: Millis | None = None

    @property
    def key(self) -> str:
        return f"{self.venue}:{self.symbol}"

    @property
    def is_flat(self) -> bool:
        return abs(self.quantity) < FLAT_EPSILON

    @property
    def notional(self) -> float:
        if self.mark_price is None:
            return abs(self.quantity) * self.average_entry_price
        return abs(self.quantity) * self.mark_price

    @property
    def signed_notional(self) -> float:
        price = self.mark_price if self.mark_price is not None else self.average_entry_price
        return self.quantity * price

    @property
    def unrealized_pnl(self) -> float:
        if self.mark_price is None or self.is_flat:
            return 0.0
        return (self.mark_price - self.average_entry_price) * self.quantity

    def apply(self, side: Side, quantity: float, price: float, fee: float) -> float:
        """Apply a fill; return the realised P&L produced by it."""
        signed = quantity * side.sign
        realized = 0.0
        if self.is_flat or (self.quantity > 0) == (signed > 0):
            # Opening or increasing: roll the average entry price.
            new_qty = self.quantity + signed
            if abs(new_qty) > FLAT_EPSILON:
                self.average_entry_price = (
                    self.average_entry_price * self.quantity + price * signed
                ) / new_qty
            self.quantity = new_qty
        else:
            closing = min(abs(signed), abs(self.quantity))
            direction = 1.0 if self.quantity > 0 else -1.0
            realized = (price - self.average_entry_price) * closing * direction
            remaining = abs(signed) - closing
            self.quantity += signed
            if abs(self.quantity) < FLAT_EPSILON:
                self.quantity = 0.0
                self.average_entry_price = 0.0
            elif remaining > 0:
                # Flipped through zero: the residual opens at the fill price.
                self.average_entry_price = price
        self.realized_pnl += realized
        self.fees_paid += fee
        return realized


class PortfolioState(Envelope):
    """The paper account's full state."""

    mode: str = "PAPER"
    initial_balance: float
    cash: float
    positions: dict[str, PositionState] = Field(default_factory=dict)
    realized_pnl: float = 0.0
    fees_paid: float = 0.0
    #: Highest equity seen so far, used for drawdown.
    peak_equity: float = 0.0
    #: Realised P&L accumulated since the current trading day started.
    day_realized_pnl: float = 0.0
    day_started_at: Millis | None = None

    @property
    def unrealized_pnl(self) -> float:
        return sum(p.unrealized_pnl for p in self.positions.values())

    @property
    def gross_pnl(self) -> float:
        """P&L before fees."""
        return self.realized_pnl + self.unrealized_pnl + self.fees_paid

    @property
    def net_pnl(self) -> float:
        return self.realized_pnl + self.unrealized_pnl

    @property
    def equity(self) -> float:
        return self.cash + sum(p.signed_notional for p in self.positions.values())

    @property
    def gross_exposure(self) -> float:
        return sum(p.notional for p in self.positions.values())

    @property
    def net_exposure(self) -> float:
        return sum(p.signed_notional for p in self.positions.values())

    @property
    def drawdown(self) -> float:
        """Absolute drawdown from peak equity (>= 0)."""
        return max(0.0, self.peak_equity - self.equity)

    @property
    def drawdown_pct(self) -> float:
        if self.peak_equity <= 0:
            return 0.0
        return self.drawdown / self.peak_equity

    def exposure_by_venue(self) -> dict[str, float]:
        out: dict[str, float] = {}
        for pos in self.positions.values():
            out[pos.venue] = out.get(pos.venue, 0.0) + pos.notional
        return out

    def net_delta_by_symbol(self) -> dict[str, float]:
        """Signed notional per symbol, aggregated across venues."""
        out: dict[str, float] = {}
        for pos in self.positions.values():
            out[pos.symbol] = out.get(pos.symbol, 0.0) + pos.signed_notional
        return out
