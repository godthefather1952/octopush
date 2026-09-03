"""Venue layer: symbol normalisation, wire parsing, and the paper boundary."""

from __future__ import annotations

import inspect

import pytest

from core.clock import ManualClock
from core.config import VenueConfig
from core.models.common import Side
from tests.conftest import START_MS
from venues.base.adapter import VenueAdapter
from venues.base.symbols import UnknownSymbol, denormalize, normalize, parse
from venues.registry import ADAPTERS, build_adapter
from venues.venue_a import parser as parser_a
from venues.venue_b import parser as parser_b


class TestSymbolNormalisation:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            # Spelling differences that do not change the instrument.
            ("BTC-USD", "BTC-USD"),
            ("btc-usd", "BTC-USD"),
            ("BTCUSD", "BTC-USD"),
            ("XBT/USD", "BTC-USD"),
            ("BTC/USDT", "BTC-USDT"),
            ("BTC_USDT", "BTC-USDT"),
            ("ETH-EUR", "ETH-EUR"),
            # Quote assets that are NOT interchangeable. These four used to
            # collapse onto BTC-USD, which is what let a Binance USDT quote be
            # compared against a Coinbase USD quote (TIDAL-C3).
            ("BTCUSDT", "BTC-USDT"),
            ("BTCUSDC", "BTC-USDC"),
            ("BTCBUSD", "BTC-BUSD"),
            ("ETHUSDT", "ETH-USDT"),
            ("ETHUSDC", "ETH-USDC"),
            ("ETHBUSD", "ETH-BUSD"),
            # Still a base alias, unchanged by this batch.
            ("WETH-USD", "ETH-USD"),
        ],
    )
    def test_every_venue_format_maps_to_one_canonical_symbol(self, raw, expected):
        assert normalize(raw) == expected

    @pytest.mark.parametrize("base", ["BTC", "ETH"])
    def test_each_stablecoin_quote_is_its_own_instrument(self, base):
        """The four dollar-ish quotes must produce four distinct instruments."""
        canonical = {normalize(f"{base}{q}") for q in ("USD", "USDT", "USDC", "BUSD")}
        assert canonical == {
            f"{base}-USD",
            f"{base}-USDT",
            f"{base}-USDC",
            f"{base}-BUSD",
        }
        assert len(canonical) == 4, "collapsing any two of these reintroduces TIDAL-C3"

    def test_unparseable_symbol_raises(self):
        with pytest.raises(UnknownSymbol):
            parse("NOTASYMBOL")

    def test_round_trip_to_venue_formats(self):
        assert denormalize("BTC-USD", "concat") == "BTCUSD"
        assert denormalize("BTC-USDT", "concat") == "BTCUSDT"
        assert denormalize("BTC-USDC", "concat") == "BTCUSDC"
        assert denormalize("BTC-USD", "dash") == "BTC-USD"
        assert denormalize("BTC-USD", "slash") == "BTC/USD"

    @pytest.mark.parametrize(
        "canonical", ["BTC-USD", "BTC-USDT", "BTC-USDC", "BTC-BUSD", "ETH-USDT"]
    )
    @pytest.mark.parametrize("style", ["concat", "dash", "slash"])
    def test_formatting_never_changes_the_instrument(self, canonical, style):
        """Every style is punctuation-only: format then re-parse is identity."""
        assert normalize(denormalize(canonical, style)) == canonical

    def test_the_quote_rewriting_style_is_gone(self):
        """``concat_usdt`` rendered BTC-USD as BTCUSDT — a different asset.

        Asserted by name so it cannot quietly come back: any style that
        rewrites the quote belongs to venue configuration, not formatting.
        """
        with pytest.raises(ValueError, match="unknown symbol style"):
            denormalize("BTC-USD", "concat_usdt")

    def test_usd_never_formats_to_a_stablecoin(self):
        for style in ("concat", "dash", "slash"):
            rendered = denormalize("BTC-USD", style)
            assert "USDT" not in rendered
            assert "USDC" not in rendered
            assert "BUSD" not in rendered

    def test_unknown_style_raises(self):
        with pytest.raises(ValueError, match="unknown symbol style"):
            denormalize("BTC-USD", "hieroglyphs")


class TestVenueAParser:
    def test_depth_update_keeps_the_whole_update_id_range(self):
        """``U`` and ``u`` are a span, and both ends have to survive parsing.

        This previously asserted ``prev_sequence == 100`` — the span collapsed
        to the single point below ``U``. That threw away the width of the
        range, and the width is exactly what the post-snapshot handshake
        (``U <= lastUpdateId + 1 <= u``) is a test of, so the handshake could
        never be satisfied (TIDAL-H1).
        """
        delta = parser_a.parse_depth_update(
            {
                "e": "depthUpdate",
                "E": START_MS,
                "s": "BTCUSDT",
                "U": 101,
                "u": 105,
                "b": [["110000.10", "0.5"], ["110000.00", "0"]],
                "a": [["110001.00", "1.5"]],
            },
            START_MS + 5,
        )
        assert delta.symbol == "BTC-USDT"
        assert delta.first_sequence == 101
        assert delta.sequence == 105
        assert delta.covers_range
        # The point convention is not used by this feed, and asserting it is
        # absent is what stops the collapse from being reintroduced.
        assert delta.prev_sequence is None
        assert [level.price for level in delta.bids] == [110000.10, 110000.00]
        # A zero size survives parsing: it is a deletion, not noise.
        assert delta.bids[1].size == 0.0

    def test_buyer_maker_flag_inverts_the_aggressor(self):
        base = {"e": "trade", "T": START_MS, "s": "BTCUSDT", "p": "110000", "q": "0.1", "t": 9}
        assert parser_a.parse_trade({**base, "m": True}, START_MS).aggressor is Side.SELL
        assert parser_a.parse_trade({**base, "m": False}, START_MS).aggressor is Side.BUY

    def test_rest_snapshot_becomes_a_checkpoint(self):
        snapshot = parser_a.parse_depth_snapshot(
            {"lastUpdateId": 500, "bids": [["100", "1"]], "asks": [["101", "1"]]},
            "BTCUSDT",
            START_MS,
        )
        assert snapshot.is_checkpoint and snapshot.sequence == 500
        assert snapshot.symbol == "BTC-USDT"

    def test_unknown_event_types_are_ignored(self):
        assert parser_a.parse_message({"data": {"e": "kline"}}, START_MS) is None

    def test_combined_stream_envelope_is_unwrapped(self):
        parsed = parser_a.parse_message(
            {
                "stream": "btcusdt@trade",
                "data": {
                    "e": "trade",
                    "T": START_MS,
                    "s": "BTCUSDT",
                    "p": "1",
                    "q": "1",
                    "m": False,
                },
            },
            START_MS,
        )
        assert parsed is not None and parsed.symbol == "BTC-USDT"

    def test_stream_names_cover_depth_and_trades(self):
        names = parser_a.stream_names(["BTC-USDT", "ETH-USDT"])
        assert "btcusdt@depth@100ms" in names
        assert "ethusdt@trade" in names

    def test_stream_names_follow_the_configured_quote(self):
        """A USD-quoted instrument subscribes to the USD market, not USDT."""
        assert parser_a.stream_names(["BTC-USD"]) == [
            "btcusd@depth@100ms",
            "btcusd@trade",
        ]


class TestVenueBParser:
    def test_snapshot_is_sorted_and_normalised(self):
        snapshot = parser_b.parse_snapshot(
            {
                "type": "snapshot",
                "product_id": "BTC-USD",
                "time": "2026-09-01T00:00:00Z",
                "bids": [["99", "1"], ["100", "2"]],
                "asks": [["102", "1"], ["101", "2"]],
            },
            START_MS,
        )
        assert [level.price for level in snapshot.bids] == [100.0, 99.0]
        assert [level.price for level in snapshot.asks] == [101.0, 102.0]
        assert snapshot.is_checkpoint

    def test_l2update_splits_sides(self):
        delta = parser_b.parse_l2update(
            {
                "type": "l2update",
                "product_id": "ETH-USD",
                "time": "2026-09-01T00:00:01Z",
                "changes": [["buy", "4000", "1.5"], ["sell", "4001", "0"]],
            },
            START_MS,
        )
        assert delta.symbol == "ETH-USD"
        assert delta.bids[0].price == 4000.0
        assert delta.asks[0].size == 0.0

    def test_match_side_is_the_maker_so_the_aggressor_is_opposite(self):
        base = {
            "type": "match",
            "product_id": "BTC-USD",
            "time": "2026-09-01T00:00:00Z",
            "price": "110000",
            "size": "0.2",
            "trade_id": 7,
        }
        assert parser_b.parse_match({**base, "side": "buy"}, START_MS).aggressor is Side.SELL
        assert parser_b.parse_match({**base, "side": "sell"}, START_MS).aggressor is Side.BUY

    def test_bad_timestamp_falls_back_to_receipt_time(self):
        assert parser_b.parse_iso_ms("not-a-time", START_MS) == START_MS
        assert parser_b.parse_iso_ms(None, START_MS) == START_MS

    def test_heartbeat_and_unknown_types_are_ignored(self):
        assert parser_b.parse_message({"type": "heartbeat"}, START_MS) is None

    def test_subscribe_message_uses_venue_symbol_format(self):
        message = parser_b.subscribe_message(["BTC-USD", "ETH-USD"])
        assert message["product_ids"] == ["BTC-USD", "ETH-USD"]
        assert "level2_batch" in message["channels"]


class TestPaperModeBoundary:
    """The structural guarantee: this build cannot reach a real exchange."""

    def test_no_adapter_declares_trading_capability(self):
        for name, factory in ADAPTERS.items():
            capabilities = factory.capabilities
            assert not capabilities.order_submission, f"{name} declares order submission"
            assert not capabilities.authenticated, f"{name} declares authentication"

    def test_adapter_interface_exposes_no_order_methods(self):
        forbidden = {
            "submit_order",
            "place_order",
            "create_order",
            "cancel_order",
            "amend_order",
            "withdraw",
            "transfer",
            "sign",
            "sign_request",
        }
        for name, factory in ADAPTERS.items():
            methods = {m for m, _ in inspect.getmembers(factory, inspect.isfunction)}
            assert not (methods & forbidden), f"{name} exposes {methods & forbidden}"
        base_methods = {m for m, _ in inspect.getmembers(VenueAdapter, inspect.isfunction)}
        assert not (base_methods & forbidden)

    def test_registry_refuses_an_adapter_that_claims_trading(self):
        from venues.base.adapter import VenueCapabilities
        from venues.simulated import SimulatedVenueAdapter

        class RogueAdapter(SimulatedVenueAdapter):
            capabilities = VenueCapabilities(order_submission=True)

        ADAPTERS["rogue"] = RogueAdapter
        try:
            with pytest.raises(RuntimeError, match="public-market-data only"):
                build_adapter(
                    VenueConfig(
                        name="ROGUE", display_name="Rogue", adapter="rogue"
                    ),
                    ManualClock(START_MS),
                    ["BTC-USD"],
                )
        finally:
            del ADAPTERS["rogue"]

    def test_paper_executor_is_the_only_executor_implementation(self):
        import execution.paper.executor  # noqa: F401
        from execution.paper import PaperExecutor
        from execution.veska.executor import Executor

        subclasses = Executor.__subclasses__()
        assert subclasses == [PaperExecutor]
        assert PaperExecutor.is_paper is True

    def test_veska_refuses_a_non_paper_executor(self, bus, clock, settings, health):
        from execution.veska import Veska

        class FakeLiveExecutor:
            is_paper = False

        with pytest.raises(RuntimeError, match="paper executors only"):
            Veska(bus, clock, settings, health, FakeLiveExecutor())

    def test_no_configuration_can_enable_live_trading(self, settings):
        from core.models.common import TradingMode

        assert settings.mode is TradingMode.PAPER
        # There is no other mode to switch to.
        assert [m.value for m in TradingMode] == ["PAPER"]
