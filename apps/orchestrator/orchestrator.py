"""The orchestrator — the head of the trading floor.

It does not perform the analysis itself.  It coordinates: it drives the tick,
validates freshness, computes consensus, decides when risk evaluation is
required, creates trade intents, moves opportunities through their state
machine, reacts to health failures, and pulls the kill switch.

Order of operations in a tick matters, and it is deliberate:

1. observe    — TIDAL publishes market state; NORO and ZEPHR recompute.
2. settle     — the executor advances, fills land, the account updates.
3. measure    — the portfolio is marked and every component heartbeats.
4. protect    — MARIN reconciles, OKAPI reports delta, the kill switch
                evaluates, and its actions are carried out.
5. manage     — exposure is hedged, and open positions are re-evaluated and
                exited if consensus decays.
6. seek       — new opportunities are detected and worked, if allowed.

Protection runs before anything new is opened, and existing positions are
managed before new ones are sought.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from agents.marin import Marin
from agents.noro import Noro
from agents.okapi import Okapi
from agents.rune import RiskContext, Rune
from agents.tidal import Tidal
from agents.zephr import Zephr
from apps.orchestrator.agent_directory import AgentDirectory
from apps.orchestrator.coordination import CoordinationRegistry
from core.bus import EventBus
from core.bus.barrier import ResponseBarrier
from core.clock import Clock
from core.config import Settings
from core.events import Event, EventType
from core.health import HealthRegistry
from core.models.agent import AgentOpinion, ConsensusResult
from core.models.common import AgentId, Millis, Side
from core.models.execution import ExecutionRole, FillEvent, OrderStatus
from core.models.hedging import HedgeRequestStatus
from core.models.market import MarketState, safe_bps
from core.models.opportunity import (
    STRATEGY_TRANSITIONS,
    CostBreakdown,
    Opportunity,
    OpportunityLeg,
    StrategyState,
    TradeIntent,
)
from core.models.ops import HealthStatus, Severity, SystemEvent
from core.models.orchestration import (
    AgentDirectorySnapshot,
    BarrierSnapshot,
    ConsensusEvaluationRecord,
    ConsensusPurpose,
    ConsensusRequestRecord,
    CoordinationReadiness,
    DecisionTrace,
    OpinionReference,
    OpportunityWorkflowSummary,
    OrchestrationPhase,
    OrchestrationSnapshot,
    OrchestrationTickRecord,
)
from core.models.portfolio import PortfolioState
from core.models.risk import CommittedExposure, RiskDecision, RiskVerdict
from core.state import OpportunityRecord, SystemState
from execution.veska import Veska
from monitoring import metrics as M
from monitoring.attribution import AttributionBuilder, Scorecard
from monitoring.metrics import MetricsRegistry
from risk.kill_switch import KillSwitch, KillSwitchInputs, KillSwitchState
from strategies.consensus import ConsensusEngine
from strategies.cross_venue import REQUIRED_COMPONENTS, STRATEGY, CrossVenueDetector

if TYPE_CHECKING:
    # Imported for typing only: storage imports core, and a runtime import
    # here would close the cycle.
    from storage import Recorder

log = logging.getLogger(__name__)

SERVICE = "ORCHESTRATOR"
VERSION = "orchestrator-0.1"


class IllegalStrategyTransition(RuntimeError):
    pass


@dataclass
class Orchestrator:
    bus: EventBus
    clock: Clock
    settings: Settings
    health: HealthRegistry
    state: SystemState
    tidal: Tidal
    noro: Noro
    zephr: Zephr
    rune: Rune
    veska: Veska
    okapi: Okapi
    marin: Marin
    kill_switch: KillSwitch
    metrics: MetricsRegistry
    consensus: ConsensusEngine
    detector: CrossVenueDetector
    #: Explicit agent-response tracking. Replaces the old assumption that
    #: bus.drain() means "the agents have answered" — a guarantee no
    #: distributed transport can make (see core/bus/base.py clause 4).
    barrier: ResponseBarrier | None = None
    #: Phase 8. The platform's memory of what it coordinated: which tick, which
    #: phase, which agents were asked, who answered, which consensus produced
    #: which intent. Written to beside the code that already decides and read
    #: by nothing in the tick path — a registry a decision consulted would be a
    #: second decision authority, and an untested one.
    coordination: CoordinationRegistry = field(default_factory=CoordinationRegistry)
    #: Phase 8. Agent metadata for display. ``ConsensusConfig.required_agents``
    #: still decides who is required; this only describes them.
    agent_directory: AgentDirectory = field(default_factory=AgentDirectory)
    scorecard: Scorecard = field(default_factory=Scorecard)
    #: Reconcile every N ticks; every tick would be wasteful and every hour
    #: would be too late.
    reconcile_every: int = 20
    #: How many times an exit is re-submitted before the residual is handed to
    #: OKAPI's standing hedge loop.
    max_exit_attempts: int = 3
    ticks: int = 0
    #: The platform starts in warm-up: books are empty, agents have not yet
    #: heartbeat, and nothing is known. It does not trade, and the kill switch
    #: is not evaluated, until every required component has reported HEALTHY
    #: at least once. Firing the kill switch on state that has simply not
    #: initialised would be noise, and trading before it has is worse.
    warmed_up: bool = False
    warmup_ticks: int = 0
    #: How often to warn while warm-up has not completed.
    warmup_warn_every: int = 40
    #: Recorder whose health gates trading. None when the session is running
    #: deliberately unrecorded.
    recorder: Recorder | None = None
    #: Consecutive failed flushes before storage is considered down. One
    #: transient write error should not halt the platform; a sustained
    #: inability to persist should.
    storage_failure_threshold: int = 3
    attributions: dict[str, AttributionBuilder] = field(default_factory=dict)
    #: Opportunity id -> GROSS quote exposure that opportunity currently has
    #: working, summed across ALL of its legs.
    #:
    #: NOT the per-leg ``approved_notional``. RUNE sizes and gates in per-leg
    #: units — a two-leg trade authorised at 25,000 puts 25,000 on each of two
    #: venues — so one working opportunity consumes ``approved_notional *
    #: len(legs)`` of the strategy's gross budget. Storing the per-leg figure
    #: here made ``gate_strategy_exposure`` compare a per-leg sum against a
    #: per-trade projection and authorise roughly ``leg count`` times
    #: ``max_strategy_exposure`` (P5-2). Read only through
    #: :meth:`_current_strategy_exposure`.
    working_notional: dict[str, float] = field(default_factory=dict)
    #: Symbol -> hedge orders still in flight. OKAPI measures delta from the
    #: portfolio, which only moves on fills, so without this a hedge would be
    #: re-submitted on every tick until the first one filled — and the symbol
    #: would end up hedged several times over.
    working_hedges: dict[str, list[str]] = field(default_factory=dict)
    #: The one logical timestamp the tick currently executing uses for every
    #: time-dependent decision — see "TICK TIME" on :meth:`tick`. ``None``
    #: outside a tick, where :attr:`tick_time` falls back to the live clock.
    _tick_time: Millis | None = None

    def __post_init__(self) -> None:
        self.health.register(SERVICE, VERSION)
        if self.recorder is not None:
            self.health.register("RECORDER", "recorder-0.1")
        if self.barrier is None:
            self.barrier = ResponseBarrier(self.clock)

    # -- wiring ------------------------------------------------------------

    def subscribe(self) -> None:
        self.bus.subscribe(
            self._on_opinion, types=[EventType.AGENT_OPINION], name="orchestrator-opinions"
        )
        self.bus.subscribe(
            self._on_fill, types=[EventType.PAPER_FILL], name="orchestrator-fills"
        )
        self.bus.subscribe(
            self._on_order,
            types=[EventType.PAPER_ORDER_CREATED, EventType.PAPER_ORDER_UPDATED],
            name="orchestrator-orders",
        )
        self.bus.subscribe(self._count_event, types=None, name="orchestrator-metrics")

    async def _count_event(self, event: Event) -> None:
        self.metrics.inc(M.EVENTS_PROCESSED, source=event.source, type=event.type.value)

    async def _on_opinion(self, event: Event) -> None:
        opinion = AgentOpinion.model_validate(event.payload)
        self.state.put_opinion(opinion)
        if opinion.correlation_id:
            self.barrier.record(opinion.correlation_id, opinion.agent_id)

    async def _on_order(self, event: Event) -> None:
        from core.models.execution import PaperOrder

        order = PaperOrder.model_validate(event.payload)
        self.state.put_order(order)
        if event.type is EventType.PAPER_ORDER_CREATED:
            self.metrics.inc(M.PAPER_ORDERS, venue=order.venue, symbol=order.symbol)

    async def _on_fill(self, event: Event) -> None:
        fill = FillEvent.model_validate(event.payload)
        if not self.state.add_fill(fill):
            return
        self.metrics.observe(M.SLIPPAGE_BPS, fill.slippage_bps, venue=fill.venue)
        builder = self.attributions.get(fill.correlation_id or "")
        if builder is not None:
            builder.add_fill(fill.notional, fill.fee, fill.slippage_bps)
            # fill.realized_pnl_delta was captured by PaperAccount.apply_fill
            # at the moment it applied THIS fill -- not re-derived here from
            # live account state. PAPER_FILL is queued at publish time, not
            # delivered, so several fills on the same venue:symbol can land
            # on the account before any of their handlers run; reading
            # current cumulative state here would attribute whichever fills
            # happened to apply first to whichever handler happens to run
            # first, regardless of which fill actually produced it.
            builder.add_realized(fill.realized_pnl_delta)
        record = self.state.opportunities.get(fill.correlation_id or "")
        if record is not None:
            record.fees += fill.fee
            record.filled_notional += fill.notional

    # -- state machine -----------------------------------------------------

    async def transition(self, record: OpportunityRecord, target: StrategyState) -> None:
        if target not in STRATEGY_TRANSITIONS[record.state]:
            raise IllegalStrategyTransition(
                f"{record.opportunity.opportunity_id}: {record.state} -> {target}"
            )
        previous = record.state
        record.state = target
        record.updated_at = self.tick_time
        await self.bus.publish(
            Event(
                type=EventType.STRATEGY_STATE_CHANGED,
                ts_ms=record.updated_at,
                source=SERVICE,
                schema_name="SystemEvent",
                correlation_id=record.opportunity.opportunity_id,
                payload=SystemEvent(
                    created_at=record.updated_at,
                    correlation_id=record.opportunity.opportunity_id,
                    kind="STRATEGY_STATE_CHANGED",
                    component=SERVICE,
                    message=f"{previous.value} -> {target.value}",
                    detail={
                        "opportunity_id": record.opportunity.opportunity_id,
                        "symbol": record.opportunity.symbol,
                        "from": previous.value,
                        "to": target.value,
                        "reason": record.rejected_reason or "",
                    },
                ).to_json_dict(),
            )
        )
        # Phase 8: mirror the state onto the trace. ``record.state`` above
        # remains the authority -- this is a copy for readers of the trace, and
        # a copy that disagrees means the mirror is stale, never that the
        # record is wrong. ``trace_for_opportunity`` returns None for an
        # opportunity with no trace, which is why nothing here can fail.
        self.coordination.update_trace_state(
            record.opportunity.opportunity_id,
            target,
            record.updated_at,
            rejected_reason=record.rejected_reason,
        )
        if target in (StrategyState.CLOSED, StrategyState.REJECTED):
            self.detector.release(record.opportunity.symbol, record.opportunity.opportunity_id)
            self.working_notional.pop(record.opportunity.opportunity_id, None)
            self.zephr.forget(record.opportunity.opportunity_id)

    async def _reject(self, record: OpportunityRecord, reason: str) -> None:
        record.rejected_reason = reason
        self.metrics.inc(M.OPPORTUNITIES_REJECTED, stage=reason)
        self.coordination.count_opportunity_rejection()
        await self.transition(record, StrategyState.REJECTED)

    # -- the tick ----------------------------------------------------------

    @property
    def tick_time(self) -> Millis:
        """The canonical logical time of the tick currently executing.

        Outside a tick this falls back to a live clock read, so anything
        reached from a bus handler rather than from :meth:`tick` still gets
        a sensible answer.
        """
        return self.clock.now_ms() if self._tick_time is None else self._tick_time

    async def tick(self) -> None:
        """Run one orchestration cycle.

        TICK TIME (Phase 2 Batch 1.3 — P2-14)
        =====================================
        A tick executes at ONE logical timestamp: :attr:`tick_time`, captured
        once at the market-snapshot boundary (immediately after
        ``_observe()``, the same instant the ``ORCHESTRATOR_TICK`` marker
        records). Every time-dependent decision belonging to this tick —
        opportunity validity, agent-response deadlines, intent creation and
        deadlines, order submission and its acknowledgement deadline,
        cancellation, expiry, fill stamping, marking — uses that one value
        rather than reading the clock again as the tick proceeds.

        This is not a stylistic preference; it is what makes the tick
        replayable. A recorded session preserves exactly one timestamp per
        tick, so replay re-executes the whole tick at that instant. In a live
        run the clock does NOT stand still meanwhile: the feed advances it
        concurrently, so a mid-tick clock read can land hundreds of
        milliseconds after the snapshot the tick is reasoning about. Before
        this batch, ``PaperExecutor.submit()`` read the clock itself and
        stamped ``submitted_at`` from such a read; replay reconstructed the
        same submission 300ms earlier, its acknowledgement deadline therefore
        fell on the other side of the very next tick, and replay filled an
        order the original had not — different fill prices, different P&L,
        a different kill-switch outcome, from an identical recorded market.

        The clock is still read freely for things that are observations
        rather than decisions (log/metric stamps, health heartbeats): those
        do not change what the platform does.
        """
        self.ticks += 1

        # First, not last: the warm-up branch below can return early, and a
        # tick that skipped persistence would reintroduce exactly the quiet
        # period this is here to close.
        await self._persist()

        market = await self._observe()

        # THE tick time: the exact instant TIDAL assembled the snapshot this
        # tick reasons about -- NOT a fresh clock read taken once _observe()
        # returns (Phase 2 Batch 1.4 -- P2-15).
        #
        # _observe() awaits the market-state publication and the bus drain
        # that follows it, and a live feed advances the clock throughout. A
        # clock read afterwards is therefore later than the snapshot: the
        # original tick would decide at that later time while its recorded
        # MarketState carried the earlier one. Replay pins its clock to the
        # marker and rebuilds the snapshot there, so the very same book would
        # be aged against two different instants across the two runs -- FRESH
        # in one, DEGRADED in the other, with everything DataQuality gates
        # (opportunity generation, RUNE's data-age check, health) diverging
        # behind it.
        #
        # Adopting market.created_at closes that: the snapshot instant, the
        # tick's decision time and the ORCHESTRATOR_TICK marker are one
        # value, in the original run and in replay alike, and replay's own
        # build_state() reads that same instant straight back.
        #
        # This is also still the correct marker timestamp for Batch 1.2's
        # ordering invariant. The watermark covers exactly the inputs TIDAL
        # had applied when build_state() ran, and every one of those was
        # published before it -- so each sorts at or before the marker, with
        # the sequence tiebreaker settling any shared millisecond (the marker
        # is published later, so it carries the higher sequence).
        self._tick_time = market.created_at
        try:
            await self._tick_body(market)
        except BaseException as exc:
            # Phase 8: record that the tick failed, then re-raise the SAME
            # exception. The registry is a witness, not a handler -- it does
            # not catch, retry, translate or downgrade anything. A framework
            # that turned a raised tick into a tidy note would be making a
            # recovery decision nobody asked for, and the caller would never
            # learn the tick did not run.
            self.coordination.fail_tick(
                self.tick_time, error=f"{type(exc).__name__}: {exc}"
            )
            raise
        else:
            self.coordination.complete_tick(self.tick_time)
        finally:
            self._tick_time = None

    async def _tick_body(self, market: MarketState) -> None:
        """The rest of the tick, running at the fixed :attr:`tick_time`."""
        now = self.tick_time

        # Phase 8 coordination metadata. Every call below sits BESIDE the code
        # that already ran; none of them changes an order of operations, a
        # branch or a return. Nothing later in this method reads what they
        # record.
        self.coordination.begin_tick(
            now,
            tick_number=self.ticks,
            market_timestamp=market.created_at,
            warming_up=not self.warmed_up,
        )
        # OBSERVE has already happened: it is what produced ``market``, at
        # exactly this instant. It is recorded as opened and closed here
        # rather than around ``_observe()`` because the tick's logical time is
        # not known until the snapshot exists, and a phase stamped from a
        # clock read would not survive replay.
        self.coordination.enter_phase(OrchestrationPhase.OBSERVE, now)
        self.coordination.complete_phase(OrchestrationPhase.OBSERVE, now)

        # Unconditional -- a warm-up tick or one that produces no trades is
        # still a real tick boundary.
        await self._mark_tick_boundary(now)

        self.coordination.enter_phase(OrchestrationPhase.SETTLE, now)
        await self._settle(now)
        self.coordination.complete_phase(OrchestrationPhase.SETTLE, now)

        self.coordination.enter_phase(OrchestrationPhase.MEASURE, now)
        portfolio = self._measure(market)
        self._refresh_risk_utilization(portfolio)
        self.coordination.complete_phase(OrchestrationPhase.MEASURE, now)

        if not self.warmed_up:
            self.warmup_ticks += 1
            if self.marin.last_result is None:
                # Establish the reconciliation baseline (and MARIN's health)
                # against an empty account before any trading is possible.
                await self.marin.run(self.tick_time)
            self.state.health = self.health.snapshot(self.tick_time)
            ok, bad = self.health.all_healthy(
                [c for c in REQUIRED_COMPONENTS if c != SERVICE], self.tick_time
            )
            if ok:
                self.warmed_up = True
                log.info("warm-up complete", extra={"ticks": self.warmup_ticks})
            else:
                self._heartbeat()
                # A warm-up that never finishes means the platform is quietly
                # doing nothing, which is the worst way for it to fail. Say so.
                if self.warmup_ticks % self.warmup_warn_every == 0:
                    log.warning(
                        "still warming up",
                        extra={"ticks": self.warmup_ticks, "pending": bad},
                    )
                    self.state.record_error(
                        f"warm-up incomplete after {self.warmup_ticks} ticks: "
                        + ",".join(bad)
                    )
                else:
                    log.debug("warming up", extra={"pending": bad})
                return

        self.coordination.enter_phase(OrchestrationPhase.PROTECT, now)
        await self._protect(market, portfolio)
        self.coordination.complete_phase(OrchestrationPhase.PROTECT, now)

        self.coordination.enter_phase(OrchestrationPhase.MANAGE, now)
        await self._manage(market, portfolio)
        self.coordination.complete_phase(OrchestrationPhase.MANAGE, now)

        if self.state.kill_switch.trading_allowed:
            # The guard is unchanged and still decides whether SEEK runs. The
            # phase is recorded inside it, so a tick with no SEEK record is a
            # tick where seeking was not allowed -- which is the fact worth
            # keeping.
            self.coordination.enter_phase(OrchestrationPhase.SEEK, now)
            await self._seek(market, portfolio)
            self.coordination.complete_phase(OrchestrationPhase.SEEK, now)
        await self._publish_state(portfolio)
        self._prune()
        self._heartbeat()

    async def _mark_tick_boundary(self, now: Millis) -> None:
        """Publish a durable timeline marker for this tick's logical position.

        Replay used to run one orchestrator tick per replayed market-input
        event -- a stand-in for the real tick cadence that happened to work
        only because nothing checked it. The true cadence (e.g. 20 book
        events between two 250ms ticks live) is a fact about the original
        run, not something derivable from how many market events a replay
        happens to read back. ``ORCHESTRATOR_TICK`` is deliberately excluded
        from ``MARKET_INPUT_TYPES`` -- replay reads it as a control marker,
        never republishes it onto the bus as data.

        The marker's OWN position in the recorded timeline (its sequence
        number) is not, by itself, sufficient to say which market inputs
        this tick actually saw: under an asynchronous bus, an input can be
        published (and recorded) well before this marker while still not
        having been *dispatched* to TIDAL yet, and conversely an input
        published after this marker's own ``bus.publish()`` call can race
        ahead and be applied before it, if the event loop happens to
        interleave that way. Publication order is not delivery order.

        ``processed_input_sequence`` is the actual fact replay needs:
        TIDAL's own record of the highest market-input sequence number it
        had *applied* to book state at the exact moment this tick's
        ``MarketState`` was built (``Tidal.last_snapshot_input_sequence``,
        frozen in ``publish_state()`` -- not read fresh here, since it can
        keep advancing during this tick's own post-snapshot ``bus.drain()``
        without that being reflected in the market state already computed).
        Replay uses this value to defer applying any input whose sequence
        exceeds it until whichever later tick's watermark does cover it,
        rather than assuming "published earlier" means "seen by this tick".
        """
        await self.bus.publish(
            Event(
                type=EventType.ORCHESTRATOR_TICK,
                ts_ms=now,
                source=SERVICE,
                schema_name="OrchestratorTick",
                payload={
                    "tick": self.ticks,
                    "warmed_up": self.warmed_up,
                    "processed_input_sequence": self.tidal.last_snapshot_input_sequence,
                },
            )
        )

    async def _persist(self) -> None:
        """Give the recorder a chance to flush on age, not only on traffic.

        Without this the buffer's age trigger only fires when a new event
        arrives, so a quiet period leaves the events explaining the quiet
        unpersisted.
        """
        if self.recorder is not None:
            await self.recorder.flush_if_due()

    def _recorder_heartbeat(self) -> None:
        """Surface persistence health alongside every other component."""
        recorder = self.recorder
        if recorder is None:
            return
        failures = recorder.consecutive_failures
        if failures >= self.storage_failure_threshold:
            status, detail = HealthStatus.OFFLINE, (
                f"{failures} consecutive flush failures; "
                f"{recorder.unpersisted} events awaiting confirmation, "
                f"{recorder.events_lost} permanently lost"
            )
        elif failures:
            status, detail = HealthStatus.DEGRADED, f"{failures} recent flush failures"
        else:
            status, detail = HealthStatus.HEALTHY, ""
        self.health.heartbeat(
            "RECORDER", status=status, version="recorder-0.1", detail=detail
        )

    def _prune(self) -> None:
        """Drop opinions whose subject is no longer live.

        Opinions are keyed by opportunity id, and opportunities are created
        continuously, so without this the map grows for the lifetime of the
        process.
        """
        keep = {r.opportunity.opportunity_id for r in self.state.open_opportunities()}
        keep |= set(self.settings.symbols)
        self.state.prune_opinions(keep)

    async def _observe(self) -> MarketState:
        market = await self.tidal.publish_state()
        await self.bus.drain()
        self.state.market = market
        self.veska.executor.update_market(market)
        for key, venue_state in market.venues.items():
            if venue_state.latency_ms is not None:
                self.metrics.observe(
                    M.MARKET_DATA_LATENCY, venue_state.latency_ms, venue=venue_state.venue
                )
            self.metrics.set(
                M.STALE_FEEDS,
                0.0 if venue_state.quality.is_usable else 1.0,
                pair=key,
            )
            self.metrics.set(
                M.WS_RECONNECTS, float(venue_state.reconnects), venue=venue_state.venue
            )
        return market

    async def _settle(self, now: Millis) -> None:
        """Advance simulated execution and let fills land."""
        fills = await self.veska.poll(now)
        if fills:
            await self.bus.drain()
        for order in list(self.veska.open_orders()):
            if order.status is OrderStatus.PARTIALLY_FILLED:
                self.metrics.inc(M.PARTIAL_FILLS, venue=order.venue)

    def _measure(self, market: MarketState) -> PortfolioState:
        marks: dict[str, float] = {}
        for key, venue_state in market.venues.items():
            if venue_state.metrics.mid is not None:
                marks[key] = venue_state.metrics.mid
        self.veska.executor.account.mark(marks, self.tick_time)
        portfolio = self.veska.executor.account.snapshot()
        self.state.portfolio = portfolio
        # Execution and risk heartbeat here, before anything reads health:
        # the kill switch must judge the health of this tick, not the last.
        self.veska.heartbeat()
        self.rune.heartbeat()
        self.marin.heartbeat()
        self._recorder_heartbeat()
        self.state.health = self.health.snapshot(self.tick_time)

        self.metrics.set(M.NET_PNL, portfolio.net_pnl)
        self.metrics.set(M.GROSS_PNL, portfolio.gross_pnl)
        self.metrics.set(M.FEES_PAID, portfolio.fees_paid)
        self.metrics.set(M.DRAWDOWN, portfolio.drawdown)
        for name, component in (self.state.health.components or {}).items():
            self.metrics.set(
                M.AGENT_UP, 1.0 if component.status is HealthStatus.HEALTHY else 0.0, agent=name
            )
        return portfolio

    def _current_strategy_exposure(self) -> float:
        """Gross quote exposure the strategy currently has working.

        The single source for both the risk gate and the dashboard. Values in
        :attr:`working_notional` are already gross across each opportunity's
        legs, so this is a plain sum — but it exists as a named method so the
        gate and the published utilization cannot drift into different units
        the way they had (P5-2 / P5-13): ``_risk_check`` fed
        ``gate_strategy_exposure`` and ``_refresh_risk_utilization`` fed the
        dashboard from two separate expressions that both happened to be
        per-leg sums against a per-trade budget.
        """
        return sum(self.working_notional.values())

    def _current_committed_exposure(self) -> CommittedExposure:
        """Exposure already committed by working, unfilled ENTRY orders.

        DERIVED, NEVER STORED
        =====================
        Computed from :attr:`SystemState.orders`, the platform's authoritative
        order state, every time it is asked for. The alternative — a mutable
        reservation ledger incremented on submit and decremented on partial
        fill, cancel, reject and expiry — is a second copy of the order
        lifecycle that has to be kept in step with the first by hand, and that
        drifts the moment one path forgets to release. A snapshot recomputed
        from the orders themselves cannot drift, because there is nothing to
        drift from.

        WHAT COUNTS: EVERYTHING NOT YET TERMINAL
        ========================================
        The liveness helper on :class:`PaperOrder` excludes UNKNOWN, which is a
        real state meaning "the venue-side truth is not known", not a failure.
        An UNKNOWN order may well be resting on the venue and may fill at any
        moment, so releasing its reservation would free budget against risk the
        platform still carries. Terminality is therefore the test used here,
        and only the four genuinely terminal statuses — FILLED, CANCELLED,
        REJECTED, EXPIRED — release.

        HOW MUCH: REMAINING QUANTITY AT THE EXPECTED PRICE
        ==================================================
        ``remaining_quantity * expected_price``. For an untouched entry order
        this reconstructs exactly the per-leg ``approved_notional`` RUNE
        authorised, because ``Veska.build_plan`` sizes an entry leg as
        ``approved_notional / expected_price``. As the order fills, the
        remaining quantity shrinks and the reservation hands over to the
        position it has become, so the two never double-count.

        It is deliberately NOT built from executed fill prices and quantities:
        those measure what already filled — which the portfolio has by then
        recorded anyway — rather than what is still exposed, and they say
        nothing at all about an order that has not filled once, which is
        precisely the case this exists for. A fully filled order releases here
        and appears as a position instead.

        The same remaining quantity also drives
        :attr:`CommittedExposure.unhedged_fill_risk`, accumulated per symbol by
        side — so a partial fill shrinks the pending leg risk by exactly the
        amount it adds to the actual residual OKAPI measures, and a terminal
        order releases it entirely (P5-18).

        WHICH ORDERS: ENTRIES ONLY
        ==========================
        Exits and hedges REDUCE exposure. Counting them as new commitments
        would make the platform's own risk-reduction look like risk-taking and
        could block the trade that closes a breach. An order is an entry when
        its ``intent_id`` is the entry intent of a known opportunity record;
        ``Orchestrator._decide`` is the only place that sets ``record.intent``,
        and ``_submit_exit`` and ``_hedge`` build their own intents which are
        never stored there.

        FAIL-CLOSED ON ANYTHING ELSE
        ============================
        An order that is neither a known entry nor accounted for as a known
        exit/hedge order is RESERVED as though it were an entry. That is the
        conservative direction: it over-reserves and blocks, where skipping it
        would under-reserve and authorise. It is reachable — an opportunity
        whose record has aged out of ``SystemState``'s bounded history, or a
        superseded UNKNOWN hedge — and it is state-dependent, never
        time-dependent: nothing here consults a clock or an order's age, so the
        same set of orders always produces the same snapshot.

        The one thing this cannot see is an order that is no longer in
        ``SystemState.orders`` at all. ``SystemState._trim_orders`` keeps every
        live order and every order a retained opportunity record names, so a
        working entry is never evicted while it is being tracked; an order that
        has gone UNKNOWN *and* whose record has aged out of the bounded history
        is the one gap, and it belongs to that retention policy rather than to
        this calculation.
        """
        entry_intents = {
            record.intent.intent_id
            for record in self.state.opportunities.values()
            if record.intent is not None
        }
        # Order ids the platform is still tracking somewhere. Used only to tell
        # a KNOWN exit/hedge order from an unclassifiable one; an entry order
        # appears here too and is caught by the entry test first.
        accounted: set[str] = set()
        for order_ids in self.working_hedges.values():
            accounted.update(order_ids)
        for record in self.state.opportunities.values():
            accounted.update(record.order_ids)

        gross = 0.0
        net = 0.0
        by_venue: dict[str, float] = {}
        by_position: dict[str, float] = {}
        # Per SYMBOL, not per venue:symbol. Unhedged residual is a delta
        # measured across venues (``PortfolioState.net_delta_by_symbol``), so a
        # BUY on one venue and a SELL on another cancel for this purpose even
        # though they are two distinct positions.
        buy_remaining: dict[str, float] = {}
        sell_remaining: dict[str, float] = {}
        for order in self.state.orders.values():
            if order.is_terminal:
                continue
            is_entry = order.intent_id is not None and order.intent_id in entry_intents
            if not is_entry and order.client_order_id in accounted:
                # Positively identified as an exit or a hedge: exposure-reducing.
                continue
            reserved = order.remaining_quantity * order.expected_price
            if reserved <= 0:
                continue
            gross += reserved
            net += reserved * order.side.sign
            side_totals = buy_remaining if order.side is Side.BUY else sell_remaining
            side_totals[order.symbol] = side_totals.get(order.symbol, 0.0) + reserved
            by_venue[order.venue] = by_venue.get(order.venue, 0.0) + reserved
            key = f"{order.venue}:{order.symbol}"
            by_position[key] = by_position.get(key, 0.0) + reserved

        # Worst-case transient residual per symbol: if every BUY still working
        # on it fills before any SELL, the book is temporarily long the whole
        # BUY side; if the SELLs go first, short the whole SELL side. The worse
        # magnitude is the larger side, NOT their net (which assumes the offset
        # lands) and NOT their sum (which assumes both sides land one-sided at
        # once). Symbols are summed because each can independently go one-sided
        # and ``Okapi.total_unhedged`` aggregates the same way — a sum of
        # per-symbol absolute residuals (P5-18).
        fill_risk = sum(
            max(buy_remaining.get(symbol, 0.0), sell_remaining.get(symbol, 0.0))
            for symbol in set(buy_remaining) | set(sell_remaining)
        )

        return CommittedExposure(
            gross_exposure=gross,
            net_exposure=net,
            venue_exposure=by_venue,
            position_exposure=by_position,
            unhedged_fill_risk=fill_risk,
        )

    def _refresh_risk_utilization(self, portfolio: PortfolioState) -> None:
        """Keep the published risk-utilization snapshot current every tick.

        Before this fix, ``state.risk_utilization`` was set only inside
        ``_risk_check()`` -- called only while evaluating a *new* trade
        intent -- so it described the portfolio as of the last new-trade
        risk evaluation, not the portfolio that exists now. A closed
        position left stale nonzero exposure on the dashboard indefinitely,
        until the next new opportunity happened to trigger another risk
        check (which, with no capital at risk, might never happen again).

        This runs unconditionally, right after ``_measure()`` has marked the
        portfolio, using the exact same canonical calculation
        (``RuneCore.utilization``) ``_risk_check`` uses -- one source of
        truth, called from two places rather than two calculations. That now
        includes committed exposure: both callers pass it, so the dashboard and
        the gates describe the same exposure rather than the dashboard showing
        only what has settled.
        """
        unhedged = self.okapi.total_unhedged(portfolio)
        self.state.risk_utilization = self.rune.core.utilization(
            portfolio,
            unhedged,
            {STRATEGY: self._current_strategy_exposure()},
            self._current_committed_exposure(),
        )

    def _storage_ok(self) -> bool:
        """Whether durable event storage is working.

        Fail-closed by default: if the audit trail is lost, reconciliation
        cannot be reconstructed and no trade taken from here on could be
        verified afterwards, so new trading stops. The recorder is optional
        (a session may run unrecorded on purpose); when there is no recorder
        attached there is nothing to be unhealthy about.
        """
        recorder = self.recorder
        if recorder is None:
            return True
        return recorder.consecutive_failures < self.storage_failure_threshold

    async def _protect(self, market: MarketState, portfolio: PortfolioState) -> None:
        """Reconcile, evaluate the kill switch, and carry out its actions."""
        reconciliation_ok = True
        # Reconcile on the first protected tick and every N thereafter, so
        # MARIN's health is established before anything depends on it.
        if self.marin.last_result is None or self.ticks % self.reconcile_every == 0:
            result = await self.marin.run(self.tick_time)
            reconciliation_ok = result.ok
            for mismatch in result.mismatches:
                self.metrics.inc(M.RECONCILIATION_MISMATCHES, kind=mismatch.kind.value)
        elif self.marin.last_result is not None:
            reconciliation_ok = self.marin.last_result.ok

        # A cross-venue strategy needs at least two usable venues *on the
        # same symbol*. Counting usable venue/symbol pairs would call the
        # platform healthy with one venue entirely dark.
        tradeable_symbols = [
            symbol
            for symbol in self.settings.symbols
            if sum(1 for s in market.states_for(symbol) if s.quality.is_usable) >= 2
        ]
        # TRANSPORT LATENCY, NOT DATA AGE (P5-10).
        #
        # ``EXCESSIVE_LATENCY`` used to be fed ``max(s.age_ms)`` — how long
        # since a venue last said anything — which made it a second, looser
        # copy of the staleness check under a name that promised something
        # else, and left genuine transport latency unmonitored. ``latency_ms``
        # is what the name claims: ``received_ts - exchange_ts``, floored at
        # zero. Data age keeps its own protections: TIDAL's quality
        # classification, RUNE's MARKET_DATA_FRESH gate, and ``market_data_ok``
        # below, none of which change here.
        max_latency = max(
            (float(s.latency_ms or 0.0) for s in market.venues.values()),
            default=0.0,
        )
        await self.okapi.publish_deltas(portfolio)
        # Derived once, here, and passed in. The kill switch's live-breach
        # predicate has to be looking at the same exposure this tick's
        # risk-utilization snapshot and RUNE's own gates are looking at; three
        # branches each re-deriving it is how two safety layers come to
        # disagree about what the platform currently holds.
        unhedged = self.okapi.total_unhedged(portfolio)
        committed = self._current_committed_exposure()
        strategy_exposure = self._current_strategy_exposure()

        fired = await self.kill_switch.evaluate(
            KillSwitchInputs(
                portfolio=portfolio,
                health=self.state.health,
                reconciliation_ok=reconciliation_ok,
                market_data_ok=bool(tradeable_symbols),
                book_corruption=any(
                    book.crossed for book in self.tidal.books.values()
                ),
                max_latency_ms=max_latency,
                storage_ok=self._storage_ok(),
                # The catastrophic-anomaly level. A breach of
                # ``max_unhedged_notional`` itself is caught one layer earlier,
                # by RISK_LIMIT_BREACH reading ``unhedged_notional`` below.
                unexpected_position=abs(unhedged)
                > self.settings.risk.max_unhedged_notional * 3,
                required_components=REQUIRED_COMPONENTS,
                committed_exposure=committed,
                strategy_exposure=strategy_exposure,
                unhedged_notional=unhedged,
            ),
            # An automatic safety decision belongs to the tick that observed
            # the state justifying it, so every KILL_SWITCH_TRIGGERED event
            # this raises carries the same instant as the market snapshot,
            # portfolio and risk decisions beside it. Without that, a replay
            # could order the safety event differently from the run that
            # produced it (P5-16).
            now_ms=self.tick_time,
        )
        self.state.kill_switch = self.kill_switch.state
        self.metrics.set(
            M.KILL_SWITCH_ENGAGED, 1.0 if self.kill_switch.state.engaged else 0.0
        )
        if fired:
            log.error("kill switch fired", extra={"triggers": fired})

        if self.kill_switch.state.cancel_all_requested:
            await self.veska.cancel_all(self.tick_time)
            self.kill_switch.acknowledge_cancel_all()
        if self.kill_switch.state.execution_disabled:
            self.veska.executor.execution_disabled = True
        if self.kill_switch.state.flatten_requested:
            await self._flatten(market)
            self.kill_switch.acknowledge_flatten()

    async def clear_kill_switch(
        self, reason: str = "manual operator clear"
    ) -> KillSwitchState:
        """The one coordinated recovery path from an engaged kill switch.

        WHY THE ORCHESTRATOR OWNS IT
        ============================
        ``RECONCILIATION_MISMATCH`` and ``UNEXPECTED_POSITION`` set
        ``KillSwitchState.execution_disabled``, and ``_protect`` latches that
        onto ``PaperExecutor.execution_disabled``. ``KillSwitch.clear()``
        builds a fresh state where the flag is False — but nothing was
        unlatching the executor, so the switch reported itself cleared while
        every submission kept being rejected for the rest of the process
        (P5-5). Recovery has to undo both halves, and only the component that
        applied the effect can undo it: the kill switch owns safety state and
        trigger evaluation, the orchestrator owns application-side effects.
        Handing ``KillSwitch`` a reference to the executor would invert that.

        DELIBERATELY MANUAL
        ===================
        Nothing calls this automatically — not ``_protect``, not ``_manage``,
        not a heartbeat, not reconciliation. A condition that stopped trading
        should be understood before trading resumes. If the unsafe condition
        still exists, the next protected tick will engage the switch again,
        which is the correct outcome rather than a failure of this method.

        One clock read, at this boundary, threaded into ``clear`` so the state
        reset and its published event share an instant (P5-16).
        """
        now = self.clock.now_ms()
        state = await self.kill_switch.clear(reason, now_ms=now)
        self.state.kill_switch = state
        self.veska.executor.execution_disabled = False
        log.warning(
            "kill switch cleared by operator",
            extra={"reason": reason, "at": now},
        )
        return state

    async def _manage(self, market: MarketState, portfolio: PortfolioState) -> None:
        """Re-evaluate open opportunities and exit those that have decayed."""
        # Hedging is a standing responsibility, not a step inside one trade's
        # lifecycle: an exit that failed to fill leaves exposure behind long
        # after its opportunity is closed, and something has to keep working
        # it down.
        await self._hedge(market, portfolio)
        # Opportunities still waiting on agent responses get another chance
        # before anything else in the lifecycle runs.
        await self._await_pending_agents(market, portfolio)
        for record in self.state.open_opportunities():
            if record.state is StrategyState.EXECUTING:
                await self._advance_execution(record, market)
            elif record.state is StrategyState.HEDGING:
                await self.transition(record, StrategyState.RECONCILING)
            elif record.state is StrategyState.RECONCILING:
                await self.transition(record, StrategyState.MONITORING)
            elif record.state is StrategyState.MONITORING:
                await self._monitor(record, market)
            elif record.state is StrategyState.EXITING:
                await self._advance_exit(record, market)

    async def _seek(self, market: MarketState, portfolio: PortfolioState) -> None:
        """Detect new opportunities and drive them to a decision."""
        for opportunity in self.detector.detect(market, self.tick_time):
            record = self.state.add_opportunity(opportunity, self.tick_time)
            self.metrics.inc(M.OPPORTUNITIES_DETECTED, symbol=opportunity.symbol)
            await self.bus.publish(
                Event(
                    type=EventType.OPPORTUNITY_DETECTED,
                    ts_ms=opportunity.created_at,
                    source=SERVICE,
                    schema_name="Opportunity",
                    correlation_id=opportunity.opportunity_id,
                    payload=opportunity.to_json_dict(),
                )
            )
            self.barrier.expect(
                opportunity.opportunity_id, set(self.settings.consensus.required_agents)
            )
            # Phase 8: record that the question was asked. Beside
            # ``barrier.expect``, never instead of it -- the barrier is still
            # the only thing that decides who answered.
            self.coordination.trace_for_opportunity(
                opportunity.opportunity_id,
                self.tick_time,
                create=True,
                correlation_id=opportunity.opportunity_id,
                strategy=opportunity.strategy,
                symbol=opportunity.symbol,
            )
            self.coordination.register_consensus_request(
                opportunity.opportunity_id,
                self.tick_time,
                purpose=ConsensusPurpose.ENTRY,
                required_agents=set(self.settings.consensus.required_agents),
                symbol=opportunity.symbol,
                strategy=opportunity.strategy,
                deadline_ms=opportunity.created_at
                + self.settings.consensus.agent_response_timeout_ms,
            )
            self.coordination.count_tick(
                opportunities_seen=1,
                opportunities_created=1,
                now_ms=self.tick_time,
            )
            await self.transition(record, StrategyState.AGENTS_EVALUATING)
            # drain() gives local completion only (bus contract clause 4), so
            # it settles in-process agents but proves nothing about remote
            # ones. The barrier is what actually decides whether the required
            # agents have answered.
            await self.bus.drain()
            await self._evaluate_or_defer(record, market, portfolio)

    async def _evaluate_or_defer(
        self, record: OpportunityRecord, market: MarketState, portfolio: PortfolioState
    ) -> None:
        """Decide now if every required agent has answered; otherwise wait.

        Waiting is done by leaving the record in AGENTS_EVALUATING and
        retrying on later ticks — never by blocking inside a tick. Blocking
        would stall the market feed and, under a manual clock, could not make
        progress at all. The opportunity's own expiry bounds the wait.
        """
        opportunity = record.opportunity
        responded = self.barrier.responded(opportunity.opportunity_id)
        required = set(self.settings.consensus.required_agents)

        if required <= responded:
            self._record_barrier_outcome(
                opportunity.opportunity_id,
                required=required,
                responded=responded,
                waited_ms=self.tick_time - opportunity.created_at,
                timed_out=False,
            )
            self.barrier.forget(opportunity.opportunity_id)
            await self._decide(record, market, portfolio)
            return

        now = self.tick_time
        waited = now - opportunity.created_at
        if waited >= self.settings.consensus.agent_response_timeout_ms or not (
            opportunity.is_valid_at(now)
        ):
            # Deadline reached. Decide with what arrived — the consensus engine
            # already treats a missing required agent as incomplete, which is
            # a rejection, not a neutral vote.
            missing = sorted(a.value for a in required - responded)
            log.warning(
                "agent responses incomplete at deadline",
                extra={
                    "opportunity_id": opportunity.opportunity_id,
                    "missing": missing,
                    "waited_ms": waited,
                },
            )
            self.metrics.inc(M.AGENT_RESPONSE_TIMEOUT, agents=",".join(missing) or "none")
            self._record_barrier_outcome(
                opportunity.opportunity_id,
                required=required,
                responded=responded,
                waited_ms=waited,
                timed_out=True,
            )
            self.barrier.forget(opportunity.opportunity_id)
            await self._decide(record, market, portfolio)

    def _record_barrier_outcome(
        self,
        correlation_id: str,
        *,
        required: set[AgentId],
        responded: set[AgentId],
        waited_ms: int,
        timed_out: bool,
    ) -> None:
        """Copy a barrier outcome into the coordination record.

        A metadata call, made just before ``barrier.forget`` discards the only
        copy of who answered. It computes nothing the barrier had not already
        established, and the platform's behaviour is identical with or without
        it — which is the property that makes it safe to add to a decision
        path.
        """
        self.coordination.record_barrier_result(
            correlation_id,
            self.tick_time,
            required=required,
            responded=responded & required,
            missing=required - responded,
            complete=required <= responded,
            timed_out=timed_out,
            waited_ms=max(0, waited_ms),
        )

    async def _await_pending_agents(
        self, market: MarketState, portfolio: PortfolioState
    ) -> None:
        """Retry every opportunity still waiting on its agents."""
        for record in self.state.open_opportunities():
            if record.state is StrategyState.AGENTS_EVALUATING:
                await self._evaluate_or_defer(record, market, portfolio)

    # -- opportunity pipeline ---------------------------------------------

    def _consensus_for(self, record: OpportunityRecord) -> ConsensusResult:
        result, _ = self._consensus_with_opinions(record)
        return result

    def _consensus_with_opinions(
        self, record: OpportunityRecord
    ) -> tuple[ConsensusResult, list[OpinionReference]]:
        """The consensus, and compact references to the opinions behind it.

        The consensus computation is byte-for-byte the one that was here
        before: the same opinions, the same engine call, the same arguments.
        The second return value is a Phase 8 addition built from the very
        opinions the engine was handed, so the record cannot describe a
        different input set than the decision used.
        """
        opportunity = record.opportunity
        opinions = self.state.opinions_for(
            opportunity.opportunity_id,
            opportunity.symbol,
            degraded_grace_ms=self.settings.consensus.degraded_grace_ms,
            now_ms=self.tick_time,
        )
        result = self.consensus.combine(
            symbol=opportunity.symbol,
            strategy=opportunity.strategy,
            opinions=opinions,
            correlation_id=opportunity.opportunity_id,
            now_ms=self.tick_time,
        )
        refs = [
            OpinionReference(
                agent_id=agent_id,
                subject=slot.opinion.correlation_id or opportunity.symbol,
                created_at=slot.opinion.created_at,
                expires_at=slot.opinion.expires_at,
                quality=slot.quality,
                signal=slot.opinion.signal,
                confidence=slot.opinion.confidence,
                abstain=slot.opinion.abstain,
                model_version=slot.opinion.model_version,
            )
            for agent_id, slot in opinions.items()
        ]
        return result, refs

    def _record_consensus(
        self,
        record: OpportunityRecord,
        result: ConsensusResult,
        refs: list[OpinionReference],
        *,
        purpose: ConsensusPurpose,
        allowed: bool | None,
    ) -> None:
        """Mirror a decided consensus into the coordination record.

        ``allowed`` is what ``ConsensusEngine.entry_allowed`` or
        ``continuation_allowed`` returned at the call site above, passed down
        rather than recomputed. Re-deriving it from ``agreement`` and a
        threshold would put a second, untested decision beside the real one.
        """
        self.coordination.record_consensus(
            result,
            self.tick_time,
            purpose=purpose,
            correlation_id=record.opportunity.opportunity_id,
            required_agents=list(self.settings.consensus.required_agents),
            opinion_refs=refs,
            entry_threshold=self.settings.consensus.entry_threshold,
            continuation_threshold=self.settings.consensus.exit_threshold,
            allowed=allowed,
            opportunity_id=record.opportunity.opportunity_id,
        )

    async def _publish_consensus(self, result: ConsensusResult) -> None:
        self.metrics.observe(M.CONSENSUS, result.agreement * 100.0)
        await self.bus.publish(
            Event(
                type=EventType.CONSENSUS_UPDATED,
                ts_ms=result.created_at,
                source=SERVICE,
                schema_name="ConsensusResult",
                correlation_id=result.correlation_id,
                payload=result.to_json_dict(),
            )
        )

    async def _decide(
        self, record: OpportunityRecord, market: MarketState, portfolio: PortfolioState
    ) -> None:
        opportunity = record.opportunity
        now = self.tick_time

        if not opportunity.is_valid_at(now):
            await self._reject(record, "OPPORTUNITY_EXPIRED")
            return

        result, opinion_refs = self._consensus_with_opinions(record)
        await self._publish_consensus(result)
        record.last_agreement = result.agreement

        if not result.complete:
            # ``allowed=None``, not False: the engine's ``entry_allowed`` was
            # never called on this path, and writing down an answer nobody
            # asked for would be the record deciding. Incompleteness is
            # already visible on the record itself.
            self._record_consensus(
                record,
                result,
                opinion_refs,
                purpose=ConsensusPurpose.ENTRY,
                allowed=None,
            )
            await self._reject(record, "CONSENSUS_INCOMPLETE")
            return
        # The engine decides; the result is then recorded. Calling it once and
        # reusing the answer keeps the record and the decision from being two
        # separate evaluations that could differ.
        entry_allowed = self.consensus.entry_allowed(result)
        self._record_consensus(
            record,
            result,
            opinion_refs,
            purpose=ConsensusPurpose.ENTRY,
            allowed=entry_allowed,
        )
        if not entry_allowed:
            await self._reject(record, "CONSENSUS_BELOW_THRESHOLD")
            return

        await self.transition(record, StrategyState.CONSENSUS_REACHED)
        record.entry_agreement = result.agreement

        intent = self._build_intent(record, result, market)
        if intent is None:
            await self._reject(record, "NO_EXECUTABLE_SIZE")
            return
        record.intent = intent
        self.coordination.link_intent(
            opportunity.opportunity_id, intent.intent_id, now
        )
        await self.bus.publish(
            Event(
                type=EventType.TRADE_INTENT,
                ts_ms=intent.created_at,
                source=SERVICE,
                schema_name="TradeIntent",
                correlation_id=intent.correlation_id,
                payload=intent.to_json_dict(),
            )
        )

        await self.transition(record, StrategyState.RISK_CHECK)
        decision = await self._risk_check(intent, record, market, portfolio, result)
        record.decision = decision
        self.coordination.link_risk_decision(
            opportunity.opportunity_id, decision.decision_id, now
        )
        self.coordination.count_tick(risk_evaluations=1, now_ms=now)
        if not decision.approved:
            self.state.record_rejection(decision)
            self.coordination.count_risk_rejection()
            for gate in decision.failed_gates:
                self.metrics.inc(M.RISK_REJECTIONS, gate=gate.name)
            await self._reject(record, "RISK_REJECTED")
            return

        await self.transition(record, StrategyState.AUTHORIZED)

        plan = self.veska.build_plan(intent, decision, market, self.tick_time)
        if plan is None:
            await self._reject(record, "PLANNING_FAILED")
            return
        plan.correlation_id = opportunity.opportunity_id

        self.attributions[opportunity.opportunity_id] = AttributionBuilder(
            trade_ref=plan.plan_id,
            opportunity_id=opportunity.opportunity_id,
            intent_id=intent.intent_id,
            strategy=intent.strategy,
            symbol=intent.symbol,
            created_at=now,
            consensus=result,
            expected_net_edge_bps=intent.expected_net_edge_bps,
            expected_costs_bps=intent.costs.total_bps,
            decision=decision,
        )
        # Gross across every leg, not the per-leg notional RUNE authorised:
        # this feeds MAX_STRATEGY_EXPOSURE, which is a gross budget, and the
        # incoming intent is projected the same way (P5-2).
        self.working_notional[opportunity.opportunity_id] = (
            decision.approved_notional * len(intent.legs)
        )

        await self.transition(record, StrategyState.EXECUTING)
        report = await self.veska.execute(plan, self.tick_time)
        record.order_ids = [order.client_order_id for order in report.orders]
        # Identifiers only. Phase 6's registry still owns the plan and its
        # orders; a copy here could drift from the truth it copied.
        self.coordination.link_execution_plan(
            opportunity.opportunity_id,
            plan.plan_id,
            self.tick_time,
            order_ids=list(record.order_ids),
        )
        self.coordination.link_trade_ref(
            opportunity.opportunity_id, plan.plan_id, self.tick_time
        )
        self.coordination.count_tick(execution_plans=1, now_ms=self.tick_time)
        await self.bus.drain()

    def _build_intent(
        self, record: OpportunityRecord, result: ConsensusResult, market: MarketState
    ) -> TradeIntent | None:
        """Size the trade from ZEPHR's curve and price it net of all costs."""
        opportunity = record.opportunity
        curve = self.zephr.curve_for(opportunity.opportunity_id)
        if curve is None or curve.best is None:
            return None
        best = curve.best

        # One implementation of the cost arithmetic, owned by the curve that
        # produced the quotes. Re-deriving the sum here meant two copies of
        # one calculation had to be kept in step by hand: the moment they
        # drifted, ZEPHR's ``net_edge_bps`` and this intent's
        # ``expected_net_edge_bps`` would describe the same trade with
        # different numbers -- and RUNE's MIN_EXPECTED_EDGE gate reads the
        # second one.
        costs = best.cost_breakdown()
        now = self.tick_time
        return TradeIntent(
            created_at=now,
            # The oldest of this intent's own legs (TIDAL-H4) — not the
            # market-wide newest timestamp, which could describe an unrelated
            # symbol's freshest venue and would let a stale leg pass the risk
            # gate's data-age check unnoticed.
            source_data_timestamp=market.source_data_timestamp_for(
                (leg.venue, leg.symbol) for leg in opportunity.legs
            ),
            correlation_id=opportunity.opportunity_id,
            opportunity_id=opportunity.opportunity_id,
            strategy=opportunity.strategy,
            symbol=opportunity.symbol,
            legs=list(opportunity.legs),
            notional=best.notional,
            gross_edge_bps=opportunity.gross_edge_bps,
            costs=costs,
            # Identical to ``best.net_edge_bps`` by construction, because the
            # breakdown above is the curve's own arithmetic rather than a
            # second copy of it.
            expected_net_edge_bps=costs.net_from(opportunity.gross_edge_bps),
            consensus_score=result.score,
            consensus_agreement=result.agreement,
            max_slippage_bps=max(1.0, opportunity.gross_edge_bps * 0.5),
            deadline_ms=now + self.settings.risk.max_data_age_ms,
            urgency=min(1.0, 0.5 + result.agreement / 2),
            # Risk-increasing activity. Metadata only in Phase 6: nothing sizes
            # or routes from it yet (docs/phase6-veska-framework.md).
            execution_role=ExecutionRole.ENTRY,
        )

    async def _risk_check(
        self,
        intent: TradeIntent,
        record: OpportunityRecord,
        market: MarketState,
        portfolio: PortfolioState,
        result: ConsensusResult,
    ) -> RiskDecision:
        curve = self.zephr.curve_for(record.opportunity.opportunity_id)
        await self.bus.publish(
            Event(
                type=EventType.RISK_EVALUATION_REQUEST,
                ts_ms=self.tick_time,
                source=SERVICE,
                schema_name="TradeIntent",
                correlation_id=intent.correlation_id,
                payload=intent.to_json_dict(),
            )
        )
        ctx = RiskContext(
            portfolio=portfolio,
            kill_switch=self.state.kill_switch,
            health=self.state.health,
            consensus_threshold=self.settings.consensus.entry_threshold,
            consensus_complete=result.complete,
            required_components=REQUIRED_COMPONENTS,
            open_orders=len(self.veska.outstanding_orders()),
            error_rate=self._error_rate(),
            unhedged_notional=self.okapi.total_unhedged(portfolio),
            strategy_exposure=self._current_strategy_exposure(),
            # What earlier authorisations in THIS tick have already committed.
            # The portfolio below has not moved for them -- nothing has filled
            # -- so without this every trade in a ``_seek`` pass would be
            # measured against the same empty book (P5-1).
            committed_exposure=self._current_committed_exposure(),
            # The unhedged budget an entry must leave unspent, so the exit and
            # hedge that unwind its temporary leg have room to work before the
            # emergency ceiling. OKAPI's own tolerance is the honest source:
            # it is the delta the operator already accepts before hedging, so
            # entry sizing and recovery are configured by one number rather
            # than by two that could drift apart (P5-18).
            hedge_tolerance_notional=self.settings.hedge_tolerance_notional,
            max_economical_notional=curve.max_economical_notional if curve else None,
            hedge_available=self.okapi.hedge_available(intent.symbol, market),
        )
        self.state.risk_utilization = self.rune.core.utilization(
            portfolio,
            ctx.unhedged_notional,
            {STRATEGY: ctx.strategy_exposure},
            ctx.committed_exposure,
        )
        return await self.rune.evaluate(intent, ctx, self.tick_time)

    def _error_rate(self) -> float:
        """Share of the bus's most recent delivery attempts that raised.

        Read straight off the bus, which records each outcome as it dispatches
        (``EventBus`` contract clause 9). It used to be recomputed here from
        ``Subscription.delivered``/``errors``, which are lifetime totals — so
        after a healthy first hour a process failing *every* current delivery
        still reported a rate near zero, and ``MAX_ERROR_RATE`` never fired
        (P5-8). Those counters remain, as per-handler diagnostics; they are the
        wrong shape for a health measurement and are no longer used as one.
        """
        return self.bus.recent_error_rate

    # -- open position management -----------------------------------------

    async def _advance_execution(self, record: OpportunityRecord, market: MarketState) -> None:
        """Move on once the plan's orders have all reached a terminal state."""
        orders = [self.state.orders.get(oid) for oid in record.order_ids]
        outstanding = [o for o in orders if o is not None and o.is_outstanding]
        if outstanding:
            deadline = record.intent.deadline_ms if record.intent else None
            if deadline is not None and self.tick_time > deadline:
                for order in outstanding:
                    await self.veska.cancel(order.client_order_id, self.tick_time)
            return
        filled = sum(o.filled_quantity for o in orders if o is not None)
        if filled <= 0:
            # Nothing traded: there is no position to hedge or monitor.
            await self.transition(record, StrategyState.CLOSED)
            await self._finish_attribution(record)
            return
        await self.transition(record, StrategyState.HEDGING)

    def _monitor_opportunity(
        self, opportunity: Opportunity, market: MarketState
    ) -> Opportunity | None:
        """The SAME trade, re-priced at the current touch.

        LIVE EXIT ECONOMICS (Phase 3+4)
        ===============================
        ``_monitor`` republishes an opportunity every tick so the agents keep
        voting after entry. Republishing the *original* record made that
        vote incoherent: agents were handed the reference prices and the
        gross edge observed at detection, so the question they answered was
        "would this trade have been good back then?" — a question whose
        answer cannot change, no matter what the market does afterwards.

        Concretely, ``gross_edge_bps`` is what ZEPHR divides its modelled
        costs against and what TIDAL's volatility penalty is scaled by. Frozen
        at the entry value, an opportunity whose dislocation has completely
        closed still presented ZEPHR with the entry edge, and ZEPHR still
        reported that execution economics survived. The exit path exists to
        notice decay, and it was being shown a snapshot in which decay was
        impossible.

        What is re-priced, and what is not:

        * **Not the venues.** The position exists on the venues it was opened
          on, and those are the venues that have to be unwound. Re-running
          detection here would retarget the trade to whichever pair is
          cheapest now and produce an edge for a position nobody holds. The
          buy leg's venue and the sell leg's venue are carried through
          untouched, and so are their sides.
        * **Not the identity.** ``opportunity_id``, ``correlation_id``,
          ``created_at`` and ``expires_at`` all belong to the original: the
          attribution trail, the response barrier and the tick-time invariant
          (an opportunity is stamped at the tick it was BORN in and never
          restamped) all depend on them.
        * **The prices.** ``current_buy_touch`` is the best ask available NOW
          on the original buy venue — what closing would cost — and
          ``current_sell_touch`` is the best bid available NOW on the original
          sell venue. Touch to touch, the same reference frame the detector
          used, so the monitored edge is directly comparable to the entry
          edge rather than a differently-defined number.

        The bps denominator is the midpoint of those two current touches, not
        the consolidated reference price: it keeps the whole computation a
        function of the two venues actually holding the position. The
        difference is a fraction of a basis point either way — a denominator
        only sets the scale — but "reads nothing but its own legs" is a
        property worth being able to state without qualification.

        Everything comes from ``market``, the tick's frozen snapshot. Nothing
        here re-runs detection, disturbs the detector's in-flight registry, or
        reads the clock.

        Returns ``None`` when either leg has no usable state or no touch on
        that side. That is fail-closed by design: an unpriceable leg means the
        current economics are unknown, and the caller treats unknown the same
        way it treats decayed — it exits — because a position whose value
        cannot be observed is not a position to keep holding.
        """
        buy_leg = next((leg for leg in opportunity.legs if leg.side is Side.BUY), None)
        sell_leg = next((leg for leg in opportunity.legs if leg.side is Side.SELL), None)
        if buy_leg is None or sell_leg is None:
            return None

        buy_state = market.venue_state(buy_leg.venue, buy_leg.symbol)
        sell_state = market.venue_state(sell_leg.venue, sell_leg.symbol)
        if (
            buy_state is None
            or sell_state is None
            or not buy_state.quality.is_usable
            or not sell_state.quality.is_usable
        ):
            return None

        current_buy_touch = buy_state.metrics.best_ask
        current_sell_touch = sell_state.metrics.best_bid
        if current_buy_touch is None or current_sell_touch is None:
            return None

        reference = (current_buy_touch + current_sell_touch) / 2
        edge = safe_bps(current_sell_touch - current_buy_touch, reference)
        if edge is None:
            return None

        legs = [
            leg.model_copy(
                update={
                    "reference_price": (
                        current_buy_touch if leg.side is Side.BUY else current_sell_touch
                    )
                }
            )
            for leg in opportunity.legs
        ]
        return opportunity.model_copy(
            update={
                "legs": legs,
                "gross_edge_bps": edge,
                # Re-derived for THIS snapshot, and still the oldest of the
                # opportunity's own legs rather than the market-wide newest
                # (TIDAL-H4). Carrying the detection-time value forward would
                # tell every downstream age gate that a tick-old re-evaluation
                # was as fresh as the moment of entry.
                "source_data_timestamp": market.source_data_timestamp_for(
                    (leg.venue, leg.symbol) for leg in opportunity.legs
                ),
                "reason_codes": [*opportunity.reason_codes, "MONITOR_REPRICED"],
                "detail": {
                    **opportunity.detail,
                    "buy_price": current_buy_touch,
                    "sell_price": current_sell_touch,
                    "reference_price": reference,
                    # Kept alongside so the decay is legible in the record
                    # rather than having to be reconstructed from two events.
                    "entry_gross_edge_bps": opportunity.gross_edge_bps,
                },
            }
        )

    async def _monitor(self, record: OpportunityRecord, market: MarketState) -> None:
        """Continuous re-evaluation: agents keep voting after entry.

        The agents are asked about the position's CURRENT economics — see
        :meth:`_monitor_opportunity`. ``record.opportunity`` is never mutated:
        it is the historical record of what was detected and what the entry
        decision was taken against, and attribution reads it after the trade
        closes.
        """
        opportunity = record.opportunity
        monitored = self._monitor_opportunity(opportunity, market)
        if monitored is None:
            # A leg cannot be priced on this tick, so there is no honest
            # question to put to the agents. Unwind rather than hold a
            # position whose live economics are unobservable. This is the
            # same outcome the old code reached by a longer route -- an
            # unusable leg made the agents return None, consensus was
            # incomplete, and an incomplete result is not a continuation --
            # but it now says so directly instead of depending on every
            # required agent independently failing closed.
            log.warning(
                "monitored opportunity cannot be re-priced; exiting",
                extra={
                    "opportunity_id": opportunity.opportunity_id,
                    "symbol": opportunity.symbol,
                },
            )
            for order_id in record.order_ids:
                await self.veska.cancel(order_id, self.tick_time)
            await self.transition(record, StrategyState.EXITING)
            await self._submit_exit(record, market)
            return

        self.barrier.expect(
            opportunity.opportunity_id, set(self.settings.consensus.required_agents)
        )
        # Phase 8: the same question, asked for a different reason. Tagging it
        # CONTINUATION is what lets a reader tell an entry consensus from a
        # continuation one; the thresholds either is measured against are
        # unchanged.
        self.coordination.register_consensus_request(
            opportunity.opportunity_id,
            self.tick_time,
            purpose=ConsensusPurpose.CONTINUATION,
            required_agents=set(self.settings.consensus.required_agents),
            symbol=opportunity.symbol,
            strategy=opportunity.strategy,
        )
        await self.bus.publish(
            Event(
                type=EventType.OPPORTUNITY_DETECTED,
                ts_ms=self.tick_time,
                source=SERVICE,
                schema_name="Opportunity",
                correlation_id=opportunity.opportunity_id,
                payload=monitored.to_json_dict(),
            )
        )
        await self.bus.drain()
        # Continuous re-evaluation uses the same completion tracking; a
        # position is not exited merely because a remote agent was slow.
        required = set(self.settings.consensus.required_agents)
        self._record_barrier_outcome(
            opportunity.opportunity_id,
            required=required,
            responded=self.barrier.responded(opportunity.opportunity_id),
            waited_ms=0,
            timed_out=False,
        )
        self.barrier.forget(opportunity.opportunity_id)
        result, opinion_refs = self._consensus_with_opinions(record)
        await self._publish_consensus(result)
        record.last_agreement = result.agreement

        continuation_allowed = self.consensus.continuation_allowed(result)
        self._record_consensus(
            record,
            result,
            opinion_refs,
            purpose=ConsensusPurpose.CONTINUATION,
            allowed=continuation_allowed,
        )
        if continuation_allowed:
            return
        # Below the continuation threshold: cancel what is working and unwind.
        for order_id in record.order_ids:
            await self.veska.cancel(order_id, self.tick_time)
        await self.transition(record, StrategyState.EXITING)
        await self._submit_exit(record, market)

    async def _submit_exit(self, record: OpportunityRecord, market: MarketState) -> None:
        """Close the position this opportunity opened, leg by leg."""
        portfolio = self.veska.executor.account.snapshot()
        legs: list[OpportunityLeg] = []
        notional = 0.0
        for leg in record.opportunity.legs:
            position = portfolio.positions.get(f"{leg.venue}:{leg.symbol}")
            if position is None or position.is_flat:
                continue
            state = market.venue_state(leg.venue, leg.symbol)
            if state is None or state.metrics.mid is None:
                continue
            # Close the quantity actually held. Sizing an exit from a notional
            # estimate is how a "closed" trade leaves a residual position
            # behind, which then eats the net-exposure limit for every trade
            # that follows.
            quantity = abs(position.quantity)
            legs.append(
                OpportunityLeg(
                    venue=leg.venue,
                    symbol=leg.symbol,
                    side=Side.SELL if position.quantity > 0 else Side.BUY,
                    reference_price=state.metrics.mid,
                    quantity=quantity,
                )
            )
            notional += quantity * state.metrics.mid

        if not legs or notional <= 0:
            await self.transition(record, StrategyState.CLOSED)
            await self._finish_attribution(record)
            return

        now = self.tick_time
        record.exit_attempts += 1
        # Each retry crosses further: getting out matters more than the last
        # basis point of the exit price.
        slippage_budget = self.settings.risk.min_expected_edge_bps * 5 * record.exit_attempts

        exit_intent = TradeIntent(
            created_at=now,
            # Oldest of the legs actually being closed (TIDAL-H4).
            source_data_timestamp=market.source_data_timestamp_for(
                (leg.venue, leg.symbol) for leg in legs
            ),
            correlation_id=record.opportunity.opportunity_id,
            opportunity_id=record.opportunity.opportunity_id,
            strategy=record.opportunity.strategy,
            symbol=record.opportunity.symbol,
            legs=legs,
            notional=notional,
            gross_edge_bps=0.0,
            costs=CostBreakdown(),
            expected_net_edge_bps=0.0,
            consensus_score=record.last_agreement or 0.0,
            consensus_agreement=record.last_agreement or 0.0,
            max_slippage_bps=slippage_budget,
            deadline_ms=now + self.settings.risk.max_data_age_ms,
            urgency=1.0,
            is_exit=True,
            # Risk-reducing: this closes a position the platform already holds.
            # ``_flatten`` reaches this same path under an engaged kill switch,
            # where FLATTEN would be the more precise word; distinguishing the
            # two would mean threading a flag through _submit_exit, which is a
            # behavioural change this construction pass does not make. Recorded
            # as a deferred extension in the framework doc.
            execution_role=ExecutionRole.EXIT,
        )
        # Exits are not gated on edge or consensus: refusing to close a
        # position because the trade is no longer attractive is how a platform
        # ends up unable to get out. RUNE still records the decision.
        decision = record.decision
        if decision is None:
            return
        exit_decision = decision.model_copy(
            update={
                "intent_id": exit_intent.intent_id,
                "approved_notional": notional,
                "requested_notional": notional,
                "reason_codes": ["EXIT_AUTHORISED"],
            }
        )
        plan = self.veska.build_plan(exit_intent, exit_decision, market, self.tick_time)
        if plan is None:
            return
        plan.correlation_id = record.opportunity.opportunity_id
        report = await self.veska.execute(plan, self.tick_time)
        record.order_ids = [order.client_order_id for order in report.orders]
        await self.bus.drain()

    async def _hedge(self, market: MarketState, portfolio: PortfolioState) -> None:
        """Work OKAPI's hedge intents.

        Hedges are exposure-*reducing*, so they are not gated on edge or
        consensus — refusing to neutralise unintended delta because the trade
        is unattractive would be backwards.  They are still bounded by the
        per-order notional limit and still refuse to run with execution
        disabled, and every one is recorded.
        """
        if self.veska.executor.execution_disabled:
            return
        intents = self.okapi.build_hedges(portfolio, market, self.tick_time)
        if not intents:
            return
        now = self.tick_time
        for hedge in intents:
            if self._hedge_in_flight(hedge.symbol):
                continue
            state = market.venue_state(hedge.venue, hedge.symbol)
            if state is None or not state.metrics.mid:
                continue
            notional = min(hedge.notional, self.settings.risk.max_order_notional)
            quantity = notional / state.metrics.mid
            if quantity <= 0:
                continue
            leg = OpportunityLeg(
                venue=hedge.venue,
                symbol=hedge.symbol,
                side=hedge.side,
                reference_price=state.metrics.mid,
                quantity=quantity,
            )
            hedge_intent = TradeIntent(
                created_at=now,
                # The one leg the hedge actually trades (TIDAL-H4).
                source_data_timestamp=market.source_data_timestamp_for(
                    [(leg.venue, leg.symbol)]
                ),
                correlation_id=hedge.hedge_id,
                opportunity_id=hedge.hedge_id,
                strategy=STRATEGY,
                symbol=hedge.symbol,
                legs=[leg],
                notional=notional,
                gross_edge_bps=0.0,
                costs=CostBreakdown(hedge_bps=self.settings.zephr.hedge_cost_bps),
                expected_net_edge_bps=0.0,
                consensus_score=0.0,
                consensus_agreement=0.0,
                max_slippage_bps=self.settings.risk.min_expected_edge_bps * 5,
                deadline_ms=now + self.settings.risk.max_data_age_ms,
                urgency=hedge.urgency,
                is_exit=True,
                # Risk-reducing, but not an exit: a hedge neutralises a
                # residual delta rather than closing a trade. ``is_exit`` stays
                # True because every existing reader uses it to mean "not an
                # entry", which is still correct.
                execution_role=ExecutionRole.HEDGE,
            )
            decision = RiskDecision(
                created_at=now,
                correlation_id=hedge.hedge_id,
                decision_id=f"hedge-{hedge.hedge_id}",
                intent_id=hedge_intent.intent_id,
                strategy=STRATEGY,
                symbol=hedge.symbol,
                verdict=RiskVerdict.APPROVED,
                approved_notional=notional,
                requested_notional=notional,
                reason_codes=["HEDGE_EXPOSURE_REDUCING"],
            )
            plan = self.veska.build_plan(hedge_intent, decision, market, self.tick_time)
            if plan is None:
                continue
            plan.correlation_id = hedge.hedge_id
            await self.okapi.publish_hedge(hedge)
            report = await self.veska.execute(plan, self.tick_time)
            self.working_hedges[hedge.symbol] = [
                order.client_order_id for order in report.orders
            ]
            # Phase 9: link the residual to what it became. Identifiers only --
            # Phase 6's registry still owns the plan and its orders, and
            # ``working_hedges`` above remains the authority on whether a hedge
            # is in flight. Nothing below is read by any branch in this method.
            self._link_hedge(hedge, hedge_intent, plan, report, now)
            await self.bus.drain()

    def _link_hedge(self, hedge, hedge_intent, plan, report, now: Millis) -> None:
        """Record a hedge's trade intent, plan and orders against its request.

        Every value is copied from objects the code above already built. The
        registry lookup goes through ``HedgeIntent.hedge_id``, which
        ``build_hedges`` minted and which is the correlation id on the trade
        intent, the plan and every order — so a downstream record can always
        find its way back.

        Returns nothing, and a missing registry record is a silent no-op: a
        bookkeeping call that raised because a mirror was absent would let the
        record break the thing it records.
        """
        record = self.okapi.hedge_registry.for_intent(hedge.hedge_id)
        if record is None:
            return
        registry = self.okapi.hedge_registry
        registry.attach_trade_intent(record.hedge_id, hedge_intent.intent_id, now)
        registry.attach_execution_plan(record.hedge_id, plan.plan_id, now)
        registry.attach_orders(
            record.hedge_id,
            [order.client_order_id for order in report.orders],
            now,
        )
        # SUBMITTING is what just happened -- the plan was handed to VESKA.
        # Where it goes next is Phase 6's to establish; ``derive_hedge_status``
        # offers a reading of it, and no caller applies that automatically.
        registry.set_status(record.hedge_id, HedgeRequestStatus.SUBMITTING, now)

    def _hedge_in_flight(self, symbol: str) -> bool:
        """Whether a hedge for this symbol is still working."""
        order_ids = self.working_hedges.get(symbol)
        if not order_ids:
            return False
        outstanding = [
            oid
            for oid in order_ids
            if (order := self.state.orders.get(oid)) is not None
            and order.is_outstanding
        ]
        if outstanding:
            self.working_hedges[symbol] = outstanding
            return True
        del self.working_hedges[symbol]
        return False

    def _residual_legs(self, record: OpportunityRecord) -> list[str]:
        """Legs of this opportunity that still carry a position."""
        portfolio = self.veska.executor.account.snapshot()
        residual: list[str] = []
        for leg in record.opportunity.legs:
            key = f"{leg.venue}:{leg.symbol}"
            position = portfolio.positions.get(key)
            if position is not None and not position.is_flat:
                residual.append(key)
        return residual

    async def _advance_exit(self, record: OpportunityRecord, market: MarketState) -> None:
        orders = [self.state.orders.get(oid) for oid in record.order_ids]
        if any(o is not None and o.is_outstanding for o in orders):
            return

        residual = self._residual_legs(record)
        if residual and record.exit_attempts < self.max_exit_attempts:
            # An exit leg can expire unfilled — an IOC whose limit the market
            # never reached. Closing the record here would orphan the
            # position, so retry with a wider slippage budget.
            log.warning(
                "exit incomplete, retrying",
                extra={
                    "opportunity_id": record.opportunity.opportunity_id,
                    "residual": residual,
                    "attempt": record.exit_attempts,
                },
            )
            await self._submit_exit(record, market)
            return

        if residual:
            # Out of retries. The position does not disappear because we gave
            # up on it: it is handed to OKAPI's standing hedge loop, and the
            # hand-off is recorded rather than swallowed.
            await self.bus.publish(
                Event(
                    type=EventType.SYSTEM_EVENT,
                    ts_ms=self.tick_time,
                    source=SERVICE,
                    schema_name="SystemEvent",
                    correlation_id=record.opportunity.opportunity_id,
                    payload=SystemEvent(
                        created_at=self.tick_time,
                        correlation_id=record.opportunity.opportunity_id,
                        kind="EXIT_INCOMPLETE",
                        severity=Severity.WARNING,
                        component=SERVICE,
                        message=(
                            f"exit left {len(residual)} leg(s) open after "
                            f"{record.exit_attempts} attempts; handed to OKAPI"
                        ),
                        detail={"residual": ",".join(residual)},
                    ).to_json_dict(),
                )
            )
        await self.transition(record, StrategyState.CLOSED)
        await self._finish_attribution(record)

    async def _flatten(self, market: MarketState) -> None:
        """Kill-switch flatten: unwind every open opportunity immediately.

        EXECUTING IS IN THE LIST DELIBERATELY
        ====================================
        It used to be the omission (P5-6). EXECUTING is precisely the state
        whose entry orders may still be live or partly filled, so skipping it
        left the trade the flatten most needed to reach untouched — neither
        cancelled by the old action sets nor unwound here.
        ``EXECUTING -> EXITING`` was already a legal transition; nothing about
        the state machine changed to allow this.

        CANCEL_ALL has already run by the time this is reached — every action
        set that flattens also cancels, and ``_protect`` applies cancel before
        flatten — so an EXECUTING record's resting entry orders are
        cancel-pending before its position is unwound.

        ``_submit_exit`` sizes from the position that ACTUALLY exists, never
        from the notional RUNE authorised: whatever portion of an entry has
        filled is in the account, and whatever has not is being cancelled. An
        order that wins the race and fills after the cancel leaves a residual,
        which the ordinary EXITING logic then notices and works down through
        its retries and OKAPI's standing hedge loop.
        """
        for record in self.state.open_opportunities():
            if record.state in (
                StrategyState.EXECUTING,
                StrategyState.MONITORING,
                StrategyState.RECONCILING,
                StrategyState.HEDGING,
            ):
                await self.transition(record, StrategyState.EXITING)
                await self._submit_exit(record, market)

    async def _finish_attribution(self, record: OpportunityRecord) -> None:
        builder = self.attributions.pop(record.opportunity.opportunity_id, None)
        if builder is None:
            return
        # Realized P&L was already accumulated fill-by-fill, isolated to this
        # opportunity's own correlation_id, in ``_on_fill`` -- not read here
        # from a position's lifetime-cumulative counter, which would inherit
        # every prior opportunity's contribution on the same venue:symbol.
        builder.closed_at = self.tick_time
        attribution = builder.build()
        record.realized_pnl = attribution.realized_pnl or 0.0
        self.scorecard.record(attribution)
        await self.bus.publish(
            Event(
                type=EventType.TRADE_ATTRIBUTION,
                ts_ms=attribution.created_at,
                source=SERVICE,
                schema_name="TradeAttribution",
                correlation_id=record.opportunity.opportunity_id,
                payload=attribution.to_json_dict(),
            )
        )

    # -- coordination observability (Phase 8) ------------------------------
    #
    # Every method below is a read. None of them is called from the tick path,
    # and nothing in the tick path calls anything that calls them. A future
    # dashboard, API or operator surface can answer "what is this platform
    # doing?" through these instead of reaching into orchestrator internals.

    def current_tick_record(self) -> OrchestrationTickRecord | None:
        """The tick currently open, or ``None`` between ticks."""
        return self.coordination.current_tick()

    def recent_ticks(self, limit: int = 20) -> list[OrchestrationTickRecord]:
        """The most recently opened tick records, newest last."""
        return self.coordination.recent_ticks(limit)

    def consensus_requests(self) -> list[ConsensusRequestRecord]:
        """Every consensus request the registry still holds."""
        return list(self.coordination.consensus_requests.values())

    def consensus_evaluations(self) -> list[ConsensusEvaluationRecord]:
        """Every recorded consensus evaluation, entry and continuation."""
        return list(self.coordination.consensus_evaluations.values())

    def trace_for_opportunity(self, opportunity_id: str) -> DecisionTrace | None:
        """The causal spine of one opportunity, in identifiers.

        Reading never creates a trace. An opportunity the platform never
        worked has no trace, and inventing an empty one to avoid returning
        ``None`` would make "we have no record of this" indistinguishable from
        "this happened and produced nothing".
        """
        return self.coordination.trace_for_opportunity(opportunity_id)

    def agent_directory_snapshot(
        self, now_ms: Millis | None = None
    ) -> AgentDirectorySnapshot:
        """The agent directory, with configuration's required list mirrored in."""
        return self.agent_directory.snapshot(
            self.tick_time if now_ms is None else now_ms,
            required_agents=list(self.settings.consensus.required_agents),
        )

    def barrier_snapshots(self, now_ms: Millis | None = None) -> list[BarrierSnapshot]:
        """Every response barrier still waiting, observed without disturbing it."""
        if self.barrier is None:
            return []
        return self.barrier.all_pending_snapshots(
            self.tick_time if now_ms is None else now_ms
        )

    def coordination_readiness(
        self, now_ms: Millis | None = None
    ) -> CoordinationReadiness:
        """Whether the coordination layer says the platform is in a fit state.

        **This gates nothing.** Warm-up still decides when trading may begin,
        the kill switch still decides when it must stop, and
        ``ConsensusResult.complete`` still decides whether a consensus counts.
        Replacing any of those with this would swap a tested decision for an
        untested one.

        ``ready`` is False whenever anything is unestablished — absence of
        evidence is not readiness.
        """
        now = self.tick_time if now_ms is None else now_ms
        required = list(self.settings.consensus.required_agents)
        agents_healthy, unhealthy = self.health.all_healthy(
            [agent.value for agent in required], now
        )
        unknown_orders = len(self.veska.unknown_orders())
        kill_switch_clear = self.state.kill_switch.trading_allowed
        reconciliation_ok = (
            self.marin.last_result is not None and self.marin.last_result.ok
        )

        reasons: list[str] = []
        if not self.warmed_up:
            reasons.append("WARMUP_INCOMPLETE")
        if not required:
            reasons.append("NO_REQUIRED_AGENTS_CONFIGURED")
        if not agents_healthy:
            reasons.extend(f"AGENT_UNHEALTHY:{name}" for name in unhealthy)
        if not kill_switch_clear:
            reasons.append("KILL_SWITCH_ENGAGED")
        if unknown_orders:
            reasons.append(f"UNKNOWN_ORDERS:{unknown_orders}")
        if not reconciliation_ok:
            reasons.append("RECONCILIATION_NOT_CLEAN")

        return CoordinationReadiness(
            ready=not reasons,
            created_at=now,
            warmup_complete=self.warmed_up,
            warmup_ticks=self.warmup_ticks,
            required_agents_known=bool(required),
            required_agents_healthy=agents_healthy,
            consensus_available=True,
            risk_available=True,
            execution_available=not self.state.kill_switch.execution_disabled,
            reconciliation_available=self.marin.last_result is not None,
            kill_switch_clear=kill_switch_clear,
            open_unknown_orders=unknown_orders,
            components=[],
            reason_codes=reasons,
        )

    def coordination_snapshot(
        self, now_ms: Millis | None = None
    ) -> OrchestrationSnapshot:
        """One serializable view of the whole coordination layer.

        Compact references and counts rather than embedded snapshots: a view
        that carried every order and every fill would be sized by session
        history rather than by what is currently happening.
        """
        now = self.tick_time if now_ms is None else now_ms
        current = self.coordination.current_tick()
        barriers = self.barrier_snapshots(now)
        execution = self.veska.metrics()
        discrepancies = self.marin.open_discrepancies()

        return OrchestrationSnapshot(
            created_at=now,
            ticks_completed=self.coordination.ticks_completed,
            current_tick_id=current.tick_id if current else None,
            current_tick_number=current.tick_number if current else 0,
            current_phase=(
                current.current_phase if current else OrchestrationPhase.IDLE
            ),
            warmed_up=self.warmed_up,
            warmup_ticks=self.warmup_ticks,
            agent_directory=self.agent_directory_snapshot(now),
            barrier_outstanding=self.barrier.outstanding if self.barrier else 0,
            barriers=barriers,
            pending_consensus_requests=self.coordination.pending_requests(),
            open_opportunities=[
                self._workflow_summary(record)
                for record in self.state.open_opportunities()
            ],
            kill_switch_engaged=self.state.kill_switch.engaged,
            kill_switch_triggers=list(self.state.kill_switch.triggered_by),
            execution_open_orders=len(self.veska.open_orders()),
            execution_outstanding_orders=execution.orders_outstanding,
            execution_unknown_orders=execution.orders_unknown,
            execution_active_plans=len(self.veska.active_plans()),
            reconciliation_ok=(
                None
                if self.marin.last_result is None
                else self.marin.last_result.ok
            ),
            reconciliation_open_critical=sum(
                1
                for discrepancy in discrepancies
                if discrepancy.severity is Severity.CRITICAL
            ),
            # Phase 9: three counts off a registry the orchestrator already
            # holds. No hedge record is embedded, and no LUMEN state is read —
            # the orchestrator has no reference to LUMEN, and giving it one to
            # populate a display field would couple the fast loop to the
            # intelligence layer for no operational gain.
            hedges_active=len(self.okapi.active_hedges()),
            hedges_outstanding=len(self.okapi.outstanding_hedges()),
            hedges_unknown=len(self.okapi.unknown_hedges()),
            coordination_metrics=self.coordination.metrics(
                late_agent_responses=self.barrier.late_responses
                if self.barrier
                else 0
            ),
            readiness=self.coordination_readiness(now),
        )

    def _workflow_summary(
        self, record: OpportunityRecord
    ) -> OpportunityWorkflowSummary:
        """Flatten one opportunity's position in the pipeline.

        Every field is copied from something that stays authoritative:
        ``OpportunityRecord`` for state and economics, the trace for the
        identifiers linking the phases together.
        """
        opportunity = record.opportunity
        trace = self.coordination.trace_for_opportunity(opportunity.opportunity_id)
        return OpportunityWorkflowSummary(
            opportunity_id=opportunity.opportunity_id,
            strategy=opportunity.strategy,
            symbol=opportunity.symbol,
            strategy_state=record.state,
            created_at=opportunity.created_at,
            updated_at=record.updated_at,
            trace_id=trace.trace_id if trace else None,
            intent_id=record.intent.intent_id if record.intent else None,
            risk_decision_id=(
                record.decision.decision_id if record.decision else None
            ),
            execution_plan_ids=list(trace.execution_plan_ids) if trace else [],
            order_ids=list(record.order_ids),
            entry_agreement=record.entry_agreement,
            last_agreement=record.last_agreement,
            filled_notional=record.filled_notional,
            fees=record.fees,
            realized_pnl=record.realized_pnl,
            rejected_reason=record.rejected_reason,
        )

    # -- publishing --------------------------------------------------------

    async def _publish_state(self, portfolio: PortfolioState) -> None:
        await self.bus.publish(
            Event(
                type=EventType.PORTFOLIO_STATE,
                ts_ms=portfolio.created_at,
                source=SERVICE,
                schema_name="PortfolioState",
                payload=portfolio.to_json_dict(),
            )
        )
        self.veska.heartbeat()
        self.rune.heartbeat()

    def _heartbeat(self) -> None:
        ok, bad = self.health.all_healthy(
            [c for c in REQUIRED_COMPONENTS if c != SERVICE], self.tick_time
        )
        status = HealthStatus.HEALTHY if ok else HealthStatus.DEGRADED
        if self.state.kill_switch.engaged:
            status = HealthStatus.DEGRADED
        self.health.heartbeat(
            SERVICE,
            status=status,
            queue_depth=self.bus.queue_depth,
            version=VERSION,
            detail=("unhealthy: " + ",".join(bad)) if bad else "",
        )

    # -- driver ------------------------------------------------------------

    async def run_forever(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception as exc:
                log.exception("orchestrator tick failed")
                self.state.record_error(str(exc))
                self.health.record_error(SERVICE, str(exc))
                await self.bus.publish(
                    Event(
                        type=EventType.ERROR,
                        ts_ms=self.clock.now_ms(),
                        source=SERVICE,
                        schema_name="SystemEvent",
                        payload=SystemEvent(
                            created_at=self.clock.now_ms(),
                            kind="TICK_FAILED",
                            severity=Severity.CRITICAL,
                            component=SERVICE,
                            message=str(exc),
                        ).to_json_dict(),
                    )
                )
            await self.clock.sleep(self.settings.tick_interval_s)


__all__ = ["SERVICE", "VERSION", "AgentId", "IllegalStrategyTransition", "Orchestrator"]
