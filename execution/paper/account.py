"""Paper account.

Cash, positions and P&L for the simulated portfolio.  Deliberately simple
arithmetic that can be recomputed from the fill log alone — which is exactly
what MARIN does to check it.

The fill log is split into a *sealed* prefix and an *unsealed tail*.  MARIN
replays only the tail, against totals carried forward in a
:class:`LedgerCheckpoint`.  A prefix is sealed only once reconciliation has
compared it against the running state and found them equal, so sealing never
buries a discrepancy: it records one that was checked.  What sealing does give
up is re-detection of a *retroactive* change to already-verified history — a
fill object mutated in place long after the fact.  Fills are immutable value
objects appended once, so that is not a failure mode this system has; the
trade is bounded memory and bounded reconciliation cost, and it is made
explicitly rather than by accident.

Full history remains available: every fill is published and persisted by the
recorder, so a from-zero rebuild is a replay-from-store operation rather than
a resident-memory one.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from core.clock import Clock
from core.models.common import Millis
from core.models.execution import FillEvent
from core.models.portfolio import PortfolioState, PositionState
from core.models.reconciliation import AccountSnapshot, PositionSummary

#: Milliseconds in a trading day, used for the daily-loss reset.
DAY_MS = 24 * 60 * 60 * 1000

#: Fills retained in memory beyond the sealed checkpoint.
#:
#: Sized from measurement, not taste: a fill costs ~3.5KB resident, so 10_000
#: retained fills is ~35MB — a bounded, predictable footprint. It is also far
#: more than any plausible burst between reconciliation runs (MARIN runs every
#: few ticks, and a tick produces single-digit fills), so under normal
#: operation the tail is sealed long before the cap is approached. The cap is
#: a backstop against a stalled reconciler, not the usual path.
DEFAULT_RETAINED_FILLS = 10_000


@dataclass
class LedgerCheckpoint:
    """Verified cumulative totals as of a sealed prefix of the fill log."""

    #: Number of fills folded into this checkpoint, from the start of the session.
    fills_sealed: int = 0
    cash: float = 0.0
    realized_pnl: float = 0.0
    fees_paid: float = 0.0
    positions: dict[str, PositionState] = field(default_factory=dict)
    sealed_at: Millis | None = None

    def clone_positions(self) -> dict[str, PositionState]:
        return {k: v.model_copy(deep=True) for k, v in self.positions.items()}


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
    #: Fills applied since the checkpoint, in order. The audit trail MARIN
    #: replays. Bounded: see :data:`DEFAULT_RETAINED_FILLS`.
    fill_log: list[FillEvent] = field(default_factory=list)
    #: Verified totals for every fill *before* ``fill_log``.
    checkpoint: LedgerCheckpoint = field(default_factory=LedgerCheckpoint)
    retained_fills: int = DEFAULT_RETAINED_FILLS
    #: Lifetime fill count, including sealed ones.
    fills_applied: int = 0
    _applied: set[str] = field(default_factory=set)
    #: Insertion order for ``_applied``, so the dedupe window can be trimmed.
    _dedupe_order: deque[str] = field(default_factory=deque)

    def __post_init__(self) -> None:
        if self.cash == 0.0:
            self.cash = self.initial_balance
        if self.peak_equity == 0.0:
            self.peak_equity = self.initial_balance
        if self.day_started_at is None:
            self.day_started_at = self.clock.now_ms()
        if self.checkpoint.fills_sealed == 0 and self.checkpoint.sealed_at is None:
            self.checkpoint.cash = self.initial_balance

    # -- mutation ----------------------------------------------------------

    def position(self, venue: str, symbol: str) -> PositionState:
        key = f"{venue}:{symbol}"
        if key not in self.positions:
            self.positions[key] = PositionState(venue=venue, symbol=symbol)
        return self.positions[key]

    def preview_fill_realized_pnl(self, fill: FillEvent) -> float:
        """Return this fill's realized-PnL contribution without mutation."""
        key = f"{fill.venue}:{fill.symbol}"
        resident = self.positions.get(key)
        position = (
            resident.model_copy(deep=True)
            if resident is not None
            else PositionState(venue=fill.venue, symbol=fill.symbol)
        )
        return position.apply(fill.side, fill.quantity, fill.price, fill.fee)

    def apply_fill(self, fill: FillEvent) -> bool:
        """Apply a fill to cash and positions. Idempotent by fill id."""
        if fill.fill_id in self._applied:
            return False
        self._applied.add(fill.fill_id)
        self._dedupe_order.append(fill.fill_id)
        self._trim_dedupe()
        self.fills_applied += 1
        self._roll_day(fill.created_at)

        position = self.position(fill.venue, fill.symbol)
        realized = position.apply(fill.side, fill.quantity, fill.price, fill.fee)
        position.updated_at = fill.created_at
        # Captured here, at the one moment this fill's own contribution to
        # the position's cumulative realized_pnl is known in isolation --
        # not left for a PAPER_FILL subscriber to reconstruct later from
        # live account state, which the bus's queue-then-dispatch semantics
        # can let several other fills mutate first (see FillEvent docstring).
        fill.realized_pnl_delta = realized

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
        """A detached copy of the account's state.

        ``deep=True`` on the positions is required, not stylistic. A shallow
        ``model_copy()`` aliases the nested ``PositionState`` objects, so the
        "snapshot" would keep changing as the account traded — every consumer
        holding one (the dashboard, an attribution record, a risk evaluation)
        would silently be reading live state instead of the moment it asked
        for. The same applies anywhere else a snapshot is taken.
        """
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

    def reconciliation_snapshot(self, now_ms: Millis) -> AccountSnapshot:
        """What this ledger believes, captured for reconciliation.

        Additional to :meth:`snapshot`, not a replacement for it: that returns
        a ``PortfolioState`` for the risk and dashboard paths and is unchanged.
        This returns the reconciliation-shaped view, which differs in three
        ways that matter to a reconciler and not to a risk gate.

        It **takes its instant** rather than reading the clock, so a capture
        made during replay carries the instant the original run recorded.

        It **names the unsealed tail** (``unsealed_fill_ids``) alongside the
        checkpoint totals, so a reader can tell which history is still
        comparable fill-by-fill from which is covered only by its aggregates.
        A reconciler that could not tell those apart would report a missing
        fill every time a prefix was sealed.

        It **summarises positions** rather than copying live objects, for the
        same reason ``snapshot`` copies them deeply: a capture that aliases
        mutable state is not a capture.

        No comparison, no mutation, no decision.
        """
        return AccountSnapshot(
            created_at=now_ms,
            initial_balance=self.initial_balance,
            cash=self.cash,
            equity=self.equity,
            peak_equity=max(self.peak_equity, self.equity),
            realized_pnl=self.realized_pnl,
            unrealized_pnl=self.unrealized_pnl,
            fees_paid=self.fees_paid,
            day_realized_pnl=self.day_realized_pnl,
            fills_applied=self.fills_applied,
            fills_sealed=self.checkpoint.fills_sealed,
            unsealed_fill_ids=[fill.fill_id for fill in self.fill_log],
            retained_fills=self.retained_fills,
            positions=[PositionSummary.of(p) for p in self.positions.values()],
            checkpoint_cash=self.checkpoint.cash,
            checkpoint_realized_pnl=self.checkpoint.realized_pnl,
            checkpoint_fees_paid=self.checkpoint.fees_paid,
            checkpoint_sealed_at=self.checkpoint.sealed_at,
        )

    def recompute_from_fills(self) -> tuple[float, dict[str, PositionState], float]:
        """Independently rebuild cash and positions from the fill log.

        MARIN uses this as the second opinion in reconciliation: two paths to
        the same numbers, compared with an epsilon.

        The rebuild starts from the last sealed checkpoint rather than from
        zero, so its cost is proportional to the unsealed tail rather than to
        lifetime history. The checkpoint's own totals were produced by this
        same replay and compared against the running state before being
        sealed, so the arithmetic being checked is still the account's, not
        the checkpoint's.
        """
        cash = self.checkpoint.cash
        realized = self.checkpoint.realized_pnl
        positions = self.checkpoint.clone_positions()
        for fill in self.fill_log:
            key = f"{fill.venue}:{fill.symbol}"
            if key not in positions:
                positions[key] = PositionState(venue=fill.venue, symbol=fill.symbol)
            realized += positions[key].apply(fill.side, fill.quantity, fill.price, fill.fee)
            cash += fill.cash_delta
        return cash, positions, realized

    # -- ledger compaction -------------------------------------------------

    @property
    def unsealed_fills(self) -> int:
        return len(self.fill_log)

    def seal(self, count: int | None = None) -> int:
        """Fold a verified prefix of the tail into the checkpoint.

        The caller is responsible for having verified the prefix first, and
        for choosing where it ends: this method records agreement, it does
        not establish it. MARIN calls it only after a reconciliation run that
        found no critical mismatch, with a boundary that keeps the account's
        and the OMS's retained windows identical.

        A *prefix* rather than a selection, because the replay is
        order-dependent: skipping a fill in the middle would rebase every
        position after it. Returns the number of fills sealed.
        """
        limit = len(self.fill_log) if count is None else min(count, len(self.fill_log))
        if limit <= 0:
            return 0

        prefix, tail = self.fill_log[:limit], self.fill_log[limit:]
        cash = self.checkpoint.cash
        realized = self.checkpoint.realized_pnl
        positions = self.checkpoint.clone_positions()
        for fill in prefix:
            key = f"{fill.venue}:{fill.symbol}"
            if key not in positions:
                positions[key] = PositionState(venue=fill.venue, symbol=fill.symbol)
            realized += positions[key].apply(fill.side, fill.quantity, fill.price, fill.fee)
            cash += fill.cash_delta
        self.checkpoint = LedgerCheckpoint(
            fills_sealed=self.checkpoint.fills_sealed + limit,
            cash=cash,
            realized_pnl=realized,
            fees_paid=self.checkpoint.fees_paid + sum(f.fee for f in prefix),
            positions=positions,
            sealed_at=self.clock.now_ms(),
        )
        self.fill_log = tail
        return limit

    def _trim_dedupe(self) -> None:
        """Bound the duplicate-detection window.

        A re-delivered fill arrives close behind the original, so a window of
        the most recent ``retained_fills`` ids covers every duplicate this
        system can actually produce. Beyond that horizon a repeat would be
        applied twice — which is precisely the discrepancy MARIN exists to
        catch, so the failure is loud rather than silent.
        """
        excess = len(self._dedupe_order) - self.retained_fills
        for _ in range(max(0, excess)):
            self._applied.discard(self._dedupe_order.popleft())
