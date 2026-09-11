"""Focused fixtures for the Phase 10 LUMEN validation audit."""

from __future__ import annotations

from typing import Any

from agents.lumen.agent import Lumen
from agents.lumen.provider import (
    IntelligenceProvider,
    IntelligenceRequest,
    IntelligenceResponse,
)
from agents.lumen.registry import IntelligenceStore
from core.clock import Clock, ManualClock
from core.health import HealthRegistry
from core.models.common import DataQuality
from core.models.intelligence import IntelligenceAnalysisRecord, IntelligenceEvidenceBundle
from core.models.market import BookMetrics, ConsolidatedView, MarketState, VenueMarketState

VALID_RESPONSE: dict[str, Any] = {
    "sentiment": -0.2,
    "attention": 0.5,
    "information_shock": False,
    "direction": -0.4,
    "confidence": 0.8,
    "ttl_seconds": 60,
    "reason_codes": ["SCRIPTED"],
}


class RecordingProvider(IntelligenceProvider):
    name = "recording"

    def __init__(
        self,
        data: dict[str, Any] | None = None,
        *,
        ok: bool = True,
        unavailable: bool = False,
        error: str | None = None,
        latency_ms: float = 7.0,
    ) -> None:
        self.data = dict(VALID_RESPONSE if data is None else data)
        self.ok = ok
        self.unavailable = unavailable
        self.error = error
        self.latency_ms = latency_ms
        self.calls = 0
        self.requests: list[IntelligenceRequest] = []

    async def analyze(self, request: IntelligenceRequest) -> IntelligenceResponse:
        self.calls += 1
        self.requests.append(request)
        return IntelligenceResponse(
            ok=self.ok,
            task=request.task,
            provider=self.name,
            model="recording-v1",
            data=dict(self.data),
            latency_ms=self.latency_ms,
            unavailable=self.unavailable,
            error=self.error,
        )


class ExplodingProvider(IntelligenceProvider):
    name = "exploding"

    def __init__(self) -> None:
        self.calls = 0

    async def analyze(self, request: IntelligenceRequest) -> IntelligenceResponse:
        self.calls += 1
        raise AssertionError("provider must not be invoked")


class FailingIntelligenceStore(IntelligenceStore):
    def put_analysis(self, record: IntelligenceAnalysisRecord) -> None:
        raise RuntimeError("analysis persistence unavailable")

    def put_evidence_bundle(self, bundle: IntelligenceEvidenceBundle) -> None:
        raise RuntimeError("bundle persistence unavailable")

    def get_analysis(self, analysis_id: str) -> IntelligenceAnalysisRecord | None:
        return None


class CapturingBus:
    def __init__(self) -> None:
        self.events: list[Any] = []

    @property
    def queue_depth(self) -> int:
        return 0

    async def publish(self, event: Any) -> None:
        self.events.append(event)


class FailingPublishBus(CapturingBus):
    async def publish(self, event: Any) -> None:
        raise RuntimeError("event bus publication failed")


class AdvancingClock(Clock):
    """Clock whose every read advances, exposing hidden multi-read assumptions."""

    def __init__(self, start_ms: int = 0, *, step_ms: int = 1) -> None:
        self.current = int(start_ms)
        self.step_ms = int(step_ms)
        self.reads = 0

    def now_ms(self) -> int:
        value = self.current
        self.current += self.step_ms
        self.reads += 1
        return value

    async def sleep(self, seconds: float) -> None:
        self.current += round(seconds * 1000)


def build_lumen(settings, *, provider=None, bus=None, clock=None) -> Lumen:
    clock = clock or ManualClock(1_788_000_000_000)
    bus = bus or CapturingBus()
    provider = provider or RecordingProvider()
    health = HealthRegistry(clock=clock)
    return Lumen(bus, clock, settings, health, provider)


def market_state(now_ms: int = 1_788_000_000_000, symbol: str = "BTC-USD") -> MarketState:
    a = VenueMarketState(
        venue="VENUE_A",
        symbol=symbol,
        metrics=BookMetrics(
            best_bid=99.0,
            best_ask=101.0,
            mid=100.0,
            short_vol_bps=4.0,
        ),
        exchange_ts=now_ms - 20,
        last_update_ts=now_ms - 10,
        as_of=now_ms,
        quality=DataQuality.FRESH,
        connected=True,
    )
    b = VenueMarketState(
        venue="VENUE_B",
        symbol=symbol,
        metrics=BookMetrics(
            best_bid=100.0,
            best_ask=102.0,
            mid=101.0,
            short_vol_bps=7.0,
        ),
        exchange_ts=now_ms - 30,
        last_update_ts=now_ms - 10,
        as_of=now_ms,
        quality=DataQuality.FRESH,
        connected=True,
    )
    view = ConsolidatedView(
        symbol=symbol,
        as_of=now_ms,
        reference_price=100.5,
        max_deviation_bps=50.0,
        quality=DataQuality.FRESH,
        usable_venues=["VENUE_A", "VENUE_B"],
    )
    return MarketState(
        created_at=now_ms,
        source_data_timestamp=now_ms - 30,
        venues={f"VENUE_A:{symbol}": a, f"VENUE_B:{symbol}": b},
        consolidated={symbol: view},
    )


__all__ = [
    "VALID_RESPONSE",
    "AdvancingClock",
    "CapturingBus",
    "ExplodingProvider",
    "FailingIntelligenceStore",
    "FailingPublishBus",
    "RecordingProvider",
    "build_lumen",
    "market_state",
]
