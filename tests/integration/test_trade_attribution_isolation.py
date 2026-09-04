"""Phase 1 polish: one attribution record reports ONLY its own trade's P&L.

Before this fix, ``Orchestrator._finish_attribution`` computed a closing
opportunity's realized P&L by reading ``PositionState.realized_pnl`` for each
leg's venue:symbol -- but that counter is a *lifetime* cumulative total for
that venue:symbol, not a per-opportunity one. A second (or later) opportunity
trading the same venue:symbol therefore inherited every earlier opportunity's
realized contribution too, on top of its own. This is exactly what produced
the reported symptom: attribution rows of $2,000-$2,700 each while total
account equity had only grown by about $430.

The fix accumulates each opportunity's realized P&L one fill at a time, in
``Orchestrator._on_fill``, from ``FillEvent.realized_pnl_delta`` -- captured
by ``PaperAccount.apply_fill`` at the exact moment it applies that fill, and
fed only to the attribution builder that fill's own ``correlation_id`` names.
See ``tests/integration/test_attribution_dispatch_ordering.py`` for a related,
narrower defect: an earlier version of this fix derived the delta from live
account state inside the PAPER_FILL handler instead of capturing it at
application time, which was itself vulnerable to bus dispatch order.
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
ETH = "ETH-USD"
STRATEGY = "cross_venue"


def make_fill(
    *,
    correlation_id: str,
    venue: str = VENUE,
    symbol: str = BTC,
    side: Side,
    quantity: float,
    price: float,
    fee: float = 1.0,
    ts: int = START_MS,
    client_order_id: str | None = None,
) -> FillEvent:
    return FillEvent(
        created_at=ts,
        client_order_id=client_order_id or f"order-{correlation_id}-{side.value}-{price}",
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
    """Register an opportunity and its attribution builder, bypassing full
    detection/consensus/planning -- the isolation bug lives entirely in
    ``_on_fill``/``_finish_attribution``, so the rest of the pipeline is
    irrelevant to reproducing or fixing it.
    """
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


async def send_fill(platform, fill: FillEvent) -> None:
    """Apply a fill to the account and publish it exactly as the paper
    executor does, so ``Orchestrator._on_fill`` runs for real.
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
    await platform.bus.drain()


async def close_opportunity(platform, record: OpportunityRecord):
    await platform.orchestrator._finish_attribution(record)
    return platform.orchestrator.scorecard.trades[-1]


class TestSequentialOpportunitiesAreIsolated:
    async def test_second_profitable_opportunity_does_not_include_the_first(self, platform):
        await platform.start(record=False)

        first = register_opportunity(platform, "opp-1")
        await send_fill(
            platform, make_fill(correlation_id="opp-1", side=Side.BUY, quantity=1.0, price=100.0)
        )
        await send_fill(
            platform, make_fill(correlation_id="opp-1", side=Side.SELL, quantity=1.0, price=110.0)
        )
        trade1 = await close_opportunity(platform, first)
        assert trade1.realized_pnl == pytest.approx(10.0 - 2.0)  # +10 gross, 2 in fees

        second = register_opportunity(platform, "opp-2")
        await send_fill(
            platform, make_fill(correlation_id="opp-2", side=Side.BUY, quantity=1.0, price=110.0)
        )
        await send_fill(
            platform, make_fill(correlation_id="opp-2", side=Side.SELL, quantity=1.0, price=115.0)
        )
        trade2 = await close_opportunity(platform, second)

        # Must reflect ONLY opp-2's own +5 move, not opp-1's prior +10 too.
        assert trade2.realized_pnl == pytest.approx(5.0 - 2.0)


class TestProfitableThenLosingTrade:
    async def test_a_loss_after_a_profit_is_not_offset_by_the_earlier_win(self, platform):
        await platform.start(record=False)

        first = register_opportunity(platform, "opp-1")
        await send_fill(
            platform, make_fill(correlation_id="opp-1", side=Side.BUY, quantity=1.0, price=100.0)
        )
        await send_fill(
            platform, make_fill(correlation_id="opp-1", side=Side.SELL, quantity=1.0, price=120.0)
        )
        trade1 = await close_opportunity(platform, first)
        assert trade1.realized_pnl == pytest.approx(20.0 - 2.0)

        second = register_opportunity(platform, "opp-2")
        await send_fill(
            platform, make_fill(correlation_id="opp-2", side=Side.BUY, quantity=1.0, price=120.0)
        )
        await send_fill(
            platform, make_fill(correlation_id="opp-2", side=Side.SELL, quantity=1.0, price=110.0)
        )
        trade2 = await close_opportunity(platform, second)

        # A -10 move, net of its own 2 in fees -- not "smoothed" by opp-1's win.
        assert trade2.realized_pnl == pytest.approx(-10.0 - 2.0)


class TestRepeatedBtcOpportunitiesStayIsolated:
    async def test_five_sequential_btc_opportunities_each_report_only_their_own_move(
        self, platform
    ):
        await platform.start(record=False)
        price = 100.0
        for i in range(5):
            record = register_opportunity(platform, f"btc-{i}")
            await send_fill(
                platform,
                make_fill(correlation_id=f"btc-{i}", side=Side.BUY, quantity=1.0, price=price),
            )
            price += 3.0
            await send_fill(
                platform,
                make_fill(correlation_id=f"btc-{i}", side=Side.SELL, quantity=1.0, price=price),
            )
            trade = await close_opportunity(platform, record)
            # Each one is a fixed +3 move net of 2 in fees, regardless of how
            # many prior BTC opportunities on this same venue:symbol closed
            # before it.
            assert trade.realized_pnl == pytest.approx(1.0)


class TestRepeatedEthOpportunitiesStayIsolatedFromBtc:
    async def test_interleaved_btc_and_eth_opportunities_do_not_cross_contaminate(
        self, platform
    ):
        await platform.start(record=False)

        btc1 = register_opportunity(platform, "btc-1", symbol=BTC)
        eth1 = register_opportunity(platform, "eth-1", symbol=ETH)

        await send_fill(
            platform,
            make_fill(correlation_id="btc-1", symbol=BTC, side=Side.BUY, quantity=1.0, price=100.0),
        )
        await send_fill(
            platform,
            make_fill(correlation_id="eth-1", symbol=ETH, side=Side.BUY, quantity=1.0, price=50.0),
        )
        await send_fill(
            platform,
            make_fill(
                correlation_id="btc-1", symbol=BTC, side=Side.SELL, quantity=1.0, price=108.0
            ),
        )
        await send_fill(
            platform,
            make_fill(correlation_id="eth-1", symbol=ETH, side=Side.SELL, quantity=1.0, price=55.0),
        )

        btc_trade = await close_opportunity(platform, btc1)
        eth_trade = await close_opportunity(platform, eth1)

        assert btc_trade.realized_pnl == pytest.approx(8.0 - 2.0)
        assert eth_trade.realized_pnl == pytest.approx(5.0 - 2.0)

        eth2 = register_opportunity(platform, "eth-2", symbol=ETH)
        await send_fill(
            platform,
            make_fill(correlation_id="eth-2", symbol=ETH, side=Side.BUY, quantity=1.0, price=55.0),
        )
        await send_fill(
            platform,
            make_fill(correlation_id="eth-2", symbol=ETH, side=Side.SELL, quantity=1.0, price=60.0),
        )
        eth_trade2 = await close_opportunity(platform, eth2)
        # Must not inherit eth-1's prior +5 contribution.
        assert eth_trade2.realized_pnl == pytest.approx(5.0 - 2.0)


class TestFeesAssignedExactlyOnce:
    async def test_fees_on_the_attribution_match_only_this_opportunitys_own_fills(
        self, platform
    ):
        await platform.start(record=False)
        register_opportunity(platform, "opp-1")
        await send_fill(
            platform,
            make_fill(correlation_id="opp-1", side=Side.BUY, quantity=1.0, price=100.0, fee=1.5),
        )
        await send_fill(
            platform,
            make_fill(correlation_id="opp-1", side=Side.SELL, quantity=1.0, price=101.0, fee=2.5),
        )
        record = platform.state.opportunities["opp-1"]
        trade = await close_opportunity(platform, record)

        assert trade.fees == pytest.approx(4.0)
        assert trade.realized_pnl == pytest.approx(1.0 - 4.0)
        # record.fees (a separate, independently-accumulated counter used by
        # the /api/state opportunity feed) must agree.
        assert record.fees == pytest.approx(4.0)


class TestEntryAndExitFillsAttributedCorrectly:
    async def test_partial_entry_and_exit_fills_all_land_on_the_same_opportunity(
        self, platform
    ):
        await platform.start(record=False)
        record = register_opportunity(platform, "opp-1")

        # Two partial entries, two partial exits.
        await send_fill(
            platform, make_fill(correlation_id="opp-1", side=Side.BUY, quantity=0.4, price=100.0)
        )
        await send_fill(
            platform, make_fill(correlation_id="opp-1", side=Side.BUY, quantity=0.6, price=102.0)
        )
        await send_fill(
            platform, make_fill(correlation_id="opp-1", side=Side.SELL, quantity=0.5, price=108.0)
        )
        await send_fill(
            platform, make_fill(correlation_id="opp-1", side=Side.SELL, quantity=0.5, price=109.0)
        )
        trade = await close_opportunity(platform, record)

        expected_gross = platform.account.realized_pnl
        assert trade.realized_pnl == pytest.approx(expected_gross - trade.fees)
        assert trade.filled_notional == pytest.approx(
            0.4 * 100.0 + 0.6 * 102.0 + 0.5 * 108.0 + 0.5 * 109.0
        )


class TestNonOverlappingOpportunitiesReconcileToAccountTotal:
    async def test_summed_attribution_matches_the_accounts_net_realized_result(self, platform):
        await platform.start(record=False)
        moves = [(100.0, 105.0), (105.0, 103.0), (103.0, 111.0)]
        total_attributed = 0.0
        for i, (entry, exit_) in enumerate(moves):
            record = register_opportunity(platform, f"opp-{i}")
            await send_fill(
                platform,
                make_fill(correlation_id=f"opp-{i}", side=Side.BUY, quantity=1.0, price=entry),
            )
            await send_fill(
                platform,
                make_fill(correlation_id=f"opp-{i}", side=Side.SELL, quantity=1.0, price=exit_),
            )
            trade = await close_opportunity(platform, record)
            total_attributed += trade.realized_pnl

        # No hedges, no non-opportunity fills: every fill belonged to exactly
        # one, fully-closed opportunity, so the sum of attributed P&L must
        # equal the account's own net realized result (realized minus fees).
        account_net_realized = platform.account.realized_pnl - platform.account.fees_paid
        assert total_attributed == pytest.approx(account_net_realized)


class TestOverlappingOpportunitiesStillAttributeExactly:
    async def test_two_concurrently_open_opportunities_on_the_same_symbol_split_correctly(
        self, platform
    ):
        """Fill-grounded attribution allocates by the fill that produced the
        P&L, not by a snapshot taken at some later close time -- so two
        opportunities interleaved on the very same venue:symbol still each
        get exactly their own contribution, in the order fills actually
        happened.
        """
        await platform.start(record=False)
        first = register_opportunity(platform, "opp-1")
        second = register_opportunity(platform, "opp-2")

        # opp-1 opens, then opp-2 opens (both long, same symbol -- the
        # position is a single shared average-cost lot), then opp-1 exits
        # first while opp-2 is still open, then opp-2 exits.
        await send_fill(
            platform, make_fill(correlation_id="opp-1", side=Side.BUY, quantity=1.0, price=100.0)
        )
        await send_fill(
            platform, make_fill(correlation_id="opp-2", side=Side.BUY, quantity=1.0, price=104.0)
        )
        await send_fill(
            platform, make_fill(correlation_id="opp-1", side=Side.SELL, quantity=1.0, price=110.0)
        )
        trade1 = await close_opportunity(platform, first)
        await send_fill(
            platform, make_fill(correlation_id="opp-2", side=Side.SELL, quantity=1.0, price=115.0)
        )
        trade2 = await close_opportunity(platform, second)

        # Average cost of the shared lot after both entries is 102; opp-1's
        # exit at 110 realizes (110 - 102) * 1 = 8 gross on the ONE unit sold,
        # opp-2's later exit at 115 realizes (115 - 102) * 1 = 13 gross.
        assert trade1.realized_pnl == pytest.approx(8.0 - 2.0)
        assert trade2.realized_pnl == pytest.approx(13.0 - 2.0)
        # The two, taken together, account for the whole position's realized
        # result -- nothing lost, nothing double-counted.
        assert trade1.realized_pnl + trade2.realized_pnl == pytest.approx(
            platform.account.realized_pnl - platform.account.fees_paid
        )


class TestScorecardUsesCorrectedAttribution:
    async def test_scorecard_records_the_corrected_realized_pnl(self, platform):
        await platform.start(record=False)
        first = register_opportunity(platform, "opp-1")
        await send_fill(
            platform, make_fill(correlation_id="opp-1", side=Side.BUY, quantity=1.0, price=100.0)
        )
        await send_fill(
            platform, make_fill(correlation_id="opp-1", side=Side.SELL, quantity=1.0, price=110.0)
        )
        await close_opportunity(platform, first)

        second = register_opportunity(platform, "opp-2")
        await send_fill(
            platform, make_fill(correlation_id="opp-2", side=Side.BUY, quantity=1.0, price=110.0)
        )
        await send_fill(
            platform, make_fill(correlation_id="opp-2", side=Side.SELL, quantity=1.0, price=115.0)
        )
        await close_opportunity(platform, second)

        trades = platform.orchestrator.scorecard.trades
        assert [t.realized_pnl for t in trades[-2:]] == [
            pytest.approx(10.0 - 2.0),
            pytest.approx(5.0 - 2.0),
        ]


class TestRunawayCumulativeAttributionRegressionIsCaught:
    async def test_many_same_symbol_trades_never_attribute_more_than_the_account_actually_made(
        self, platform
    ):
        """Direct regression test for the reported bug: repeated
        trade-attribution rows around $2,000-$2,700 each while total account
        equity had only grown by about $430. Under the old cumulative-read
        bug, each successive same-symbol closure would report a LARGER
        realized P&L than the one before it, growing without bound as more
        trades closed -- even though each individual trade's own economic
        result was small and roughly constant.
        """
        await platform.start(record=False)
        price = 100.0
        reported = []
        for i in range(8):
            record = register_opportunity(platform, f"opp-{i}")
            await send_fill(
                platform,
                make_fill(correlation_id=f"opp-{i}", side=Side.BUY, quantity=1.0, price=price),
            )
            price += 2.0
            await send_fill(
                platform,
                make_fill(correlation_id=f"opp-{i}", side=Side.SELL, quantity=1.0, price=price),
            )
            trade = await close_opportunity(platform, record)
            reported.append(trade.realized_pnl)

        # Every trade reports the same, small, constant result -- it does not
        # grow as more same-symbol opportunities close before it.
        assert reported == [pytest.approx(0.0) for _ in reported]
        assert sum(reported) == pytest.approx(
            platform.account.realized_pnl - platform.account.fees_paid
        )
