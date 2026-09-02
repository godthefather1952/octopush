"""ZEPHR — liquidity and executability agent.

Prices the opportunity at several sizes against the real book, subtracts every
modelled cost, and reports the largest size that still carries edge.  Its
signal is how much net edge survives relative to what the strategy needs.
"""

from __future__ import annotations

import math

from agents.zephr.liquidity import SizingCurve, build_sizing_curve
from core.bus import EventBus
from core.clock import Clock
from core.config import Settings
from core.events import Event, EventType
from core.health import HealthRegistry
from core.models.agent import AgentOpinion
from core.models.common import AgentId, Side
from core.models.market import MarketState, PriceLevel
from core.models.opportunity import Opportunity
from core.models.ops import HealthStatus

SERVICE = "ZEPHR"
VERSION = "zephr-0.1"


class Zephr:
    def __init__(
        self,
        bus: EventBus,
        clock: Clock,
        settings: Settings,
        health: HealthRegistry,
    ) -> None:
        self.bus = bus
        self.clock = clock
        self.settings = settings
        self.health = health
        self.market: MarketState | None = None
        #: Latest curve per opportunity, consumed by the orchestrator when it
        #: sizes the trade intent.
        self.curves: dict[str, SizingCurve] = {}
        self.evaluations = 0
        health.register(SERVICE, VERSION)

    def subscribe(self) -> None:
        self.bus.subscribe(
            self.on_event,
            types=[EventType.MARKET_STATE, EventType.OPPORTUNITY_DETECTED],
            name="zephr",
        )

    async def on_event(self, event: Event) -> None:
        if event.type is EventType.MARKET_STATE:
            self.market = MarketState.model_validate(event.payload)
            self._heartbeat()
        elif event.type is EventType.OPPORTUNITY_DETECTED:
            opinion = self.evaluate(Opportunity.model_validate(event.payload))
            if opinion is not None:
                await self.publish(opinion)

    # -- evaluation --------------------------------------------------------

    def _levels(self, venue: str, symbol: str, side: Side) -> list[PriceLevel]:
        """Book levels the given side of a trade would consume.

        A buyer consumes asks, a seller consumes bids.
        """
        if self.market is None:
            return []
        state = self.market.venue_state(venue, symbol)
        if state is None or state.book is None:
            return []
        return state.book.asks if side is Side.BUY else state.book.bids

    def build_curve(self, opportunity: Opportunity) -> SizingCurve | None:
        if self.market is None:
            return None
        leg_inputs = []
        for leg in opportunity.legs:
            state = self.market.venue_state(leg.venue, leg.symbol)
            if state is None or not state.quality.is_usable:
                return None
            levels = self._levels(leg.venue, leg.symbol, leg.side)
            if not levels:
                return None
            fees = self.settings.venue(leg.venue).fees
            leg_inputs.append((state, leg.side, levels, fees))
        if not leg_inputs:
            return None
        return build_sizing_curve(
            opportunity.symbol,
            opportunity.gross_edge_bps,
            leg_inputs,
            self.settings.zephr,
            max_notional=self.settings.risk.max_order_notional,
        )

    def evaluate(self, opportunity: Opportunity) -> AgentOpinion | None:
        curve = self.build_curve(opportunity)
        if curve is None:
            # ZEPHR could not price the opportunity. The orchestrator sees a
            # missing required agent and suspends, rather than trading blind.
            return None
        self.curves[opportunity.opportunity_id] = curve
        self.evaluations += 1
        now = self.clock.now_ms()

        reasons: list[str] = []
        detail: dict[str, float | int | str | bool | None] = {
            "max_economical_notional": curve.max_economical_notional,
            "ladder_points": len(curve.points),
        }
        for point in curve.points:
            detail[f"net_edge_bps@{point.notional:.0f}"] = round(point.net_edge_bps, 3)

        if curve.best is None:
            signal = -1.0
            confidence = 0.9
            reasons.append("NO_ECONOMICAL_SIZE")
            worst = min(curve.points, key=lambda p: p.total_cost_bps, default=None)
            if worst is not None:
                detail["cheapest_cost_bps"] = round(worst.total_cost_bps, 3)
                detail["net_edge_at_min_size_bps"] = round(curve.points[0].net_edge_bps, 3)
            if any(p.legs and any(leg.exhausted for leg in p.legs) for p in curve.points):
                reasons.append("INSUFFICIENT_DEPTH")
        else:
            best = curve.best
            detail["chosen_notional"] = best.notional
            detail["expected_net_edge_bps"] = round(best.net_edge_bps, 3)
            detail["expected_cost_bps"] = round(best.total_cost_bps, 3)
            detail["expected_profit"] = round(best.expected_profit, 4)
            for leg in best.legs:
                detail[f"expected_price_{leg.venue}"] = leg.expected_price
                detail[f"slippage_bps_{leg.venue}"] = round(leg.slippage_bps, 3)
                # Impact is unbounded when a book side is exhausted. The value
                # serialises as null, so state *why* rather than leaving a
                # bare null to be guessed at.
                detail[f"impact_status_{leg.venue}"] = (
                    "OK" if math.isfinite(leg.impact_bps) else "INSUFFICIENT_LIQUIDITY"
                )
            # Signal saturates at three times the minimum acceptable edge.
            floor = max(self.settings.zephr.min_net_edge_bps, 1e-9)
            signal = min(1.0, best.net_edge_bps / (floor * 3.0))
            confidence = min(
                1.0, 0.4 + 0.6 * min(1.0, best.legs[0].usable_liquidity / 200_000.0)
            )
            reasons.append("EDGE_SURVIVES_EXECUTION")
            if best.notional < curve.points[-1].notional:
                reasons.append("SIZE_CAPPED_BY_LIQUIDITY")

        return AgentOpinion(
            agent_id=AgentId.ZEPHR,
            symbol=opportunity.symbol,
            created_at=now,
            source_data_timestamp=self.market.source_data_timestamp if self.market else None,
            correlation_id=opportunity.opportunity_id,
            signal=signal,
            confidence=confidence,
            expires_at=now + self.settings.noro.ttl_ms,
            reason_codes=reasons,
            model_version=VERSION,
            detail=detail,
        )

    def curve_for(self, opportunity_id: str) -> SizingCurve | None:
        return self.curves.get(opportunity_id)

    def forget(self, opportunity_id: str) -> None:
        self.curves.pop(opportunity_id, None)

    async def publish(self, opinion: AgentOpinion) -> None:
        await self.bus.publish(
            Event(
                type=EventType.AGENT_OPINION,
                ts_ms=opinion.created_at,
                source=SERVICE,
                schema_name="AgentOpinion",
                correlation_id=opinion.correlation_id,
                payload=opinion.to_json_dict(),
            )
        )

    def _heartbeat(self) -> None:
        usable = 0
        if self.market is not None:
            usable = sum(1 for s in self.market.venues.values() if s.quality.is_usable)
        status = HealthStatus.HEALTHY if usable >= 2 else HealthStatus.DEGRADED
        if usable == 0:
            status = HealthStatus.OFFLINE
        self.health.heartbeat(
            SERVICE,
            status=status,
            queue_depth=self.bus.queue_depth,
            version=VERSION,
            detail=f"{usable} usable venue/symbol pairs",
        )
