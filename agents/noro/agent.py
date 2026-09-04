"""NORO — fair-value agent.

Answers one question about a proposed cross-venue trade: *does the valuation
discrepancy actually exist?*  The signal is how much of the opportunity's
direction is confirmed by the liquidity-weighted fair value, saturating at the
configured deviation.
"""

from __future__ import annotations

from agents.noro.fair_value import FairValue, compute_fair_value
from core.bus import EventBus
from core.clock import Clock
from core.config import Settings
from core.events import Event, EventType
from core.health import HealthRegistry
from core.models.agent import AgentOpinion
from core.models.common import AgentId, Millis, Side
from core.models.market import MarketState
from core.models.opportunity import Opportunity
from core.models.ops import HealthStatus

SERVICE = "NORO"
VERSION = "noro-0.1"


def _clamp(value: float, low: float = -1.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


class Noro:
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
        self.fair_values: dict[str, FairValue] = {}
        self.evaluations = 0
        health.register(SERVICE, VERSION)

    def subscribe(self) -> None:
        self.bus.subscribe(
            self.on_event,
            types=[EventType.MARKET_STATE, EventType.OPPORTUNITY_DETECTED],
            name="noro",
        )

    async def on_event(self, event: Event) -> None:
        if event.type is EventType.MARKET_STATE:
            self.on_market_state(MarketState.model_validate(event.payload))
        elif event.type is EventType.OPPORTUNITY_DETECTED:
            opinion = self.evaluate(
                Opportunity.model_validate(event.payload), event.ts_ms
            )
            if opinion is not None:
                await self.publish(opinion)

    def on_market_state(self, state: MarketState) -> None:
        """Recompute fair value for every symbol TIDAL can price."""
        self.market = state
        self.fair_values = {}
        for symbol in self.settings.symbols:
            fair = compute_fair_value(symbol, state.states_for(symbol), self.settings.noro)
            if fair is not None:
                self.fair_values[symbol] = fair
        self._heartbeat()

    def fair_value(self, symbol: str) -> FairValue | None:
        return self.fair_values.get(symbol)

    # -- evaluation --------------------------------------------------------

    def evaluate(
        self, opportunity: Opportunity, now_ms: Millis
    ) -> AgentOpinion | None:
        """Score how well fair value confirms the opportunity's direction.

        Returns ``None`` when NORO cannot form a view — the orchestrator then
        sees a *missing* agent, which suspends the strategy.  It never sees a
        fabricated neutral opinion.

        ``now_ms`` is the request's logical time -- the tick that asked for
        this opinion, carried on the OPPORTUNITY_DETECTED event -- never a
        clock read taken when this subscriber happened to be scheduled
        (Phase 2 Batch 1.4). The opinion's ``created_at``/``expires_at``
        decide its freshness at every later tick, so a live-clock read here
        would let an opinion outlive its replayed twin purely because
        dispatch was slower in one run than in the other.
        """
        fair = self.fair_values.get(opportunity.symbol)
        if fair is None or self.market is None:
            return None

        now = now_ms
        reasons: list[str] = []
        confirmations: list[float] = []
        detail: dict[str, float | int | str | bool | None] = {
            "fair_value": fair.fair_value,
            "total_liquidity": fair.total_liquidity,
            "venues_priced": len(fair.venues),
        }

        for leg in opportunity.legs:
            deviation = fair.deviation(leg.venue)
            if deviation is None:
                return None
            detail[f"deviation_bps_{leg.venue}"] = deviation
            # Buying should happen where the venue trades below fair value
            # (negative deviation); selling where it trades above.
            confirmation = -deviation if leg.side is Side.BUY else deviation
            confirmations.append(confirmation)

        if not confirmations:
            return None

        # The trade is only confirmed to the extent that *both* legs agree; the
        # weakest leg governs, so a single rich venue cannot carry the trade.
        edge_bps = min(confirmations) + sum(confirmations) / len(confirmations)
        signal = _clamp(edge_bps / self.settings.noro.saturation_bps)
        detail["confirmed_edge_bps"] = edge_bps

        if signal > 0:
            reasons.append("FAIR_VALUE_CONFIRMS_DISLOCATION")
        else:
            reasons.append("FAIR_VALUE_CONTRADICTS_DISLOCATION")
        if any(c < 0 for c in confirmations):
            reasons.append("LEG_AGAINST_FAIR_VALUE")

        # Confidence rises with the liquidity behind the estimate and with the
        # number of venues that could be priced.
        liquidity_confidence = min(1.0, fair.total_liquidity / 250_000.0)
        breadth = min(1.0, len(fair.venues) / 2.0)
        confidence = _clamp(0.35 + 0.45 * liquidity_confidence + 0.2 * breadth, 0.0, 1.0)

        self.evaluations += 1
        return AgentOpinion(
            agent_id=AgentId.NORO,
            symbol=opportunity.symbol,
            created_at=now,
            source_data_timestamp=self.market.source_data_timestamp,
            correlation_id=opportunity.opportunity_id,
            signal=signal,
            confidence=confidence,
            expires_at=now + self.settings.noro.ttl_ms,
            reason_codes=reasons,
            model_version=VERSION,
            detail=detail,
        )

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
        priced = len(self.fair_values)
        expected = len(self.settings.symbols)
        status = HealthStatus.HEALTHY
        detail = ""
        if priced == 0:
            status = HealthStatus.OFFLINE
            detail = "no symbol priceable"
        elif priced < expected:
            status = HealthStatus.DEGRADED
            detail = f"priced {priced}/{expected} symbols"
        self.health.heartbeat(
            SERVICE,
            status=status,
            queue_depth=self.bus.queue_depth,
            version=VERSION,
            detail=detail,
        )
