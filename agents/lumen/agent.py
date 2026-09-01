"""LUMEN — the Claude intelligence agent.

LUMEN reads unstructured information and returns structure.  It runs on the
slow intelligence loop, measured in seconds and events, never per market tick:
the fast loop must never wait on a model.

Its published opinion answers one question with a well-defined sign:

    How favourable is it, right now, to take short-horizon relative-value risk
    in this symbol?

Calm markets score near zero; an information shock, a spike in attention or a
sharp narrative change scores negative, because a dislocation during a
repricing event is more likely to be real information than noise.  The raw
sentiment, direction and attention readings ride along in ``detail`` for
attribution.

LUMEN is optional by construction.  It is not in ``required_agents``, so if
Claude is unavailable the platform loses one weighted input and nothing else.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from agents.lumen.provider import IntelligenceProvider, IntelligenceRequest
from core.bus import EventBus
from core.clock import Clock
from core.config import Settings
from core.events import Event, EventType
from core.health import HealthRegistry
from core.models.agent import AgentOpinion
from core.models.common import AgentId
from core.models.market import MarketState
from core.models.ops import HealthStatus

log = logging.getLogger(__name__)

SERVICE = "LUMEN"
VERSION = "lumen-0.1"

SYSTEM_PROMPT = (
    "You are the information-analysis agent of an automated paper-trading "
    "platform. You receive structured market context and any recent headlines "
    "or announcements. Return a structured read of the information environment: "
    "sentiment, how much attention the asset is receiving, whether an "
    "information shock has occurred, and the direction the news implies. "
    "You never see orders, balances or risk limits, and your output is one "
    "weighted input among several. Be calibrated: if nothing notable is "
    "happening, say so with low confidence rather than inventing a narrative."
)

RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": [
        "sentiment",
        "attention",
        "information_shock",
        "direction",
        "confidence",
        "ttl_seconds",
        "reason_codes",
    ],
    "properties": {
        "sentiment": {"type": "number", "minimum": -1, "maximum": 1},
        "attention": {"type": "number", "minimum": 0, "maximum": 1},
        "information_shock": {"type": "boolean"},
        "direction": {"type": "number", "minimum": -1, "maximum": 1},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "ttl_seconds": {"type": "integer", "minimum": 5, "maximum": 900},
        "reason_codes": {"type": "array", "items": {"type": "string"}},
    },
}


@dataclass
class NewsItem:
    """One piece of unstructured input."""

    headline: str
    source: str
    published_ms: int
    body: str = ""

    def to_payload(self) -> dict[str, Any]:
        return {
            "headline": self.headline,
            "source": self.source,
            "published_ms": self.published_ms,
            "body": self.body[:1000],
        }


@dataclass
class Lumen:
    bus: EventBus
    clock: Clock
    settings: Settings
    health: HealthRegistry
    provider: IntelligenceProvider
    #: Injected by a news adapter. Empty is a valid state — LUMEN then reads
    #: only the market context it is given.
    headlines: list[NewsItem] = field(default_factory=list)
    market: MarketState | None = None
    calls: int = 0
    failures: int = 0
    consecutive_failures: int = 0
    last_call_ms: int | None = None
    latencies_ms: list[float] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.health.register(SERVICE, VERSION)

    # -- input -------------------------------------------------------------

    def on_market_state(self, state: MarketState) -> None:
        self.market = state

    def add_headline(self, item: NewsItem) -> None:
        self.headlines.append(item)
        if len(self.headlines) > 50:
            del self.headlines[:-50]

    def _context(self, symbol: str) -> dict[str, Any]:
        """The structured facts LUMEN is given.

        Deliberately *not* the same context the pricing agents receive: feeding
        every agent identical input defeats the point of independent analysis.
        LUMEN sees the information environment and only a coarse market summary.
        """
        payload: dict[str, Any] = {
            "symbol": symbol,
            "as_of_ms": self.clock.now_ms(),
            "recent_headlines": [
                item.to_payload()
                for item in self.headlines[-10:]
                if item.published_ms >= self.clock.now_ms() - 3_600_000
            ],
        }
        if self.market is not None:
            view = self.market.consolidated.get(symbol)
            states = self.market.states_for(symbol)
            payload["market_summary"] = {
                "reference_price": view.reference_price if view else None,
                "venues_quoting": len(states),
                "max_cross_venue_deviation_bps": view.max_deviation_bps if view else None,
                "short_vol_bps": max(
                    (s.metrics.short_vol_bps for s in states), default=0.0
                ),
            }
        return payload

    # -- the slow loop -----------------------------------------------------

    async def evaluate(self, symbol: str) -> AgentOpinion | None:
        """Ask the provider for a read. Returns ``None`` when unavailable."""
        self.calls += 1
        self.last_call_ms = self.clock.now_ms()
        response = await self.provider.analyze(
            IntelligenceRequest(
                task="information_environment",
                system=SYSTEM_PROMPT,
                payload=self._context(symbol),
                response_schema=RESPONSE_SCHEMA,
                max_tokens=self.settings.lumen.max_tokens,
                timeout_s=self.settings.lumen.timeout_s,
            )
        )
        self.latencies_ms.append(response.latency_ms)
        if len(self.latencies_ms) > 200:
            del self.latencies_ms[:100]

        if not response.ok:
            self.failures += 1
            self.consecutive_failures += 1
            self._heartbeat()
            # No opinion is published. The orchestrator sees LUMEN missing,
            # which is honest; it does not see a neutral opinion, which would
            # be a lie.
            return None

        self.consecutive_failures = 0
        opinion = self._to_opinion(symbol, response.data)
        self._heartbeat()
        return opinion

    def _to_opinion(self, symbol: str, data: dict[str, Any]) -> AgentOpinion | None:
        try:
            sentiment = float(data["sentiment"])
            attention = float(data["attention"])
            shock = bool(data["information_shock"])
            direction = float(data["direction"])
            confidence = float(data["confidence"])
            ttl_seconds = int(data.get("ttl_seconds", self.settings.lumen.ttl_s))
        except (KeyError, TypeError, ValueError) as exc:
            log.warning("LUMEN response malformed", extra={"error": str(exc)})
            self.failures += 1
            return None

        reason_codes = [str(code) for code in data.get("reason_codes", [])][:8]
        # Relative-value risk is unfavourable when the information environment
        # is moving: a shock, high attention, or a strong directional narrative
        # all argue that a cross-venue gap is repricing rather than noise.
        turbulence = min(
            1.0,
            0.6 * max(0.0, attention) + 0.4 * abs(direction) + (0.4 if shock else 0.0),
        )
        signal = max(-1.0, min(1.0, -turbulence))
        if shock and "NEGATIVE_INFORMATION_SHOCK" not in reason_codes and direction < 0:
            reason_codes.append("NEGATIVE_INFORMATION_SHOCK")
        if not shock and attention < 0.3:
            reason_codes.append("QUIET_INFORMATION_ENVIRONMENT")

        now = self.clock.now_ms()
        ttl_ms = max(5_000, min(900_000, ttl_seconds * 1000))
        return AgentOpinion(
            agent_id=AgentId.LUMEN,
            symbol=symbol,
            created_at=now,
            source_data_timestamp=(
                self.market.source_data_timestamp if self.market is not None else None
            ),
            signal=signal,
            confidence=max(0.0, min(1.0, confidence)),
            expires_at=now + ttl_ms,
            reason_codes=reason_codes,
            model_version=VERSION,
            detail={
                "sentiment": sentiment,
                "attention": attention,
                "direction": direction,
                "information_shock": shock,
                "turbulence": turbulence,
            },
        )

    async def run_once(self) -> list[AgentOpinion]:
        """One pass of the intelligence loop, over every configured symbol."""
        published: list[AgentOpinion] = []
        for symbol in self.settings.symbols:
            opinion = await self.evaluate(symbol)
            if opinion is None:
                continue
            await self.bus.publish(
                Event(
                    type=EventType.AGENT_OPINION,
                    ts_ms=opinion.created_at,
                    source=SERVICE,
                    schema_name="AgentOpinion",
                    payload=opinion.to_json_dict(),
                )
            )
            published.append(opinion)
        return published

    async def run_forever(self) -> None:
        while True:
            try:
                await self.run_once()
            except Exception:
                log.exception("LUMEN loop failed")
                self.failures += 1
            await self.clock.sleep(self.settings.lumen.poll_interval_s)

    @property
    def mean_latency_ms(self) -> float:
        return sum(self.latencies_ms) / len(self.latencies_ms) if self.latencies_ms else 0.0

    def _heartbeat(self) -> None:
        threshold = self.settings.lumen.failure_threshold
        if self.consecutive_failures >= threshold:
            status = HealthStatus.OFFLINE
            detail = f"{self.consecutive_failures} consecutive provider failures"
        elif self.consecutive_failures:
            status = HealthStatus.DEGRADED
            detail = f"{self.consecutive_failures} recent provider failures"
        else:
            status = HealthStatus.HEALTHY
            detail = f"provider={self.provider.name}"
        self.health.heartbeat(
            SERVICE,
            status=status,
            queue_depth=self.bus.queue_depth,
            version=VERSION,
            detail=detail,
        )
