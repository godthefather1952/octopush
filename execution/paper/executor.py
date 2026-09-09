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
from core.models.common import QTY_EPSILON, Millis, TimeInForce
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
from execution.paper.simulator import (
    BookView,
    FillSimulator,
    is_marketable,
    would_cross,
)
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
class _DeferredOrderUpdate:
    """A derived order snapshot whose canonical fill is already accepted."""

    snapshot: PaperOrder
    ts_ms: Millis


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
    #: Last observed rolling-volume stock per venue/symbol. Queue progress is
    #: credited from positive deltas only, never from repeated observations of
    #: the same rolling window.
    _last_rolling_volume: dict[tuple[str, str], float] = field(default_factory=dict)
    #: Derived order updates that failed after a PAPER_FILL was already
    #: accepted. They are retried before the affected order can advance again.
    _pending_order_updates: dict[str, _DeferredOrderUpdate] = field(
        default_factory=dict
    )
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
        """Credit resting orders only for newly observed rolling-volume flow."""
        if self.market is None:
            return

        deltas: dict[tuple[str, str], float] = {}
        for state in self.market.venues.values():
            key = (state.venue, state.symbol)
            current = state.metrics.buy_volume + state.metrics.sell_volume
            previous = self._last_rolling_volume.get(key)
            self._last_rolling_volume[key] = current
            deltas[key] = (
                0.0 if previous is None else max(0.0, current - previous)
            )

        for order in self.oms.live_orders():
            pending = self._pending.get(order.client_order_id)
            if pending is None or order.limit_price is None:
                continue
            delta = deltas.get((order.venue, order.symbol), 0.0)
            if delta > 0:
                pending.traded_through += delta * 0.01

    def _latency(self, venue: str) -> int:
        """Configured venue latency; unknown venues never receive defaults."""
        return self.settings.venue(venue).latency_ms

    async def _publish_then_commit_order(
        self,
        order: PaperOrder,
        statuses: tuple[OrderStatus, ...],
        now_ms: Millis,
        *,
        event_type: EventType = EventType.PAPER_ORDER_UPDATED,
        submitted_at: Millis | None = None,
        acknowledged_at: Millis | None = None,
        reject_reason: str | None = None,
    ) -> PaperOrder:
        """Publish a projected order state before committing it to the OMS."""
        projected = order.model_copy(deep=True)
        if submitted_at is not None:
            projected.submitted_at = submitted_at
        if acknowledged_at is not None:
            projected.acknowledged_at = acknowledged_at
        if reject_reason is not None:
            projected.reject_reason = reject_reason
        for status in statuses:
            projected.transition(status, now_ms)

        await self._publish_order(projected, event_type, now_ms)

        if submitted_at is not None:
            order.submitted_at = submitted_at
        if acknowledged_at is not None:
            order.acknowledged_at = acknowledged_at
        if reject_reason is not None:
            order.reject_reason = reject_reason
        for status in statuses:
            self.oms.transition(order.client_order_id, status, now_ms=now_ms)
        return order

    async def _retry_deferred_order_update(self, client_order_id: str) -> None:
        deferred = self._pending_order_updates.get(client_order_id)
        if deferred is None:
            return
        await self._publish_order(
            deferred.snapshot,
            EventType.PAPER_ORDER_UPDATED,
            deferred.ts_ms,
        )
        self._pending_order_updates.pop(client_order_id, None)

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
        enabled_venues = {venue.name for venue in self.settings.enabled_venues}
        for planned in plan.orders:
            if planned.client_order_id in seen_ids:
                duplicate_ids.add(planned.client_order_id)
            seen_ids.add(planned.client_order_id)
            if planned.venue not in enabled_venues:
                raise ValueError(
                    f"venue {planned.venue!r} is not configured and enabled"
                )
            if not PAPER_CAPABILITIES.supports_order_type(planned.order_type):
                raise ValueError(
                    f"unsupported order type: {planned.order_type.value}"
                )
            if not PAPER_CAPABILITIES.supports_time_in_force(
                planned.time_in_force
            ):
                raise ValueError(
                    f"unsupported time in force: {planned.time_in_force.value}"
                )
        if duplicate_ids:
            duplicates = ", ".join(sorted(duplicate_ids))
            raise ValueError(
                f"duplicate client_order_id(s) in plan {plan.plan_id}: {duplicates}"
            )

        for planned in plan.orders:
            order = self.oms.from_plan(
                planned,
                plan_id=plan.plan_id,
                intent_id=plan.intent_id,
                strategy=plan.strategy,
                now_ms=now,
            )
            order.correlation_id = plan.correlation_id
            if self.execution_disabled:
                await self._publish_then_commit_order(
                    order,
                    (OrderStatus.REJECTED,),
                    now,
                    reject_reason="execution disabled",
                )
                self.rejected_submissions += 1
                notes.append(f"{order.client_order_id}: execution disabled")
                orders.append(order)
                continue

            await self._publish_then_commit_order(
                order,
                (OrderStatus.SUBMITTING,),
                now,
                event_type=EventType.PAPER_ORDER_CREATED,
                submitted_at=now,
            )
            self._pending[order.client_order_id] = _Pending(
                ack_at=now + self._latency(order.venue)
            )
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
            await self._publish_then_commit_order(
                order, (OrderStatus.CANCEL_PENDING,), now_ms
            )
            pending = self._pending.setdefault(
                client_order_id, _Pending(ack_at=now_ms)
            )
            pending.cancel_at = now_ms
            return

        await self._publish_then_commit_order(
            order, (OrderStatus.CANCEL_PENDING,), now_ms
        )
        pending = self._pending.setdefault(client_order_id, _Pending(ack_at=now_ms))
        pending.cancel_at = (
            now_ms + self.settings.venue(order.venue).cancel_latency_ms
        )

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
            await self._retry_deferred_order_update(order.client_order_id)
            if order.is_terminal or order.status is OrderStatus.UNKNOWN:
                continue
            pending = self._pending.get(order.client_order_id)
            if pending is None:
                continue

            if pending.force_unknown:
                await self._publish_then_commit_order(
                    order,
                    (OrderStatus.UNKNOWN,),
                    now_ms,
                    reject_reason="simulated venue timeout",
                )
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
                await self._publish_then_commit_order(
                    order, (OrderStatus.CANCELLED,), now_ms
                )
                continue

            if order.status is OrderStatus.SUBMITTING:
                arrival_view = self._book_view(order)
                if (
                    order.time_in_force is TimeInForce.POST_ONLY
                    and would_cross(order, arrival_view)
                ):
                    await self._publish_then_commit_order(
                        order,
                        (OrderStatus.ACKNOWLEDGED, OrderStatus.REJECTED),
                        now_ms,
                        acknowledged_at=now_ms,
                        reject_reason="post-only order would take liquidity",
                    )
                    continue
                await self._publish_then_commit_order(
                    order,
                    (OrderStatus.ACKNOWLEDGED, OrderStatus.OPEN),
                    now_ms,
                    acknowledged_at=now_ms,
                )

            view = self._book_view(order)

            # A cancel that has arrived may still lose the race to a fill.
            if order.status is OrderStatus.CANCEL_PENDING:
                if pending.cancel_at is not None and now_ms >= pending.cancel_at:
                    if self.simulator.cancel_wins_race(order, view):
                        await self._publish_then_commit_order(
                            order, (OrderStatus.CANCELLED,), now_ms
                        )
                        continue
                else:
                    continue

            fill = self._attempt_fill(order, view, now_ms)
            if fill is not None:
                fills.append(fill)
                await self._record_fill(order, fill, now_ms)

            if order.is_terminal:
                continue

            if order.time_in_force is TimeInForce.IOC:
                await self._publish_then_commit_order(
                    order, (OrderStatus.CANCELLED,), now_ms
                )
                continue

            if order.expires_at is not None and now_ms >= order.expires_at:
                target = (
                    OrderStatus.EXPIRED
                    if order.filled_quantity <= 0
                    else OrderStatus.CANCELLED
                )
                await self._publish_then_commit_order(
                    order, (target,), now_ms
                )

        return fills

    def _source_timestamp(self, order: PaperOrder) -> Millis | None:
        """Authoritative exchange timestamp for exactly this order's leg."""
        if self.market is None:
            return None
        return self.market.source_data_timestamp_for(
            [(order.venue, order.symbol)]
        )

    def _synthetic_latency_ms(
        self,
        order: PaperOrder,
        pending: _Pending | None,
        source_ts: Millis | None,
    ) -> float:
        """Latency not already represented by the observed leg snapshot."""
        configured = float(self._latency(order.venue))
        if pending is None or source_ts is None:
            return configured
        return min(
            configured,
            float(max(0, pending.ack_at - source_ts)),
        )

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

        source_ts = self._source_timestamp(order)
        pending = self._pending.get(order.client_order_id)
        latency = self._synthetic_latency_ms(order, pending, source_ts)
        taking = is_marketable(order) or (
            order.time_in_force is not TimeInForce.POST_ONLY
            and would_cross(order, view)
        )
        simulated = (
            self.simulator.fill_marketable(
                order,
                view,
                self.settings.venue(order.venue).fees,
                latency,
            )
            if taking
            else self.simulator.fill_passive(
                order, view, self.settings.venue(order.venue).fees
            )
        )
        if simulated is None or simulated.quantity <= QTY_EPSILON:
            return None
        return self.simulator.build_fill(order, simulated, now_ms, source_ts)

    async def _record_fill(
        self, order: PaperOrder, fill: FillEvent, now_ms: Millis
    ) -> None:
        if not self.oms.validate_fill(fill, count_duplicate=True):
            return

        fill.realized_pnl_delta = self.account.preview_fill_realized_pnl(fill)
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

        if not self.oms.apply_fill(fill, now_ms=now_ms):
            return
        self.account.apply_fill(fill)

        snapshot = order.model_copy(deep=True)
        try:
            await self._publish_order(
                snapshot, EventType.PAPER_ORDER_UPDATED, now_ms
            )
        except Exception:
            self._pending_order_updates[order.client_order_id] = (
                _DeferredOrderUpdate(snapshot=snapshot, ts_ms=now_ms)
            )
            raise

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

        await self._publish_then_commit_order(
            order, (authoritative_status,), now_ms
        )
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
        """Release OMS and venue-timing state under one compaction decision.

        Only terminal orders with no unsealed fills are compactable. UNKNOWN is
        non-terminal, so its timing state survives automatically.
        """
        protected_fill_ids = {
            fill.fill_id
            for client_order_id in self._pending_order_updates
            for order in [self.oms.get(client_order_id)]
            if order is not None
            for fill in order.fills
        }
        effective_unsealed = set(unsealed_fills) | protected_fill_ids
        compactable = self.oms.compactable_order_ids(effective_unsealed)
        released = self.oms.compact(effective_unsealed)
        for client_order_id in compactable:
            self._pending.pop(client_order_id, None)
        return released

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
