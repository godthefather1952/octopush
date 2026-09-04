"""Attribution must not depend on PAPER_FILL bus-dispatch order.

``PaperExecutor.poll()`` applies every live order's fill straight to
``PaperAccount`` (synchronously, as it iterates) and only *afterward* does
``Orchestrator._settle()`` call ``bus.drain()`` once for the whole poll
cycle -- ``InMemoryEventBus.publish()`` enqueues an event, it does not
dispatch it. So two fills on the same venue:symbol, from two different
opportunities, can both land on the shared ``PositionState`` before either
one's PAPER_FILL handler ever runs.

An earlier version of the trade-attribution fix (see
``test_trade_attribution_isolation.py``) computed each fill's realized delta
*inside* the PAPER_FILL handler, by comparing the position's current
cumulative ``realized_pnl`` against a remembered baseline. That is exactly
the code path this race defeats: by the time the first handler runs, the
account may already reflect fills that were applied *after* it but
dispatched *before* it, so "current cumulative minus my baseline" attributes
someone else's fill's contribution.

The fix instead captures each fill's own realized-P&L delta inside
``PaperAccount.apply_fill`` itself, at the exact moment that fill is
applied, and carries it on the (still-mutable, not-yet-published)
``FillEvent`` as ``realized_pnl_delta``. The PAPER_FILL handler then only
ever reads a value that was frozen before the event was ever queued, so it
cannot matter which fill's handler happens to run first.
"""

from __future__ import annotations

import pytest

from core.events import Event, EventType
from core.models.agent import ConsensusResult
from core.models.common import Side
from core.models.execution import FillEvent
from core.models.opportunity import Opportunity, OpportunityKind, OpportunityLeg
from core.models.risk import RiskDecision, RiskVerdict
from core.state import OpportunityRecord
from monitoring.attribution import AttributionBuilder
from tests.conftest import START_MS

VENUE = "VENUE_A"
BTC = "BTC-USD"
STRATEGY = "cross_venue"


def make_fill(
    *,
    correlation_id: str | None,
    venue: str = VENUE,
    symbol: str = BTC,
    side: Side,
    quantity: float,
    price: float,
    fee: float = 0.0,
    ts: int = START_MS,
    client_order_id: str | None = None,
) -> FillEvent:
    return FillEvent(
        created_at=ts,
        client_order_id=client_order_id
        or f"order-{correlation_id}-{side.value}-{price}-{quantity}",
        correlation_id=correlation_id,
        venue=venue,
        symbol=symbol,
        side=side,
        quantity=quantity,
        price=price,
        fee=fee,
    )


def register_opportunity(
    platform, opportunity_id: str, *, venue: str = VENUE, symbol: str = BTC, ts: int = START_MS
) -> OpportunityRecord:
    opportunity = Opportunity(
        created_at=ts,
        expires_at=ts + 5_000,
        opportunity_id=opportunity_id,
        kind=OpportunityKind.CROSS_VENUE_DISLOCATION,
        strategy=STRATEGY,
        symbol=symbol,
        gross_edge_bps=20.0,
        legs=[
            OpportunityLeg(venue=venue, symbol=symbol, side=Side.BUY, reference_price=100.0)
        ],
    )
    record = OpportunityRecord(opportunity=opportunity, updated_at=ts)
    platform.state.opportunities[opportunity_id] = record

    consensus = ConsensusResult(
        created_at=ts, symbol=symbol, strategy=STRATEGY, score=0.5, agreement=0.8
    )
    decision = RiskDecision(
        created_at=ts,
        decision_id=f"dec-{opportunity_id}",
        intent_id=f"int-{opportunity_id}",
        strategy=STRATEGY,
        symbol=symbol,
        verdict=RiskVerdict.APPROVED,
        approved_notional=1_000.0,
        requested_notional=1_000.0,
    )
    builder = AttributionBuilder(
        trade_ref=f"trade-{opportunity_id}",
        opportunity_id=opportunity_id,
        intent_id=f"int-{opportunity_id}",
        strategy=STRATEGY,
        symbol=symbol,
        created_at=ts,
        consensus=consensus,
        expected_net_edge_bps=10.0,
        expected_costs_bps=2.0,
        decision=decision,
    )
    platform.orchestrator.attributions[opportunity_id] = builder
    return record


async def queue_fill(platform, fill: FillEvent) -> None:
    """Apply a fill to the account and enqueue its PAPER_FILL event, WITHOUT
    draining the bus -- reproducing ``PaperExecutor.poll()``'s own ordering:
    every live order's fill lands on the account synchronously as poll()
    iterates, and ``Orchestrator._settle()`` drains the bus only once, after
    poll() returns for the whole cycle. ``bus.publish()`` only enqueues; it
    never dispatches.
    """
    platform.account.apply_fill(fill)
    await platform.bus.publish(
        Event(
            type=EventType.PAPER_FILL,
            ts_ms=fill.created_at,
            source="TEST",
            schema_name="FillEvent",
            correlation_id=fill.correlation_id,
            payload=fill.to_json_dict(),
        )
    )


class TestAttributionSurvivesUndrainedFillOrdering:
    async def test_two_opportunities_closing_the_same_symbol_are_not_misattributed(
        self, platform
    ):
        """The scenario from the bug report, reproduced exactly:

        fill A takes cumulative realized P&L 0 -> +10, fill B (a different
        opportunity, same venue:symbol) then takes it +10 -> +25 -- but
        BOTH mutations happen, and BOTH PAPER_FILL events are queued,
        before either handler runs. A must get its own +10, not +25; B must
        get its own +15, not 0.
        """
        await platform.start(record=False)
        # An unrelated, already-settled position: 2 BTC @ 100 average cost.
        # Opening a position never realizes anything, so this does not
        # perturb the cumulative counter -- it only gives fills A and B
        # something to reduce.
        platform.account.apply_fill(
            make_fill(correlation_id=None, side=Side.BUY, quantity=2.0, price=100.0)
        )

        register_opportunity(platform, "opp-1")
        register_opportunity(platform, "opp-2")

        fill_a = make_fill(correlation_id="opp-1", side=Side.SELL, quantity=1.0, price=110.0)
        fill_b = make_fill(correlation_id="opp-2", side=Side.SELL, quantity=1.0, price=115.0)

        await queue_fill(platform, fill_a)
        await queue_fill(platform, fill_b)
        # Both mutations already happened (checked below); neither handler
        # has run yet -- this is the critical ordering the race depends on.
        assert platform.bus.queue_depth == 2
        assert platform.account.realized_pnl == pytest.approx(25.0)

        await platform.bus.drain()

        builder_a = platform.orchestrator.attributions["opp-1"]
        builder_b = platform.orchestrator.attributions["opp-2"]
        assert builder_a.realized_pnl_gross == pytest.approx(10.0), (
            "opp-1 must get only its own +10 move, not the +25 combined total "
            "that happened to be on the account by the time its handler ran"
        )
        assert builder_b.realized_pnl_gross == pytest.approx(15.0), (
            "opp-2 must get its own +15 contribution, not 0"
        )

    async def test_two_fills_belonging_to_the_same_opportunity_both_still_count(
        self, platform
    ):
        await platform.start(record=False)
        platform.account.apply_fill(
            make_fill(correlation_id=None, side=Side.BUY, quantity=2.0, price=100.0)
        )
        record = register_opportunity(platform, "opp-1")

        fill_1 = make_fill(correlation_id="opp-1", side=Side.SELL, quantity=1.0, price=110.0)
        fill_2 = make_fill(correlation_id="opp-1", side=Side.SELL, quantity=1.0, price=120.0)
        await queue_fill(platform, fill_1)
        await queue_fill(platform, fill_2)
        assert platform.bus.queue_depth == 2

        await platform.bus.drain()

        builder = platform.orchestrator.attributions["opp-1"]
        assert builder.realized_pnl_gross == pytest.approx(10.0 + 20.0)
        assert record.filled_notional == pytest.approx(110.0 + 120.0)

    async def test_a_hedge_fill_between_two_attributed_fills_does_not_corrupt_either(
        self, platform
    ):
        await platform.start(record=False)
        platform.account.apply_fill(
            make_fill(correlation_id=None, side=Side.BUY, quantity=3.0, price=100.0)
        )
        register_opportunity(platform, "opp-1")
        register_opportunity(platform, "opp-2")

        fill_a = make_fill(correlation_id="opp-1", side=Side.SELL, quantity=1.0, price=110.0)
        # A hedge fill on the SAME venue:symbol, correlated to something no
        # attribution builder was ever registered for.
        hedge_fill = make_fill(correlation_id="hedge-xyz", side=Side.SELL, quantity=1.0, price=90.0)
        fill_b = make_fill(correlation_id="opp-2", side=Side.SELL, quantity=1.0, price=130.0)

        await queue_fill(platform, fill_a)
        await queue_fill(platform, hedge_fill)
        await queue_fill(platform, fill_b)
        assert platform.bus.queue_depth == 3

        await platform.bus.drain()

        builder_a = platform.orchestrator.attributions["opp-1"]
        builder_b = platform.orchestrator.attributions["opp-2"]
        assert builder_a.realized_pnl_gross == pytest.approx(10.0)
        assert builder_b.realized_pnl_gross == pytest.approx(30.0)

    async def test_duplicate_paper_fill_delivery_is_not_double_counted(self, platform):
        await platform.start(record=False)
        platform.account.apply_fill(
            make_fill(correlation_id=None, side=Side.BUY, quantity=1.0, price=100.0)
        )
        register_opportunity(platform, "opp-1")

        fill = make_fill(correlation_id="opp-1", side=Side.SELL, quantity=1.0, price=110.0)
        # Apply once (idempotent by fill_id -- a second apply_fill would be a
        # no-op), but simulate the PAPER_FILL event being delivered twice,
        # e.g. by a transport-level redelivery.
        platform.account.apply_fill(fill)
        event = Event(
            type=EventType.PAPER_FILL,
            ts_ms=fill.created_at,
            source="TEST",
            schema_name="FillEvent",
            correlation_id=fill.correlation_id,
            payload=fill.to_json_dict(),
        )
        await platform.bus.publish(event)
        await platform.bus.publish(event.model_copy(deep=True))
        assert platform.bus.queue_depth == 2

        await platform.bus.drain()

        builder = platform.orchestrator.attributions["opp-1"]
        assert builder.realized_pnl_gross == pytest.approx(10.0), (
            "a redelivered PAPER_FILL must not be counted twice"
        )
