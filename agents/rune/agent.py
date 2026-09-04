"""RUNE — the risk agent.

Two layers, one verdict.  ``RuneCore`` decides; ``RuneAI`` comments.  This
class wires them together and publishes the decision, attaching commentary only
as metadata.
"""

from __future__ import annotations

from agents.rune.ai import RiskCommentary, RuneAI
from agents.rune.core import RiskContext, RuneCore
from core.bus import EventBus
from core.clock import Clock
from core.config import Settings
from core.events import Event, EventType
from core.health import HealthRegistry
from core.models.common import Millis
from core.models.opportunity import TradeIntent
from core.models.ops import HealthStatus
from core.models.risk import RiskDecision

SERVICE = "RUNE"
VERSION = "rune-0.1"


class Rune:
    def __init__(
        self,
        bus: EventBus,
        clock: Clock,
        settings: Settings,
        health: HealthRegistry,
        *,
        ai: RuneAI | None = None,
    ) -> None:
        self.bus = bus
        self.clock = clock
        self.settings = settings
        self.health = health
        self.core = RuneCore(settings.risk, clock)
        self.ai = ai
        self.decisions: list[RiskDecision] = []
        health.register(SERVICE, VERSION)

    async def evaluate(
        self, intent: TradeIntent, ctx: RiskContext, now_ms: Millis | None = None
    ) -> RiskDecision:
        """Run the deterministic gates and publish the decision."""
        decision = self.core.evaluate(intent, ctx, now_ms)

        commentary: RiskCommentary | None = self.ai.latest if self.ai is not None else None
        if commentary is not None and commentary.ok:
            # Recorded for attribution and shown on the dashboard. Deliberately
            # applied *after* the verdict so it cannot influence it.
            decision.ai_commentary = commentary.commentary
            decision.ai_concern_level = commentary.concern_level
            if commentary.reason_codes:
                decision.reason_codes = [
                    *decision.reason_codes,
                    *[f"AI:{code}" for code in commentary.reason_codes[:3]],
                ]

        self.decisions.append(decision)
        if len(self.decisions) > 500:
            del self.decisions[:100]

        await self.bus.publish(
            Event(
                type=EventType.RISK_PASS if decision.approved else EventType.RISK_FAIL,
                ts_ms=decision.created_at,
                source=SERVICE,
                schema_name="RiskDecision",
                correlation_id=decision.correlation_id,
                payload=decision.to_json_dict(),
            )
        )
        self.heartbeat()
        return decision

    def heartbeat(self) -> None:
        status = HealthStatus.HEALTHY
        detail = ""
        if self.ai is not None and self.ai.failures and self.ai.calls:
            rate = self.ai.failures / self.ai.calls
            if rate > 0.5:
                # RUNE stays HEALTHY: the AI layer is advisory, and losing it
                # must not suspend the strategies that depend on RUNE.
                detail = f"RUNE-AI failing ({self.ai.failures}/{self.ai.calls})"
        self.health.heartbeat(
            SERVICE,
            status=status,
            queue_depth=self.bus.queue_depth,
            version=VERSION,
            detail=detail,
        )
