"""Causal snapshot, and live economics for an open position.

Two defects, one shape: a decision made against market data that is not the
data the decision claims to be about.

**Fix A — TIDAL evaluated a market it was never handed.** ``evaluate`` called
``self.venue_state()``, which rebuilds a venue's state from the current local
book and ages it against ``clock.now_ms()`` at the moment the bus happened to
dispatch the handler. The tick had already published a ``MarketState``; the
detector found the opportunity in it; ``tick_time`` is its ``created_at``. So
the agent was scoring one market while the rest of the tick reasoned about
another, and replay — which restores the tick instant but cannot restore
dispatch latency — would compute different ages, a different confidence, and
eventually a different verdict from an identical recorded market.

**Fix B — the monitor asked a question that could not change its answer.**
``_monitor`` republished the *original* opportunity every tick, so ZEPHR
divided its costs against the gross edge measured at detection and TIDAL
scaled its volatility penalty by the same frozen number. An opportunity whose
dislocation had entirely closed still presented the entry edge, and the exit
path — whose whole job is to notice decay — was shown a snapshot in which
decay was impossible.

What is deliberately NOT re-detected: the venues. The position exists on the
venues it was opened on, and those are the venues that have to be unwound.
"""

from __future__ import annotations

import inspect

import pytest

from agents.tidal.agent import Tidal
from apps.orchestrator import orchestrator as orch
from core.bus import InMemoryEventBus
from core.clock import ManualClock
from core.config import Settings, load_settings, simulated_venues
from core.health import HealthRegistry
from core.models.common import DataQuality, Side
from core.models.market import (
    MarketState,
    OrderBookSnapshot,
    PriceLevel,
    VenueMarketState,
)
from core.models.opportunity import Opportunity, OpportunityKind, OpportunityLeg
from tests.conftest import START_MS, make_book, venue_state_from_book

SYMBOL = "BTC-USD"
BUY_VENUE = "VENUE_A"
SELL_VENUE = "VENUE_B"
THIRD_VENUE = "VENUE_C"


# ======================================================================
# builders
# ======================================================================


def lopsided_book(
    venue: str,
    *,
    mid: float = 100.0,
    bid_size: float,
    ask_size: float,
    ts: int = START_MS,
    sequence: int = 1,
    symbol: str = SYMBOL,
) -> OrderBookSnapshot:
    """A one-level-per-side book with an exactly controlled depth imbalance.

    ``imbalance`` is ``(bid_notional - ask_notional) / total``, so setting the
    two sizes sets the microstructure input directly.
    """
    return OrderBookSnapshot(
        venue=venue,
        symbol=symbol,
        exchange_ts=ts,
        received_ts=ts,
        sequence=sequence,
        bids=[PriceLevel(price=mid - 0.01, size=bid_size)],
        asks=[PriceLevel(price=mid + 0.01, size=ask_size)],
        is_checkpoint=True,
    )


def build_tidal() -> Tidal:
    clock = ManualClock(START_MS)
    settings: Settings = load_settings().model_copy(
        update={"venues": simulated_venues()}
    )
    return Tidal(
        bus=InMemoryEventBus(raise_on_handler_error=True),
        clock=clock,
        settings=settings,
        health=HealthRegistry(clock=clock),
    )


def opportunity(
    *,
    gross_edge_bps: float = 20.0,
    buy_price: float = 100.0,
    sell_price: float = 100.2,
    buy_venue: str = BUY_VENUE,
    sell_venue: str = SELL_VENUE,
    created_at: int = START_MS,
) -> Opportunity:
    opp = Opportunity(
        created_at=created_at,
        source_data_timestamp=created_at,
        kind=OpportunityKind.CROSS_VENUE_DISLOCATION,
        strategy="cross_venue",
        symbol=SYMBOL,
        legs=[
            OpportunityLeg(
                venue=buy_venue,
                symbol=SYMBOL,
                side=Side.BUY,
                reference_price=buy_price,
            ),
            OpportunityLeg(
                venue=sell_venue,
                symbol=SYMBOL,
                side=Side.SELL,
                reference_price=sell_price,
            ),
        ],
        gross_edge_bps=gross_edge_bps,
        expires_at=created_at + 2_000,
        reason_codes=["CROSS_VENUE_DISLOCATION"],
        detail={
            "buy_venue": buy_venue,
            "sell_venue": sell_venue,
            "buy_price": buy_price,
            "sell_price": sell_price,
            "reference_price": (buy_price + sell_price) / 2,
        },
    )
    opp.correlation_id = opp.opportunity_id
    return opp


def market_of(*states: VenueMarketState, created_at: int = START_MS) -> MarketState:
    return MarketState(
        created_at=created_at,
        # The market-wide NEWEST, exactly as ``build_state`` computes it. The
        # tests below rely on this differing from the per-leg oldest.
        source_data_timestamp=max(
            s.last_update_ts for s in states if s.last_update_ts is not None
        ),
        venues={f"{s.venue}:{s.symbol}": s for s in states},
    )


# ======================================================================
# FIX A — TIDAL evaluates the frozen snapshot
# ======================================================================


class TestTidalEvaluatesTheFrozenSnapshot:
    async def test_the_opinion_reflects_the_published_snapshot_not_the_live_book(
        self,
    ):
        """The decisive test for Fix A.

        The books are moved AFTER the snapshot was published and BEFORE the
        opportunity is evaluated — exactly the window a live feed occupies
        between ``publish_state()`` and the bus dispatching this handler. The
        opinion must describe the published market.
        """
        tidal = build_tidal()
        await tidal.on_snapshot(lopsided_book(BUY_VENUE, bid_size=9.0, ask_size=1.0))
        await tidal.on_snapshot(lopsided_book(SELL_VENUE, bid_size=1.0, ask_size=9.0))
        snapshot = tidal.build_state(START_MS)
        published_buy = snapshot.venue_state(BUY_VENUE, SYMBOL).metrics.imbalance
        assert published_buy == pytest.approx(0.8, abs=1e-3), (
            "the fixture must be lopsided"
        )

        # The market moves, hard, and in the opposite direction. Nothing
        # republishes the state.
        await tidal.on_snapshot(
            lopsided_book(BUY_VENUE, bid_size=1.0, ask_size=9.0, sequence=2)
        )
        await tidal.on_snapshot(
            lopsided_book(SELL_VENUE, bid_size=9.0, ask_size=1.0, sequence=2)
        )
        live_buy = tidal.venue_state(BUY_VENUE, SYMBOL).metrics.imbalance
        assert live_buy == pytest.approx(-0.8, abs=1e-3), (
            "the live book must genuinely disagree with the snapshot, or this "
            "test cannot tell the two apart"
        )

        opinion = tidal.evaluate(opportunity(), START_MS)
        assert opinion is not None
        assert opinion.detail[f"imbalance_{BUY_VENUE}"] == pytest.approx(
            published_buy, abs=1e-4
        )
        assert opinion.detail[f"imbalance_{BUY_VENUE}"] != pytest.approx(
            live_buy, abs=1e-4
        )

    async def test_no_published_snapshot_means_no_opinion(self):
        """Books exist, but nothing has been published yet. There is no
        observation to reason about, so TIDAL is MISSING — not abstaining, and
        certainly not guessing from the raw books."""
        tidal = build_tidal()
        await tidal.on_snapshot(lopsided_book(BUY_VENUE, bid_size=9.0, ask_size=1.0))
        await tidal.on_snapshot(lopsided_book(SELL_VENUE, bid_size=1.0, ask_size=9.0))
        assert tidal.state is None
        assert tidal.evaluate(opportunity(), START_MS) is None

    async def test_the_verdict_does_not_move_when_the_clock_does(self):
        """Dispatch latency is a scheduling accident and replay never
        reproduces it. Two identical markets, two wildly different clock
        positions, one answer."""
        opinions = []
        for drift_ms in (0, 5_000, 9_000_000):
            tidal = build_tidal()
            await tidal.on_snapshot(
                lopsided_book(BUY_VENUE, bid_size=9.0, ask_size=1.0)
            )
            await tidal.on_snapshot(
                lopsided_book(SELL_VENUE, bid_size=1.0, ask_size=9.0)
            )
            tidal.build_state(START_MS)
            tidal.clock.advance(drift_ms)
            opinion = tidal.evaluate(opportunity(), START_MS)
            assert opinion is not None
            opinions.append(
                (opinion.signal, opinion.confidence, opinion.abstain, opinion.detail)
            )

        assert opinions[0] == opinions[1] == opinions[2], (
            "the clock moved between the snapshot and the evaluation, and the "
            "opinion changed with it"
        )

    async def test_the_reported_age_is_the_snapshots_age_not_a_live_one(self):
        """``max_data_age_ms`` is ``as_of - last_update_ts`` on the frozen
        state. Advancing the clock afterwards must not age the observation."""
        tidal = build_tidal()
        await tidal.on_snapshot(lopsided_book(BUY_VENUE, bid_size=9.0, ask_size=1.0))
        await tidal.on_snapshot(lopsided_book(SELL_VENUE, bid_size=1.0, ask_size=9.0))
        tidal.build_state(START_MS + 300)
        tidal.clock.advance(120_000)

        opinion = tidal.evaluate(opportunity(), START_MS + 300)
        assert opinion.detail["max_data_age_ms"] == 300

    def test_the_evaluation_path_cannot_silently_go_back(self):
        """Structural guard. Both regressions are one method call away, and
        neither would fail a behavioural test on a stationary clock."""
        source = inspect.getsource(Tidal.evaluate)
        assert "self.venue_state(" not in source, (
            "venue_state() rebuilds metrics against the live clock"
        )
        assert "self.clock" not in source
        assert "compute_metrics" not in source
        assert "self.state" in source

    def test_no_accessor_on_this_class_reopens_the_hole(self):
        """``microstructure_metrics`` is the other way into the same bug: an
        innocuous-looking helper that recomputes against the clock."""
        source = inspect.getsource(Tidal.microstructure_metrics)
        assert "self.venue_state(" not in source
        assert "self.state" in source


class TestTidalStampsTheOldestLeg:
    """TIDAL-H4 / P3-7, closed for TIDAL.

    A record derived from several legs is only as fresh as its stalest one.
    ``MarketState.source_data_timestamp`` is the market-wide NEWEST, so
    stamping an opinion with it launders a stale leg into a fresh-looking
    record — and the age gates downstream read exactly that field.
    """

    @staticmethod
    def _snapshot() -> MarketState:
        stale = venue_state_from_book(
            make_book(BUY_VENUE, SYMBOL, 100.0, ts=START_MS - 500)
        )
        fresh = venue_state_from_book(make_book(SELL_VENUE, SYMBOL, 100.2, ts=START_MS))
        unrelated = venue_state_from_book(
            make_book(THIRD_VENUE, SYMBOL, 100.1, ts=START_MS)
        )
        return market_of(stale, fresh, unrelated)

    def test_the_stamp_is_the_oldest_leg(self):
        snapshot = self._snapshot()
        assert snapshot.source_data_timestamp == START_MS, (
            "the market-wide newest must differ from the oldest leg, or this "
            "test proves nothing"
        )
        tidal = build_tidal()
        tidal.state = snapshot
        opinion = tidal.evaluate(opportunity(), START_MS)
        assert opinion is not None
        assert opinion.source_data_timestamp == START_MS - 500

    def test_the_stamp_is_not_the_market_wide_newest(self):
        tidal = build_tidal()
        tidal.state = self._snapshot()
        opinion = tidal.evaluate(opportunity(), START_MS)
        assert opinion.source_data_timestamp != tidal.state.source_data_timestamp

    def test_the_reported_age_follows_the_stalest_leg(self):
        tidal = build_tidal()
        tidal.state = self._snapshot()
        opinion = tidal.evaluate(opportunity(), START_MS)
        assert opinion.data_age_ms == 500

    def test_a_third_venue_on_the_same_symbol_cannot_refresh_the_stamp(self):
        """The unrelated venue in the fixture is the same symbol and the
        freshest thing in the snapshot. It is not a leg, so it contributes
        nothing to this opinion's freshness."""
        tidal = build_tidal()
        tidal.state = self._snapshot()
        with_third = tidal.evaluate(opportunity(), START_MS)

        without_third = build_tidal()
        without_third.state = market_of(
            venue_state_from_book(make_book(BUY_VENUE, SYMBOL, 100.0, ts=START_MS - 500)),
            venue_state_from_book(make_book(SELL_VENUE, SYMBOL, 100.2, ts=START_MS)),
        )
        assert (
            with_third.source_data_timestamp
            == without_third.evaluate(opportunity(), START_MS).source_data_timestamp
        )


# ======================================================================
# FIX B — the monitor prices the position that exists, as it stands now
# ======================================================================


def monitored(orchestrator, opp: Opportunity, market: MarketState):
    return orchestrator._monitor_opportunity(opp, market)


def snapshot_with(
    *, buy_ask: float, sell_bid: float, third_ask: float | None = None
) -> MarketState:
    """A snapshot whose touches are set by construction.

    ``make_book`` centres a symmetric spread on ``mid``, so the buy leg's best
    ask and the sell leg's best bid are both derived from the mid and the
    2 bps default spread. Working back from the touch keeps the tests written
    in the units the production code actually reads.
    """
    half_bps = 1.0  # half of make_book's 2 bps default spread
    states = [
        venue_state_from_book(
            make_book(BUY_VENUE, SYMBOL, buy_ask / (1 + half_bps / 10_000))
        ),
        venue_state_from_book(
            make_book(SELL_VENUE, SYMBOL, sell_bid / (1 - half_bps / 10_000))
        ),
    ]
    if third_ask is not None:
        states.append(
            venue_state_from_book(
                make_book(THIRD_VENUE, SYMBOL, third_ask / (1 + half_bps / 10_000))
            )
        )
    return market_of(*states)


class TestTheMonitorPricesTheCurrentMarket:
    def test_the_republished_edge_is_the_current_one(self, platform):
        """The decisive test for Fix B. The dislocation has closed completely
        since entry; the monitored opportunity must say so."""
        entry = opportunity(gross_edge_bps=20.0)
        market = snapshot_with(buy_ask=100.00, sell_bid=100.00)

        result = monitored(platform.orchestrator, entry, market)
        assert result is not None
        assert entry.gross_edge_bps == pytest.approx(20.0)
        assert result.gross_edge_bps == pytest.approx(0.0, abs=0.5)

    def test_the_edge_can_go_negative(self, platform):
        """A position that is now underwater must be reported as underwater.
        Clamping at zero would make "closed" and "inverted" look alike to
        every agent downstream."""
        entry = opportunity(gross_edge_bps=20.0)
        market = snapshot_with(buy_ask=100.10, sell_bid=99.90)
        result = monitored(platform.orchestrator, entry, market)
        assert result.gross_edge_bps < 0

    def test_a_surviving_edge_is_still_reported(self, platform):
        entry = opportunity(gross_edge_bps=20.0)
        market = snapshot_with(buy_ask=100.00, sell_bid=100.15)
        result = monitored(platform.orchestrator, entry, market)
        assert result.gross_edge_bps > 10.0

    def test_the_touches_are_ask_on_the_buy_leg_and_bid_on_the_sell_leg(
        self, platform
    ):
        """Touch to touch, the frame the detector used. Reading mids here
        would overstate the surviving edge by a full spread."""
        market = snapshot_with(buy_ask=100.00, sell_bid=100.15)
        result = monitored(platform.orchestrator, opportunity(), market)

        buy_state = market.venue_state(BUY_VENUE, SYMBOL)
        sell_state = market.venue_state(SELL_VENUE, SYMBOL)
        legs = {leg.side: leg for leg in result.legs}
        assert legs[Side.BUY].reference_price == pytest.approx(
            buy_state.metrics.best_ask
        )
        assert legs[Side.SELL].reference_price == pytest.approx(
            sell_state.metrics.best_bid
        )
        # Not the mids, which is what makes this a distinct assertion.
        assert legs[Side.BUY].reference_price != pytest.approx(buy_state.metrics.mid)

    def test_the_reference_prices_are_updated_from_the_entry_values(self, platform):
        entry = opportunity(buy_price=100.0, sell_price=100.2)
        market = snapshot_with(buy_ask=101.00, sell_bid=101.05)
        result = monitored(platform.orchestrator, entry, market)
        assert [leg.reference_price for leg in result.legs] != [
            leg.reference_price for leg in entry.legs
        ]

    def test_the_entry_edge_is_kept_alongside_for_comparison(self, platform):
        entry = opportunity(gross_edge_bps=20.0)
        result = monitored(
            platform.orchestrator, entry, snapshot_with(buy_ask=100.0, sell_bid=100.0)
        )
        assert result.detail["entry_gross_edge_bps"] == pytest.approx(20.0)
        assert "MONITOR_REPRICED" in result.reason_codes

    def test_the_source_timestamp_is_re_derived_for_this_snapshot(self, platform):
        entry = opportunity(created_at=START_MS - 10_000)
        stale = venue_state_from_book(
            make_book(BUY_VENUE, SYMBOL, 100.0, ts=START_MS - 400)
        )
        fresh = venue_state_from_book(make_book(SELL_VENUE, SYMBOL, 100.0, ts=START_MS))
        result = monitored(platform.orchestrator, entry, market_of(stale, fresh))
        assert result.source_data_timestamp == START_MS - 400
        assert result.source_data_timestamp != entry.source_data_timestamp


class TestTheMonitorNeverRetargets:
    """The position exists on specific venues. Whichever pair is cheapest now
    is irrelevant to unwinding it."""

    def test_a_better_third_venue_is_ignored(self, platform):
        entry = opportunity(gross_edge_bps=20.0)
        # VENUE_C is far cheaper to buy than VENUE_A, so re-detection would
        # pick it and report a large surviving edge on a position nobody
        # holds. The two original legs have no edge left at all.
        market = snapshot_with(buy_ask=100.00, sell_bid=100.00, third_ask=98.00)
        result = monitored(platform.orchestrator, entry, market)

        assert {leg.venue for leg in result.legs} == {BUY_VENUE, SELL_VENUE}
        assert THIRD_VENUE not in {leg.venue for leg in result.legs}
        assert result.gross_edge_bps == pytest.approx(0.0, abs=0.5), (
            "the third venue leaked into the monitored economics"
        )

    def test_the_sides_are_preserved(self, platform):
        entry = opportunity()
        result = monitored(
            platform.orchestrator, entry, snapshot_with(buy_ask=100.0, sell_bid=100.3)
        )
        assert [(leg.venue, leg.side) for leg in result.legs] == [
            (leg.venue, leg.side) for leg in entry.legs
        ]

    def test_the_monitor_does_not_run_detection(self):
        """Structural. Re-detecting here would also clobber
        ``detector.active``, whose whole job is to stop one live dislocation
        being opened twice."""
        for method in (orch.Orchestrator._monitor, orch.Orchestrator._monitor_opportunity):
            source = inspect.getsource(method)
            assert "detector.detect" not in source
            assert "detector.active" not in source
            assert "self.clock" not in source

    def test_the_monitor_publishes_the_repriced_copy(self):
        source = inspect.getsource(orch.Orchestrator._monitor)
        assert "EventType.OPPORTUNITY_DETECTED" in source
        assert "ts_ms=self.tick_time" in source, (
            "the re-publish is stamped NOW, not at the opportunity's birth"
        )
        assert "payload=monitored.to_json_dict()" in source


class TestTheOriginalOpportunityIsImmutable:
    """``record.opportunity`` is the historical record of what was detected
    and what the entry decision was taken against. Attribution reads it after
    the trade closes."""

    def test_nothing_on_the_original_changes(self, platform):
        entry = opportunity(gross_edge_bps=20.0, buy_price=100.0, sell_price=100.2)
        before = entry.model_dump()
        monitored(
            platform.orchestrator, entry, snapshot_with(buy_ask=101.0, sell_bid=101.0)
        )
        assert entry.model_dump() == before

    def test_the_leg_objects_are_not_shared(self, platform):
        """``model_copy`` is shallow: a monitored copy that reused the leg
        list would mutate the original through it."""
        entry = opportunity()
        result = monitored(
            platform.orchestrator, entry, snapshot_with(buy_ask=101.0, sell_bid=101.0)
        )
        assert result.legs is not entry.legs
        for new_leg, old_leg in zip(result.legs, entry.legs, strict=True):
            assert new_leg is not old_leg

    def test_the_detail_dict_is_not_shared(self, platform):
        entry = opportunity()
        result = monitored(
            platform.orchestrator, entry, snapshot_with(buy_ask=101.0, sell_bid=101.0)
        )
        assert result.detail is not entry.detail
        assert result.reason_codes is not entry.reason_codes

    def test_identity_and_lifetime_are_carried_through(self, platform):
        """The barrier, the attribution trail and the tick-time invariant all
        key off these. An opportunity is stamped at the tick it was BORN in
        and never restamped."""
        entry = opportunity(created_at=START_MS - 3_000)
        result = monitored(
            platform.orchestrator, entry, snapshot_with(buy_ask=100.0, sell_bid=100.3)
        )
        assert result.opportunity_id == entry.opportunity_id
        assert result.correlation_id == entry.correlation_id
        assert result.created_at == entry.created_at
        assert result.expires_at == entry.expires_at
        assert result.kind is entry.kind
        assert result.strategy == entry.strategy
        assert result.symbol == entry.symbol


class TestTheMonitorFailsClosed:
    @pytest.mark.parametrize(
        "quality",
        [DataQuality.DEGRADED, DataQuality.STALE, DataQuality.UNAVAILABLE],
    )
    def test_an_unusable_leg_yields_no_monitored_opportunity(self, platform, quality):
        market = market_of(
            venue_state_from_book(make_book(BUY_VENUE, SYMBOL, 100.0)),
            venue_state_from_book(
                make_book(SELL_VENUE, SYMBOL, 100.2), quality=quality
            ),
        )
        assert monitored(platform.orchestrator, opportunity(), market) is None

    def test_a_missing_leg_yields_no_monitored_opportunity(self, platform):
        market = market_of(venue_state_from_book(make_book(BUY_VENUE, SYMBOL, 100.0)))
        assert monitored(platform.orchestrator, opportunity(), market) is None

    def test_an_empty_snapshot_yields_no_monitored_opportunity(self, platform):
        market = MarketState(created_at=START_MS, venues={})
        assert monitored(platform.orchestrator, opportunity(), market) is None

    def test_the_caller_exits_rather_than_holding_an_unpriceable_position(self):
        """Fail-closed means unwind, not "carry on with the last known
        numbers". A position whose live economics cannot be observed is not a
        position to keep holding."""
        source = inspect.getsource(orch.Orchestrator._monitor)
        head = source.split("self.barrier.expect")[0]
        assert "if monitored is None:" in head
        assert "StrategyState.EXITING" in head
        assert "self._submit_exit" in head
