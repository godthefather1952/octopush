"""The live public venue endpoints, pinned: P12-F2 / P12-F3.

WHY THESE ARE PINNED
====================
Two of these values were changed on the strength of field evidence, and both
are the kind of value that gets "tidied" later by someone who does not know
what it cost to establish.

VENUE_A moved to Binance.US because the global Binance public endpoints answer
HTTP 451 from the environment this platform is rehearsed in. That was not
routed around — no proxy, no mirror, no VPN, no undocumented endpoint — and the
replacement was adopted only after a race-free public probe corroborated every
semantic the adapter depends on, including a trade delivered on the stream
while public REST independently showed that trade occurring.

VENUE_B's storage ceiling is 50,000 because Coinbase's level-2 subscription
delivers the entire order book and a live session measured 21,203 levels on one
side. That is now field-validated end to end.

WHAT MUST NOT DRIFT
===================
The symbol lists above all. VENUE_A settles in USDT, VENUE_B settles in USD,
and they are different instruments. Rewriting one into the other manufactures
a cross-venue pair out of two markets that do not share a settlement asset —
the TIDAL-C3 defect this platform already removed once. A live run finding no
cross-venue opportunity is the correct result, not a bug to configure away.
"""

from __future__ import annotations

from core.config import default_venues, simulated_venues

BINANCE_US_WS = "wss://stream.binance.us:9443/stream"
BINANCE_US_REST = "https://api.binance.us"
COINBASE_WS = "wss://ws-feed.exchange.coinbase.com"
COINBASE_REST = "https://api.exchange.coinbase.com"


def live(name: str):
    return next(v for v in default_venues() if v.name == name)


class TestVenueAEndpoints:
    def test_websocket_is_the_binance_us_combined_stream(self):
        assert live("VENUE_A").ws_url == BINANCE_US_WS

    def test_rest_is_the_binance_us_public_api(self):
        assert live("VENUE_A").rest_url == BINANCE_US_REST

    def test_the_blocked_global_endpoints_are_no_longer_configured(self):
        """They answer HTTP 451 from the rehearsal environment."""
        config = live("VENUE_A")
        assert "stream.binance.com" not in (config.ws_url or "")
        assert "api.binance.com" not in (config.rest_url or "")

    def test_the_adapter_is_unchanged(self):
        assert live("VENUE_A").adapter == "binance_public"

    def test_fees_and_latency_are_unchanged(self):
        config = live("VENUE_A")
        assert config.fees.maker_bps == 1.0
        assert config.fees.taker_bps == 5.0
        assert config.latency_ms == 35


class TestSymbolIntegrity:
    """USD and USDT are different instruments. This is the one that matters."""

    def test_venue_a_symbols_are_usdt(self):
        assert live("VENUE_A").symbols == ["BTC-USDT", "ETH-USDT"]

    def test_venue_b_carries_its_usd_and_usdt_markets(self):
        """Coinbase lists both, and a public probe confirmed it serves both."""
        assert live("VENUE_B").symbols == [
            "BTC-USD",
            "ETH-USD",
            "BTC-USDT",
            "ETH-USDT",
        ]

    def test_the_two_venues_overlap_on_exactly_the_usdt_pairs(self):
        """P13-B1. The overlap exists because both venues genuinely list these.

        This assertion replaced an earlier one requiring *no* overlap. That
        was right while VENUE_B carried only USD markets; it would be wrong
        now, and weakening it to "some overlap" would lose the point. The
        exact set is what matters.
        """
        overlap = set(live("VENUE_A").symbols) & set(live("VENUE_B").symbols)
        assert overlap == {"BTC-USDT", "ETH-USDT"}

    def test_the_usd_markets_remain_single_venue(self):
        """They are Coinbase's own, and are not duplicates of the USDT pairs."""
        assert {"BTC-USD", "ETH-USD"}.isdisjoint(live("VENUE_A").symbols)

    def test_a_venue_lists_a_settlement_asset_only_where_it_really_trades_it(self):
        """The replacement for the old one-quote-per-venue rule.

        That rule encoded an accident of the configuration rather than a
        property of the venues: Coinbase genuinely trades both USD and USDT
        markets, so requiring one settlement asset per venue would now force
        the configuration to lie. What must stay true is narrower and real —
        every configured symbol names an instrument the venue actually lists,
        and USD is never written where USDT is meant.
        """
        listings = {config.name: set(config.symbols) for config in default_venues()}
        # Binance.US is configured for its USDT markets only.
        assert listings["VENUE_A"] == {"BTC-USDT", "ETH-USDT"}
        # Coinbase for both, which is what it lists.
        assert listings["VENUE_B"] == {"BTC-USD", "ETH-USD", "BTC-USDT", "ETH-USDT"}
        # And no canonical symbol is ever spelled two ways.
        for symbols in listings.values():
            for symbol in symbols:
                base, quote = symbol.split("-", 1)
                assert quote in {"USD", "USDT"}
                assert f"{base}-{quote}" == symbol


class TestVenueBEndpointsAndCeiling:
    def test_coinbase_endpoints_are_unchanged(self):
        config = live("VENUE_B")
        assert config.ws_url == COINBASE_WS
        assert config.rest_url == COINBASE_REST

    def test_the_full_book_storage_ceiling_is_intact(self):
        """P12-F3, now field-validated. Do not lower this without evidence."""
        assert live("VENUE_B").max_book_levels_per_side == 50_000

    def test_the_adapter_is_unchanged(self):
        assert live("VENUE_B").adapter == "coinbase_public"


class TestStorageAndTransportBoundsAcrossVenues:
    def test_venue_a_keeps_the_conservative_generic_ceiling(self):
        assert live("VENUE_A").max_book_levels_per_side == 10_000

    def test_simulated_venues_keep_the_generic_ceiling(self):
        for config in simulated_venues():
            assert config.max_book_levels_per_side == 10_000

    def test_every_live_venue_keeps_the_finite_transport_bound(self):
        """P12-F1. Finite, 8 MiB, and never None."""
        for config in default_venues():
            assert config.ws_max_message_bytes == 8_388_608
            assert isinstance(config.ws_max_message_bytes, int)

    def test_book_depth_levels_is_unchanged(self):
        for config in default_venues():
            assert config.book_depth_levels == 25


class TestPaperOnlyBoundaryAtTheVenueLayer:
    def test_no_live_venue_configures_a_private_or_authenticated_url(self):
        for config in default_venues():
            for url in (config.ws_url or "", config.rest_url or ""):
                lowered = url.lower()
                for token in ("key", "secret", "token", "sign", "@", "userdata"):
                    assert token not in lowered, (
                        f"{config.name} URL looks non-public: {url}"
                    )

    def test_every_configured_adapter_declares_no_trading_capability(self):
        from core.clock import ManualClock
        from venues.registry import build_adapter

        for config in default_venues():
            adapter = build_adapter(config, ManualClock(0), list(config.symbols))
            assert adapter.capabilities.authenticated is False
            assert adapter.capabilities.order_submission is False
            assert adapter.capabilities.public_market_data is True

    def test_the_registry_refuses_an_adapter_claiming_trading_capability(self):
        """The structural guard, still armed after the endpoint change."""
        import pytest

        from core.clock import ManualClock
        from venues.base.adapter import VenueCapabilities
        from venues.registry import ADAPTERS, build_adapter

        class TradingAdapter(ADAPTERS["binance_public"]):  # type: ignore[misc]
            capabilities = VenueCapabilities(
                public_market_data=True, order_submission=True, authenticated=True
            )

        original = ADAPTERS["binance_public"]
        ADAPTERS["binance_public"] = TradingAdapter
        try:
            with pytest.raises(RuntimeError, match="trading capability"):
                build_adapter(live("VENUE_A"), ManualClock(0), ["BTC-USDT"])
        finally:
            ADAPTERS["binance_public"] = original
