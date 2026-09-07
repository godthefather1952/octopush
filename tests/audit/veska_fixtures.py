"""Deterministic builders for the Phase 6 VESKA / paper-execution audit.

Nothing here is production code, and nothing here may be wired into
production. Every builder constructs the smallest object that isolates one
variable, because an audit that changes two things at once cannot attribute
what it observes.

THREE CLOCKS, DELIBERATELY SEPARATE
===================================
The audit's central instrument is the ability to drive execution at one
logical instant while the wired clock reads another. ``ManualClock`` is what
the OMS reads; ``now_ms`` is what the executor is told. Production couples
them by convention (the orchestrator passes ``tick_time``), and several
hypotheses here exist precisely to ask what happens when they diverge — which
is the ordinary case under a live feed, where the clock advances while a tick
is still running.

WHY BOOKS ARE BUILT BY HAND
===========================
``tests/conftest`` derives depth buckets from the book it is handed, which is
convenient and makes it impossible to construct the shapes this audit needs: a
one-level book with exactly the size that tests a partial fill, a book whose
second level is far enough away to breach a slippage limit, a book that is
crossed for a POST_ONLY order at arrival. Every metric here is set explicitly
so the audit controls exactly one variable at a time.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from core.bus import EventBus, InMemoryEventBus
from core.clock import Clock, ManualClock
from core.config import Settings, load_settings
from core.events import Event, EventType
from core.health import HealthRegistry
from core.models.common import (
    DataQuality,
    Liquidity,
    Millis,
    OrderType,
    Side,
    TimeInForce,
)
from core.models.execution import FillEvent, OrderStatus, PaperOrder
from core.models.market import (
    BookMetrics,
    ConsolidatedView,
    MarketState,
    OrderBookSnapshot,
    PriceLevel,
    VenueMarketState,
)
from core.models.opportunity import (
    CostBreakdown,
    ExecutionPlan,
    OpportunityLeg,
    PlannedOrder,
    TradeIntent,
)
from core.models.risk import RiskDecision, RiskVerdict
from execution.oms import OrderManager
from execution.paper import FillSimulator, PaperAccount, PaperExecutor
from execution.veska import Veska

#: The two venues the audit trades between. Both exist in the simulated venue
#: set the shipped configuration builds, so ``Settings.venue()`` resolves them.
VENUE_A = "VENUE_A"
VENUE_B = "VENUE_B"
SYMBOL = "BTC-USD"

#: An instant far from zero, so an off-by-one against a default never passes by
#: accident.
T0: Millis = 1_700_000_000_000


# ======================================================================
# configuration
# ======================================================================


def audit_settings(**overrides: Any) -> Settings:
    """Settings for the audit, built from the shipped simulated configuration.

    **No threshold, seed or simulator default is changed here.**
    ``load_settings`` is called exactly as production calls it, and overrides
    are applied only where a hypothesis needs one variable pinned — and every
    such call site says which variable and why. An audit that quietly widened a
    limit would be measuring a platform nobody ships.
    """
    return load_settings(**overrides)


def deterministic_settings(**overrides: Any) -> Settings:
    """Settings with randomness removed from the fill simulator.

    ``liquidity_vanish_probability`` and ``maker_fill_probability`` make the
    simulator stochastic by design, which is right for production and useless
    for an invariant test: a test that fails one run in twenty is not evidence
    of anything.

    These overrides pin the *dice*, never the economics. Depth, fees, latency,
    slippage budgets, partial-fill caps and queue modelling are untouched, so
    every price and quantity this produces is the number the shipped simulator
    would produce on a run where the dice fell this way.
    """
    execution = {
        "liquidity_vanish_probability": 0.0,
        "maker_fill_probability": 1.0,
    }
    execution.update(overrides.pop("execution", {}))
    return load_settings(execution=execution, **overrides)


# ======================================================================
# market construction
# ======================================================================


def price_levels(*pairs: tuple[float, float]) -> list[PriceLevel]:
    """``(price, size)`` pairs, in the order given. No sorting is applied.

    Deliberately unsorted: a book whose levels are out of order is a shape the
    audit must be able to construct, because nothing in the execution path
    re-sorts what it is handed.
    """
    return [PriceLevel(price=price, size=size) for price, size in pairs]


def venue_state(
    *,
    venue: str = VENUE_A,
    symbol: str = SYMBOL,
    bids: list[PriceLevel] | None = None,
    asks: list[PriceLevel] | None = None,
    as_of: Millis = T0,
    exchange_ts: Millis | None = None,
    quality: DataQuality = DataQuality.FRESH,
    buy_volume: float = 0.0,
    sell_volume: float = 0.0,
    short_vol_bps: float = 0.0,
) -> VenueMarketState:
    """One venue's book, with every metric stated rather than derived."""
    bids = bids if bids is not None else price_levels((100.0, 10.0))
    asks = asks if asks is not None else price_levels((100.1, 10.0))
    best_bid = bids[0].price if bids else None
    best_ask = asks[0].price if asks else None
    mid = (
        (best_bid + best_ask) / 2
        if best_bid is not None and best_ask is not None
        else None
    )
    spread_bps = (
        (best_ask - best_bid) / mid * 10_000
        if best_bid is not None and best_ask is not None and mid
        else None
    )
    return VenueMarketState(
        venue=venue,
        symbol=symbol,
        as_of=as_of,
        exchange_ts=exchange_ts if exchange_ts is not None else as_of,
        last_update_ts=as_of,
        quality=quality,
        book=OrderBookSnapshot(
            venue=venue,
            symbol=symbol,
            exchange_ts=exchange_ts if exchange_ts is not None else as_of,
            received_ts=as_of,
            sequence=1,
            bids=bids,
            asks=asks,
            is_checkpoint=True,
        ),
        metrics=BookMetrics(
            best_bid=best_bid,
            best_ask=best_ask,
            mid=mid,
            spread_bps=spread_bps,
            buy_volume=buy_volume,
            sell_volume=sell_volume,
            short_vol_bps=short_vol_bps,
        ),
    )


def market_state(
    *states: VenueMarketState,
    created_at: Millis = T0,
    source_data_timestamp: Millis | None = None,
) -> MarketState:
    """Assemble a ``MarketState`` from explicit venue states.

    ``source_data_timestamp`` is set explicitly rather than derived, because
    H18 asks precisely whether a fill inherits a market-wide timestamp that
    does not belong to its own venue and symbol.
    """
    venues = {f"{s.venue}:{s.symbol}": s for s in states}
    consolidated: dict[str, ConsolidatedView] = {}
    for symbol in {s.symbol for s in states}:
        matching = [s for s in states if s.symbol == symbol]
        mids = [s.metrics.mid for s in matching if s.metrics.mid is not None]
        if not mids:
            continue
        reference = sum(mids) / len(mids)
        consolidated[symbol] = ConsolidatedView(
            symbol=symbol,
            as_of=created_at,
            reference_price=reference,
            max_deviation_bps=(
                (max(mids) - min(mids)) / reference * 10_000 if reference else 0.0
            ),
        )
    return MarketState(
        created_at=created_at,
        source_data_timestamp=(
            source_data_timestamp
            if source_data_timestamp is not None
            else created_at
        ),
        venues=venues,
        consolidated=consolidated,
    )


def two_venue_market(
    *,
    created_at: Millis = T0,
    a_bid: float = 100.0,
    a_ask: float = 100.1,
    b_bid: float = 100.4,
    b_ask: float = 100.5,
    size: float = 100.0,
    quality: DataQuality = DataQuality.FRESH,
) -> MarketState:
    """The shape the shipped cross-venue strategy trades: A cheap, B dear."""
    return market_state(
        venue_state(
            venue=VENUE_A,
            bids=price_levels((a_bid, size)),
            asks=price_levels((a_ask, size)),
            as_of=created_at,
            quality=quality,
        ),
        venue_state(
            venue=VENUE_B,
            bids=price_levels((b_bid, size)),
            asks=price_levels((b_ask, size)),
            as_of=created_at,
            quality=quality,
        ),
        created_at=created_at,
    )


# ======================================================================
# plans and orders
# ======================================================================


def planned_order(
    *,
    venue: str = VENUE_A,
    symbol: str = SYMBOL,
    side: Side = Side.BUY,
    quantity: float = 1.0,
    order_type: OrderType = OrderType.LIMIT,
    time_in_force: TimeInForce = TimeInForce.IOC,
    limit_price: float | None = 101.0,
    expected_price: float = 100.0,
    expected_fee_bps: float = 6.0,
    ttl_ms: int = 5_000,
    client_order_id: str | None = None,
) -> PlannedOrder:
    """One planned order, every field explicit."""
    kwargs: dict[str, Any] = {
        "venue": venue,
        "symbol": symbol,
        "side": side,
        "quantity": quantity,
        "order_type": order_type,
        "time_in_force": time_in_force,
        "limit_price": limit_price,
        "expected_price": expected_price,
        "expected_fee_bps": expected_fee_bps,
        "ttl_ms": ttl_ms,
    }
    if client_order_id is not None:
        kwargs["client_order_id"] = client_order_id
    return PlannedOrder(**kwargs)


def execution_plan(
    *orders: PlannedOrder,
    created_at: Millis = T0,
    deadline_ms: Millis | None = None,
    max_slippage_bps: float = 50.0,
    notional: float = 100.0,
    requested_notional: float | None = None,
    approved_notional: float | None = None,
    correlation_id: str = "opp-audit",
    intent_id: str = "intent-audit",
    plan_id: str | None = None,
    strategy: str = "cross_venue",
    symbol: str = SYMBOL,
    source_data_timestamp: Millis | None = None,
) -> ExecutionPlan:
    """A plan built by hand, bypassing ``build_plan``.

    Used where a hypothesis needs a shape the router cannot emit — an FOK
    instruction, a MARKET order, a duplicate client order id. That is not a
    contrivance: ``Veska.execute`` accepts any plan it is handed, including one
    rebuilt from a recording, so the shapes it will accept are exactly what the
    audit must probe.
    """
    kwargs: dict[str, Any] = {
        "created_at": created_at,
        "source_data_timestamp": (
            source_data_timestamp if source_data_timestamp is not None else created_at
        ),
        "correlation_id": correlation_id,
        "intent_id": intent_id,
        "strategy": strategy,
        "symbol": symbol,
        "orders": list(orders),
        "deadline_ms": deadline_ms if deadline_ms is not None else created_at + 10_000,
        "max_slippage_bps": max_slippage_bps,
        "notional": notional,
        "requested_notional": (
            requested_notional if requested_notional is not None else notional
        ),
        "approved_notional": (
            approved_notional if approved_notional is not None else notional
        ),
    }
    if plan_id is not None:
        kwargs["plan_id"] = plan_id
    return ExecutionPlan(**kwargs)


def trade_intent(
    *legs: OpportunityLeg,
    created_at: Millis = T0,
    notional: float = 1_000.0,
    max_slippage_bps: float = 50.0,
    urgency: float = 0.9,
    deadline_ms: Millis | None = None,
    symbol: str = SYMBOL,
    strategy: str = "cross_venue",
    correlation_id: str = "opp-audit",
) -> TradeIntent:
    """An intent shaped like the orchestrator's, with sizing left to RUNE."""
    return TradeIntent(
        created_at=created_at,
        source_data_timestamp=created_at,
        correlation_id=correlation_id,
        opportunity_id=correlation_id,
        strategy=strategy,
        symbol=symbol,
        legs=list(legs),
        notional=notional,
        gross_edge_bps=40.0,
        costs=CostBreakdown(fees_bps=6.0),
        expected_net_edge_bps=34.0,
        consensus_score=0.8,
        consensus_agreement=0.8,
        max_slippage_bps=max_slippage_bps,
        deadline_ms=deadline_ms if deadline_ms is not None else created_at + 10_000,
        urgency=urgency,
    )


def risk_decision(
    *,
    intent_id: str = "intent-audit",
    approved_notional: float = 1_000.0,
    requested_notional: float | None = None,
    created_at: Millis = T0,
    symbol: str = SYMBOL,
    strategy: str = "cross_venue",
    correlation_id: str = "opp-audit",
) -> RiskDecision:
    """An APPROVED decision, so planning is exercised rather than refusal."""
    return RiskDecision(
        created_at=created_at,
        correlation_id=correlation_id,
        decision_id=f"risk-{intent_id}",
        intent_id=intent_id,
        strategy=strategy,
        symbol=symbol,
        verdict=RiskVerdict.APPROVED,
        approved_notional=approved_notional,
        requested_notional=(
            requested_notional if requested_notional is not None else approved_notional
        ),
    )


def leg(
    *,
    venue: str = VENUE_A,
    symbol: str = SYMBOL,
    side: Side = Side.BUY,
    reference_price: float = 100.0,
    quantity: float | None = None,
) -> OpportunityLeg:
    """One opportunity leg. ``quantity=None`` is the shipped entry shape."""
    return OpportunityLeg(
        venue=venue,
        symbol=symbol,
        side=side,
        reference_price=reference_price,
        quantity=quantity,
    )


# ======================================================================
# the execution stack
# ======================================================================


@dataclass
class ExecutionHarness:
    """A wired paper-execution stack with every collaborator reachable.

    Deliberately not the whole platform: no orchestrator, no agents, no
    detector. The Phase 6 boundary is VESKA down to the OMS, and building more
    than that would let an unrelated component's behaviour explain a result.
    """

    settings: Settings
    clock: ManualClock
    bus: EventBus
    oms: OrderManager
    account: PaperAccount
    simulator: FillSimulator
    executor: PaperExecutor
    veska: Veska
    health: HealthRegistry
    published: list[Event] = field(default_factory=list)

    # -- time ---------------------------------------------------------

    def set_clock(self, ms: Millis) -> None:
        """Move the wired clock without touching logical execution time.

        The instrument for H1: production reads this clock inside the OMS for
        every ``created_at``, every history entry and every ``expires_at``,
        while the executor stamps ``submitted_at`` and fill times from the
        ``now_ms`` its caller supplies. Driving the two apart is how the audit
        asks whether one economic action can carry two timelines.
        """
        self.clock.set(ms)

    # -- market -------------------------------------------------------

    def update_market(self, market: MarketState) -> None:
        self.executor.update_market(market)

    # -- events -------------------------------------------------------

    def events_of(self, *types: EventType) -> list[Event]:
        wanted = set(types)
        return [e for e in self.published if e.type in wanted]

    def orders_of(self, plan_id: str) -> list[PaperOrder]:
        return self.oms.orders_for_plan(plan_id)


def build_harness(
    *,
    settings: Settings | None = None,
    clock_ms: Millis = T0,
    seed: int | None = None,
    bus: EventBus | None = None,
) -> ExecutionHarness:
    """Wire the paper-execution stack the way ``build_platform`` wires it.

    The construction order and the collaborators are the composition root's,
    so what the audit measures is the stack production runs. The only
    difference is that the clock is manual and every published event is
    captured, neither of which changes an execution decision.
    """
    settings = settings or deterministic_settings()
    clock = ManualClock(start_ms=clock_ms)
    event_bus = bus if bus is not None else InMemoryEventBus(
        raise_on_handler_error=True
    )
    health = HealthRegistry(clock=clock)
    oms = OrderManager(clock=clock)
    account = PaperAccount(
        clock=clock, initial_balance=settings.paper_initial_balance
    )
    simulator = FillSimulator(settings.execution, seed=seed)
    executor = PaperExecutor(
        bus=event_bus,
        clock=clock,
        settings=settings,
        oms=oms,
        account=account,
        simulator=simulator,
    )
    veska = Veska(event_bus, clock, settings, health, executor)

    harness = ExecutionHarness(
        settings=settings,
        clock=clock,
        bus=event_bus,
        oms=oms,
        account=account,
        simulator=simulator,
        executor=executor,
        veska=veska,
        health=health,
    )

    async def _capture(event: Event) -> None:
        harness.published.append(event)

    event_bus.subscribe(_capture, types=None, name="audit-capture")
    return harness


# ======================================================================
# failure injection — audit-only, never wired into production
# ======================================================================


class ExplodingBus:
    """A bus that raises on the Nth publish, and counts everything.

    Duck-typed rather than a subclass of ``InMemoryEventBus``, and for a
    reason worth stating: that bus *enqueues* rather than dispatching, so a
    raising subscriber never propagates out of ``publish``. Modelling a
    mid-submission publication failure needs the raise to come from ``publish``
    itself, which is exactly what a real transport failure would do.

    Only the surface ``PaperExecutor`` and ``Veska`` actually touch is
    implemented. Anything else is an error the audit wants to see, not silently
    absorb.
    """

    def __init__(self, *, fail_on_publish: int | None = None) -> None:
        self.fail_on_publish = fail_on_publish
        self.publishes = 0
        self.published: list[Event] = []
        self.queue_depth = 0

    async def publish(self, event: Event) -> None:
        self.publishes += 1
        if self.fail_on_publish is not None and self.publishes >= self.fail_on_publish:
            raise RuntimeError(
                f"audit-injected transport failure on publish #{self.publishes}"
            )
        self.published.append(event)

    def subscribe(self, handler, types=None, name=None):
        return None

    def add_middleware(self, middleware) -> None:
        return None

    async def drain(self) -> None:
        return None

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    def recent_error_rate(self) -> float:
        return 0.0


class RecordingClock(Clock):
    """A clock that counts reads, so the audit can prove where time comes from.

    ``PaperExecutor`` documents that it never reads a clock. The OMS makes no
    such claim and reads one on every create, transition and fill. Counting
    reads separates the two without inferring anything from timestamps.
    """

    def __init__(self, start_ms: Millis = T0) -> None:
        self._now = start_ms
        self.reads = 0

    def now_ms(self) -> Millis:
        self.reads += 1
        return self._now

    def set(self, ms: Millis) -> None:
        self._now = ms

    async def sleep(self, seconds: float) -> None:
        self._now += int(seconds * 1000)


def fill_for(
    order: PaperOrder,
    *,
    quantity: float,
    price: float,
    now_ms: Millis,
    fee: float = 0.0,
    liquidity: Liquidity = Liquidity.TAKER,
    slippage_bps: float = 0.0,
    fill_id: str | None = None,
    source_data_timestamp: Millis | None = None,
) -> FillEvent:
    """A fill constructed directly, for duplicate and overfill probes."""
    kwargs: dict[str, Any] = {
        "created_at": now_ms,
        "source_data_timestamp": source_data_timestamp,
        "correlation_id": order.correlation_id,
        "client_order_id": order.client_order_id,
        "venue": order.venue,
        "symbol": order.symbol,
        "side": order.side,
        "quantity": quantity,
        "price": price,
        "fee": fee,
        "liquidity": liquidity,
        "slippage_bps": slippage_bps,
        "strategy": order.strategy,
    }
    if fill_id is not None:
        kwargs["fill_id"] = fill_id
    return FillEvent(**kwargs)


def statuses(orders: list[PaperOrder]) -> list[OrderStatus]:
    return [o.status for o in orders]


__all__ = [
    "SYMBOL",
    "T0",
    "VENUE_A",
    "VENUE_B",
    "ExecutionHarness",
    "ExplodingBus",
    "RecordingClock",
    "audit_settings",
    "build_harness",
    "deterministic_settings",
    "execution_plan",
    "fill_for",
    "leg",
    "market_state",
    "planned_order",
    "price_levels",
    "risk_decision",
    "statuses",
    "trade_intent",
    "two_venue_market",
    "venue_state",
]
