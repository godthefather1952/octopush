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

from agents.marin import Marin
from agents.noro import Noro
from agents.okapi import Okapi
from agents.rune import RiskContext, Rune
from agents.tidal import Tidal
from agents.zephr import Zephr
from core.bus import EventBus
from core.bus.barrier import ResponseBarrier
from core.clock import Clock
from core.config import Settings
from core.events import Event, EventType
from core.health import HealthRegistry
from core.models.agent import AgentOpinion, ConsensusResult
from core.models.common import AgentId, Millis, Side
from core.models.execution import FillEvent, OrderStatus
from core.models.market import MarketState
from core.models.opportunity import (
    STRATEGY_TRANSITIONS,
    CostBreakdown,
    OpportunityLeg,
    StrategyState,
    TradeIntent,
)
from core.models.ops import HealthStatus, Severity, SystemEvent
from core.models.portfolio import PortfolioState
from core.models.risk import RiskDecision, RiskVerdict
from core.state import OpportunityRecord, SystemState
from execution.veska import Veska
from monitoring import metrics as M
from monitoring.attribution import AttributionBuilder, Scorecard
from monitoring.metrics import MetricsRegistry
from risk.kill_switch import KillSwitch, KillSwitchInputs
from strategies.consensus import ConsensusEngine
from strategies.cross_venue import REQUIRED_COMPONENTS, STRATEGY, CrossVenueDetector

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
    recorder: object | None = None
    #: Consecutive failed flushes before storage is considered down. One
    #: transient write error should not halt the platform; a sustained
    #: inability to persist should.
    storage_failure_threshold: int = 3
    attributions: dict[str, AttributionBuilder] = field(default_factory=dict)
    #: Opportunity id -> notional currently working, for strategy exposure.
    working_notional: dict[str, float] = field(default_factory=dict)
    #: Symbol -> hedge orders still in flight. OKAPI measures delta from the
    #: portfolio, which only moves on fills, so without this a hedge would be
    #: re-submitted on every tick until the first one filled — and the symbol
    #: would end up hedged several times over.
    working_hedges: dict[str, list[str]] = field(default_factory=dict)

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
        record.updated_at = self.clock.now_ms()
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
                    },
                ).to_json_dict(),
            )
        )
        if target in (StrategyState.CLOSED, StrategyState.REJECTED):
            self.detector.release(record.opportunity.symbol, record.opportunity.opportunity_id)
            self.working_notional.pop(record.opportunity.opportunity_id, None)
            self.zephr.forget(record.opportunity.opportunity_id)

    async def _reject(self, record: OpportunityRecord, reason: str) -> None:
        record.rejected_reason = reason
        self.metrics.inc(M.OPPORTUNITIES_REJECTED, stage=reason)
        await self.transition(record, StrategyState.REJECTED)

    # -- the tick ----------------------------------------------------------

    async def tick(self) -> None:
        self.ticks += 1
        now = self.clock.now_ms()

        market = await self._observe()
        await self._settle(now)
        portfolio = self._measure(market)

        if not self.warmed_up:
            self.warmup_ticks += 1
            if self.marin.last_result is None:
                # Establish the reconciliation baseline (and MARIN's health)
                # against an empty account before any trading is possible.
                await self.marin.run()
            self.state.health = self.health.snapshot()
            ok, bad = self.health.all_healthy(
                [c for c in REQUIRED_COMPONENTS if c != SERVICE]
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

        await self._protect(market, portfolio)
        await self._manage(market, portfolio)
        if self.state.kill_switch.trading_allowed:
            await self._seek(market, portfolio)
        await self._publish_state(portfolio)
        self._prune()
        self._heartbeat()

    def _recorder_heartbeat(self) -> None:
        """Surface persistence health alongside every other component."""
        recorder = self.recorder
        if recorder is None:
            return
        failures = recorder.consecutive_failures
        if failures >= self.storage_failure_threshold:
            status, detail = HealthStatus.OFFLINE, (
                f"{failures} consecutive flush failures; "
                f"{recorder.events_lost} events unpersisted"
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
        self.veska.executor.account.mark(marks, self.clock.now_ms())
        portfolio = self.veska.executor.account.snapshot()
        self.state.portfolio = portfolio
        # Execution and risk heartbeat here, before anything reads health:
        # the kill switch must judge the health of this tick, not the last.
        self.veska.heartbeat()
        self.rune.heartbeat()
        self.marin.heartbeat()
        self._recorder_heartbeat()
        self.state.health = self.health.snapshot()

        self.metrics.set(M.NET_PNL, portfolio.net_pnl)
        self.metrics.set(M.GROSS_PNL, portfolio.gross_pnl)
        self.metrics.set(M.FEES_PAID, portfolio.fees_paid)
        self.metrics.set(M.DRAWDOWN, portfolio.drawdown)
        for name, component in (self.state.health.components or {}).items():
            self.metrics.set(
                M.AGENT_UP, 1.0 if component.status is HealthStatus.HEALTHY else 0.0, agent=name
            )
        return portfolio

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
            result = await self.marin.run()
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
        max_age = max(
            (s.age_ms or 0 for s in market.venues.values()),
            default=0,
        )
        await self.okapi.publish_deltas(portfolio)
        unhedged = self.okapi.total_unhedged(portfolio)

        fired = await self.kill_switch.evaluate(
            KillSwitchInputs(
                portfolio=portfolio,
                health=self.state.health,
                reconciliation_ok=reconciliation_ok,
                market_data_ok=bool(tradeable_symbols),
                book_corruption=any(
                    book.crossed for book in self.tidal.books.values()
                ),
                max_latency_ms=float(max_age),
                storage_ok=self._storage_ok(),
                unexpected_position=abs(unhedged)
                > self.settings.risk.max_unhedged_notional * 3,
                required_components=REQUIRED_COMPONENTS,
            )
        )
        self.state.kill_switch = self.kill_switch.state
        self.metrics.set(
            M.KILL_SWITCH_ENGAGED, 1.0 if self.kill_switch.state.engaged else 0.0
        )
        if fired:
            log.error("kill switch fired", extra={"triggers": fired})

        if self.kill_switch.state.cancel_all_requested:
            await self.veska.cancel_all()
            self.kill_switch.acknowledge_cancel_all()
        if self.kill_switch.state.execution_disabled:
            self.veska.executor.execution_disabled = True
        if self.kill_switch.state.flatten_requested:
            await self._flatten(market)
            self.kill_switch.acknowledge_flatten()

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
        for opportunity in self.detector.detect(market):
            record = self.state.add_opportunity(opportunity)
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
            self.barrier.forget(opportunity.opportunity_id)
            await self._decide(record, market, portfolio)
            return

        now = self.clock.now_ms()
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
            self.barrier.forget(opportunity.opportunity_id)
            await self._decide(record, market, portfolio)

    async def _await_pending_agents(
        self, market: MarketState, portfolio: PortfolioState
    ) -> None:
        """Retry every opportunity still waiting on its agents."""
        for record in self.state.open_opportunities():
            if record.state is StrategyState.AGENTS_EVALUATING:
                await self._evaluate_or_defer(record, market, portfolio)

    # -- opportunity pipeline ---------------------------------------------

    def _consensus_for(self, record: OpportunityRecord) -> ConsensusResult:
        opportunity = record.opportunity
        opinions = self.state.opinions_for(
            opportunity.opportunity_id,
            opportunity.symbol,
            degraded_grace_ms=self.settings.consensus.degraded_grace_ms,
        )
        return self.consensus.combine(
            symbol=opportunity.symbol,
            strategy=opportunity.strategy,
            opinions=opinions,
            correlation_id=opportunity.opportunity_id,
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
        now = self.clock.now_ms()

        if not opportunity.is_valid_at(now):
            await self._reject(record, "OPPORTUNITY_EXPIRED")
            return

        result = self._consensus_for(record)
        await self._publish_consensus(result)
        record.last_agreement = result.agreement

        if not result.complete:
            await self._reject(record, "CONSENSUS_INCOMPLETE")
            return
        if not self.consensus.entry_allowed(result):
            await self._reject(record, "CONSENSUS_BELOW_THRESHOLD")
            return

        await self.transition(record, StrategyState.CONSENSUS_REACHED)
        record.entry_agreement = result.agreement

        intent = self._build_intent(record, result, market)
        if intent is None:
            await self._reject(record, "NO_EXECUTABLE_SIZE")
            return
        record.intent = intent
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
        if not decision.approved:
            self.state.record_rejection(decision)
            for gate in decision.failed_gates:
                self.metrics.inc(M.RISK_REJECTIONS, gate=gate.name)
            await self._reject(record, "RISK_REJECTED")
            return

        await self.transition(record, StrategyState.AUTHORIZED)

        plan = self.veska.build_plan(intent, decision, market)
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
        self.working_notional[opportunity.opportunity_id] = decision.approved_notional

        await self.transition(record, StrategyState.EXECUTING)
        report = await self.veska.execute(plan)
        record.order_ids = [order.client_order_id for order in report.orders]
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

        fees = sum(leg.fee_bps for leg in best.legs)
        spread = sum(leg.spread_bps for leg in best.legs)
        slippage = sum(leg.slippage_bps + leg.impact_bps for leg in best.legs)
        latency = sum(leg.latency_bps for leg in best.legs)
        costs = CostBreakdown(
            fees_bps=fees,
            spread_bps=spread,
            slippage_bps=slippage,
            latency_bps=latency,
            hedge_bps=self.settings.zephr.hedge_cost_bps,
        )
        now = self.clock.now_ms()
        return TradeIntent(
            created_at=now,
            source_data_timestamp=market.source_data_timestamp,
            correlation_id=opportunity.opportunity_id,
            opportunity_id=opportunity.opportunity_id,
            strategy=opportunity.strategy,
            symbol=opportunity.symbol,
            legs=list(opportunity.legs),
            notional=best.notional,
            gross_edge_bps=opportunity.gross_edge_bps,
            costs=costs,
            expected_net_edge_bps=costs.net_from(opportunity.gross_edge_bps),
            consensus_score=result.score,
            consensus_agreement=result.agreement,
            max_slippage_bps=max(1.0, opportunity.gross_edge_bps * 0.5),
            deadline_ms=now + self.settings.risk.max_data_age_ms,
            urgency=min(1.0, 0.5 + result.agreement / 2),
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
                ts_ms=self.clock.now_ms(),
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
            open_orders=len(self.veska.open_orders()),
            error_rate=self._error_rate(),
            unhedged_notional=self.okapi.total_unhedged(portfolio),
            strategy_exposure=sum(self.working_notional.values()),
            max_economical_notional=curve.max_economical_notional if curve else None,
            hedge_available=self.okapi.hedge_available(intent.symbol, market),
        )
        self.state.risk_utilization = self.rune.core.utilization(
            portfolio, ctx, {STRATEGY: sum(self.working_notional.values())}
        )
        return await self.rune.evaluate(intent, ctx)

    def _error_rate(self) -> float:
        """Rolling share of bus deliveries that raised."""
        subs = getattr(self.bus, "subscriptions", [])
        delivered = sum(s.delivered for s in subs)
        errors = sum(s.errors for s in subs)
        total = delivered + errors
        return errors / total if total else 0.0

    # -- open position management -----------------------------------------

    async def _advance_execution(self, record: OpportunityRecord, market: MarketState) -> None:
        """Move on once the plan's orders have all reached a terminal state."""
        orders = [self.state.orders.get(oid) for oid in record.order_ids]
        live = [o for o in orders if o is not None and o.is_live]
        if live:
            deadline = record.intent.deadline_ms if record.intent else None
            if deadline is not None and self.clock.now_ms() > deadline:
                for order in live:
                    await self.veska.cancel(order.client_order_id)
            return
        filled = sum(o.filled_quantity for o in orders if o is not None)
        if filled <= 0:
            # Nothing traded: there is no position to hedge or monitor.
            await self.transition(record, StrategyState.CLOSED)
            await self._finish_attribution(record)
            return
        await self.transition(record, StrategyState.HEDGING)

    async def _monitor(self, record: OpportunityRecord, market: MarketState) -> None:
        """Continuous re-evaluation: agents keep voting after entry."""
        opportunity = record.opportunity
        self.barrier.expect(
            opportunity.opportunity_id, set(self.settings.consensus.required_agents)
        )
        await self.bus.publish(
            Event(
                type=EventType.OPPORTUNITY_DETECTED,
                ts_ms=self.clock.now_ms(),
                source=SERVICE,
                schema_name="Opportunity",
                correlation_id=opportunity.opportunity_id,
                payload=opportunity.to_json_dict(),
            )
        )
        await self.bus.drain()
        # Continuous re-evaluation uses the same completion tracking; a
        # position is not exited merely because a remote agent was slow.
        self.barrier.forget(opportunity.opportunity_id)
        result = self._consensus_for(record)
        await self._publish_consensus(result)
        record.last_agreement = result.agreement

        if self.consensus.continuation_allowed(result):
            return
        # Below the continuation threshold: cancel what is working and unwind.
        for order_id in record.order_ids:
            await self.veska.cancel(order_id)
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

        now = self.clock.now_ms()
        record.exit_attempts += 1
        # Each retry crosses further: getting out matters more than the last
        # basis point of the exit price.
        slippage_budget = self.settings.risk.min_expected_edge_bps * 5 * record.exit_attempts

        exit_intent = TradeIntent(
            created_at=now,
            source_data_timestamp=market.source_data_timestamp,
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
        plan = self.veska.build_plan(exit_intent, exit_decision, market)
        if plan is None:
            return
        plan.correlation_id = record.opportunity.opportunity_id
        report = await self.veska.execute(plan)
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
        intents = self.okapi.build_hedges(portfolio, market)
        if not intents:
            return
        now = self.clock.now_ms()
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
                source_data_timestamp=market.source_data_timestamp,
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
            plan = self.veska.build_plan(hedge_intent, decision, market)
            if plan is None:
                continue
            plan.correlation_id = hedge.hedge_id
            await self.okapi.publish_hedge(hedge)
            report = await self.veska.execute(plan)
            self.working_hedges[hedge.symbol] = [
                order.client_order_id for order in report.orders
            ]
            await self.bus.drain()

    def _hedge_in_flight(self, symbol: str) -> bool:
        """Whether a hedge for this symbol is still working."""
        order_ids = self.working_hedges.get(symbol)
        if not order_ids:
            return False
        live = [
            oid
            for oid in order_ids
            if (order := self.state.orders.get(oid)) is not None and order.is_live
        ]
        if live:
            self.working_hedges[symbol] = live
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
        if any(o is not None and o.is_live for o in orders):
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
                    ts_ms=self.clock.now_ms(),
                    source=SERVICE,
                    schema_name="SystemEvent",
                    correlation_id=record.opportunity.opportunity_id,
                    payload=SystemEvent(
                        created_at=self.clock.now_ms(),
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
        """Kill-switch flatten: unwind every open opportunity immediately."""
        for record in self.state.open_opportunities():
            if record.state in (
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
        portfolio = self.veska.executor.account.snapshot()
        realized = 0.0
        for leg in record.opportunity.legs:
            position = portfolio.positions.get(f"{leg.venue}:{leg.symbol}")
            if position is not None:
                realized += position.realized_pnl
        builder.realized_pnl = realized - record.fees
        builder.closed_at = self.clock.now_ms()
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
            [c for c in REQUIRED_COMPONENTS if c != SERVICE]
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
