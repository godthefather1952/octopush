"""OKAPI — hedge engine.

Watches the gap between the exposure a strategy *intends* to carry and the
exposure it actually has, and asks for simulated hedges to close it.

For V1 the job is narrow and concrete: a cross-venue relative-value trade is
supposed to be delta-neutral, and partial fills on one leg break that.  OKAPI
notices and proposes the offsetting trade.
"""

from __future__ import annotations

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
    #: Symbol -> signed notional the strategies intend to carry. Cross-venue
    #: relative value intends zero.
    desired_delta: dict[str, float] = field(default_factory=dict)
    hedges_requested: int = 0
    #: Phase 9. What became of each hedge request: status, and links to the
    #: trade intent, plan, orders and any reconciliation run. Written to beside
    #: the code that already decides and read by nothing in the hedging path --
    #: ``working_hedges`` and ``_hedge_in_flight`` in the orchestrator remain
    #: the authority on whether a hedge is in flight.
    hedge_registry: HedgeRegistry = field(default_factory=HedgeRegistry)
    #: Phase 9. Metadata mirroring :attr:`desired_delta`, which stays the value
    #: :meth:`delta_reports` actually measures against.
    target_registry: HedgeTargetRegistry = field(default_factory=HedgeTargetRegistry)

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
        # Phase 9: record what was just decided. This runs after every economic
        # value above is fixed, copies each intent field for field, and its
        # result is not read by anything -- the returned ``intents`` list is
        # unchanged and is still what the orchestrator works.
        self._mirror_hedge_intents(intents, now)
        return intents

    # -- observation (Phase 9) --------------------------------------------
    #
    # Everything below records or reports. None of it is called from the
    # hedging path above except ``_mirror_hedge_intents``, which is a write
    # whose result nothing branches on.

    def _mirror_hedge_intents(
        self, intents: list[HedgeIntent], now_ms: Millis
    ) -> list[HedgeRequestRecord]:
        """Copy freshly built hedge intents into the registry.

        Side, venue, notional, both deltas, urgency and reason codes are taken
        straight off the intent. Nothing is recomputed, and the cause stays
        UNCLASSIFIED: portfolio delta does not say what left the exposure
        behind, and a guess would read like evidence.
        """
        return [
            self.hedge_registry.register_request(
                intent,
                now_ms,
                tolerance=self.settings.hedge_tolerance_notional,
            )
            for intent in intents
        ]

    def hedge_targets(self) -> list[HedgeTarget]:
        """Recorded target metadata. ``desired_delta`` remains the authority."""
        return self.target_registry.all_targets()

    def mirror_targets(self, now_ms: Millis, *, strategy: str = "") -> list[HedgeTarget]:
        """Copy :attr:`desired_delta` into the target registry at ``now_ms``.

        Explicitly caller-driven, and takes a logical instant rather than
        reading the clock. ``set_desired_delta`` deliberately does not call
        this: it has no ``now_ms`` to pass, and adding a clock read inside it
        would put a wall-clock timestamp into the hedging path.
        """
        return self.target_registry.mirror(
            self.desired_delta, now_ms, strategy=strategy
        )

    def hedge_requests(self) -> list[HedgeRequestRecord]:
        return self.hedge_registry.all()

    def active_hedges(self) -> list[HedgeRequestRecord]:
        """Hedges currently known to be working. Excludes UNKNOWN."""
        return self.hedge_registry.active()

    def outstanding_hedges(self) -> list[HedgeRequestRecord]:
        """Hedges whose final execution truth is not known. Includes UNKNOWN."""
        return self.hedge_registry.outstanding()

    def unknown_hedges(self) -> list[HedgeRequestRecord]:
        """Hedges whose venue-side truth is unresolved. Never auto-resolved."""
        return self.hedge_registry.unknown()

    def hedge_for_id(self, hedge_id: str) -> HedgeRequestRecord | None:
        """By registry id, or by the ``HedgeIntent.hedge_id`` it was built from.

        The orchestrator uses the intent's id as the correlation id on the
        trade intent, the plan and every order, so a caller holding a
        downstream record almost always has that id rather than this one.
        """
        return self.hedge_registry.get(hedge_id) or self.hedge_registry.for_intent(
            hedge_id
        )

    def delta_snapshot(
        self, portfolio: PortfolioState, now_ms: Millis
    ) -> DeltaSnapshot:
        """Capture exposure at a caller-supplied instant.

        Carries the ``DeltaReport`` objects :meth:`delta_reports` produced.
        ``total_unhedged`` is the sum of their absolute residuals -- the same
        expression :meth:`total_unhedged` evaluates, applied to the reports
        already in hand so the snapshot does not trigger a second measurement
        at a different instant. No second delta formula exists here: a platform
        with two answers to "how much are we unhedged?" has no answer at all.
        """
        reports = self.delta_reports(portfolio, now_ms)
        self.hedge_registry.delta_snapshots += 1
        # Mirror the targets at the instant the caller supplied. Doing it here
        # rather than inside ``set_desired_delta`` is deliberate: that method
        # has no logical time to hand, and giving it a clock read would put a
        # wall-clock timestamp into the hedging path.
        self.mirror_targets(now_ms)
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
        """What the venue choice looked like, and which venue was chosen.

        ``selected_venue`` **copies** :meth:`_hedge_venue`. This method runs no
        comparison of its own — a buy hedge still wants the lowest ask and a
        sell hedge the highest bid, decided in exactly one place, and a second
        implementation here could quietly disagree with the first.
        """
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
        """Whether hedging is in a fit state. **Reporting only.**

        This gates nothing. RUNE still calls :meth:`hedge_available` as its
        mandatory pre-entry gate, unchanged; substituting this for it would
        replace a tested decision with an untested one.

        ``ready`` is False whenever anything is unestablished, including when
        no market has been seen. Absence of evidence is not readiness.
        """
        reports = self.delta_reports(portfolio, now_ms)
        total = sum(abs(r.unhedged_delta) for r in reports)
        symbols = sorted({r.symbol for r in reports} | set(self.desired_delta))
        # Copies what ``hedge_available`` answers for every symbol in scope.
        # Never a substitute for it: RUNE still calls it per symbol, per
        # entry, and that call is the gate.
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
        """One serializable view of hedging state.

        Compact: targets, the current delta reports, and *ids* for the hedges
        in flight. A snapshot that embedded every hedge record with its orders
        and fills would be sized by session history rather than by what is
        currently outstanding.
        """
        reports = self.delta_reports(portfolio, now_ms)
        symbols = sorted({r.symbol for r in reports} | set(self.desired_delta))
        # Same as ``delta_snapshot``: the mirror is refreshed at capture time,
        # from the caller's logical instant, never from a clock read.
        self.mirror_targets(now_ms)
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
