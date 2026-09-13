"""The live venues share a real instrument: P13-B1.

THE BLOCKER
===========
The Phase 13 audit proved, from code rather than from a field log, that the
cross-venue strategy could never fire on live data. ``find_dislocation``
filters to states whose ``symbol`` matches exactly and needs two of them from
different venues; the live configuration gave four canonical symbols with one
contributor each. No overlap, therefore no dislocation, therefore no
opportunity, no plan, no paper order and nothing to measure.

That was not a defect. Binance settles in USDT and Coinbase settled in USD
here, and BTC-USD and BTC-USDT are different instruments — conflating them is
the TIDAL-C3 defect the platform removed once already.

THE REMEDIATION, AND WHY IT IS HONEST
=====================================
Coinbase also lists BTC-USDT and ETH-USDT. A public protocol probe against the
already-configured endpoint, channels and parser confirmed it serves them:
both products returned a full snapshot, live ``l2update`` messages and public
trade prints, with zero exchange errors and zero parser failures.

So the overlap comes from two venues genuinely listing the same instrument.
Nothing was renamed, aliased or substituted, and the USD markets stay exactly
as they were — real markets, single-contributor by design.

These tests prove the structural consequence: the detector can now find a
BTC-USDT dislocation across two venues, and still cannot find one for BTC-USD,
because only one venue lists it.
"""

from __future__ import annotations

from collections import defaultdict

import pytest

from core.clock import ManualClock
from core.config import default_venues, load_settings
from strategies.cross_venue.detector import find_dislocation
from tests.conftest import make_book, venue_state_from_book
from venues.base.symbols import denormalize
from venues.registry import build_adapter
from venues.venue_b import parser as parser_b

OVERLAP = {"BTC-USDT", "ETH-USDT"}
SINGLE_VENUE = {"BTC-USD", "ETH-USD"}


def live(name: str):
    return next(v for v in default_venues() if v.name == name)


def contributors() -> dict[str, list[str]]:
    """Canonical symbol -> the venues configured to quote it."""
    out: dict[str, list[str]] = defaultdict(list)
    for venue in default_venues():
        for symbol in venue.symbols:
            out[symbol].append(venue.name)
    return dict(out)


def state(venue: str, symbol: str, mid: float, **kwargs):
    return venue_state_from_book(make_book(venue, symbol, mid, **kwargs))


# ======================================================================
# A / B / C / D — the configured universe
# ======================================================================


class TestConfiguredUniverse:
    def test_venue_b_lists_its_usd_and_usdt_markets(self):
        assert live("VENUE_B").symbols == [
            "BTC-USD",
            "ETH-USD",
            "BTC-USDT",
            "ETH-USDT",
        ]

    def test_venue_a_is_unchanged(self):
        assert live("VENUE_A").symbols == ["BTC-USDT", "ETH-USDT"]

    def test_the_overlap_is_exactly_the_usdt_pairs(self):
        overlap = {s for s, v in contributors().items() if len(v) >= 2}
        assert overlap == OVERLAP

    def test_every_overlapping_symbol_has_exactly_two_contributors(self):
        found = contributors()
        for symbol in OVERLAP:
            assert sorted(found[symbol]) == ["VENUE_A", "VENUE_B"]

    def test_the_usd_markets_still_have_one_contributor_each(self):
        found = contributors()
        for symbol in SINGLE_VENUE:
            assert found[symbol] == ["VENUE_B"]

    def test_usd_and_usdt_remain_distinct_canonical_symbols(self):
        """The TIDAL-C3 invariant, restated where it now matters most."""
        assert "BTC-USD" != "BTC-USDT"
        assert "ETH-USD" != "ETH-USDT"
        found = contributors()
        assert set(found["BTC-USD"]) != set(found["BTC-USDT"])
        assert "BTC-USD" not in live("VENUE_A").symbols


# ======================================================================
# E — the derived strategy universe
# ======================================================================


class TestDerivedStrategyUniverse:
    def test_the_universe_is_the_deduplicated_union(self, monkeypatch):
        monkeypatch.delenv("TF_SYMBOLS", raising=False)
        monkeypatch.setenv("TF_FEED", "live")
        settings = load_settings()
        assert settings.symbols == ["BTC-USD", "BTC-USDT", "ETH-USD", "ETH-USDT"]

    def test_overlap_does_not_duplicate_a_symbol(self, monkeypatch):
        """Two venues quoting BTC-USDT is one instrument, not two."""
        monkeypatch.delenv("TF_SYMBOLS", raising=False)
        monkeypatch.setenv("TF_FEED", "live")
        settings = load_settings()
        assert len(settings.symbols) == len(set(settings.symbols))

    def test_the_universe_covers_every_configured_instrument(self, monkeypatch):
        monkeypatch.delenv("TF_SYMBOLS", raising=False)
        monkeypatch.setenv("TF_FEED", "live")
        settings = load_settings()
        assert set(settings.symbols) == set(contributors())


# ======================================================================
# F — the paper-only boundary is untouched
# ======================================================================


class TestVenueBSafetyBoundary:
    def test_the_adapter_declares_public_data_only(self):
        config = live("VENUE_B")
        adapter = build_adapter(config, ManualClock(0), list(config.symbols))
        assert adapter.capabilities.public_market_data is True
        assert adapter.capabilities.authenticated is False
        assert adapter.capabilities.order_submission is False

    def test_the_endpoint_is_unchanged(self):
        assert live("VENUE_B").ws_url == "wss://ws-feed.exchange.coinbase.com"
        assert live("VENUE_B").rest_url == "https://api.exchange.coinbase.com"

    def test_the_adapter_is_unchanged(self):
        assert live("VENUE_B").adapter == "coinbase_public"


# ======================================================================
# G — rendering, with no quote substitution
# ======================================================================


class TestSymbolRendering:
    @pytest.mark.parametrize(
        "symbol", ["BTC-USD", "ETH-USD", "BTC-USDT", "ETH-USDT"]
    )
    def test_each_symbol_renders_as_itself(self, symbol):
        assert denormalize(symbol, parser_b.SYMBOL_STYLE) == symbol

    def test_the_subscription_requests_every_configured_product(self):
        config = live("VENUE_B")
        message = parser_b.subscribe_message(config.symbols)
        assert message["product_ids"] == [
            "BTC-USD",
            "ETH-USD",
            "BTC-USDT",
            "ETH-USDT",
        ]

    def test_no_usd_product_is_requested_as_usdt(self):
        message = parser_b.subscribe_message(live("VENUE_B").symbols)
        assert message["product_ids"].count("BTC-USDT") == 1
        assert message["product_ids"].count("BTC-USD") == 1


# ======================================================================
# H / I / J — the structural consequence, through the real detector
# ======================================================================


class TestTheDetectorCanNowFindACrossVenueDislocation:
    """The point of the whole batch, proven against the real function."""

    @pytest.mark.parametrize("symbol", sorted(OVERLAP))
    def test_two_venues_on_one_instrument_produce_a_dislocation(self, symbol):
        states = [
            state("VENUE_A", symbol, 100_000.0),
            state("VENUE_B", symbol, 100_150.0),
        ]
        dislocation = find_dislocation(symbol, states, 100_075.0)

        assert dislocation is not None, (
            f"{symbol} has two configured contributors; the detector must be "
            "able to compare them"
        )
        assert dislocation.symbol == symbol
        assert dislocation.buy_venue != dislocation.sell_venue
        assert {dislocation.buy_venue, dislocation.sell_venue} == {
            "VENUE_A",
            "VENUE_B",
        }
        assert dislocation.gross_edge_bps > 0

    @pytest.mark.parametrize("symbol", sorted(SINGLE_VENUE))
    def test_one_venue_alone_cannot_produce_a_dislocation(self, symbol):
        """BTC-USD and ETH-USD are Coinbase-only, and stay unpaired."""
        states = [state("VENUE_B", symbol, 100_000.0)]
        assert find_dislocation(symbol, states, 100_000.0) is None

    @pytest.mark.parametrize("symbol", sorted(SINGLE_VENUE))
    def test_the_usdt_pair_never_stands_in_for_the_usd_one(self, symbol):
        """The TIDAL-C3 guard, re-proved now that both are configured.

        A USD symbol with a USDT state available on another venue must still
        find nothing: the detector groups by exact canonical symbol.
        """
        usdt = f"{symbol}T"
        states = [
            state("VENUE_B", symbol, 100_000.0),
            state("VENUE_A", usdt, 101_000.0),
            state("VENUE_B", usdt, 101_000.0),
        ]
        assert find_dislocation(symbol, states, 100_000.0) is None

    def test_a_dislocation_below_nothing_is_still_symbol_scoped(self):
        """Passing the whole mixed state list never leaks another instrument."""
        states = [
            state("VENUE_A", "BTC-USDT", 100_000.0),
            state("VENUE_B", "BTC-USDT", 100_150.0),
            state("VENUE_B", "BTC-USD", 90_000.0),
        ]
        dislocation = find_dislocation("BTC-USDT", states, 100_075.0)
        assert dislocation is not None
        # The 90,000 BTC-USD book is a different instrument and must not have
        # been treated as the cheap side.
        assert dislocation.buy_price > 99_000.0


# ======================================================================
# K — the P12-F3 storage protections are unaffected
# ======================================================================


class TestStorageBoundsUnaffected:
    def test_the_venue_b_storage_ceiling_is_unchanged(self):
        assert live("VENUE_B").max_book_levels_per_side == 50_000

    def test_the_transport_bound_is_unchanged(self):
        for config in default_venues():
            assert config.ws_max_message_bytes == 8_388_608

    def test_book_depth_levels_is_unchanged(self):
        for config in default_venues():
            assert config.book_depth_levels == 25

    def test_venue_a_keeps_the_generic_ceiling(self):
        assert live("VENUE_A").max_book_levels_per_side == 10_000

    def test_the_ceiling_is_per_side_not_per_venue(self):
        """More books on a venue does not mean a bigger book.

        The observed USDT snapshots were far smaller than the USD ones
        (1,234/1,288 and 523/796 against 21,109/21,203), so doubling the
        subscription count does not approach the per-side ceiling.
        """
        config = live("VENUE_B")
        largest_observed_side = 21_203
        assert largest_observed_side < config.max_book_levels_per_side
        for observed in (1_234, 1_288, 523, 796):
            assert observed < config.max_book_levels_per_side
