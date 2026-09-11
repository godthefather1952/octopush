"""OKAPI — hedge engine.

Watches the gap between the exposure a strategy *intends* to carry and the
exposure it actually has, and asks for simulated hedges to close it.

For V1 the job is narrow and concrete: a cross-venue relative-value trade is
supposed to be delta-neutral, and partial fills on one leg break that. OKAPI
notices and proposes the offsetting trade.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from agents.okapi.registry import HedgeRegistry
from agents.okapi.targets import HedgeTargetRegistry
from core.bus import EventBus
from core.clock import Clock
from core.config import Settings
from core.events import Event, EventType
from core.health import HealthRegistry
from core.models.common import Millis, Side
from core.models.hedging import (
    DeltaSnapshot,
    HedgeRequestRecord,
    HedgeRouteSnapshot,
    HedgeTarget,
    HedgeVenueCandidate,
    OkapiReadiness,
    OkapiSnapshot,
    summarize_delta_report,
)
from core.models.market import MarketState
from core.models.ops import DeltaReport, HealthStatus, HedgeIntent
from core.models.portfolio import PortfolioState

SERVICE = "OKAPI"
VERSION = "okapi-0.1"


@dataclass
class Okapi:
    bus: EventBus
    clock: Clock
    settings: Settings
    health: HealthRegistry
    desired_delta: dict[str, float] = field(default_factory=dict)
    hedges_requested: int = 0
    hedge_registry: HedgeRegistry = field(default_factory=HedgeRegistry)
    target_registry: HedgeTargetRegistry = field(default_factory=HedgeTargetRegistry)

    def __post_init__(self) -> None:
        self.health.register(SERVICE, VERSION)

    def set_desired_delta(self, symbol: str, notional: float) -> None:
        if isinstance(notional, bool) or not isinstance(notional, (int, float)):
            raise TypeError("desired delta must be a finite number")
        value = float(notional)
        if not math.isfinite(value):
            raise ValueError("desired delta must be finite")
        self.desired_delta[symbol] = value
        # Mirror observational metadata on the write path. Snapshot/read
        # methods must never create or advance registry state.
        self.target_registry.set_target(symbol, value, self.clock.now_ms())

    def target(self, symbol: str) -> float:
        return self.desired_delta.get(symbol, 0.0)

    def delta_reports(
        self, portfolio: PortfolioState, now_ms: Millis | None = None
    ) -> list[DeltaReport]:
        now = self.clock.now_ms() if now_ms is None else now_ms
        tolerance = self.settings.hedge_tolerance_notional
        reports: list[DeltaReport] = []
        actuals = portfolio.net_delta_by_symbol()
        symbols = set(actuals) | set(self.desired_delta)
        for symbol in sorted(symbols):
            actual = actuals.get(symbol, 0.0)
            desired = self.target(symbol)
            unhedged = actual - desired
            reports.append(
                DeltaReport(
                    created_at=now,
                    symbol=symbol,
                    desired_delta=desired,
                    actual_delta=actual,
                    unhedged_delta=unhedged,
                    within_tolerance=abs(unhedged) <= tolerance,
                    tolerance=tolerance,
                )
            )
        return reports

    def total_unhedged(self, portfolio: PortfolioState) -> float:
        return sum(abs(r.unhedged_delta) for r in self.delta_reports(portfolio))

    def _hedge_venue(self, symbol: str, side: Side, market: MarketState) -> str | None:
        candidates = [
            state
            for state in market.states_for(symbol)
            if state.quality.is_usable
            and (state.metrics.best_ask if side is Side.BUY else state.metrics.best_bid)
        ]
        if not candidates:
            return None
        if side is Side.BUY:
            return min(candidates, key=lambda s: s.metrics.best_ask).venue
        return max(candidates, key=lambda s: s.metrics.best_bid).venue

    def hedge_available(self, symbol: str, market: MarketState) -> bool:
        return (
            self._hedge_venue(symbol, Side.BUY, market) is not None
            and self._hedge_venue(symbol, Side.SELL, market) is not None
        )

    def build_hedges(
        self,
        portfolio: PortfolioState,
        market: MarketState,
        now_ms: Millis | None = None,
    ) -> list[HedgeIntent]:
        """Build economic hedge candidates without opening request history.

        The orchestrator may suppress a candidate because the same symbol is
        already being hedged. Registration therefore happens when the hedge is
        actually published for work, not while candidates are merely measured.
        """
        now = self.clock.now_ms() if now_ms is None else now_ms
        intents: list[HedgeIntent] = []
        for report in self.delta_reports(portfolio, now):
            if report.within_tolerance or abs(report.unhedged_delta) <= 0:
                continue
            side = Side.SELL if report.unhedged_delta > 0 else Side.BUY
            venue = self._hedge_venue(report.symbol, side, market)
            if venue is None:
                continue
            intents.append(
                HedgeIntent(
                    created_at=now,
                    source_data_timestamp=market.source_data_timestamp_for(
                        [(venue, report.symbol)]
                    ),
                    symbol=report.symbol,
                    venue=venue,
                    side=side,
                    notional=abs(report.unhedged_delta),
                    current_delta=report.actual_delta,
                    target_delta=report.desired_delta,
                    reason_codes=["UNHEDGED_DELTA"],
                    urgency=min(
                        1.0,
                        abs(report.unhedged_delta)
                        / max(1e-9, self.settings.risk.max_unhedged_notional),
                    ),
                )
            )
        return intents

    def _mirror_hedge_intents(
        self, intents: list[HedgeIntent], now_ms: Millis
    ) -> list[HedgeRequestRecord]:
        return [
            self.hedge_registry.register_request(
                intent,
                now_ms,
                tolerance=self.settings.hedge_tolerance_notional,
            )
            for intent in intents
        ]

    def hedge_targets(self) -> list[HedgeTarget]:
        return self.target_registry.all_targets()

    def mirror_targets(self, now_ms: Millis, *, strategy: str = "") -> list[HedgeTarget]:
        return self.target_registry.mirror(
            self.desired_delta, now_ms, strategy=strategy
        )

    def hedge_requests(self) -> list[HedgeRequestRecord]:
        return self.hedge_registry.all()

    def active_hedges(self) -> list[HedgeRequestRecord]:
        return self.hedge_registry.active()

    def outstanding_hedges(self) -> list[HedgeRequestRecord]:
        return self.hedge_registry.outstanding()

    def unknown_hedges(self) -> list[HedgeRequestRecord]:
        return self.hedge_registry.unknown()

    def hedge_for_id(self, hedge_id: str) -> HedgeRequestRecord | None:
        return self.hedge_registry.get(hedge_id) or self.hedge_registry.for_intent(
            hedge_id
        )

    def delta_snapshot(
        self, portfolio: PortfolioState, now_ms: Millis
    ) -> DeltaSnapshot:
        reports = self.delta_reports(portfolio, now_ms)
        self.hedge_registry.delta_snapshots += 1
        return DeltaSnapshot(
            created_at=now_ms,
            reports=reports,
            targets=self.target_registry.all_targets(),
            summaries=[summarize_delta_report(report) for report in reports],
            total_unhedged=sum(abs(r.unhedged_delta) for r in reports),
            tolerance=self.settings.hedge_tolerance_notional,
        )

    def route_snapshot(
        self, symbol: str, side: Side, market: MarketState, now_ms: Millis
    ) -> HedgeRouteSnapshot:
        candidates = [
            HedgeVenueCandidate(
                venue=state.venue,
                symbol=state.symbol,
                side=side,
                best_bid=state.metrics.best_bid,
                best_ask=state.metrics.best_ask,
                quality=state.quality,
                usable=bool(
                    state.quality.is_usable
                    and (
                        state.metrics.best_ask
                        if side is Side.BUY
                        else state.metrics.best_bid
                    )
                ),
                source_data_timestamp=state.exchange_ts,
            )
            for state in market.states_for(symbol)
        ]
        return HedgeRouteSnapshot(
            created_at=now_ms,
            symbol=symbol,
            side=side,
            candidates=candidates,
            selected_venue=self._hedge_venue(symbol, side, market),
        )

    def readiness(
        self,
        portfolio: PortfolioState,
        market: MarketState | None,
        now_ms: Millis,
    ) -> OkapiReadiness:
        reports = self.delta_reports(portfolio, now_ms)
        total = sum(abs(r.unhedged_delta) for r in reports)
        symbols = sorted({r.symbol for r in reports} | set(self.desired_delta))
        if market is None or not symbols:
            hedging_available = False
        else:
            hedging_available = all(
                self.hedge_available(symbol, market) for symbol in symbols
            )
        outstanding = self.hedge_registry.outstanding()
        unknown = self.hedge_registry.unknown()
        reasons: list[str] = []
        if not self.desired_delta:
            reasons.append("NO_TARGETS_ESTABLISHED")
        if market is None:
            reasons.append("NO_MARKET_STATE")
        elif not hedging_available:
            reasons.append("HEDGE_VENUE_UNAVAILABLE")
        if unknown:
            reasons.append(f"UNKNOWN_HEDGES:{len(unknown)}")
        if total > self.settings.risk.max_unhedged_notional:
            reasons.append("UNHEDGED_ABOVE_LIMIT")
        return OkapiReadiness(
            ready=not reasons,
            created_at=now_ms,
            targets_established=bool(self.desired_delta),
            market_available=market is not None,
            hedging_available=hedging_available,
            unknown_hedges=len(unknown),
            outstanding_hedges=len(outstanding),
            total_unhedged=total,
            tolerance=self.settings.hedge_tolerance_notional,
            reason_codes=reasons,
        )

    def okapi_snapshot(
        self,
        portfolio: PortfolioState,
        market: MarketState | None,
        now_ms: Millis,
    ) -> OkapiSnapshot:
        reports = self.delta_reports(portfolio, now_ms)
        # These reports are embedded in an observational snapshot, not
        # published as events. Give them stable derived identities so two
        # reads at the same logical instant are equivalent rather than minting
        # fresh random event ids on every GET.
        reports = [
            report.model_copy(
                update={"event_id": f"snapshot-delta-{report.symbol}-{now_ms}"},
                deep=True,
            )
            for report in reports
        ]
        symbols = sorted({r.symbol for r in reports} | set(self.desired_delta))
        return OkapiSnapshot(
            created_at=now_ms,
            targets=self.target_registry.all_targets(),
            delta_reports=reports,
            total_unhedged=sum(abs(r.unhedged_delta) for r in reports),
            hedge_tolerance=self.settings.hedge_tolerance_notional,
            active_hedge_ids=[r.hedge_id for r in self.hedge_registry.active()],
            outstanding_hedge_ids=[
                r.hedge_id for r in self.hedge_registry.outstanding()
            ],
            unknown_hedge_ids=[r.hedge_id for r in self.hedge_registry.unknown()],
            hedge_available_by_symbol=(
                {symbol: self.hedge_available(symbol, market) for symbol in symbols}
                if market is not None
                else {}
            ),
            metrics=self.hedge_registry.metrics(),
            readiness=self.readiness(portfolio, market, now_ms),
        )

    async def publish_deltas(self, portfolio: PortfolioState) -> list[DeltaReport]:
        reports = self.delta_reports(portfolio)
        for report in reports:
            await self.bus.publish(
                Event(
                    type=EventType.DELTA_REPORT,
                    ts_ms=report.created_at,
                    source=SERVICE,
                    schema_name="DeltaReport",
                    payload=report.to_json_dict(),
                )
            )
        self._heartbeat(portfolio)
        return reports

    async def publish_hedge(self, intent: HedgeIntent) -> None:
        """Publish a hedge that has passed orchestrator in-flight suppression."""
        existing = self.hedge_registry.for_intent(intent.hedge_id)
        if existing is None:
            self.hedges_requested += 1
            self._mirror_hedge_intents([intent], intent.created_at)
        await self.bus.publish(
            Event(
                type=EventType.HEDGE_INTENT,
                ts_ms=intent.created_at,
                source=SERVICE,
                schema_name="HedgeIntent",
                correlation_id=intent.hedge_id,
                payload=intent.to_json_dict(),
            )
        )

    def _heartbeat(self, portfolio: PortfolioState) -> None:
        unhedged = self.total_unhedged(portfolio)
        limit = self.settings.risk.max_unhedged_notional
        status = HealthStatus.HEALTHY
        if unhedged > limit:
            status = HealthStatus.DEGRADED
        self.health.heartbeat(
            SERVICE,
            status=status,
            queue_depth=self.bus.queue_depth,
            version=VERSION,
            detail=f"unhedged {unhedged:.2f} / limit {limit:.2f}",
        )
