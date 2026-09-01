"""Paper account.

Cash, positions and P&L for the simulated portfolio.  Deliberately simple
arithmetic that can be recomputed from the fill log alone — which is exactly
what MARIN does to check it.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from core.clock import Clock
from core.models.common import Millis
from core.models.execution import FillEvent
from core.models.portfolio import PortfolioState, PositionState

#: Milliseconds in a trading day, used for the daily-loss reset.
DAY_MS = 24 * 60 * 60 * 1000


@dataclass
class PaperAccount:
    """Mutable paper-trading account state."""

    clock: Clock
    initial_balance: float
    cash: float = 0.0
    positions: dict[str, PositionState] = field(default_factory=dict)
    realized_pnl: float = 0.0
    fees_paid: float = 0.0
    peak_equity: float = 0.0
    day_realized_pnl: float = 0.0
    day_started_at: Millis | None = None
    #: Every fill applied, in order. The audit trail MARIN replays.
    fill_log: list[FillEvent] = field(default_factory=list)
    _applied: set[str] = field(default_factory=set)

    def __post_init__(self) -> None:
        if self.cash == 0.0:
            self.cash = self.initial_balance
        if self.peak_equity == 0.0:
            self.peak_equity = self.initial_balance
        if self.day_started_at is None:
            self.day_started_at = self.clock.now_ms()

    # -- mutation ----------------------------------------------------------

    def position(self, venue: str, symbol: str) -> PositionState:
        key = f"{venue}:{symbol}"
        if key not in self.positions:
            self.positions[key] = PositionState(venue=venue, symbol=symbol)
        return self.positions[key]

    def apply_fill(self, fill: FillEvent) -> bool:
        """Apply a fill to cash and positions. Idempotent by fill id."""
        if fill.fill_id in self._applied:
            return False
        self._applied.add(fill.fill_id)
        self._roll_day(fill.created_at)

        position = self.position(fill.venue, fill.symbol)
        realized = position.apply(fill.side, fill.quantity, fill.price, fill.fee)
        position.updated_at = fill.created_at

        self.cash += fill.cash_delta
        self.realized_pnl += realized
        self.day_realized_pnl += realized - fill.fee
        self.fees_paid += fill.fee
        self.fill_log.append(fill)
        return True

    def mark(self, marks: dict[str, float], now_ms: Millis | None = None) -> None:
        """Update mark prices. Keys are ``venue:symbol`` or bare ``symbol``."""
        stamp = now_ms if now_ms is not None else self.clock.now_ms()
        for key, position in self.positions.items():
            price = marks.get(key, marks.get(position.symbol))
            if price is not None and price > 0:
                position.mark_price = price
                position.updated_at = stamp
        self.peak_equity = max(self.peak_equity, self.equity)

    def _roll_day(self, now_ms: Millis) -> None:
        if self.day_started_at is None:
            self.day_started_at = now_ms
            return
        if now_ms - self.day_started_at >= DAY_MS:
            self.day_started_at = now_ms
            self.day_realized_pnl = 0.0

    # -- reads -------------------------------------------------------------

    @property
    def equity(self) -> float:
        return self.cash + sum(p.signed_notional for p in self.positions.values())

    @property
    def unrealized_pnl(self) -> float:
        return sum(p.unrealized_pnl for p in self.positions.values())

    def snapshot(self) -> PortfolioState:
        now = self.clock.now_ms()
        state = PortfolioState(
            created_at=now,
            initial_balance=self.initial_balance,
            cash=self.cash,
            positions={k: v.model_copy(deep=True) for k, v in self.positions.items()},
            realized_pnl=self.realized_pnl,
            fees_paid=self.fees_paid,
            peak_equity=max(self.peak_equity, self.equity),
            day_realized_pnl=self.day_realized_pnl,
            day_started_at=self.day_started_at,
        )
        return state

    def recompute_from_fills(self) -> tuple[float, dict[str, PositionState], float]:
        """Independently rebuild cash and positions from the fill log.

        MARIN uses this as the second opinion in reconciliation: two paths to
        the same numbers, compared with an epsilon.
        """
        cash = self.initial_balance
        realized = 0.0
        positions: dict[str, PositionState] = {}
        for fill in self.fill_log:
            key = f"{fill.venue}:{fill.symbol}"
            if key not in positions:
                positions[key] = PositionState(venue=fill.venue, symbol=fill.symbol)
            realized += positions[key].apply(fill.side, fill.quantity, fill.price, fill.fee)
            cash += fill.cash_delta
        return cash, positions, realized
