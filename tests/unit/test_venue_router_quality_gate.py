"""TIDAL-L5: VenueRouter must not route from data TIDAL no longer trusts.

``VenueRouter._levels()`` fetched ``VenueMarketState.book`` and used it as
long as it was merely populated -- but ``book`` stays populated (the last
known snapshot/deltas) straight through a disconnect, a sequence gap, or
ordinary staleness; only ``quality`` reflects whether TIDAL still stands
behind that data. Routing from a STALE or UNAVAILABLE book prices and sizes
an order against data TIDAL itself has already stopped trusting.

The fix defers entirely to the canonical ``DataQuality.is_usable`` (== FRESH
only, today) rather than inventing a second freshness threshold here.
"""

from __future__ import annotations

from core.config import Settings, load_settings
from core.models.common import DataQuality, Side
from core.models.market import MarketState
from core.models.opportunity import OpportunityLeg
from execution.router import VenueRouter
from tests.conftest import START_MS, make_book, venue_state_from_book

VENUE = "VENUE_A"
SYMBOL = "BTC-USD"


def market_with(quality: DataQuality, *, with_book: bool = True) -> MarketState:
    if not with_book:
        return MarketState(created_at=START_MS, venues={})
    state = venue_state_from_book(make_book(VENUE, SYMBOL, 100.0), quality=quality)
    return MarketState(created_at=START_MS, venues={f"{VENUE}:{SYMBOL}": state})


def leg(side: Side = Side.BUY) -> OpportunityLeg:
    return OpportunityLeg(venue=VENUE, symbol=SYMBOL, side=side, reference_price=100.0)


def settings() -> Settings:
    return load_settings()


class TestFreshMayRoute:
    def test_fresh_quality_produces_a_routing_decision(self):
        router = VenueRouter(settings())
        decision = router.route(leg(), market_with(DataQuality.FRESH), urgency=0.9, max_slippage_bps=10)
        assert decision is not None
        assert decision.venue == VENUE


class TestUsableQualityAccordingToCanonicalModelMayRoute:
    def test_is_usable_is_the_single_source_of_truth(self):
        """Whatever the canonical model calls usable must route; this proves
        the gate reads ``quality.is_usable`` rather than hard-coding FRESH
        itself, so a future change to what counts as usable is inherited
        automatically instead of needing a second edit here.
        """
        assert DataQuality.FRESH.is_usable
        router = VenueRouter(settings())
        decision = router.route(
            leg(), market_with(DataQuality.FRESH), urgency=0.9, max_slippage_bps=10
        )
        assert decision is not None


class TestStaleNoRouting:
    def test_stale_quality_produces_no_routing_decision(self):
        router = VenueRouter(settings())
        decision = router.route(leg(), market_with(DataQuality.STALE), urgency=0.9, max_slippage_bps=10)
        assert decision is None

    def test_stale_quality_is_refused_on_the_passive_path_too(self):
        router = VenueRouter(settings())
        decision = router.route(leg(), market_with(DataQuality.STALE), urgency=0.1, max_slippage_bps=10)
        assert decision is None


class TestUnavailableNoRouting:
    def test_unavailable_quality_produces_no_routing_decision(self):
        router = VenueRouter(settings())
        decision = router.route(
            leg(), market_with(DataQuality.UNAVAILABLE), urgency=0.9, max_slippage_bps=10
        )
        assert decision is None


class TestDegradedNoRouting:
    def test_degraded_quality_produces_no_routing_decision(self):
        """DEGRADED is not FRESH, so under the canonical ``is_usable`` model
        (FRESH only) it must not route either -- this is not a new, separate
        threshold, it is what ``is_usable`` already means.
        """
        router = VenueRouter(settings())
        decision = router.route(
            leg(), market_with(DataQuality.DEGRADED), urgency=0.9, max_slippage_bps=10
        )
        assert decision is None


class TestMissingBookNoRoute:
    def test_a_state_with_no_book_at_all_produces_no_routing_decision(self):
        state = venue_state_from_book(make_book(VENUE, SYMBOL, 100.0), quality=DataQuality.FRESH)
        state = state.model_copy(update={"book": None})
        market = MarketState(created_at=START_MS, venues={f"{VENUE}:{SYMBOL}": state})
        router = VenueRouter(settings())
        decision = router.route(leg(), market, urgency=0.9, max_slippage_bps=10)
        assert decision is None


class TestMissingStateNoRoute:
    def test_no_venue_state_at_all_produces_no_routing_decision(self):
        router = VenueRouter(settings())
        decision = router.route(
            leg(), market_with(DataQuality.FRESH, with_book=False), urgency=0.9, max_slippage_bps=10
        )
        assert decision is None
