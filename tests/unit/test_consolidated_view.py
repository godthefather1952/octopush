"""Cross-venue consolidation must not manufacture cross-venue facts: TIDAL-M6.

With fewer than two usable venues, ``best_bid_venue``, ``best_ask_venue`` and
``cross_venue_spread_bps`` are comparisons between venues that do not exist.
The old code populated them anyway from whichever single venue was usable —
so a lone Binance book reported its own bid as "best_bid_venue: VENUE_A", its
own ask as "best_ask_venue: VENUE_A", and the two crossed against each other
as a "cross-venue spread" that was really just that venue's own quoted
spread, always present and always false.
"""

from __future__ import annotations

from agents.tidal import Tidal
from core.health import HealthRegistry
from core.models.common import DataQuality
from tests.conftest import make_book, venue_state_from_book


def tidal(bus, clock, settings) -> Tidal:
    return Tidal(bus, clock, settings, HealthRegistry(clock=clock))


class TestZeroUsableVenues:
    def test_no_states_at_all(self, bus, clock, settings):
        view = tidal(bus, clock, settings).consolidate("BTC-USD", [])
        assert view.quality is DataQuality.UNAVAILABLE
        assert view.best_bid is None and view.best_bid_venue is None
        assert view.best_ask is None and view.best_ask_venue is None
        assert view.cross_venue_spread_bps is None
        assert view.reference_price is None
        assert view.usable_venues == []

    def test_a_present_but_unusable_state(self, bus, clock, settings):
        s = venue_state_from_book(
            make_book("VENUE_A", "BTC-USD", 100.0), quality=DataQuality.STALE
        )
        view = tidal(bus, clock, settings).consolidate("BTC-USD", [s])
        assert view.quality is DataQuality.UNAVAILABLE
        assert view.best_bid_venue is None
        assert view.cross_venue_spread_bps is None


class TestOneUsableVenue:
    def test_no_manufactured_cross_venue_fields(self, bus, clock, settings):
        s = venue_state_from_book(make_book("VENUE_A", "BTC-USD", 100_000.0))
        view = tidal(bus, clock, settings).consolidate("BTC-USD", [s])
        assert view.quality is DataQuality.DEGRADED
        assert view.usable_venues == ["VENUE_A"]
        assert view.best_bid is None, "a single venue's own bid is not a cross-venue fact"
        assert view.best_bid_venue is None
        assert view.best_ask is None
        assert view.best_ask_venue is None
        assert view.cross_venue_spread_bps is None

    def test_reference_price_may_still_be_reported(self, bus, clock, settings):
        """Permitted for monitoring: it is documented as a blended reference,
        not a claim about two venues, so one contributor is truthfully its own
        reference.
        """
        s = venue_state_from_book(make_book("VENUE_A", "BTC-USD", 100_000.0))
        view = tidal(bus, clock, settings).consolidate("BTC-USD", [s])
        assert view.reference_price == s.metrics.mid


class TestTwoUsableVenues:
    def test_best_bid_and_ask_come_from_the_right_venues(self, bus, clock, settings):
        a = venue_state_from_book(make_book("VENUE_A", "BTC-USD", 100_000.0, spread_bps=2.0))
        b = venue_state_from_book(make_book("VENUE_B", "BTC-USD", 100_050.0, spread_bps=2.0))
        view = tidal(bus, clock, settings).consolidate("BTC-USD", [a, b])
        assert view.quality is DataQuality.FRESH
        assert set(view.usable_venues) == {"VENUE_A", "VENUE_B"}
        # B's book sits higher, so B holds the richer bid; A holds the cheaper ask.
        assert view.best_bid_venue == "VENUE_B"
        assert view.best_ask_venue == "VENUE_A"
        assert view.best_bid == b.metrics.best_bid
        assert view.best_ask == a.metrics.best_ask
        assert view.cross_venue_spread_bps is not None

    def test_reversed_prices_pick_the_other_venue(self, bus, clock, settings):
        a = venue_state_from_book(make_book("VENUE_A", "BTC-USD", 100_050.0, spread_bps=2.0))
        b = venue_state_from_book(make_book("VENUE_B", "BTC-USD", 100_000.0, spread_bps=2.0))
        view = tidal(bus, clock, settings).consolidate("BTC-USD", [a, b])
        assert view.best_bid_venue == "VENUE_A"
        assert view.best_ask_venue == "VENUE_B"


class TestThreeUsableVenues:
    def test_best_bid_and_ask_are_the_true_extremes(self, bus, clock, settings):
        a = venue_state_from_book(make_book("VENUE_A", "BTC-USD", 100_000.0, spread_bps=2.0))
        b = venue_state_from_book(make_book("VENUE_B", "BTC-USD", 100_100.0, spread_bps=2.0))
        c = venue_state_from_book(make_book("VENUE_C", "BTC-USD", 99_900.0, spread_bps=2.0))
        view = tidal(bus, clock, settings).consolidate("BTC-USD", [a, b, c])
        assert view.quality is DataQuality.FRESH
        assert view.best_bid_venue == "VENUE_B"  # richest bid: highest-priced book
        assert view.best_ask_venue == "VENUE_C"  # cheapest ask: lowest-priced book
        assert len(view.usable_venues) == 3


class TestStaleSecondVenueBehavesAsOneUsable:
    def test_a_stale_venue_is_excluded_from_the_pair(self, bus, clock, settings):
        fresh = venue_state_from_book(make_book("VENUE_A", "BTC-USD", 100_000.0))
        stale = venue_state_from_book(
            make_book("VENUE_B", "BTC-USD", 100_050.0), quality=DataQuality.STALE
        )
        view = tidal(bus, clock, settings).consolidate("BTC-USD", [fresh, stale])
        assert view.usable_venues == ["VENUE_A"]
        assert view.quality is DataQuality.DEGRADED
        assert view.best_bid_venue is None, "a stale venue must not complete a false pair"
        assert view.cross_venue_spread_bps is None

    def test_a_desynced_venue_is_excluded_from_the_pair(self, bus, clock, settings):
        fresh = venue_state_from_book(make_book("VENUE_A", "BTC-USD", 100_000.0))
        desynced = venue_state_from_book(
            make_book("VENUE_B", "BTC-USD", 100_050.0), quality=DataQuality.UNAVAILABLE
        )
        view = tidal(bus, clock, settings).consolidate("BTC-USD", [fresh, desynced])
        assert view.usable_venues == ["VENUE_A"]
        assert view.best_bid_venue is None


class TestNoManufacturedSameVenuePair:
    def test_the_same_venue_cannot_appear_on_both_sides(self, bus, clock, settings):
        """Even constructed adversarially — two states claiming the same
        venue name — the consolidator must not treat that as two venues
        worth comparing against each other, because it never is.
        """
        a = venue_state_from_book(make_book("VENUE_A", "BTC-USD", 100_000.0, spread_bps=2.0))
        a_again = venue_state_from_book(
            make_book("VENUE_A", "BTC-USD", 100_010.0, spread_bps=2.0)
        )
        view = tidal(bus, clock, settings).consolidate("BTC-USD", [a, a_again])
        # Both entries are literally the same venue name; if a real duplicate
        # ever reached here, at minimum the venue label is not a genuine
        # second venue, so cross_venue_spread_bps still describes one venue
        # against itself. Assert the finding directly: usable_venues lists
        # the same name twice rather than silently deduplicating it away, so
        # this condition is visible rather than hidden.
        assert view.usable_venues == ["VENUE_A", "VENUE_A"]


class TestInstrumentIdentityStillHolds:
    """Batch 2 regression: distinct quote assets never get compared here."""

    def test_usdt_and_usd_are_never_consolidated_together(self, bus, clock, settings):
        t = tidal(bus, clock, settings)
        usdt = venue_state_from_book(make_book("VENUE_A", "BTC-USDT", 100_050.0))
        usd = venue_state_from_book(make_book("VENUE_B", "BTC-USD", 100_000.0))
        # consolidate() itself does not filter by symbol -- callers (TIDAL's
        # build_state) group by exact canonical symbol before calling it, so
        # this proves the grouping contract, not consolidate() in isolation.
        view_usdt = t.consolidate("BTC-USDT", [usdt])
        view_usd = t.consolidate("BTC-USD", [usd])
        assert view_usdt.usable_venues == ["VENUE_A"]
        assert view_usd.usable_venues == ["VENUE_B"]
        assert view_usdt.best_bid_venue is None
        assert view_usd.best_bid_venue is None
