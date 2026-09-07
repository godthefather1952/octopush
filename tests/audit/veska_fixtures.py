"""Shared builders for the Phase 6 VESKA / paper-execution audit.

Audit-only. Nothing here is production code and nothing here may be wired into
production.

Everything is constructed explicitly rather than by driving a running platform.
The audit's job is to vary exactly one execution dimension at a time — one
order, one book, one logical instant — and observe what the execution boundary
does. A scenario built by stepping the synthetic market would couple every
answer to whatever the market happened to do that tick, and a hypothesis about
(say) IOC lifetime would become a claim about the generator.

The builders deliberately mirror ``tests/unit/test_execution.py``'s shapes so
an audit result can be read against the existing unit suite without translating
between two sets of fixtures. They are duplicated rather than imported: the
unit suite is a validated baseline this pass must not disturb, and an audit
that reached into it would make the two move together.

TWO CLOCKS ON PURPOSE
=====================
``rig`` takes a separate ``oms_now`` so a test can put the OMS's clock
somewhere other than the logical time passed to ``submit``/``poll``/``cancel``.
That is not an artificial condition: the executor is documented to take its
instant from its caller precisely because a live feed can advance the clock
between a tick's snapshot and the moment execution runs. H1 asks what the OMS
stamps when that happens.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from core.bus import InMemoryEventBus
from core.clock import ManualClock
from core.config import Settings, load_settings, simulated_venues
from core.events import Event, EventType
from core.models.common import DataQuality, OrderType, Side, TimeInForce
from core.models.execution import PaperOrder
from core.models.market import (
    BookMetrics,
    MarketState,
    OrderBookSnapshot,
    PriceLevel,
    VenueMarketState,
)
from core.models.opportunity import ExecutionPlan, OpportunityLeg, PlannedOrder
from execution.oms import OrderManager
from execution.paper.account import PaperAccount
from execution.paper.executor import PaperExecutor
from execution.paper.simulator import FillSimulator
from tests.conftest import START_MS

SYMBOL = "BTC-USD"
VENUE_A = "VENUE_A"
VENUE_B = "VENUE_B"

#: The configured venue latencies, restated so a test can reason about arrival
#: without reaching back into settings mid-assertion.
VENUE_A_LATENCY_MS = 35
VENUE_B_LATENCY_MS = 55
VENUE_A_CANCEL_LATENCY_MS = 60


# ======================================================================
# settings
# ======================================================================


def audit_settings(**execution_overrides) -> Settings:
    """Shipped settings with the simulated venues, plus execution overrides.

    Overrides go to ``ExecutionConfig`` only. Nothing here changes a risk
    limit, a consensus threshold or the market — the audit measures the
    shipped configuration and varies simulator parameters only where a
    hypothesis is explicitly about one of them (``max_partial_fraction``,
    ``liquidity_vanish_probability``, ``latency_drift_bps_per_100ms``).
    """
    base = load_settings().model_copy(update={"venues": simulated_venues()})
    if not execution_overrides:
        return base
    execution = base.execution.model_copy(update=execution_overrides)
    return base.model_copy(update={"execution": execution})


#: A simulator with every random source pinned off, so a test that is about
#: order *semantics* is not answering a question about the RNG. Vanishing
#: liquidity off, passive fills certain, no queue ahead, no latency drift.
def deterministic_execution(**overrides) -> dict:
    settings = {
        "liquidity_vanish_probability": 0.0,
        "maker_fill_probability": 1.0,
        "queue_ahead_fraction": 0.0,
        "latency_drift_bps_per_100ms": 0.0,
    }
    settings.update(overrides)
    return settings


# ======================================================================
# market
# ======================================================================


def book(
    venue: str = VENUE_A,
    symbol: str = SYMBOL,
    *,
    mid: float = 40_000.0,
    spread: float = 2.0,
    levels: int = 5,
    size: float = 1.0,
    tick: float = 1.0,
    ts: int = START_MS,
) -> OrderBookSnapshot:
    """A book with exactly the shape the caller asks for.

    ``spread`` is absolute (quote units) rather than bps so a test can put the
    touch at a round number and reason about a limit price by hand.
    """
    half = spread / 2.0
    return OrderBookSnapshot(
        venue=venue,
        symbol=symbol,
        exchange_ts=ts,
        received_ts=ts,
        sequence=1,
        bids=[
            PriceLevel(price=mid - half - i * tick, size=size) for i in range(levels)
        ],
        asks=[
            PriceLevel(price=mid + half + i * tick, size=size) for i in range(levels)
        ],
        is_checkpoint=True,
    )


def venue_state(
    snapshot: OrderBookSnapshot,
    *,
    as_of: int = START_MS,
    quality: DataQuality = DataQuality.FRESH,
    buy_volume: float = 0.0,
    sell_volume: float = 0.0,
) -> VenueMarketState:
    """A venue state carrying ``snapshot``.

    ``buy_volume``/``sell_volume`` are the rolling trade-flow totals TIDAL
    publishes. They are settable because H15 is precisely about how the
    executor consumes them.
    """
    bid, ask = snapshot.best_bid, snapshot.best_ask
    mid = (bid + ask) / 2
    bid_depth = sum(level.notional for level in snapshot.bids)
    ask_depth = sum(level.notional for level in snapshot.asks)
    metrics = BookMetrics(
        best_bid=bid,
        best_ask=ask,
        mid=mid,
        microprice=mid,
        spread=ask - bid,
        spread_bps=(ask - bid) / mid * 10_000,
        bid_depth_notional=bid_depth,
        ask_depth_notional=ask_depth,
        bid_depth_by_bps={"1": bid_depth, "5": bid_depth, "10": bid_depth, "25": bid_depth},
        ask_depth_by_bps={"1": ask_depth, "5": ask_depth, "10": ask_depth, "25": ask_depth},
        imbalance=0.0,
        buy_volume=buy_volume,
        sell_volume=sell_volume,
    )
    return VenueMarketState(
        venue=snapshot.venue,
        symbol=snapshot.symbol,
        metrics=metrics,
        book=snapshot,
        exchange_ts=snapshot.exchange_ts,
        last_update_ts=snapshot.received_ts,
        as_of=as_of,
        quality=quality,
        latency_ms=5.0,
        connected=True,
    )


def market(*states: VenueMarketState, created_at: int = START_MS) -> MarketState:
    return MarketState(
        created_at=created_at,
        venues={f"{s.venue}:{s.symbol}": s for s in states},
        consolidated={},
    )


def one_venue_market(
    *,
    mid: float = 40_000.0,
    spread: float = 2.0,
    levels: int = 5,
    size: float = 1.0,
    tick: float = 1.0,
    venue: str = VENUE_A,
    symbol: str = SYMBOL,
    ts: int = START_MS,
    **state_kwargs,
) -> MarketState:
    """One venue quoting one symbol.

    ``levels``/``size`` shape the depth: a single small level is how a test
    forces a partial fill without touching any simulator parameter.
    """
    return market(
        venue_state(
            book(
                venue,
                symbol,
                mid=mid,
                spread=spread,
                levels=levels,
                size=size,
                tick=tick,
                ts=ts,
            ),
            as_of=ts,
            **state_kwargs,
        ),
        created_at=ts,
    )


def empty_market(created_at: int = START_MS) -> MarketState:
    """A market with no venues at all: nothing is fillable anywhere."""
    return MarketState(created_at=created_at, venues={}, consolidated={})


# ======================================================================
# executor
# ======================================================================


@dataclass
class Rig:
    """One executor and everything it was built from."""

    executor: PaperExecutor
    oms: OrderManager
    account: PaperAccount
    bus: InMemoryEventBus
    clock: ManualClock
    settings: Settings
    published: list[Event] = field(default_factory=list)

    def events(self, *types: EventType) -> list[Event]:
        if not types:
            return list(self.published)
        wanted = set(types)
        return [e for e in self.published if e.type in wanted]

    def order(self, client_order_id: str) -> PaperOrder:
        found = self.oms.get(client_order_id)
        assert found is not None, f"no order {client_order_id}"
        return found

    def only_order(self) -> PaperOrder:
        orders = list(self.oms.orders.values())
        assert len(orders) == 1, f"expected one order, found {len(orders)}"
        return orders[0]

    def statuses(self, client_order_id: str) -> list[str]:
        return [status.value for _ts, status in self.order(client_order_id).history]

    def history_times(self, client_order_id: str) -> list[int]:
        return [ts for ts, _status in self.order(client_order_id).history]


def rig(
    *,
    settings: Settings | None = None,
    oms_now: int = START_MS,
    market_state: MarketState | None = None,
    seed: int | None = None,
) -> Rig:
    """A PaperExecutor wired to real collaborators, capturing every event.

    ``oms_now`` seeds the OMS/account clock. Leave it at ``START_MS`` for the
    ordinary case; move it to make the H1 divergence visible.
    """
    resolved = settings if settings is not None else audit_settings()
    clock = ManualClock(oms_now)
    bus = InMemoryEventBus()
    oms = OrderManager(clock=clock)
    account = PaperAccount(clock=clock, initial_balance=resolved.paper_initial_balance)
    simulator = FillSimulator(resolved.execution, seed=seed)
    executor = PaperExecutor(
        bus=bus,
        clock=clock,
        settings=resolved,
        oms=oms,
        account=account,
        simulator=simulator,
    )
    built = Rig(
        executor=executor,
        oms=oms,
        account=account,
        bus=bus,
        clock=clock,
        settings=resolved,
    )
    bus.subscribe(_capture(built), name="audit-capture")
    if market_state is not None:
        executor.update_market(market_state)
    return built


def _capture(built: Rig):
    async def handler(event: Event) -> None:
        built.published.append(event)

    return handler


async def drain(built: Rig) -> None:
    await built.bus.drain()


# ======================================================================
# plans
# ======================================================================


def planned(
    *,
    venue: str = VENUE_A,
    symbol: str = SYMBOL,
    side: Side = Side.BUY,
    quantity: float = 0.10,
    order_type: OrderType = OrderType.LIMIT,
    time_in_force: TimeInForce = TimeInForce.IOC,
    limit_price: float | None = None,
    expected_price: float = 40_001.0,
    expected_fee_bps: float = 5.0,
    ttl_ms: int = 5_000,
    client_order_id: str | None = None,
) -> PlannedOrder:
    """One planned order.

    Defaults describe the shipped aggressive entry leg: a crossing LIMIT with
    IOC, priced at the ask with a slippage-budgeted limit above it.
    """
    fields = dict(
        venue=venue,
        symbol=symbol,
        side=side,
        quantity=quantity,
        order_type=order_type,
        time_in_force=time_in_force,
        limit_price=limit_price if limit_price is not None else expected_price * 1.001,
        expected_price=expected_price,
        expected_fee_bps=expected_fee_bps,
        ttl_ms=ttl_ms,
    )
    if client_order_id is not None:
        fields["client_order_id"] = client_order_id
    return PlannedOrder(**fields)


def plan(
    *orders: PlannedOrder,
    created_at: int = START_MS,
    deadline_ms: int | None = None,
    max_slippage_bps: float = 10.0,
    notional: float = 4_000.0,
    strategy: str = "cross_venue",
    symbol: str = SYMBOL,
    intent_id: str = "int-audit",
    correlation_id: str = "opp-audit",
) -> ExecutionPlan:
    built = orders or (planned(),)
    return ExecutionPlan(
        created_at=created_at,
        correlation_id=correlation_id,
        intent_id=intent_id,
        strategy=strategy,
        symbol=symbol,
        orders=list(built),
        deadline_ms=deadline_ms if deadline_ms is not None else created_at + 10_000,
        max_slippage_bps=max_slippage_bps,
        notional=notional,
    )


def leg(
    venue: str = VENUE_A,
    side: Side = Side.BUY,
    *,
    symbol: str = SYMBOL,
    reference_price: float = 40_000.0,
    quantity: float | None = None,
) -> OpportunityLeg:
    return OpportunityLeg(
        venue=venue,
        symbol=symbol,
        side=side,
        reference_price=reference_price,
        quantity=quantity,
    )


# ======================================================================
# reporting helpers
# ======================================================================


def fill_notional(fills) -> float:
    return sum(f.quantity * f.price for f in fills)


def signed_slippage_bps(fill, expected_price: float) -> float:
    """Positive is worse than expected, on either side."""
    return (fill.price - expected_price) / expected_price * 10_000 * fill.side.sign


__all__ = [
    "SYMBOL",
    "VENUE_A",
    "VENUE_A_CANCEL_LATENCY_MS",
    "VENUE_A_LATENCY_MS",
    "VENUE_B",
    "VENUE_B_LATENCY_MS",
    "Rig",
    "audit_settings",
    "book",
    "deterministic_execution",
    "drain",
    "empty_market",
    "fill_notional",
    "leg",
    "market",
    "one_venue_market",
    "plan",
    "planned",
    "rig",
    "signed_slippage_bps",
    "venue_state",
]
