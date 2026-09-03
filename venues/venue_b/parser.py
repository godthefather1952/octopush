"""Venue B wire-format parsing (Coinbase-style public feed)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from core.models.common import Millis, Side
from core.models.market import OrderBookSnapshot, PriceLevel, TradeEvent
from venues.base.messages import BookDelta
from venues.base.symbols import normalize

VENUE = "VENUE_B"
SYMBOL_STYLE = "dash"


def parse_iso_ms(value: str | None, fallback: Millis) -> Millis:
    if not value:
        return fallback
    try:
        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return fallback


def _levels(raw: list[Any], *, descending: bool) -> list[PriceLevel]:
    out = [
        PriceLevel(price=float(entry[0]), size=float(entry[1]))
        for entry in raw
        if float(entry[0]) > 0
    ]
    out.sort(key=lambda level: level.price, reverse=descending)
    return out


def parse_snapshot(data: dict[str, Any], received_ts: Millis) -> OrderBookSnapshot:
    """``snapshot`` -> a checkpoint snapshot.

    Neither this snapshot nor the ``l2update`` messages that follow it carry a
    sequence number on the public ``level2_batch`` channel. ``LocalOrderBook``
    falls back to comparing each update's exchange timestamp against the
    newest one already applied, which is enough to drop a message that is
    provably not newer than what the book already holds — but it is *not*
    proof that no update was missed: a channel with no sequence numbers gives
    no signal that could prove that. See ``agents/tidal/book.py``'s
    ``_check_unordered`` and the Batch 3 report for the full account.
    """
    return OrderBookSnapshot(
        venue=VENUE,
        symbol=normalize(data["product_id"]),
        exchange_ts=parse_iso_ms(data.get("time"), received_ts),
        received_ts=received_ts,
        sequence=int(data["sequence"]) if data.get("sequence") is not None else None,
        bids=_levels(data.get("bids", []), descending=True),
        asks=_levels(data.get("asks", []), descending=False),
        is_checkpoint=True,
    )


def parse_l2update(data: dict[str, Any], received_ts: Millis) -> BookDelta:
    """``l2update`` -> :class:`BookDelta`. Changes are ``[side, price, size]``."""
    bids: list[PriceLevel] = []
    asks: list[PriceLevel] = []
    for side, price, size in data.get("changes", []):
        level = PriceLevel(price=float(price), size=float(size))
        (bids if side == "buy" else asks).append(level)
    bids.sort(key=lambda level: level.price, reverse=True)
    asks.sort(key=lambda level: level.price)
    sequence = data.get("sequence")
    return BookDelta(
        venue=VENUE,
        symbol=normalize(data["product_id"]),
        exchange_ts=parse_iso_ms(data.get("time"), received_ts),
        received_ts=received_ts,
        sequence=int(sequence) if sequence is not None else None,
        bids=bids,
        asks=asks,
    )


def parse_match(data: dict[str, Any], received_ts: Millis) -> TradeEvent:
    """``match``/``last_match`` -> :class:`TradeEvent`.

    ``side`` on this feed is the *maker's* side, so the aggressor is the
    opposite of what the field says.
    """
    maker_side = Side.BUY if data.get("side") == "buy" else Side.SELL
    return TradeEvent(
        venue=VENUE,
        symbol=normalize(data["product_id"]),
        exchange_ts=parse_iso_ms(data.get("time"), received_ts),
        received_ts=received_ts,
        price=float(data["price"]),
        size=float(data["size"]),
        aggressor=maker_side.opposite,
        trade_id=str(data.get("trade_id")) if data.get("trade_id") is not None else None,
    )


def parse_message(
    message: dict[str, Any], received_ts: Millis
) -> OrderBookSnapshot | BookDelta | TradeEvent | None:
    kind = message.get("type")
    if kind == "snapshot":
        return parse_snapshot(message, received_ts)
    if kind == "l2update":
        return parse_l2update(message, received_ts)
    if kind in ("match", "last_match"):
        return parse_match(message, received_ts)
    return None


def subscribe_message(symbols: list[str]) -> dict[str, Any]:
    from venues.base.symbols import denormalize

    return {
        "type": "subscribe",
        "product_ids": [denormalize(s, SYMBOL_STYLE) for s in symbols],
        "channels": ["level2_batch", "matches", "heartbeat"],
    }
