"""VESKA — the execution engine.

Turns an authorised intent into a concrete plan and hands it to the executor.
VESKA decides order type, size, routing, maker/taker and cancellation
behaviour; it does not decide *whether* to trade — that decision was already
made, and validated by RUNE, before anything reaches here.

PLAN LIFECYCLE (Phase 6)
========================
VESKA owns an :class:`~execution.veska.registry.ExecutionRegistry`: one record
per plan, tracking what became of it. The registry is state and observability
only — it never submits, cancels, sizes or fills anything, and no fill decision
reads it. What it buys is that "what is happening with this trade's execution?"
now has one place to ask, instead of being assembled by each caller from the
OMS, the executor's private timing records and an orchestrator dictionary.

Every registry write takes the caller's logical instant, for the same reason
the executor does: a record stamped from a clock read is a record replay cannot
reconstruct (P2-14).
"""

from __future__ import annotations

import logging

from core.bus import EventBus
from core.clock import Clock
from core.config import Settings
from core.events import Event, EventType
from core.health import HealthRegistry
from core.models.common import Millis
from core.models.execution import (
    ExecutionCommandResult,
    ExecutionMetrics,
    ExecutionPlanRecord,
    ExecutionPlanStatus,
    ExecutionRole,
    ExecutionReport,
    ExecutionSnapshot,
    ExecutorCapabilities,
    FillEvent,
    OrderStatus,
    PaperOrder,
)
from core.models.market import MarketState
from core.models.opportunity import ExecutionPlan, PlannedOrder, TradeIntent
from core.models.ops import HealthStatus
from core.models.risk import RiskDecision
from execution.router import VenueRouter
from execution.veska.executor import Executor
from execution.veska.preflight import PlanPreflight, preflight_plan
from execution.veska.registry import ExecutionRegistry

log = logging.getLogger(__name__)

SERVICE = "VESKA"
VERSION = "veska-0.2"


class PlanningError(RuntimeError):
    pass


class Veska:
    def __init__(
        self,
        bus: EventBus,
        clock: Clock,
        settings: Settings,
        health: HealthRegistry,
        executor: Executor,
    ) -> None:
        if not executor.is_paper:
            # Structural guard, matching the venue-adapter guard: this build
            # cannot be wired to anything that reaches a real exchange.
            raise RuntimeError("this build accepts paper executors only")
        self.bus = bus
        self.clock = clock
        self.settings = settings
        self.health = health
        self.executor = executor
        self.router = VenueRouter(settings)
        self.registry = ExecutionRegistry()
        self.plan_failures = 0
        health.register(SERVICE, VERSION)

    # -- compatibility -----------------------------------------------------

    @property
    def plans(self) -> dict[str, ExecutionPlan]:
        """Every plan VESKA has built, keyed by ``plan_id``.

        Retained as a read-only view over the registry so existing callers keep
        working unchanged. New code should use :meth:`get_plan`,
        :meth:`active_plans` and :meth:`unresolved_plans` instead — those
        answer questions about a plan's *state*, which this dictionary never
        could, and they do not require the caller to know how VESKA stores
        anything.

        Migration note: this is a view, not a store. Assigning into it no
        longer registers a plan; :meth:`build_plan` does that.
        """
        return self.registry.plans

    # -- planning ----------------------------------------------------------

    def build_plan(
        self,
        intent: TradeIntent,
        decision: RiskDecision,
        market: MarketState,
        now_ms: Millis,
    ) -> ExecutionPlan | None:
        """Build the plan for an approved intent, sized to what RUNE allowed.

        ``now_ms`` is the caller's canonical logical time (the orchestrator's
        tick time), not a clock read taken here — see ``Executor``'s
        "EXECUTION TIME IS ALWAYS EXPLICIT" note.
        """
        notional = decision.approved_notional
        if notional <= 0:
            return None

        orders: list[PlannedOrder] = []
        for leg in intent.legs:
            routing = self.router.route(
                leg,
                market,
                urgency=intent.urgency,
                max_slippage_bps=intent.max_slippage_bps,
            )
            if routing is None:
                self.plan_failures += 1
                return None
            if routing.expected_price <= 0:
                self.plan_failures += 1
                return None
            # ENTRY is risk-increasing and is always bounded by the notional
            # RUNE authorised. EXIT/HEDGE may carry an exact quantity because
            # their job is to neutralise exposure that already exists.
            if intent.execution_role is ExecutionRole.ENTRY:
                quantity = notional / routing.expected_price
            elif leg.quantity is not None and leg.quantity > 0:
                quantity = leg.quantity
            else:
                quantity = notional / routing.expected_price
            if quantity <= 0:
                continue
            fees = self.settings.venue(leg.venue).fees
            orders.append(
                PlannedOrder(
                    venue=routing.venue,
                    symbol=leg.symbol,
                    side=leg.side,
                    quantity=quantity,
                    order_type=routing.order_type,
                    time_in_force=routing.time_in_force,
                    limit_price=routing.limit_price,
                    expected_price=routing.expected_price,
                    expected_fee_bps=fees.fee_bps(routing.is_maker),
                    ttl_ms=self.settings.execution.default_order_ttl_ms,
                )
            )

        if not orders:
            return None
        plan = ExecutionPlan(
            created_at=now_ms,
            source_data_timestamp=market.source_data_timestamp,
            correlation_id=intent.correlation_id,
            intent_id=intent.intent_id,
            strategy=intent.strategy,
            symbol=intent.symbol,
            orders=orders,
            deadline_ms=intent.deadline_ms,
            max_slippage_bps=intent.max_slippage_bps,
            # Canonical per-leg meaning, unchanged. The two fields below make
            # requested-versus-authorised explicit without reinterpreting it.
            notional=notional,
            requested_notional=intent.notional,
            approved_notional=notional,
            execution_role=intent.execution_role,
        )
        self.registry.register_plan(plan, now_ms)
        return plan

    def preflight(self, plan: ExecutionPlan) -> PlanPreflight:
        """Check a plan's structural workability against this executor.

        Construction phase: offered, not enforced. ``execute`` does not consult
        it, because turning a new check into a submission gate is a behavioural
        change and this pass makes none. The seam exists so a later pass has
        one place to put the checks it has evidence for.
        """
        return preflight_plan(plan, capabilities=self.executor.capabilities)

    # -- execution ---------------------------------------------------------

    async def execute(self, plan: ExecutionPlan, now_ms: Millis) -> ExecutionReport:
        # A plan id names one immutable execution attempt. A build_plan-created
        # record may exist before submission, but reusing that id for different
        # instructions must fail closed rather than inherit the old record.
        existing_plan = self.registry.plan(plan.plan_id)
        if existing_plan is not None and existing_plan != plan:
            raise ValueError(
                f"plan_id {plan.plan_id} is already registered with different instructions"
            )

        record = self.registry.register_plan(plan, now_ms)
        resident = self.executor.orders_for_plan(plan.plan_id)
        if resident:
            planned_ids = [o.client_order_id for o in plan.orders]
            resident_ids = [o.client_order_id for o in resident]
            if (
                len(resident_ids) != len(planned_ids)
                or set(resident_ids) != set(planned_ids)
            ):
                raise RuntimeError(
                    f"plan {plan.plan_id} is partially submitted; existing orders "
                    f"{resident_ids} do not match planned orders {planned_ids}"
                )
            if any(note.startswith("submission interrupted") for note in record.notes):
                raise RuntimeError(
                    f"plan {plan.plan_id} has orders from an interrupted submission; "
                    "cancel or reconcile them before retrying"
                )
            return ExecutionReport(
                created_at=now_ms,
                correlation_id=plan.correlation_id,
                plan_id=plan.plan_id,
                intent_id=plan.intent_id,
                orders=resident,
                complete=False,
                notes=["idempotent retry: existing order truth returned"],
            )

        self.registry.set_status(plan.plan_id, ExecutionPlanStatus.SUBMITTING, now_ms)

        await self.bus.publish(
            Event(
                type=EventType.EXECUTION_PLAN,
                ts_ms=plan.created_at,
                source=SERVICE,
                schema_name="ExecutionPlan",
                correlation_id=plan.correlation_id,
                payload=plan.to_json_dict(),
            )
        )

        try:
            report = await self.executor.submit(plan, now_ms)
        except Exception:
            actual_orders = self.executor.orders_for_plan(plan.plan_id)
            self.registry.attach_orders(
                plan.plan_id,
                [o.client_order_id for o in actual_orders],
                now_ms,
            )
            self.refresh_plan(plan.plan_id, now_ms)
            self.registry.note(
                plan.plan_id,
                f"submission interrupted after creating {len(actual_orders)} order(s)",
                now_ms,
            )
            raise

        self.registry.attach_orders(
            plan.plan_id, [o.client_order_id for o in report.orders], now_ms
        )
        self.refresh_plan(plan.plan_id, now_ms)

        await self.bus.publish(
            Event(
                type=EventType.EXECUTION_REPORT,
                ts_ms=report.created_at,
                source=SERVICE,
                schema_name="ExecutionReport",
                correlation_id=plan.correlation_id,
                payload=report.to_json_dict(),
            )
        )
        return report

    async def poll(self, now_ms: Millis) -> list[FillEvent]:
        fills = await self.executor.poll(now_ms)
        self._refresh_plans_for_fills(fills, now_ms)
        return fills

    async def cancel(self, client_order_id: str, now_ms: Millis) -> None:
        await self.executor.cancel(client_order_id, now_ms)
        record = self.registry.plan_for_order(client_order_id)
        if record is not None:
            self.refresh_plan(record.plan_id, now_ms)

    async def cancel_all(self, now_ms: Millis) -> int:
        count = await self.executor.cancel_all(now_ms)
        self.refresh_all_plans(now_ms)
        return count

    async def cancel_plan(
        self, plan_id: str, now_ms: Millis
    ) -> ExecutionCommandResult:
        """Cancel every outstanding order belonging to one plan.

        The plan-level control surface a future operator action and Phase 7
        both need. It cancels what the plan owns and nothing else, which is
        what distinguishes it from :meth:`cancel_all` — the kill switch's
        blunt instrument, which stays exactly as it is.

        Orders are selected by :attr:`PaperOrder.is_outstanding` rather than by
        ``is_live``: an order whose state is UNKNOWN might still be working,
        and leaving it out of a cancellation because nobody knows what it is
        doing would be the wrong way round. The executor decides what it can
        actually act on.
        """
        record = self.registry.get(plan_id)
        if record is None:
            return ExecutionCommandResult(
                accepted=False,
                plan_id=plan_id,
                reason="no such plan",
                at_ms=now_ms,
            )

        requested = 0
        for client_order_id in record.order_ids:
            order = self.executor.get_order(client_order_id)
            if order is None or not order.is_outstanding:
                continue
            await self.executor.cancel(client_order_id, now_ms)
            requested += 1

        self.registry.note(plan_id, f"cancel requested for {requested} order(s)", now_ms)
        refreshed = self.refresh_plan(plan_id, now_ms)
        return ExecutionCommandResult(
            accepted=True,
            plan_id=plan_id,
            plan_status=refreshed.status if refreshed is not None else record.status,
            reason=f"cancel requested for {requested} outstanding order(s)",
            at_ms=now_ms,
        )

    async def resolve_unknown(
        self,
        client_order_id: str,
        authoritative_status: OrderStatus,
        now_ms: Millis,
    ) -> ExecutionCommandResult:
        """Pass an authoritative answer for an UNKNOWN order to the executor.

        VESKA adds nothing to the decision: it forwards the caller's answer and
        refreshes the affected plan. Nothing in this build calls it — see
        ``Executor.resolve_unknown``.
        """
        result = await self.executor.resolve_unknown(
            client_order_id, authoritative_status, now_ms
        )
        record = self.registry.plan_for_order(client_order_id)
        if record is not None:
            refreshed = self.refresh_plan(record.plan_id, now_ms)
            return result.model_copy(
                update={
                    "plan_id": record.plan_id,
                    "plan_status": (
                        refreshed.status if refreshed is not None else record.status
                    ),
                }
            )
        return result

    # -- plan state --------------------------------------------------------

    def refresh_plan(
        self, plan_id: str, now_ms: Millis
    ) -> ExecutionPlanRecord | None:
        """Recompute one plan's status from the orders it owns."""
        return self.registry.refresh(
            plan_id, self.executor.orders_for_plan(plan_id), now_ms
        )

    def refresh_all_plans(self, now_ms: Millis) -> None:
        """Recompute every plan that is still expected to move."""
        for record in [*self.registry.active_plans(), *self.registry.unresolved_plans()]:
            self.refresh_plan(record.plan_id, now_ms)

    def _refresh_plans_for_fills(
        self, fills: list[FillEvent], now_ms: Millis
    ) -> None:
        touched: set[str] = set()
        for fill in fills:
            record = self.registry.plan_for_order(fill.client_order_id)
            if record is not None:
                touched.add(record.plan_id)
        for plan_id in touched:
            self.refresh_plan(plan_id, now_ms)

    # -- queries -----------------------------------------------------------

    def open_orders(self) -> list[PaperOrder]:
        """Orders known to be working. Excludes UNKNOWN."""
        return self.executor.open_orders()

    def outstanding_orders(self) -> list[PaperOrder]:
        """Orders whose final venue truth is not yet known."""
        return self.executor.outstanding_orders()

    def unknown_orders(self) -> list[PaperOrder]:
        return self.executor.unknown_orders()

    def get_plan(self, plan_id: str) -> ExecutionPlanRecord | None:
        return self.registry.get(plan_id)

    def active_plans(self) -> list[ExecutionPlanRecord]:
        return self.registry.active_plans()

    def unresolved_plans(self) -> list[ExecutionPlanRecord]:
        return self.registry.unresolved_plans()

    def plans_for_intent(self, intent_id: str) -> list[ExecutionPlanRecord]:
        return self.registry.plans_for_intent(intent_id)

    def plans_for_correlation(self, correlation_id: str) -> list[ExecutionPlanRecord]:
        return self.registry.plans_for_correlation(correlation_id)

    @property
    def capabilities(self) -> ExecutorCapabilities:
        return self.executor.capabilities

    def metrics(self) -> ExecutionMetrics:
        """Counters describing what execution has done this session.

        Totals only. No thresholds, no rates, no judgement about what a healthy
        number looks like — that belongs to whoever later has evidence.
        """
        outstanding = self.executor.outstanding_orders()
        unknown = self.executor.unknown_orders()
        resident = self.executor.all_orders()
        return ExecutionMetrics(
            plans_created=self.registry.plans_registered,
            plans_submitted=self.registry.plans_submitted,
            plans_completed=self.registry.plans_completed,
            plans_cancelled=self.registry.plans_cancelled,
            plans_failed=self.registry.plans_failed,
            orders_created=sum(1 for _ in resident),
            orders_outstanding=len(outstanding),
            orders_unknown=len(unknown),
            fills=sum(len(o.fills) for o in resident),
            partial_fills=sum(
                1 for o in resident if 0 < o.filled_quantity < o.quantity
            ),
            cancel_requests=sum(
                1 for o in resident if o.status is OrderStatus.CANCEL_PENDING
            ),
        )

    def execution_snapshot(self, now_ms: Millis) -> ExecutionSnapshot:
        """One canonical view of execution at ``now_ms``, plans included.

        The surface future reconciliation consumes. It takes the executor's
        order-level snapshot and adds VESKA's plan-level view, so a reader
        never has to combine the two by hand — or reach into either.
        """
        snapshot = self.executor.execution_snapshot(now_ms)
        return snapshot.model_copy(
            update={
                "active_plan_ids": [r.plan_id for r in self.registry.active_plans()],
                "unresolved_plan_ids": [
                    r.plan_id for r in self.registry.unresolved_plans()
                ],
                "metrics": self.metrics(),
            }
        )

    def heartbeat(self) -> None:
        open_count = len(self.open_orders())
        status = HealthStatus.HEALTHY
        detail = f"{open_count} open orders"
        if open_count >= self.settings.risk.max_open_orders:
            status = HealthStatus.DEGRADED
            detail = "open-order limit reached"
        self.health.heartbeat(
            SERVICE,
            status=status,
            queue_depth=self.bus.queue_depth,
            version=VERSION,
            detail=detail,
        )
