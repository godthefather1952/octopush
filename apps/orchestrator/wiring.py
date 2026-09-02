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
from agents.noro import Noro
from agents.okapi import Okapi
from agents.rune import Rune, RuneAI
from agents.tidal import Tidal
from agents.zephr import Zephr
from apps.orchestrator.orchestrator import Orchestrator
from core.bus import EventBus, InMemoryEventBus, build_bus
from core.clock import Clock, SystemClock
from core.config import Settings, load_settings
from core.events import Event, EventType
from core.health import HealthRegistry
from core.models.market import OrderBookSnapshot, TradeEvent
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
from strategies.cross_venue import CrossVenueDetector
from venues.base.adapter import VenueAdapter
from venues.base.messages import BookDelta, RawMessage, VenueStatus, VenueStatusKind
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
    adapters: dict[str, VenueAdapter] = field(default_factory=dict)
    publishers: dict[str, VenueFeedPublisher] = field(default_factory=dict)
    sim_driver: SimulatedMarketDriver | None = None
    market_generator: SyntheticMarket | None = None
    _started: bool = False
    _recording: bool = False

    @property
    def session_id(self) -> str:
        return self.recorder.session_id

    async def start(self, *, record: bool = True, feeds: bool = True) -> None:
        """Start the platform.

        ``feeds=False`` leaves the venue adapters and the synthetic driver
        stopped, which is what replay wants: the recorded events *are* the
        market, and a live feed running alongside them would corrupt the run.
        """
        if self._started:
            return
        if record:
            await self.recorder.start()
            self.recorder.attach(self.bus)
            self._recording = True
        if feeds:
            for adapter in self.adapters.values():
                await adapter.start()
            if self.sim_driver is not None:
                await self.sim_driver.start()
        self._started = True

    async def stop(self) -> None:
        if self.sim_driver is not None:
            await self.sim_driver.stop()
        for adapter in self.adapters.values():
            await adapter.stop()
        await self.bus.stop()
        if self._recording:
            # Only close a store this platform actually opened.
            await self.recorder.stop()
            self._recording = False
        self._started = False

    async def step_market(self, steps: int = 1) -> None:
        """Advance the synthetic market by ``steps`` and process the result."""
        if self.sim_driver is None:
            raise RuntimeError("platform is not running a simulated market")
        for _ in range(steps):
            await self.sim_driver.step()
            await self.bus.drain()


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
    bus = bus or (
        InMemoryEventBus(raise_on_handler_error=raise_on_handler_error)
        if settings.bus == "memory"
        else build_bus(settings.bus, settings.redis_url)
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

    kill_switch = KillSwitch(bus, clock, settings)
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
    )

    # Cross-venue relative value intends to carry no directional exposure.
    for symbol in settings.symbols:
        okapi.set_desired_delta(symbol, 0.0)

    tidal.subscribe()
    noro.subscribe()
    zephr.subscribe()
    orchestrator.subscribe()
    bus.subscribe(
        lambda event: _lumen_market(lumen, event),
        types=[EventType.MARKET_STATE],
        name="lumen-market",
    )

    # --- venues ---------------------------------------------------------
    adapters: dict[str, VenueAdapter] = {}
    publishers: dict[str, VenueFeedPublisher] = {}
    for venue_config in settings.enabled_venues:
        adapter = build_adapter(venue_config, clock, settings.symbols)
        publisher = VenueFeedPublisher(bus, clock, venue_config.name)
        adapter.bind(
            publisher.publish,
            publisher.publish_raw if settings.storage.record_raw else None,
        )
        adapters[venue_config.name] = adapter
        publishers[venue_config.name] = publisher

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
        generator = generator or default_market(
            seed=settings.execution.seed,
            symbols=settings.symbols,
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
        adapters=adapters,
        publishers=publishers,
        sim_driver=sim_driver,
        market_generator=generator,
    )


async def _lumen_market(lumen: Lumen, event: Event) -> None:
    from core.models.market import MarketState

    lumen.on_market_state(MarketState.model_validate(event.payload))
