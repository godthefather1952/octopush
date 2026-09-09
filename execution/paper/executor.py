"""PaperExecutor — the only executor in this build.

Latency is modelled *logically*, not by sleeping: each order carries the time
at which it becomes visible to the simulated venue, and :meth:`poll` advances
state to a caller-supplied logical time.  That keeps execution deterministic
under replay and keeps the orchestrator's loop free of blocking waits.

NO CLOCK READS (Phase 2 Batch 1.3 — P2-14)
==========================================
This class deliberately never calls ``self.clock.now_ms()``.  Every
time-dependent decision it makes — when an order was submitted, when it
becomes acknowledged, when a cancel reaches the venue, when a fill is
stamped, when an order expires — uses the logical time its caller passed in.

The reason is replay fidelity.  A recorded session preserves exactly one
timestamp per orchestrator tick (the ``ORCHESTRATOR_TICK`` marker), so replay
can only ever re-execute a tick at that one instant.  A clock read taken
*here* is not that instant: with a live feed running concurrently with the
tick, the clock can advance arbitrarily far between the tick's snapshot and
the moment execution happens to run.  Recording ``submitted_at`` from such a
read produced orders whose acknowledgement deadline replay reconstructed
300ms earlier, so replay acknowledged (and filled) at a tick where the
original had not — the P2-14 divergence.

The ``clock`` field is retained because it is part of the constructed wiring
contract, not because anything here reads it; the absence of reads is the
invariant, and ``tests/unit/test_execution_time_fidelity.py`` asserts it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from core.bus import EventBus
from core.clock import Clock
from core.config import Settings
from core.events import Event, EventType
from core.models.common import QTY_EPSILON, Millis
from core.models.execution import (
    ExecutionCommandResult,
    ExecutionReport,
    ExecutionSnapshot,
    ExecutorCapabilities,
    FillEvent,
    OrderStatus,
    OrderSummary,
    PaperOrder,
)
from core.models.market import MarketState, PriceLevel
from core.models.opportunity import ExecutionPlan
from execution.oms import OrderManager
from execution.paper.account import PaperAccount
from execution.paper.simulator import BookView, FillSimulator, is_marketable
from execution.veska.executor import Executor

log = logging.getLogger(__name__)

SERVICE = "PAPER_EXECUTOR"

NAME = "paper"
VERSION = "paper-executor-0.2"

#: What this executor claims to implement.
#:
#: Written to describe the CURRENT implementation rather than an aspiration.
#: ``supports_market`` is False because the router never emits a MARKET order
#: and the fill path has never been exercised for one; ``supports_fok`` is
#: False because nothing here distinguishes fill-or-kill from any other
#: marketable instruction. Declaring either True would be a claim this build
#: has not earned.
#:
#: A capability being True is a statement of intent, not a proof. Whether the
#: implementation honours what it advertises is a validation question, and this
#: construction pass does not answer it.
PAPER_CAPABILITIES = ExecutorCapabilities(
    is_paper=True,
    supports_market=False,
    supports_limit=True,
    supports_ioc=True,
    supports_fok=False,
    supports_post_only=True,
    supports_gtc=True,
    supports_cancel=True,
    supports_cancel_all=True,
    supports_order_lookup=True,
    supports_unknown_resolution=True,
    supports_execution_snapshot=True,
)


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

    async def submit(self, plan: ExecutionPlan, now_ms: Millis) -> ExecutionReport:
        # ``now_ms`` is the caller's canonical logical time, never a clock
        # read taken here (Phase 2 Batch 1.3 -- P2-14). Under a live feed
        # running concurrently with the tick that produced this plan, a
        # clock read here can land arbitrarily later than the tick's own
        # recorded timestamp, which fixes ``submitted_at`` -- and therefore
        # ``ack_at`` -- at an instant replay has no way to reconstruct.
        now = now_ms
        orders: list[PaperOrder] = []
        notes: list[str] = []

        seen_ids: set[str] = set()
        duplicate_ids: set[str] = set()
        for planned in plan.orders:
            if planned.client_order_id in seen_ids:
                duplicate_ids.add(planned.client_order_id)
            seen_ids.add(planned.client_order_id)
        if duplicate_ids:
            duplicates = ", ".join(sorted(duplicate_ids))
            raise ValueError(
                f"duplicate client_order_id(s) in plan {plan.plan_id}: {duplicates}"
            )

        for planned in plan.orders:
            order = self.oms.from_plan(
                planned, plan_id=plan.plan_id, intent_id=plan.intent_id, strategy=plan.strategy
            )
            order.correlation_id = plan.correlation_id
            if self.execution_disabled:
                self.rejected_submissions += 1
                self.oms.reject(order.client_order_id, "execution disabled")
                notes.append(f"{order.client_order_id}: execution disabled")
                await self._publish_order(order, EventType.PAPER_ORDER_UPDATED, now)
                orders.append(order)
                continue

            self.oms.transition(order.client_order_id, OrderStatus.SUBMITTING)
            order.submitted_at = now
            self._pending[order.client_order_id] = _Pending(
                ack_at=now + self._latency(order.venue)
            )
            await self._publish_order(order, EventType.PAPER_ORDER_CREATED, now)
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

    async def cancel(self, client_order_id: str, now_ms: Millis) -> None:
        order = self.oms.get(client_order_id)
        if order is None or not order.is_live:
            return
        if order.status in (OrderStatus.CREATED, OrderStatus.SUBMITTING):
            # A pre-ack cancel is explicit state, not a timing side-channel.
            # Preserve the original arrival instant and consume the request
            # deterministically on arrival before the order can work.
            self.oms.transition(client_order_id, OrderStatus.CANCEL_PENDING)
            pending = self._pending.setdefault(
                client_order_id, _Pending(ack_at=now_ms)
            )
            pending.cancel_at = now_ms
            await self._publish_order(
                order, EventType.PAPER_ORDER_UPDATED, now_ms
            )
            return
        self.oms.transition(client_order_id, OrderStatus.CANCEL_PENDING)
        pending = self._pending.setdefault(client_order_id, _Pending(ack_at=now_ms))
        pending.cancel_at = now_ms + self.settings.venue(order.venue).cancel_latency_ms
        await self._publish_order(order, EventType.PAPER_ORDER_UPDATED, now_ms)

    async def cancel_all(self, now_ms: Millis) -> int:
        live = self.oms.live_orders()
        for order in live:
            await self.cancel(order.client_order_id, now_ms)
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
                await self._publish_order(order, EventType.PAPER_ORDER_UPDATED, now_ms)
                continue

            if now_ms < pending.ack_at:
                continue

            # A cancellation requested before acknowledgement is cancel-on-
            # arrival. It never enters the working/fill path and therefore
            # cannot lose the ordinary post-ack cancel/fill race.
            if (
                order.status is OrderStatus.CANCEL_PENDING
                and pending.cancel_at is not None
                and pending.cancel_at <= pending.ack_at
            ):
                self.oms.transition(order.client_order_id, OrderStatus.CANCELLED)
                await self._publish_order(
                    order, EventType.PAPER_ORDER_UPDATED, now_ms
                )
                continue

            if order.status is OrderStatus.SUBMITTING:
                self.oms.transition(order.client_order_id, OrderStatus.ACKNOWLEDGED)
                order.acknowledged_at = now_ms
                self.oms.transition(order.client_order_id, OrderStatus.OPEN)
                await self._publish_order(order, EventType.PAPER_ORDER_UPDATED, now_ms)

            view = self._book_view(order)

            # A cancel that has arrived may still lose the race to a fill.
            if order.status is OrderStatus.CANCEL_PENDING:
                if pending.cancel_at is not None and now_ms >= pending.cancel_at:
                    if self.simulator.cancel_wins_race(order, view):
                        self.oms.transition(order.client_order_id, OrderStatus.CANCELLED)
                        await self._publish_order(order, EventType.PAPER_ORDER_UPDATED, now_ms)
                        continue
                else:
                    continue

            fill = self._attempt_fill(order, view, now_ms)
            if fill is not None:
                fills.append(fill)
                await self._record_fill(order, fill, now_ms)

            if order.is_terminal:
                continue

            if order.expires_at is not None and now_ms >= order.expires_at:
                target = (
                    OrderStatus.EXPIRED
                    if order.filled_quantity <= 0
                    else OrderStatus.CANCELLED
                )
                self.oms.transition(order.client_order_id, target)
                await self._publish_order(order, EventType.PAPER_ORDER_UPDATED, now_ms)

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

    async def _record_fill(
        self, order: PaperOrder, fill: FillEvent, now_ms: Millis
    ) -> None:
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
        await self._publish_order(order, EventType.PAPER_ORDER_UPDATED, now_ms)

    # -- identity ----------------------------------------------------------

    @property
    def name(self) -> str:
        return NAME

    @property
    def version(self) -> str:
        return VERSION

    @property
    def capabilities(self) -> ExecutorCapabilities:
        return PAPER_CAPABILITIES

    # -- unknown resolution ------------------------------------------------

    async def resolve_unknown(
        self,
        client_order_id: str,
        authoritative_status: OrderStatus,
        now_ms: Millis,
    ) -> ExecutionCommandResult:
        """Apply an authoritative answer to an UNKNOWN order.

        NOTHING HERE DECIDES WHAT THE ANSWER IS
        =======================================
        The caller supplies ``authoritative_status`` because the caller is the
        one holding evidence. This executor has none: the paper venue is this
        object, and it does not learn anything by being asked again. It refuses
        to resolve an order that is not UNKNOWN, and it never invents a status.

        Nothing in this build calls this. It is the seam reconciliation (Phase
        7) will use when it has a venue's answer to apply, and it exists now so
        that when it does, it will not have to reach into the OMS.
        """
        order = self.oms.get(client_order_id)
        if order is None:
            return ExecutionCommandResult(
                accepted=False,
                client_order_id=client_order_id,
                reason="no such order",
                at_ms=now_ms,
            )
        if order.status is not OrderStatus.UNKNOWN:
            return ExecutionCommandResult(
                accepted=False,
                client_order_id=client_order_id,
                plan_id=order.plan_id,
                status=order.status,
                reason=f"order is {order.status.value}, not UNKNOWN",
                at_ms=now_ms,
            )

        self.oms.resolve_unknown(client_order_id, authoritative_status)
        await self._publish_order(order, EventType.PAPER_ORDER_UPDATED, now_ms)
        return ExecutionCommandResult(
            accepted=True,
            client_order_id=client_order_id,
            plan_id=order.plan_id,
            status=order.status,
            reason="resolved by an authoritative caller",
            at_ms=now_ms,
        )

    # -- queries -----------------------------------------------------------

    def open_orders(self) -> list[PaperOrder]:
        """Orders known to be working. Excludes UNKNOWN — see below."""
        return self.oms.live_orders()

    def outstanding_orders(self) -> list[PaperOrder]:
        """Orders whose final venue truth is not yet known.

        Everything :meth:`open_orders` returns, plus the UNKNOWN ones. The
        difference is exactly the set of orders that might still turn out to
        have traded while nobody can say so.
        """
        return self.oms.outstanding_orders()

    def unknown_orders(self) -> list[PaperOrder]:
        return self.oms.unknown_orders()

    def all_orders(self) -> list[PaperOrder]:
        return self.oms.all_orders()

    def get_order(self, client_order_id: str) -> PaperOrder | None:
        return self.oms.get(client_order_id)

    def orders_for_plan(self, plan_id: str) -> list[PaperOrder]:
        return self.oms.orders_for_plan(plan_id)

    def execution_snapshot(self, now_ms: Millis) -> ExecutionSnapshot:
        """What this executor believes at the supplied logical instant.

        Built entirely from the OMS, which is the book of record. Nothing here
        keeps a second copy of order state to fall out of step with it.
        """
        resident = self.oms.all_orders()
        return ExecutionSnapshot(
            created_at=now_ms,
            orders=[OrderSummary.of(o) for o in resident],
            open_order_ids=[o.client_order_id for o in resident if o.is_live],
            outstanding_order_ids=[
                o.client_order_id for o in resident if o.is_outstanding
            ],
            unknown_order_ids=[
                o.client_order_id
                for o in resident
                if o.status is OrderStatus.UNKNOWN
            ],
            counts_by_status=self.oms.counts_by_status(),
            fills_applied=self.oms.fills_applied,
            orders_created=self.oms.orders_created,
            duplicate_fills=self.oms.duplicate_fills,
            illegal_transitions=self.oms.illegal_transitions,
            archived_orders=self.oms.archived.count,
        )

    # -- retention ---------------------------------------------------------

    def compact_terminal_state(self, *, unsealed_fills: set[str]) -> int:
        """Release per-order bookkeeping the platform has finished with.

        Delegates the order-side decision to ``OrderManager.compact``, which
        already refuses anything that is not terminal or whose fills are still
        awaiting reconciliation — and therefore never touches an UNKNOWN order,
        because UNKNOWN is not terminal.

        This executor's own ``_pending`` records are deliberately left alone in
        this construction pass. Deciding when a venue-side timing record stops
        being needed is a retention question with a safety edge (an UNKNOWN
        order's arrival and cancel schedule are still needed if it ever
        resolves), and answering it by deleting things now would be exactly the
        kind of unmeasured change this phase is meant to avoid. The hook is
        here; the policy is not.
        """
        return self.oms.compact(unsealed_fills)

    async def _publish_order(
        self, order: PaperOrder, event_type: EventType, now_ms: Millis
    ) -> None:
        await self.bus.publish(
            Event(
                type=event_type,
                ts_ms=now_ms,
                source=SERVICE,
                schema_name="PaperOrder",
                correlation_id=order.correlation_id,
                payload=order.to_json_dict(),
            )
        )
