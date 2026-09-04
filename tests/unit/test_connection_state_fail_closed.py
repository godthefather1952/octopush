"""TIDAL-L1: one fail-closed interpretation of connection state.

``_quality()`` used to default an unknown venue to *connected*
(``self.connected.get(book.venue, True)``) while the publicly-exposed
``VenueMarketState.connected`` field defaulted the same lookup to
*disconnected* (``self.connected.get(venue, False)``). Two different answers
to "is this venue connected" from the same dict meant a book could in
principle be judged usable internally while the state TIDAL published about
it said otherwise. The fix makes both sides agree: UNKNOWN CONNECTION STATE
!= CONNECTED, everywhere.

In the real ingestion path this is usually a no-op — ``on_snapshot`` always
populates ``self.connected`` (via ``setdefault``) before a book can even
become ``synced`` — but it closes the gap for any caller that reaches
``_quality()``/``venue_state()`` through a different path, and is the single
authoritative rule the audit asked for.
"""

from __future__ import annotations

import pytest

from agents.tidal import Tidal
from core.health import HealthRegistry
from core.models.common import DataQuality
from core.models.market import OrderBookSnapshot, PriceLevel
from tests.conftest import START_MS

VENUE = "VENUE_A"
SYMBOL = "BTC-USDT"


def snap(ts: int = START_MS, *, bid=100.0, ask=101.0) -> OrderBookSnapshot:
    return OrderBookSnapshot(
        venue=VENUE, symbol=SYMBOL, exchange_ts=ts, received_ts=ts, sequence=1,
        bids=[PriceLevel(price=bid, size=1.0)], asks=[PriceLevel(price=ask, size=1.0)],
        is_checkpoint=True,
    )


@pytest.fixture
def tidal(bus, clock, settings) -> Tidal:
    return Tidal(bus, clock, settings, HealthRegistry(clock=clock))


class TestUnknownConnectionStateIsNotFresh:
    async def test_a_book_with_no_connection_record_at_all_is_unavailable(self, tidal, clock):
        """Synthesize the otherwise-unreachable case directly: a synced book
        whose venue was never recorded in ``self.connected`` at all (not
        True, not False -- simply absent), which the old permissive default
        would have treated as connected.
        """
        await tidal.on_snapshot(snap(clock.now_ms()))
        assert tidal.venue_state(VENUE, SYMBOL).quality is DataQuality.FRESH
        del tidal.connected[VENUE]  # simulate "never heard from this venue"
        state = tidal.venue_state(VENUE, SYMBOL)
        assert state.quality is DataQuality.UNAVAILABLE
        assert state.connected is False, "the published field already used this default"


class TestExplicitDisconnectedIsUnavailable:
    async def test_disconnected_after_a_fresh_book_is_unavailable(self, tidal, clock):
        await tidal.on_snapshot(snap(clock.now_ms()))
        assert tidal.venue_state(VENUE, SYMBOL).quality is DataQuality.FRESH
        tidal.connected[VENUE] = False
        state = tidal.venue_state(VENUE, SYMBOL)
        assert state.quality is DataQuality.UNAVAILABLE
        assert state.connected is False


class TestConnectedAndSyncedIsEligibleForFresh:
    async def test_connected_plus_current_timestamps_is_fresh(self, tidal, clock):
        tidal.connected[VENUE] = True
        await tidal.on_snapshot(snap(clock.now_ms()))
        state = tidal.venue_state(VENUE, SYMBOL)
        assert state.quality is DataQuality.FRESH
        assert state.connected is True

    async def test_a_snapshot_alone_implicitly_establishes_connected(self, tidal, clock):
        """No explicit VENUE_CONNECTED event at all -- receiving real data is
        itself proof of connectivity (``on_snapshot``'s ``setdefault``).
        """
        assert VENUE not in tidal.connected
        await tidal.on_snapshot(snap(clock.now_ms()))
        assert tidal.connected[VENUE] is True
        assert tidal.venue_state(VENUE, SYMBOL).quality is DataQuality.FRESH


class TestReconnectTransitionsRemainCorrect:
    async def test_disconnect_then_reconnect_then_fresh_snapshot_recovers(self, tidal, clock):
        await tidal.on_snapshot(snap(clock.now_ms()))
        assert tidal.venue_state(VENUE, SYMBOL).quality is DataQuality.FRESH

        tidal.connected[VENUE] = False
        assert tidal.venue_state(VENUE, SYMBOL).quality is DataQuality.UNAVAILABLE

        tidal.connected[VENUE] = True
        # Reconnecting alone (no fresh data yet) must not resurrect the old
        # book's usability by itself if it was invalidated -- but this book
        # was never invalidated, only the connection flag flipped, so once
        # the flag is back and timestamps are still fresh it recovers.
        assert tidal.venue_state(VENUE, SYMBOL).quality is DataQuality.FRESH

    async def test_disconnect_invalidates_and_reconnect_requires_a_fresh_snapshot(
        self, tidal, clock
    ):
        """The realistic path: disconnection also invalidates every book on
        that venue (existing VENUE_DISCONNECTED handling), so reconnecting
        the flag alone is not enough -- a fresh snapshot is still required.
        """
        await tidal.on_snapshot(snap(clock.now_ms()))
        for (venue, _symbol), book in tidal.books.items():
            if venue == VENUE:
                book.invalidate("venue disconnected")
        tidal.connected[VENUE] = False
        assert tidal.venue_state(VENUE, SYMBOL).quality is DataQuality.UNAVAILABLE

        tidal.connected[VENUE] = True
        assert tidal.venue_state(VENUE, SYMBOL).quality is DataQuality.UNAVAILABLE, (
            "reconnecting the flag alone must not resurrect an invalidated book"
        )

        await tidal.on_snapshot(snap(clock.now_ms(), bid=100.5, ask=101.5))
        assert tidal.venue_state(VENUE, SYMBOL).quality is DataQuality.FRESH


class TestSimulatorStartupBehaviorRemainsCorrect:
    async def test_a_freshly_built_platform_reports_no_venue_as_fresh_before_any_data(
        self, platform
    ):
        """Startup must not deadlock or crash, and must not report a venue as
        usable before it has actually produced anything -- there is simply no
        state to query yet.
        """
        for venue_symbols in platform.settings.enabled_venues:
            for symbol in venue_symbols.symbols:
                assert platform.tidal.venue_state(venue_symbols.name, symbol) is None

    async def test_a_running_simulated_platform_reaches_fresh_normally(self, platform):
        from tests.conftest import run_platform

        await run_platform(platform, ticks=3)
        market = platform.state.market
        assert market is not None
        found_fresh = any(
            view.quality is DataQuality.FRESH for view in market.consolidated.values()
        )
        assert found_fresh, "ordinary simulated startup must still reach FRESH"
        await platform.stop()
