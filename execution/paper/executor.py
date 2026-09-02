"""PaperExecutor — the only executor in this build.

Latency is modelled *logically*, not by sleeping: each order carries the time
at which it becomes visible to the simulated venue, and :meth:`poll` advances
state to the current clock.  That keeps execution deterministic under replay
and keeps the orchestrator's loop free of blocking waits.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from core.bus import EventBus
from core.clock import Clock
from core.config import Settings
from core.events import Event, EventType
from core.models.common import QTY_EPSILON, Millis
from core.models.execution import ExecutionReport, FillEvent, OrderStatus, PaperOrder
from core.models.market import MarketState, PriceLevel
from core.models.opportunity import ExecutionPlan
from execution.oms import OrderManager
from execution.paper.account import PaperAccount
from execution.paper.simulator import BookView, FillSimulator, is_marketable
from execution.veska.executor import Executor

log = logging.getLogger(__name__)

SERVICE = "PAPER_EXECUTOR"


@dataclass
class _Pending:
    """Simulated venue-side timing for one order."""

    ack_at: Millis
    cancel_at: Millis | None = None
    #: Notional that has traded at or through the order's price since it rested.
    traded_through: float = 0.0
    #: Set by failure injection: the order's true state becomes unknowable.
    force_unknown: bool = False


@dataclass
class PaperExecutor(Executor):
    """Simulates a venue for the orders VESKA gives it."""

    bus: EventBus
    clock: Clock
    settings: Settings
    oms: OrderManager
    account: PaperAccount
    simulator: FillSimulator
    market: MarketState | None = None
    is_paper: bool = True
    _pending: dict[str, _Pending] = field(default_factory=dict)
    #: Set by the kill switch. Blocks new submissions without touching
    #: outstanding orders, which still need to be cancelled or resolved.
    execution_disabled: bool = False
    rejected_submissions: int = 0

    # -- market data -------------------------------------------------------

    def update_market(self, state: MarketState) -> None:
        self.market = state
        self._accrue_trade_flow()

    def _levels(self, venue: str, symbol: str, consuming: str) -> list[PriceLevel]:
        if self.market is None:
            return []
        state = self.market.venue_state(venue, symbol)
        if state is None or state.book is None or not state.quality.is_usable:
            return []
        return state.book.asks if consuming == "asks" else state.book.bids

    def _book_view(self, order: PaperOrder) -> BookView:
        consuming = "asks" if order.side.value == "BUY" else "bids"
        own = "bids" if consuming == "asks" else "asks"
        opposing = self._levels(order.venue, order.symbol, consuming)
        own_levels = self._levels(order.venue, order.symbol, own)
        pending = self._pending.get(order.client_order_id)
        return BookView(
            opposing=opposing,
            own_touch=own_levels[0].price if own_levels else None,
            traded_through=pending.traded_through if pending else 0.0,
        )

    def _accrue_trade_flow(self) -> None:
        """Credit resting orders with volume that traded at their price."""
        if self.market is None:
            return
        for order in self.oms.live_orders():
            pending = self._pending.get(order.client_order_id)
            if pending is None or order.limit_price is None:
                continue
            state = self.market.venue_state(order.venue, order.symbol)
            if state is None:
                continue
            volume = state.metrics.buy_volume + state.metrics.sell_volume
            pending.traded_through += volume * 0.01

    def _latency(self, venue: str) -> int:
        try:
            return self.settings.venue(venue).latency_ms
        except KeyError:
            return 40

    # -- submission --------------------------------------------------------

    async def submit(self, plan: ExecutionPlan) -> ExecutionReport:
        now = self.clock.now_ms()
        orders: list[PaperOrder] = []
        notes: list[str] = []

        for planned in plan.orders:
            order = self.oms.from_plan(
                planned, plan_id=plan.plan_id, intent_id=plan.intent_id, strategy=plan.strategy
            )
            order.correlation_id = plan.correlation_id
            if self.execution_disabled:
                self.rejected_submissions += 1
                self.oms.reject(order.client_order_id, "execution disabled")
                notes.append(f"{order.client_order_id}: execution disabled")
                await self._publish_order(order, EventType.PAPER_ORDER_UPDATED)
                orders.append(order)
                continue

            self.oms.transition(order.client_order_id, OrderStatus.SUBMITTING)
            order.submitted_at = now
            self._pending[order.client_order_id] = _Pending(
                ack_at=now + self._latency(order.venue)
            )
            await self._publish_order(order, EventType.PAPER_ORDER_CREATED)
            orders.append(order)

        return ExecutionReport(
            created_at=now,
            correlation_id=plan.correlation_id,
            plan_id=plan.plan_id,
            intent_id=plan.intent_id,
            orders=orders,
            complete=False,
            notes=notes,
        )

    # -- cancellation ------------------------------------------------------

    async def cancel(self, client_order_id: str) -> None:
        order = self.oms.get(client_order_id)
        if order is None or not order.is_live:
            return
        if order.status in (OrderStatus.CREATED, OrderStatus.SUBMITTING):
            # Not yet acknowledged; the venue has nothing to cancel, so the
            # order resolves once it arrives.
            self._pending.setdefault(
                client_order_id, _Pending(ack_at=self.clock.now_ms())
            ).cancel_at = self.clock.now_ms()
            return
        self.oms.transition(client_order_id, OrderStatus.CANCEL_PENDING)
        pending = self._pending.setdefault(client_order_id, _Pending(ack_at=self.clock.now_ms()))
        pending.cancel_at = self.clock.now_ms() + self.settings.venue(order.venue).cancel_latency_ms
        await self._publish_order(order, EventType.PAPER_ORDER_UPDATED)

    async def cancel_all(self) -> int:
        live = self.oms.live_orders()
        for order in live:
            await self.cancel(order.client_order_id)
        return len(live)

    def inject_timeout(self, client_order_id: str) -> None:
        """Failure injection: make an order's true state unknowable."""
        pending = self._pending.get(client_order_id)
        if pending is not None:
            pending.force_unknown = True

    # -- the simulation step ----------------------------------------------

    async def poll(self, now_ms: Millis) -> list[FillEvent]:
        """Advance every live order to ``now_ms``."""
        fills: list[FillEvent] = []
        for order in list(self.oms.orders.values()):
            if order.is_terminal or order.status is OrderStatus.UNKNOWN:
                continue
            pending = self._pending.get(order.client_order_id)
            if pending is None:
                continue

            if pending.force_unknown:
                self.oms.mark_unknown(order.client_order_id, "simulated venue timeout")
                await self._publish_order(order, EventType.PAPER_ORDER_UPDATED)
                continue

            if now_ms < pending.ack_at:
                continue

            if order.status is OrderStatus.SUBMITTING:
                self.oms.transition(order.client_order_id, OrderStatus.ACKNOWLEDGED)
                order.acknowledged_at = now_ms
                self.oms.transition(order.client_order_id, OrderStatus.OPEN)
                await self._publish_order(order, EventType.PAPER_ORDER_UPDATED)

            view = self._book_view(order)

            # A cancel that has arrived may still lose the race to a fill.
            if order.status is OrderStatus.CANCEL_PENDING:
                if pending.cancel_at is not None and now_ms >= pending.cancel_at:
                    if self.simulator.cancel_wins_race(order, view):
                        self.oms.transition(order.client_order_id, OrderStatus.CANCELLED)
                        await self._publish_order(order, EventType.PAPER_ORDER_UPDATED)
                        continue
                else:
                    continue

            fill = self._attempt_fill(order, view, now_ms)
            if fill is not None:
                fills.append(fill)
                await self._record_fill(order, fill)

            if order.is_terminal:
                continue

            if order.expires_at is not None and now_ms >= order.expires_at:
                target = (
                    OrderStatus.EXPIRED
                    if order.filled_quantity <= 0
                    else OrderStatus.CANCELLED
                )
                self.oms.transition(order.client_order_id, target)
                await self._publish_order(order, EventType.PAPER_ORDER_UPDATED)

        return fills

    def _attempt_fill(
        self, order: PaperOrder, view: BookView, now_ms: Millis
    ) -> FillEvent | None:
        if order.status not in (
            OrderStatus.OPEN,
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.CANCEL_PENDING,
        ):
            return None
        if not view.opposing:
            return None

        latency = float(self._latency(order.venue))
        simulated = (
            self.simulator.fill_marketable(
                order,
                view,
                self.settings.venue(order.venue).fees,
                latency,
            )
            if is_marketable(order)
            else self.simulator.fill_passive(
                order, view, self.settings.venue(order.venue).fees
            )
        )
        if simulated is None or simulated.quantity <= QTY_EPSILON:
            return None
        source_ts = self.market.source_data_timestamp if self.market else None
        return self.simulator.build_fill(order, simulated, now_ms, source_ts)

    async def _record_fill(self, order: PaperOrder, fill: FillEvent) -> None:
        if not self.oms.apply_fill(fill):
            return
        self.account.apply_fill(fill)
        await self.bus.publish(
            Event(
                type=EventType.PAPER_FILL,
                ts_ms=fill.created_at,
                source=SERVICE,
                schema_name="FillEvent",
                correlation_id=fill.correlation_id,
                payload=fill.to_json_dict(),
            )
        )
        await self._publish_order(order, EventType.PAPER_ORDER_UPDATED)

    def open_orders(self) -> list[PaperOrder]:
        return self.oms.live_orders()

    async def _publish_order(self, order: PaperOrder, event_type: EventType) -> None:
        await self.bus.publish(
            Event(
                type=event_type,
                ts_ms=self.clock.now_ms(),
                source=SERVICE,
                schema_name="PaperOrder",
                correlation_id=order.correlation_id,
                payload=order.to_json_dict(),
            )
        )
