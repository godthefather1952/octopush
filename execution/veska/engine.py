"""VESKA — the execution engine.

Turns an authorised intent into a concrete plan and hands it to the executor.
VESKA decides order type, size, routing, maker/taker and cancellation
behaviour; it does not decide *whether* to trade — that decision was already
made, and validated by RUNE, before anything reaches here.
"""

from __future__ import annotations

import logging

from core.bus import EventBus
from core.clock import Clock
from core.config import Settings
from core.events import Event, EventType
from core.health import HealthRegistry
from core.models.common import Millis
from core.models.execution import ExecutionReport, FillEvent, PaperOrder
from core.models.market import MarketState
from core.models.opportunity import ExecutionPlan, PlannedOrder, TradeIntent
from core.models.ops import HealthStatus
from core.models.risk import RiskDecision
from execution.router import VenueRouter
from execution.veska.executor import Executor

log = logging.getLogger(__name__)

SERVICE = "VESKA"
VERSION = "veska-0.1"


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
        self.plans: dict[str, ExecutionPlan] = {}
        self.plan_failures = 0
        health.register(SERVICE, VERSION)

    # -- planning ----------------------------------------------------------

    def build_plan(
        self,
        intent: TradeIntent,
        decision: RiskDecision,
        market: MarketState,
    ) -> ExecutionPlan | None:
        """Build the plan for an approved intent, sized to what RUNE allowed."""
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
            # An exit or hedge leg carries the exact quantity to trade; an
            # entry leg is sized from the notional RUNE authorised.
            quantity = (
                leg.quantity
                if leg.quantity is not None and leg.quantity > 0
                else notional / routing.expected_price
            )
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
            created_at=self.clock.now_ms(),
            source_data_timestamp=market.source_data_timestamp,
            correlation_id=intent.correlation_id,
            intent_id=intent.intent_id,
            strategy=intent.strategy,
            symbol=intent.symbol,
            orders=orders,
            deadline_ms=intent.deadline_ms,
            max_slippage_bps=intent.max_slippage_bps,
            notional=notional,
        )
        self.plans[plan.plan_id] = plan
        return plan

    # -- execution ---------------------------------------------------------

    async def execute(self, plan: ExecutionPlan) -> ExecutionReport:
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
        report = await self.executor.submit(plan)
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
        return await self.executor.poll(now_ms)

    async def cancel(self, client_order_id: str) -> None:
        await self.executor.cancel(client_order_id)

    async def cancel_all(self) -> int:
        return await self.executor.cancel_all()

    def open_orders(self) -> list[PaperOrder]:
        return self.executor.open_orders()

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
