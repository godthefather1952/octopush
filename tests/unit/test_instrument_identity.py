"""Settlement-asset identity: TIDAL-C3.

The finding was that ``BTCUSDT``, ``BTCUSDC`` and ``BTCBUSD`` were all
canonicalised to ``BTC-USD``, so a Binance USDT quote and a Coinbase USD quote
looked like one instrument on two venues. The USDT/USD basis then read as a
bitcoin cross-venue dislocation, above a 4 bps detection threshold, with no
model of the stablecoin exposure the resulting position actually carried.

These tests pin the corrected identity from both directions: different quote
assets must never be compared, and identical ones must still be.
"""

from __future__ import annotations

import pytest

from agents.tidal import Tidal
from core.config import default_venues, load_settings, simulated_venues
from core.health import HealthRegistry
from core.models.common import DataQuality
from strategies.cross_venue.detector import CrossVenueDetector, find_dislocation
from tests.conftest import START_MS, make_book, venue_state_from_book
from venues.venue_a import parser as parser_a
from venues.venue_b import parser as parser_b


def state(venue: str, symbol: str, mid: float, **kwargs):
    return venue_state_from_book(make_book(venue, symbol, mid, **kwargs))


# ======================================================================
# C. Venue-specific subscriptions
# ======================================================================


class TestLiveVenueSubscriptions:
    def test_binance_subscribes_to_its_usdt_markets(self):
        venue = next(v for v in default_venues() if v.name == "VENUE_A")
        assert venue.symbols == ["BTC-USDT", "ETH-USDT"]
        names = parser_a.stream_names(venue.symbols)
        assert names == [
            "btcusdt@depth@100ms",
            "btcusdt@trade",
            "ethusdt@depth@100ms",
            "ethusdt@trade",
        ]

    def test_coinbase_subscribes_to_its_usd_markets(self):
        venue = next(v for v in default_venues() if v.name == "VENUE_B")
        assert venue.symbols == ["BTC-USD", "ETH-USD"]
        message = parser_b.subscribe_message(venue.symbols)
        assert message["product_ids"] == ["BTC-USD", "ETH-USD"]

    def test_the_live_venues_share_no_instrument(self):
        """The honest consequence of the fix, asserted so it stays deliberate.

        If a future change makes these overlap it should be because the venues
        genuinely list the same instrument, not because a formatter rewrote a
        quote again.
        """
        venues = {v.name: set(v.symbols) for v in default_venues()}
        assert venues["VENUE_A"] & venues["VENUE_B"] == set()

    def test_simulated_venues_share_every_instrument(self):
        venues = {v.name: set(v.symbols) for v in simulated_venues()}
        assert venues["VENUE_A"] == venues["VENUE_B"] == {"BTC-USD", "ETH-USD"}


class TestSettingsUniverse:
    def test_the_strategy_universe_is_the_union_of_venue_symbols(self, monkeypatch):
        monkeypatch.setenv("TF_FEED", "live")
        settings = load_settings()
        assert settings.symbols == ["BTC-USD", "BTC-USDT", "ETH-USD", "ETH-USDT"]

    def test_simulated_universe_is_the_shared_pair(self, monkeypatch):
        monkeypatch.delenv("TF_FEED", raising=False)
        settings = load_settings()
        assert settings.symbols == ["BTC-USD", "ETH-USD"]

    def test_an_explicit_universe_still_wins(self, monkeypatch):
        monkeypatch.setenv("TF_SYMBOLS", '["BTC-USD"]')
        settings = load_settings()
        assert settings.symbols == ["BTC-USD"]


# ======================================================================
# D. Adapter wiring
# ======================================================================


class TestAdapterWiring:
    def test_each_adapter_gets_its_own_venue_symbols(self, monkeypatch, clock, bus, store):
        """Not the global universe — that is what forced the substitution."""
        from apps.orchestrator.wiring import build_platform

        monkeypatch.setenv("TF_FEED", "live")
        settings = load_settings()
        platform = build_platform(settings, clock=clock, bus=bus, store=store)

        assert platform.adapters["VENUE_A"].symbols == ["BTC-USDT", "ETH-USDT"]
        assert platform.adapters["VENUE_B"].symbols == ["BTC-USD", "ETH-USD"]
        # The universe is wider than either venue; neither adapter inherited it.
        assert settings.symbols != platform.adapters["VENUE_A"].symbols
        assert settings.symbols != platform.adapters["VENUE_B"].symbols

    def test_the_binance_url_asks_for_the_configured_markets(
        self, monkeypatch, clock, bus, store
    ):
        from apps.orchestrator.wiring import build_platform

        monkeypatch.setenv("TF_FEED", "live")
        platform = build_platform(load_settings(), clock=clock, bus=bus, store=store)
        url = platform.adapters["VENUE_A"].connect_url()
        assert "btcusdt@depth@100ms" in url
        assert "ethusdt@depth@100ms" in url
        # The USD markets belong to the other venue and must not appear here.
        assert "btcusd@" not in url

    def test_simulated_wiring_is_unchanged(self, platform):
        for adapter in platform.adapters.values():
            assert adapter.symbols == ["BTC-USD", "ETH-USD"]


# ======================================================================
# E. No false cross-quote arbitrage
# ======================================================================


class TestNoCrossQuoteComparison:
    @pytest.mark.parametrize(
        ("usdt_mid", "usd_mid", "label"),
        [
            (100_050.0, 100_000.0, "a plausible 5 bps basis"),
            (100_000.0, 100_050.0, "the same basis, reversed"),
            (101_000.0, 100_000.0, "a 100 bps gap"),
            (100_000.0, 110_000.0, "a 1000 bps gap — a depeg, not an edge"),
        ],
    )
    def test_usdt_and_usd_never_form_a_dislocation(self, usdt_mid, usd_mid, label):
        """Size of the price difference is irrelevant. They are not one asset."""
        states = [
            state("VENUE_A", "BTC-USDT", usdt_mid),
            state("VENUE_B", "BTC-USD", usd_mid),
        ]
        # Each instrument is considered on its own, which is the only way a
        # detector that groups by symbol can consider them.
        for symbol in ("BTC-USDT", "BTC-USD"):
            same = [s for s in states if s.symbol == symbol]
            assert find_dislocation(symbol, same, None) is None, label

    def test_the_detector_finds_nothing_across_quotes(self, settings, clock):
        """End to end through the real detector and a real MarketState."""
        tidal = Tidal(None, clock, settings, HealthRegistry(clock=clock))  # type: ignore[arg-type]
        tidal.books.clear()
        market = _market_with(
            tidal,
            state("VENUE_A", "BTC-USDT", 100_050.0),
            state("VENUE_B", "BTC-USD", 100_000.0),
        )
        detector = CrossVenueDetector(
            settings.model_copy(update={"symbols": ["BTC-USD", "BTC-USDT"]}), clock
        )
        assert detector.detect(market, clock.now_ms()) == []

    def test_a_usdt_only_universe_produces_no_opportunity_from_one_venue(
        self, settings, clock
    ):
        tidal = Tidal(None, clock, settings, HealthRegistry(clock=clock))  # type: ignore[arg-type]
        market = _market_with(tidal, state("VENUE_A", "BTC-USDT", 100_050.0))
        detector = CrossVenueDetector(
            settings.model_copy(update={"symbols": ["BTC-USDT"]}), clock
        )
        assert detector.detect(market, clock.now_ms()) == []


# ======================================================================
# F. Same-instrument comparison preserved
# ======================================================================


class TestSameInstrumentStillCompares:
    @pytest.mark.parametrize("symbol", ["BTC-USD", "BTC-USDT"])
    def test_two_venues_on_one_instrument_still_produce_a_dislocation(self, symbol):
        states = [
            state("VENUE_A", symbol, 100_000.0),
            state("VENUE_B", symbol, 100_200.0),
        ]
        dislocation = find_dislocation(symbol, states, None)
        assert dislocation is not None, "the fix must not disable cross-venue trading"
        assert dislocation.buy_venue == "VENUE_A"
        assert dislocation.sell_venue == "VENUE_B"
        assert dislocation.gross_edge_bps > 0

    @pytest.mark.parametrize("symbol", ["BTC-USD", "BTC-USDT"])
    def test_the_detector_emits_the_opportunity(self, settings, clock, symbol):
        tidal = Tidal(None, clock, settings, HealthRegistry(clock=clock))  # type: ignore[arg-type]
        market = _market_with(
            tidal,
            state("VENUE_A", symbol, 100_000.0),
            state("VENUE_B", symbol, 100_200.0),
        )
        detector = CrossVenueDetector(
            settings.model_copy(update={"symbols": [symbol]}), clock
        )
        opportunities = detector.detect(market, clock.now_ms())
        assert len(opportunities) == 1
        assert opportunities[0].symbol == symbol
        assert {leg.venue for leg in opportunities[0].legs} == {"VENUE_A", "VENUE_B"}

    def test_the_rule_is_base_and_quote_together(self):
        """Same base, different quote: no. Same base and quote: yes."""
        mixed = [
            state("VENUE_A", "BTC-USDT", 100_000.0),
            state("VENUE_B", "BTC-USD", 100_200.0),
        ]
        matched = [
            state("VENUE_A", "BTC-USDT", 100_000.0),
            state("VENUE_B", "BTC-USDT", 100_200.0),
        ]
        assert find_dislocation("BTC-USDT", mixed, None) is None
        assert find_dislocation("BTC-USDT", matched, None) is not None


# ======================================================================
# G. Market-state identity
# ======================================================================


def _market_with(tidal: Tidal, *states):
    """A MarketState assembled from ready-made venue states."""
    from core.models.market import MarketState

    venues = {f"{s.venue}:{s.symbol}": s for s in states}
    consolidated = {}
    for symbol in sorted({s.symbol for s in states}):
        group = [s for s in venues.values() if s.symbol == symbol]
        consolidated[symbol] = tidal.consolidate(symbol, group)
    return MarketState(
        created_at=START_MS,
        source_data_timestamp=START_MS,
        venues=venues,
        consolidated=consolidated,
    )


class TestMarketStateIdentity:
    async def test_the_two_instruments_coexist_without_overwriting(
        self, settings, clock, bus
    ):
        tidal = Tidal(bus, clock, settings, HealthRegistry(clock=clock))
        await tidal.on_snapshot(make_book("VENUE_A", "BTC-USDT", 100_050.0))
        await tidal.on_snapshot(make_book("VENUE_A", "ETH-USDT", 3_000.0))
        await tidal.on_snapshot(make_book("VENUE_B", "BTC-USD", 100_000.0))
        await tidal.on_snapshot(make_book("VENUE_B", "ETH-USD", 2_995.0))

        assert set(tidal.books) == {
            ("VENUE_A", "BTC-USDT"),
            ("VENUE_A", "ETH-USDT"),
            ("VENUE_B", "BTC-USD"),
            ("VENUE_B", "ETH-USD"),
        }
        market = tidal.build_state()
        assert set(market.venues) == {
            "VENUE_A:BTC-USDT",
            "VENUE_A:ETH-USDT",
            "VENUE_B:BTC-USD",
            "VENUE_B:ETH-USD",
        }
        # The real canonical symbol is what each state reports.
        assert market.venues["VENUE_A:BTC-USDT"].symbol == "BTC-USDT"
        assert market.venues["VENUE_B:BTC-USD"].symbol == "BTC-USD"
        # Distinct prices survive: neither book overwrote the other.
        a = market.venues["VENUE_A:BTC-USDT"].metrics.mid
        b = market.venues["VENUE_B:BTC-USD"].metrics.mid
        assert a is not None and b is not None and a != b

    async def test_every_received_instrument_appears_in_consolidated_state(
        self, settings, clock, bus
    ):
        """Truthfulness: a book that exists must be visible, even alone."""
        tidal = Tidal(bus, clock, settings, HealthRegistry(clock=clock))
        await tidal.on_snapshot(make_book("VENUE_A", "BTC-USDT", 100_050.0))
        await tidal.on_snapshot(make_book("VENUE_B", "BTC-USD", 100_000.0))

        market = tidal.build_state()
        assert set(market.consolidated) == {"BTC-USDT", "BTC-USD"}
        for symbol, view in market.consolidated.items():
            assert view.symbol == symbol
            # One contributing venue each: honest, and not usable for a
            # cross-venue trade.
            assert view.usable_venues == [
                "VENUE_A" if symbol == "BTC-USDT" else "VENUE_B"
            ]
            assert view.quality is not DataQuality.FRESH

    async def test_no_consolidated_view_mixes_quote_assets(self, settings, clock, bus):
        tidal = Tidal(bus, clock, settings, HealthRegistry(clock=clock))
        await tidal.on_snapshot(make_book("VENUE_A", "BTC-USDT", 100_050.0))
        await tidal.on_snapshot(make_book("VENUE_B", "BTC-USD", 100_000.0))
        market = tidal.build_state()

        for symbol, view in market.consolidated.items():
            contributors = [
                s for s in market.venues.values() if s.venue in (view.usable_venues or [])
            ]
            assert all(s.symbol == symbol for s in contributors)


# ======================================================================
# 13. The safety statement this batch exists to prove
# ======================================================================


class TestLiveFeedCannotAliasInstruments:
    """"Enabling TF_FEED=live cannot cause Binance BTC-USDT to be presented to
    the strategy as Coinbase BTC-USD."

    Proved along the whole path a symbol travels: what is subscribed, what the
    parser returns, and what the strategy layer is given.
    """

    def test_the_subscribed_markets_are_distinct(self, monkeypatch):
        monkeypatch.setenv("TF_FEED", "live")
        settings = load_settings()
        by_venue = {v.name: v.symbols for v in settings.enabled_venues}
        assert by_venue["VENUE_A"] == ["BTC-USDT", "ETH-USDT"]
        assert by_venue["VENUE_B"] == ["BTC-USD", "ETH-USD"]

    def test_the_binance_parser_reports_usdt(self):
        """Whatever the configuration says, the wire says USDT."""
        delta = parser_a.parse_depth_update(
            {"e": "depthUpdate", "E": START_MS, "s": "BTCUSDT", "U": 1, "u": 2},
            START_MS,
        )
        assert delta.symbol == "BTC-USDT"
        assert delta.symbol != "BTC-USD"

    def test_no_formatting_path_turns_usd_into_usdt(self):
        from venues.base.symbols import denormalize

        for style in ("concat", "dash", "slash"):
            assert "USDT" not in denormalize("BTC-USD", style)

    async def test_a_live_shaped_state_offers_the_strategy_no_cross_venue_pair(
        self, monkeypatch, clock, bus
    ):
        """The end of the path: two live-shaped books, no opportunity."""
        monkeypatch.setenv("TF_FEED", "live")
        settings = load_settings()
        tidal = Tidal(bus, clock, settings, HealthRegistry(clock=clock))
        # Exactly what the live venues would deliver, at a realistic basis.
        await tidal.on_snapshot(make_book("VENUE_A", "BTC-USDT", 100_050.0))
        await tidal.on_snapshot(make_book("VENUE_B", "BTC-USD", 100_000.0))
        await tidal.on_snapshot(make_book("VENUE_A", "ETH-USDT", 3_001.5))
        await tidal.on_snapshot(make_book("VENUE_B", "ETH-USD", 3_000.0))

        market = tidal.build_state()
        detector = CrossVenueDetector(settings, clock)
        assert detector.detect(market, clock.now_ms()) == [], (
            "a live feed must not manufacture a cross-venue pair out of two "
            "different settlement assets"
        )
        # And no consolidated view claims two venues.
        for view in market.consolidated.values():
            assert len(view.usable_venues or []) <= 1
