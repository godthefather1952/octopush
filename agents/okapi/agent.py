"""OKAPI — hedge engine.

Watches the gap between the exposure a strategy *intends* to carry and the
exposure it actually has, and asks for simulated hedges to close it.

For V1 the job is narrow and concrete: a cross-venue relative-value trade is
supposed to be delta-neutral, and partial fills on one leg break that.  OKAPI
notices and proposes the offsetting trade.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from core.bus import EventBus
from core.clock import Clock
from core.config import Settings
from core.events import Event, EventType
from core.health import HealthRegistry
from core.models.common import Millis, Side
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
    #: Symbol -> signed notional the strategies intend to carry. Cross-venue
    #: relative value intends zero.
    desired_delta: dict[str, float] = field(default_factory=dict)
    hedges_requested: int = 0

    def __post_init__(self) -> None:
        self.health.register(SERVICE, VERSION)

    # -- intent ------------------------------------------------------------

    def set_desired_delta(self, symbol: str, notional: float) -> None:
        self.desired_delta[symbol] = notional

    def target(self, symbol: str) -> float:
        return self.desired_delta.get(symbol, 0.0)

    # -- measurement -------------------------------------------------------

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

    # -- hedging -----------------------------------------------------------

    def _hedge_venue(self, symbol: str, side: Side, market: MarketState) -> str | None:
        """Cheapest usable venue to put the hedge on.

        A buy hedge wants the lowest ask, a sell hedge the highest bid.
        """
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
        """Whether an offsetting venue is quoting at all.

        RUNE consults this as a mandatory gate: a delta-neutral strategy must
        not enter if it could not hedge the leg risk it is about to take.
        """
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
        # A hedge intent's created_at/deadline are gated by RUNE, so this is
        # economic time, not metadata (Phase 2 Batch 1.4).
        now = self.clock.now_ms() if now_ms is None else now_ms
        intents: list[HedgeIntent] = []
        for report in self.delta_reports(portfolio, now):
            if report.within_tolerance or abs(report.unhedged_delta) <= 0:
                continue
            # Long too much -> sell; short too much -> buy.
            side = Side.SELL if report.unhedged_delta > 0 else Side.BUY
            venue = self._hedge_venue(report.symbol, side, market)
            if venue is None:
                continue
            intents.append(
                HedgeIntent(
                    created_at=now,
                    source_data_timestamp=market.source_data_timestamp,
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
        self.hedges_requested += len(intents)
        return intents

    async def publish_deltas(self, portfolio: PortfolioState) -> list[DeltaReport]:
        """Publish the measurement. Hedge intents are published when worked."""
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
