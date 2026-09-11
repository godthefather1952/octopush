"""Composition root.

The one place that knows how the pieces fit together.  Everything else takes
its collaborators as arguments, which is what makes the whole platform
constructible in a test with a manual clock and a synthetic market.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from agents.lumen import Lumen, build_provider
from agents.marin import Marin
from agents.marin.source import (
    AccountReconciliationSource,
    ExecutionReconciliationSource,
)
from agents.noro import Noro
from agents.okapi import Okapi
from agents.rune import Rune, RuneAI
from agents.tidal import Tidal
from agents.zephr import Zephr
from apps.operations import OperationalRegistry
from apps.orchestrator.agent_directory import AgentDirectory
from apps.orchestrator.coordination import CoordinationRegistry
from apps.orchestrator.orchestrator import Orchestrator
from apps.shadow import ShadowObserver, ShadowRegistry
from core.bus import EventBus, InMemoryEventBus, build_bus
from core.clock import Clock, SystemClock
from core.config import Settings, load_settings
from core.events import Event, EventType
from core.health import HealthRegistry
from core.models.common import AgentId, TradingMode
from core.models.market import OrderBookSnapshot, TradeEvent
from core.models.ops import HealthStatus
from core.models.orchestration import AgentCadence, AgentSubjectScope
from core.models.runtime import (
    FeedKind,
    OperationalComponentSummary,
    OperationalProfile,
    OperationalReadiness,
    OperationalSnapshot,
    PreLiveReadinessSnapshot,
    SessionManifest,
    SessionStatus,
    SessionSummary,
    ShutdownStage,
    StartupStage,
)
from core.models.shadow import (
    MarketDataProvenance,
    ShadowReadiness,
    ShadowSnapshot,
)
from core.state import SystemState
from execution.oms import OrderManager
from execution.paper import FillSimulator, PaperAccount, PaperExecutor
from execution.veska import Veska
from monitoring import MetricsRegistry, build_registry
from replay.engine import config_digest
from risk.kill_switch import KillSwitch
from simulation.market import SyntheticMarket, default_market
from storage import EventStore, Recorder, build_store
from strategies.consensus import ConsensusEngine
from strategies.cross_venue import REQUIRED_COMPONENTS, CrossVenueDetector
from venues.base.adapter import VenueAdapter
from venues.base.messages import (
    BookDelta,
    RawMessage,
    ResyncRequest,
    VenueStatus,
    VenueStatusKind,
)
from venues.registry import build_adapter
from venues.simulated import SimulatedMarketDriver, SimulatedVenueAdapter

log = logging.getLogger(__name__)

#: Which venue message maps to which bus topic.
_MESSAGE_TOPICS = {
    OrderBookSnapshot: EventType.BOOK_SNAPSHOT,
    BookDelta: EventType.BOOK_DELTA,
    TradeEvent: EventType.TRADE_PRINT,
}


class VenueFeedPublisher:
    """Bridges an adapter's normalised output onto the event bus.

    Everything TIDAL consumes therefore arrives as a recorded bus event, which
    is precisely what makes replay a matter of re-publishing them.
    """

    def __init__(self, bus: EventBus, clock: Clock, venue: str) -> None:
        self.bus = bus
        self.clock = clock
        self.venue = venue
        self.published = 0

    async def publish(self, message) -> None:
        if isinstance(message, VenueStatus):
            topic = {
                VenueStatusKind.CONNECTED: EventType.VENUE_CONNECTED,
                VenueStatusKind.DISCONNECTED: EventType.VENUE_DISCONNECTED,
                VenueStatusKind.RESUBSCRIBED: EventType.VENUE_CONNECTED,
                VenueStatusKind.ERROR: EventType.VENUE_DISCONNECTED,
            }.get(message.kind)
            if topic is None:
                return
            schema = "VenueStatus"
        else:
            topic = _MESSAGE_TOPICS.get(type(message))
            if topic is None:
                return
            schema = type(message).__name__

        self.published += 1
        await self.bus.publish(
            Event(
                type=topic,
                ts_ms=getattr(message, "received_ts", self.clock.now_ms()),
                source=self.venue,
                schema_name=schema,
                payload=message.model_dump(mode="json"),
            )
        )

    async def publish_raw(self, raw: RawMessage) -> None:
        await self.bus.publish(
            Event(
                type=EventType.MARKET_UPDATE,
                ts_ms=raw.received_ts,
                source=self.venue,
                schema_name="RawMessage",
                payload=raw.model_dump(mode="json"),
            )
        )


class ResyncBridge:
    """Routes a resync request from the bus to the adapter that owns the feed.

    This is the whole of TIDAL's coupling to the venue layer: TIDAL publishes
    "this book needs re-establishing" and something in the composition root —
    the only place allowed to know both sides — hands it to the right adapter.
    TIDAL never learns an adapter exists, and the adapter never learns the bus
    exists.

    A request naming an unknown venue is dropped rather than raised on: it can
    only come from a recording made against a different venue set, and a
    replay of that recording should not die on it.
    """

    def __init__(self, adapters: dict[str, VenueAdapter]) -> None:
        self.adapters = adapters
        self.forwarded = 0
        self.unknown_venue = 0

    async def __call__(self, event: Event) -> None:
        request = ResyncRequest.model_validate(event.payload)
        adapter = self.adapters.get(request.venue)
        if adapter is None:
            self.unknown_venue += 1
            return
        self.forwarded += 1
        await adapter.request_resync(request.symbol, request.reason)


@dataclass
class Platform:
    """Every component, wired and ready to run."""

    settings: Settings
    clock: Clock
    bus: EventBus
    health: HealthRegistry
    metrics: MetricsRegistry
    state: SystemState
    store: EventStore
    recorder: Recorder
    tidal: Tidal
    noro: Noro
    zephr: Zephr
    lumen: Lumen
    rune: Rune
    okapi: Okapi
    marin: Marin
    veska: Veska
    executor: PaperExecutor
    account: PaperAccount
    oms: OrderManager
    kill_switch: KillSwitch
    orchestrator: Orchestrator
    #: Phase 11. The platform's memory of its own runs: manifest, startup
    #: stage, status, counters. Written to beside the existing lifecycle and
    #: read by nothing that decides.
    operations: OperationalRegistry = field(default_factory=OperationalRegistry)
    #: Phase 12. The rehearsal record. Empty and unattached under the PAPER
    #: profile.
    shadow: ShadowRegistry = field(default_factory=ShadowRegistry)
    shadow_observer: ShadowObserver | None = None
    adapters: dict[str, VenueAdapter] = field(default_factory=dict)
    publishers: dict[str, VenueFeedPublisher] = field(default_factory=dict)
    sim_driver: SimulatedMarketDriver | None = None
    market_generator: SyntheticMarket | None = None
    resync_bridge: ResyncBridge | None = None
    _started: bool = False
    _bus_started: bool = False
    _feeds_requested: bool = False
    _feeds_started: bool = False
    _recording: bool = False

    @property
    def session_id(self) -> str:
        return self.recorder.session_id

    async def start(self, *, record: bool = True, feeds: bool = True) -> None:
        """Start the platform and witness the real startup order.

        ``feeds=False`` leaves the venue adapters and the synthetic driver
        stopped, which is what replay wants: the recorded events *are* the
        market, and a live feed running alongside them would corrupt the run.
        """
        if self._started:
            return
        now = self.clock.now_ms()
        self.operations.create_session(self.session_manifest(now, recording=record), now)
        self.operations.mark_starting(now)
        self._feeds_requested = feeds
        try:
            # The process always started the bus before storage and feeds.
            # Keeping that action inside Platform.start() lets the operational
            # record witness a bus-start failure without changing the order.
            self.operations.set_startup_stage(StartupStage.BUS, self.clock.now_ms())
            await self.bus.start()
            self._bus_started = True

            if record:
                self.operations.set_startup_stage(
                    StartupStage.STORAGE, self.clock.now_ms()
                )
                await self.recorder.start()
                self.recorder.attach(self.bus)
                self._recording = True

            self.operations.set_startup_stage(
                StartupStage.MARKET_FEEDS, self.clock.now_ms()
            )
            if feeds:
                for adapter in self.adapters.values():
                    await adapter.start()
                if self.sim_driver is not None:
                    await self.sim_driver.start()
                self._feeds_started = True
        except BaseException as exc:
            # Record that startup raised, then re-raise the SAME exception.
            self.operations.mark_failed(
                self.clock.now_ms(), failure=f"{type(exc).__name__}: {exc}"
            )
            raise
        self._started = True
        self.operations.mark_running(self.clock.now_ms())

    async def stop(self) -> None:
        record = self.current_session()
        if (
            record is not None
            and record.status in (SessionStatus.STOPPED, SessionStatus.FAILED)
            and record.stopped_at is not None
        ):
            return

        now = self.clock.now_ms()
        self.operations.mark_stopping(now)
        try:
            self.operations.set_shutdown_stage(
                ShutdownStage.STOPPING_FEEDS, self.clock.now_ms()
            )
            if self.sim_driver is not None:
                await self.sim_driver.stop()
            for adapter in self.adapters.values():
                await adapter.stop()
            self._feeds_started = False

            self.operations.set_shutdown_stage(
                ShutdownStage.DRAINING_BUS, self.clock.now_ms()
            )
            await self.bus.stop()
            self._bus_started = False

            if self._recording:
                # Only close a store this platform actually opened.
                self.operations.set_shutdown_stage(
                    ShutdownStage.STOPPING_RECORDER, self.clock.now_ms()
                )
                await self.recorder.stop()
                self._recording = False
        except BaseException as exc:
            self.operations.mark_failed(
                self.clock.now_ms(), failure=f"{type(exc).__name__}: {exc}"
            )
            raise
        self._started = False
        stopped_at = self.clock.now_ms()
        self.operations.update_counts(
            stopped_at,
            ticks=self.orchestrator.ticks,
            events_recorded=self.recorder.events_recorded,
        )
        self.operations.mark_stopped(stopped_at)

    async def step_market(self, steps: int = 1) -> None:
        """Advance the synthetic market by ``steps`` and process the result."""
        if self.sim_driver is None:
            raise RuntimeError("platform is not running a simulated market")
        for _ in range(steps):
            await self.sim_driver.step()
            await self.bus.drain()

    # -- operational observability (Phases 11 + 12) ------------------------
    #
    # Every method below is a read. None is called from the tick path, the
    # startup path or the shutdown path, and none mutates anything a decision
    # depends on. This is the one aggregation surface, so a future dashboard,
    # API or operator tool does not have to rummage through every component.

    @property
    def profile(self) -> OperationalProfile:
        """What this session is for. **Not the trading mode.**

        ``settings.mode`` is PAPER and stays PAPER whatever this says: SHADOW
        runs the same ``PaperExecutor`` against the same ``PaperAccount``.
        """
        return self.settings.operational_profile

    @property
    def is_shadow(self) -> bool:
        return self.profile is OperationalProfile.SHADOW

    def session_manifest(
        self, now_ms: int, *, recording: bool = True
    ) -> SessionManifest:
        """What this session was configured to be.

        ``session_id`` is the recorder's, not a second identifier: every
        recorded event already carries it, and minting another would give one
        run two names.

        The last three fields state the boundary in the record itself, so a
        SHADOW manifest cannot be misread as a live one.
        """
        return SessionManifest(
            session_id=self.session_id,
            created_at=now_ms,
            label=self.recorder.label,
            trading_mode=self.settings.mode,
            operational_profile=self.profile,
            feed=self.settings.feed,
            symbols=list(self.settings.symbols),
            venues=[v.name for v in self.settings.enabled_venues],
            bus_backend=self.settings.bus,
            storage_backend=self.settings.storage.backend,
            initial_paper_balance=self.settings.paper_initial_balance,
            intelligence_provider=self.lumen.provider.name,
            config_digest=self.recorder.config_hash,
            recording_requested=recording,
            paper_executor=True,
            private_venue_access=False,
            real_order_submission=False,
        )

    def current_session(self):
        """The record for the run this process is executing, if started."""
        return self.operations.current_session()

    def operational_readiness(self, now_ms: int) -> OperationalReadiness:
        """Whether the platform is in a fit state to operate. **Reporting only.**

        This gates nothing: ``start()`` still starts, warm-up still decides
        when trading may begin, and the kill switch still decides when it must
        stop.

        **LUMEN's absence never makes this unready.** LUMEN is optional — not
        in the required-component set, not in ``required_agents`` — and the
        shipped default provider is permanently unavailable. Reporting a fault
        because the default configuration is the default configuration would
        be reporting a fault where there is none.
        """
        # ``REQUIRED_COMPONENTS`` is TIDAL, NORO, ZEPHR, RUNE, VESKA, MARIN.
        # LUMEN is deliberately not in it and is not added here: the same set
        # the warm-up path already uses is the set this reports on, so the two
        # cannot come to disagree about what the platform needs.
        agents_ok, unhealthy = self.health.all_healthy(REQUIRED_COMPONENTS, now_ms)
        market_ok = self.state.market is not None
        kill_clear = self.state.kill_switch.trading_allowed
        unknown_orders = len(self.veska.unknown_orders())
        record = self.current_session()
        recording_requested = bool(
            record is not None
            and record.manifest is not None
            and record.manifest.recording_requested
        )

        bus_ready = self._bus_started
        # A replay deliberately starts with feeds=False; in that case the
        # feed requirement is satisfied by recorded inputs rather than a live
        # adapter task. Before Platform.start(), _started remains False.
        feed_ready = self._started and (
            not self._feeds_requested or self._feeds_started
        )
        storage_ready = bool(
            self._started and (not recording_requested or self.recorder.healthy)
        )
        recording_ready = bool(
            self._started and (not recording_requested or self._recording)
        )
        risk_ready = self.health.status_of("RUNE", now_ms) is HealthStatus.HEALTHY
        execution_ready = (
            self.health.status_of("VESKA", now_ms) is HealthStatus.HEALTHY
            and not self.state.kill_switch.execution_disabled
        )
        reconciliation_ready = bool(
            self.marin.last_result is not None
            and self.marin.last_result.ok
            and self.health.status_of("MARIN", now_ms) is HealthStatus.HEALTHY
        )
        portfolio = self.state.portfolio or self.executor.account.snapshot()
        hedging_ready = self.okapi.readiness(
            portfolio, self.state.market, now_ms
        ).ready

        reasons: list[str] = []
        if not bus_ready:
            reasons.append("BUS_NOT_STARTED")
        if not feed_ready:
            reasons.append("FEED_NOT_READY")
        if not market_ok:
            reasons.append("NO_MARKET_STATE")
        if not agents_ok:
            reasons.extend(f"COMPONENT_UNHEALTHY:{name}" for name in unhealthy)
        if not risk_ready:
            reasons.append("RISK_NOT_READY")
        if not execution_ready:
            reasons.append("EXECUTION_NOT_READY")
        if not reconciliation_ready:
            reasons.append("RECONCILIATION_NOT_READY")
        if not hedging_ready:
            reasons.append("HEDGING_NOT_READY")
        if unknown_orders:
            reasons.append(f"UNKNOWN_ORDERS:{unknown_orders}")
        if recording_requested and not storage_ready:
            reasons.append("STORAGE_UNHEALTHY")
        if recording_requested and not recording_ready:
            reasons.append("NOT_RECORDING")
        if not kill_clear:
            reasons.append("KILL_SWITCH_ENGAGED")

        return OperationalReadiness(
            ready=not reasons,
            created_at=now_ms,
            profile=self.profile,
            feed=self.settings.feed,
            paper_mode_confirmed=self.settings.mode is TradingMode.PAPER,
            feed_ready=feed_ready,
            storage_ready=storage_ready,
            bus_ready=bus_ready,
            market_ready=market_ok,
            required_agents_ready=agents_ok,
            risk_ready=risk_ready,
            execution_ready=execution_ready,
            reconciliation_ready=reconciliation_ready,
            hedging_ready=hedging_ready,
            recording_ready=recording_ready,
            kill_switch_clear=kill_clear,
            # Reported, never required.
            intelligence_available=self.lumen.consecutive_failures == 0
            and self.lumen.calls > 0,
            reason_codes=reasons,
        )

    def operational_snapshot(self, now_ms: int) -> OperationalSnapshot:
        """One serializable view of the whole running platform.

        Each nested field is the owning phase's own snapshot, serialized.
        Copied, never recomputed and never interpreted — no decision is made
        from any aggregate here.
        """
        snapshot = self.health.snapshot(now_ms)
        portfolio = self.state.portfolio
        record = self.current_session()
        recording_requested = bool(
            record is not None
            and record.manifest is not None
            and record.manifest.recording_requested
        )
        execution_metrics = self.veska.metrics()
        reconciliation_metrics = self.marin.metrics()
        intelligence_snapshot = self.lumen.lumen_snapshot(now_ms)
        return OperationalSnapshot(
            created_at=now_ms,
            session=self.current_session(),
            components=[
                OperationalComponentSummary(
                    component=name,
                    status=component.status.value,
                    version=component.version,
                    required=name in REQUIRED_COMPONENTS,
                    last_heartbeat_ms=component.last_heartbeat_ms,
                    queue_depth=component.queue_depth,
                    error_count=component.error_count,
                    detail=component.detail,
                )
                for name, component in sorted(snapshot.components.items())
            ],
            coordination=self.orchestrator.coordination_snapshot(now_ms).model_dump(
                mode="json"
            ),
            risk={
                "kill_switch_engaged": self.state.kill_switch.engaged,
                "triggered_by": list(self.state.kill_switch.triggered_by),
                "utilization": (
                    self.state.risk_utilization.model_dump(mode="json")
                    if self.state.risk_utilization
                    else {}
                ),
            },
            execution=execution_metrics.model_dump(mode="json"),
            reconciliation={
                "ok": (
                    None if self.marin.last_result is None else self.marin.last_result.ok
                ),
                "open_discrepancies": len(self.marin.open_discrepancies()),
            },
            hedging=self.okapi.okapi_snapshot(
                self.executor.account.snapshot(), self.state.market, now_ms
            ).model_dump(mode="json"),
            intelligence=intelligence_snapshot.model_dump(mode="json"),
            portfolio=(portfolio.model_dump(mode="json") if portfolio else {}),
            recording={
                "requested": recording_requested,
                "active": self._recording and self.recorder.healthy,
                "session_id": self.session_id,
                "events_recorded": self.recorder.events_recorded,
                "storage_backend": self.settings.storage.backend,
                "config_digest": self.recorder.config_hash,
            },
            metrics=self.operations.metrics(
                ticks=self.orchestrator.ticks,
                events_recorded=self.recorder.events_recorded,
                paper_orders=self.oms.orders_created,
                paper_fills=self.oms.fills_applied,
                opportunities=self.orchestrator.coordination.opportunities_created_total,
                risk_rejections=self.orchestrator.coordination.risk_rejections_total,
                hedges=self.okapi.hedges_requested,
                reconciliations=reconciliation_metrics.runs_completed,
                intelligence_analyses=self.lumen.intel_registry.analyses_completed,
                shadow_decisions=len(self.shadow.decisions),
            ),
            readiness=self.operational_readiness(now_ms),
            incidents=self.operations.open_incidents(session_id=self.session_id),
        )

    def session_summary(self, now_ms: int) -> SessionSummary:
        """What this session did. **No judgement of any kind.**

        Facts only: counts and P&L. Nothing here classifies a session as good,
        bad, profitable enough, or ready for anything.
        """
        record = self.current_session()
        portfolio = self.state.portfolio
        reconciliation = self.marin.metrics()
        return SessionSummary(
            session_id=self.session_id,
            profile=self.profile,
            feed=self.settings.feed,
            started_at=record.started_at if record else None,
            stopped_at=record.stopped_at if record else None,
            ticks=self.orchestrator.ticks,
            events_recorded=self.recorder.events_recorded,
            opportunities=self.orchestrator.coordination.opportunities_created_total,
            orders=self.oms.orders_created,
            fills=self.oms.fills_applied,
            rejections=self.orchestrator.coordination.opportunity_rejections_total,
            hedges=self.okapi.hedges_requested,
            reconciliations=reconciliation.runs_completed,
            starting_equity=self.settings.paper_initial_balance,
            ending_equity=(portfolio.equity if portfolio else 0.0),
            gross_pnl=(portfolio.gross_pnl if portfolio else 0.0),
            net_pnl=(portfolio.net_pnl if portfolio else 0.0),
            fees=(portfolio.fees_paid if portfolio else 0.0),
            kill_switch_triggers=list(self.state.kill_switch.triggered_by),
        )

    def pre_live_readiness(self, now_ms: int) -> PreLiveReadinessSnapshot:
        """What a live deployment would need, and what actually exists.

        **This starts nothing.** There is no promotion path and no code that
        reads it to permit anything. Every live-side field is
        ``NOT_IMPLEMENTED`` because that is the truth, and every framework
        field is ``NOT_VALIDATED`` because a framework existing is not a
        framework working.
        """
        return PreLiveReadinessSnapshot(created_at=now_ms)

    # -- shadow ------------------------------------------------------------

    def shadow_readiness(self, now_ms: int) -> ShadowReadiness:
        """Whether a rehearsal is set up the way one should be. **Observational.**

        Blocks nothing. A shadow session against the simulated feed still
        runs; it is simply not a genuine live-market rehearsal, and this says
        so here rather than refusing at configuration load, where it would be
        a policy this phase may not set.

        ``private_execution_absent`` reports True because only
        ``PaperExecutor`` exists. It is phrased as an absence deliberately: a
        field named for the presence of live execution would be a place for
        someone to later set True, and there must be no such place.
        """
        live_feed = self.settings.feed is FeedKind.LIVE
        reasons: list[str] = []
        if not self.is_shadow:
            reasons.append("PROFILE_NOT_SHADOW")
        if not live_feed:
            reasons.append("FEED_NOT_PUBLIC_LIVE")
        if self.state.market is None:
            reasons.append("NO_MARKET_STATE")
        if not self._recording:
            reasons.append("NOT_RECORDING")
        if self.marin.last_result is None:
            reasons.append("NO_RECONCILIATION_BASELINE")

        return ShadowReadiness(
            ready=not reasons,
            created_at=now_ms,
            profile_is_shadow=self.is_shadow,
            paper_mode_confirmed=self.settings.mode is TradingMode.PAPER,
            public_live_feed_configured=live_feed,
            market_data_available=self.state.market is not None,
            recording_active=self._recording,
            coordination_available=True,
            risk_available=True,
            paper_execution_available=not self.state.kill_switch.execution_disabled,
            reconciliation_available=self.marin.last_result is not None,
            hedging_available=bool(self.okapi.desired_delta),
            private_execution_absent=True,
            reason_codes=reasons,
        )

    def shadow_snapshot(self, now_ms: int) -> ShadowSnapshot:
        """The rehearsal, summarised.

        Under the PAPER profile this reports ``enabled=False`` with empty
        counts: the observer is not attached and records nothing.

        Equity and P&L come from the platform's single ``PaperAccount``. There
        is no second shadow ledger — two would eventually disagree and nobody
        would know which to believe.
        """
        portfolio = self.state.portfolio
        return self.shadow.snapshot(
            now_ms,
            session_id=self.session_id,
            enabled=self.is_shadow,
            paper_equity=(portfolio.equity if portfolio else 0.0),
            paper_pnl=(portfolio.net_pnl if portfolio else 0.0),
            readiness=self.shadow_readiness(now_ms),
        )

    def shadow_decision(self, decision_id: str):
        """One rehearsal record, by registry id or by opportunity id."""
        return self.shadow.get(decision_id) or self.shadow.for_opportunity(decision_id)

    def shadow_decisions(self, limit: int = 50):
        """The most recent rehearsal records, newest last."""
        return self.shadow.recent(limit)

    def shadow_executions(self, decision_id: str | None = None):
        """Hypothetical execution records. **These are simulator output.**"""
        return self.shadow.execution_records(decision_id)


def build_platform(
    settings: Settings | None = None,
    *,
    clock: Clock | None = None,
    bus: EventBus | None = None,
    store: EventStore | None = None,
    market: SyntheticMarket | None = None,
    session_label: str = "",
    intelligence_provider=None,
    raise_on_handler_error: bool = False,
) -> Platform:
    """Construct the whole platform.

    Defaults produce a fully offline, deterministic paper session: a manual
    clock is *not* the default (live paper trading needs real time), but every
    other dependency can be swapped by argument.
    """
    settings = settings or load_settings()
    clock = clock or SystemClock()
    if bus is None:
        window = settings.risk.error_rate_window_deliveries
        bus = (
            InMemoryEventBus(
                raise_on_handler_error=raise_on_handler_error,
                error_rate_window=window,
            )
            if settings.bus == "memory"
            else build_bus(settings.bus, settings.redis_url, error_rate_window=window)
        )
    elif not hasattr(bus, "recent_error_rate"):
        # A caller-supplied bus is never swapped out for one this function
        # would rather have -- the whole point of the argument is that the
        # caller controls the transport. But RUNE's MAX_ERROR_RATE gate reads
        # this measurement, so a bus that cannot answer must fail here, at
        # construction, rather than have the orchestrator quietly fall back to
        # lifetime totals and feed the gate a number that means something else
        # (P5-8).
        raise TypeError(
            f"{type(bus).__name__} does not implement EventBus.recent_error_rate; "
            "the risk error-rate gate has no health input it can trust"
        )
    store = store or build_store(
        settings.storage.backend,
        sqlite_path=settings.storage.sqlite_path,
        postgres_dsn=settings.storage.postgres_dsn.get_secret_value(),
    )

    health = HealthRegistry(clock=clock)
    metrics = build_registry()
    state = SystemState(clock=clock)

    recorder = Recorder(
        store=store,
        clock=clock,
        label=session_label,
        config_hash=config_digest(settings.model_dump()),
    )

    # --- agents ---------------------------------------------------------
    tidal = Tidal(bus, clock, settings, health)
    noro = Noro(bus, clock, settings, health)
    zephr = Zephr(bus, clock, settings, health)
    provider = intelligence_provider or build_provider(
        settings.lumen.provider, model=settings.lumen.model
    )
    lumen = Lumen(bus, clock, settings, health, provider)
    rune = Rune(bus, clock, settings, health, ai=RuneAI(provider))
    okapi = Okapi(bus, clock, settings, health)

    # --- execution ------------------------------------------------------
    oms = OrderManager(clock=clock)
    account = PaperAccount(clock=clock, initial_balance=settings.paper_initial_balance)
    simulator = FillSimulator(settings.execution)
    executor = PaperExecutor(
        bus=bus,
        clock=clock,
        settings=settings,
        oms=oms,
        account=account,
        simulator=simulator,
    )
    veska = Veska(bus, clock, settings, health, executor)
    marin = Marin(bus=bus, clock=clock, health=health, oms=oms, account=account)
    # Phase 7: the two accounts of the truth this build actually has. The
    # execution source reads VESKA's snapshot rather than the OMS directly, so
    # it sees plan state as well as orders. No venue source is constructed --
    # none exists -- and no recorded source either; both are interfaces only.
    # Attaching these changes nothing about how reconciliation runs: MARIN's
    # existing algorithm is untouched, and the sources feed only the Phase 7
    # capture surface.
    marin.attach_sources(
        execution=ExecutionReconciliationSource(veska),
        account=AccountReconciliationSource(account),
    )

    kill_switch = KillSwitch(bus, clock, settings)

    # Phase 8: describe the participants this composition root just built.
    # Metadata only -- no callable, no address, no credential. Registration
    # changes nothing about trading: an unregistered agent still publishes
    # opinions, is still tracked by the barrier and is still weighed by the
    # consensus engine. ``settings.consensus`` is read, never written.
    agent_directory = _build_agent_directory(settings)

    orchestrator = Orchestrator(
        bus=bus,
        clock=clock,
        settings=settings,
        health=health,
        state=state,
        tidal=tidal,
        noro=noro,
        zephr=zephr,
        rune=rune,
        veska=veska,
        okapi=okapi,
        marin=marin,
        kill_switch=kill_switch,
        metrics=metrics,
        consensus=ConsensusEngine(settings.consensus, clock),
        detector=CrossVenueDetector(settings, clock),
        recorder=recorder,
        coordination=CoordinationRegistry(),
        agent_directory=agent_directory,
    )

    # Cross-venue relative value intends to carry no directional exposure.
    for symbol in settings.symbols:
        okapi.set_desired_delta(symbol, 0.0)

    # Phase 12: the rehearsal record, and the observer that fills it.
    #
    # The observer is attached to the bus ONLY under the SHADOW profile, so an
    # ordinary paper session neither subscribes nor accumulates a history it
    # will never read. That activation is observational: no economic branch
    # anywhere differs because the observer is running, which is the property
    # that makes a shadow session a rehearsal of THIS platform rather than of
    # a slightly different one.
    #
    # It is given a bus and a registry, and nothing else. There is no
    # orchestrator, executor or account reference on it, so there is no path
    # through the observer to anything that trades.
    is_shadow = settings.operational_profile is OperationalProfile.SHADOW
    shadow_registry = ShadowRegistry(
        market_data=(
            MarketDataProvenance.PUBLIC_LIVE_FEED
            if settings.feed is FeedKind.LIVE
            else MarketDataProvenance.SIMULATED
        )
    )
    shadow_observer = ShadowObserver(bus, shadow_registry, enabled=is_shadow)

    tidal.subscribe()
    noro.subscribe()
    zephr.subscribe()
    orchestrator.subscribe()
    shadow_observer.subscribe()
    bus.subscribe(
        lambda event: _lumen_market(lumen, event),
        types=[EventType.MARKET_STATE],
        name="lumen-market",
    )

    # --- venues ---------------------------------------------------------
    adapters: dict[str, VenueAdapter] = {}
    publishers: dict[str, VenueFeedPublisher] = {}
    for venue_config in settings.enabled_venues:
        # Each venue subscribes to the instruments it actually lists. Passing
        # the global strategy universe here was what forced every adapter to
        # ask its venue for BTC-USD, which the Binance formatter then "fixed"
        # by substituting BTC-USDT — the mechanism behind TIDAL-C3.
        adapter = build_adapter(venue_config, clock, venue_config.symbols)
        publisher = VenueFeedPublisher(bus, clock, venue_config.name)
        adapter.bind(
            publisher.publish,
            publisher.publish_raw if settings.storage.record_raw else None,
        )
        adapters[venue_config.name] = adapter
        publishers[venue_config.name] = publisher

    # Recovery path for a gapped book: TIDAL publishes the request, this hands
    # it to the adapter that owns the feed. Registered after the adapters exist
    # so the bridge sees the whole set.
    resync_bridge = ResyncBridge(adapters)
    bus.subscribe(
        resync_bridge,
        types=[EventType.BOOK_RESYNC_REQUESTED],
        name="venue-resync",
    )

    sim_driver = None
    simulated = {
        name: adapter
        for name, adapter in adapters.items()
        if isinstance(adapter, SimulatedVenueAdapter)
    }
    generator = market
    if simulated:
        # The generator must start on the platform's own clock. Seeding it at
        # a fixed epoch while the clock reads wall time makes every synthetic
        # book look days stale, and TIDAL — correctly — refuses to use it.
        # Generate exactly the instruments the simulated venues subscribe to,
        # not the whole strategy universe: a generated symbol nobody
        # subscribes to is wasted work, and a subscribed symbol nobody
        # generates is a book that never appears.
        simulated_symbols = sorted(
            {
                symbol
                for venue_config in settings.enabled_venues
                if venue_config.name in simulated
                for symbol in venue_config.symbols
            }
        )
        generator = generator or default_market(
            seed=settings.execution.seed,
            symbols=simulated_symbols,
            start_ms=clock.now_ms(),
        )
        sim_driver = SimulatedMarketDriver(generator, simulated, clock)

    return Platform(
        settings=settings,
        clock=clock,
        bus=bus,
        health=health,
        metrics=metrics,
        state=state,
        store=store,
        recorder=recorder,
        tidal=tidal,
        noro=noro,
        zephr=zephr,
        lumen=lumen,
        rune=rune,
        okapi=okapi,
        marin=marin,
        veska=veska,
        executor=executor,
        account=account,
        oms=oms,
        kill_switch=kill_switch,
        orchestrator=orchestrator,
        operations=OperationalRegistry(),
        shadow=shadow_registry,
        shadow_observer=shadow_observer,
        adapters=adapters,
        publishers=publishers,
        sim_driver=sim_driver,
        market_generator=generator,
        resync_bridge=resync_bridge,
    )


#: What each agent looks at and how often, as this build actually wires them.
#: Description only -- nothing dispatches on a scope or a cadence, and getting
#: one wrong changes no behaviour, only a display.
_AGENT_METADATA: dict[AgentId, tuple[str, AgentSubjectScope, AgentCadence, str]] = {
    AgentId.TIDAL: (
        "tidal-0.1",
        AgentSubjectScope.SYMBOL,
        AgentCadence.FAST,
        "Market data and microstructure: books, spreads, executability.",
    ),
    AgentId.NORO: (
        "noro-0.1",
        AgentSubjectScope.OPPORTUNITY,
        AgentCadence.FAST,
        "Relative valuation across venues.",
    ),
    AgentId.ZEPHR: (
        "zephr-0.1",
        AgentSubjectScope.OPPORTUNITY,
        AgentCadence.FAST,
        "Execution feasibility of a proposed trade.",
    ),
    AgentId.LUMEN: (
        "lumen-0.1",
        AgentSubjectScope.SYMBOL,
        AgentCadence.SLOW,
        "Narrative and regime context from an intelligence provider.",
    ),
    AgentId.OKAPI: (
        "okapi-0.1",
        AgentSubjectScope.SYMBOL,
        AgentCadence.EVENT_DRIVEN,
        "Delta measurement and standing hedge maintenance.",
    ),
    AgentId.RUNE: (
        "rune-0.1",
        AgentSubjectScope.GLOBAL,
        AgentCadence.EVENT_DRIVEN,
        "Risk authority: sizing and gates. Not a consensus participant.",
    ),
    AgentId.MARIN: (
        "marin-0.1",
        AgentSubjectScope.GLOBAL,
        AgentCadence.SLOW,
        "Reconciliation between execution and account truth.",
    ),
    AgentId.VESKA: (
        "veska-0.1",
        AgentSubjectScope.GLOBAL,
        AgentCadence.EVENT_DRIVEN,
        "Execution: planning, submission and order lifecycle.",
    ),
}


def _build_agent_directory(settings: Settings) -> AgentDirectory:
    """Describe the platform's participants for display.

    ``settings.consensus.required_agents`` and ``settings.consensus.weights``
    are mirrored into each descriptor and remain the authority: if the
    directory and the configuration ever disagree, the configuration is right
    and the directory is stale. Nothing here writes to ``settings``.
    """
    directory = AgentDirectory()
    required = set(settings.consensus.required_agents)
    for agent_id, (version, scope, cadence, description) in _AGENT_METADATA.items():
        directory.register(
            agent_id,
            service=agent_id.value,
            version=version,
            scope=scope,
            cadence=cadence,
            required_by_default=agent_id in required,
            weight=settings.consensus.weights.get(agent_id),
            description=description,
        )
    return directory


async def _lumen_market(lumen: Lumen, event: Event) -> None:
    from core.models.market import MarketState

    lumen.on_market_state(MarketState.model_validate(event.payload))
