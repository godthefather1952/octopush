"""Venue B wire-format parsing (Coinbase-style public feed).

Every parse function raises :class:`MalformedVenueMessage` — carrying venue,
symbol (when known) and message type — rather than letting a bad field
propagate as a bare exception. See ``venues/venue_a/parser.py`` for the same
pattern and the reasoning behind it (TIDAL-M4).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from core.models.common import Millis, Side
from core.models.market import OrderBookSnapshot, PriceLevel, TradeEvent
from venues.base.messages import BookDelta, MalformedVenueMessage
from venues.base.symbols import normalize

VENUE = "VENUE_B"
SYMBOL_STYLE = "dash"

#: See venue_a/parser.py's identical constant for why each of these belongs.
_PARSE_ERRORS = (ValueError, TypeError, KeyError, IndexError, AttributeError)


def parse_iso_ms(value: Any, fallback: Millis) -> Millis:
    """Coinbase's ISO-8601 ``time`` field -> epoch milliseconds.

    Coinbase documents this field as an ISO-8601 string and nothing else —
    not a Unix timestamp in either unit. So the only value this function ever
    attempts to interpret as a timestamp is a string that parses as ISO-8601;
    everything else (missing, wrong type, unparseable) falls back to local
    receipt time rather than guessing at a numeric convention the protocol
    does not document (TIDAL-M8). A non-string input used to reach
    ``str.replace`` directly and raise ``AttributeError`` uncaught — this
    still doesn't guess, it just no longer crashes the caller for guessing.
    """
    if not isinstance(value, str) or not value:
        return fallback
    try:
        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return fallback


def _levels(raw: list[Any], *, descending: bool) -> list[PriceLevel]:
    out = [PriceLevel(price=float(entry[0]), size=float(entry[1])) for entry in raw]
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
    symbol: str | None = None
    try:
        symbol = normalize(data["product_id"])
        return OrderBookSnapshot(
            venue=VENUE,
            symbol=symbol,
            exchange_ts=parse_iso_ms(data.get("time"), received_ts),
            received_ts=received_ts,
            sequence=int(data["sequence"]) if data.get("sequence") is not None else None,
            bids=_levels(data.get("bids", []), descending=True),
            asks=_levels(data.get("asks", []), descending=False),
            is_checkpoint=True,
        )
    except _PARSE_ERRORS as exc:
        raise MalformedVenueMessage(
            str(exc), venue=VENUE, symbol=symbol, message_type="snapshot"
        ) from exc


def parse_l2update(data: dict[str, Any], received_ts: Millis) -> BookDelta:
    """``l2update`` -> :class:`BookDelta`. Changes are ``[side, price, size]``.

    Only ``"buy"`` and ``"sell"`` are valid sides. Coinbase's public feed
    never sends anything else, so a different value is not a variant to
    tolerate — it means this message is not what it claims to be, and every
    change in it is now suspect, not only the one with the bad side field
    (TIDAL-M2). The old code took anything that wasn't literally ``"buy"`` as
    an ask, which would have put a buy-side change on the wrong side of the
    book — the one error a book can least afford, since it can manufacture a
    crossed book or hide real liquidity rather than merely losing a level.
    """
    symbol: str | None = None
    try:
        symbol = normalize(data["product_id"])
        bids: list[PriceLevel] = []
        asks: list[PriceLevel] = []
        for side, price, size in data.get("changes", []):
            level = PriceLevel(price=float(price), size=float(size))
            if side == "buy":
                bids.append(level)
            elif side == "sell":
                asks.append(level)
            else:
                raise MalformedVenueMessage(
                    f"unknown l2update side: {side!r}",
                    venue=VENUE,
                    symbol=symbol,
                    message_type="l2update",
                )
        bids.sort(key=lambda level: level.price, reverse=True)
        asks.sort(key=lambda level: level.price)
        sequence = data.get("sequence")
        return BookDelta(
            venue=VENUE,
            symbol=symbol,
            exchange_ts=parse_iso_ms(data.get("time"), received_ts),
            received_ts=received_ts,
            sequence=int(sequence) if sequence is not None else None,
            bids=bids,
            asks=asks,
        )
    except MalformedVenueMessage:
        raise
    except _PARSE_ERRORS as exc:
        raise MalformedVenueMessage(
            str(exc), venue=VENUE, symbol=symbol, message_type="l2update"
        ) from exc


def parse_match(data: dict[str, Any], received_ts: Millis) -> TradeEvent:
    """``match``/``last_match`` -> :class:`TradeEvent`.

    ``side`` on this feed is the *maker's* side, so the aggressor is the
    opposite of what the field says.
    """
    symbol: str | None = None
    try:
        symbol = normalize(data["product_id"])
        maker_side = Side.BUY if data.get("side") == "buy" else Side.SELL
        return TradeEvent(
            venue=VENUE,
            symbol=symbol,
            exchange_ts=parse_iso_ms(data.get("time"), received_ts),
            received_ts=received_ts,
            price=float(data["price"]),
            size=float(data["size"]),
            aggressor=maker_side.opposite,
            trade_id=str(data.get("trade_id")) if data.get("trade_id") is not None else None,
        )
    except _PARSE_ERRORS as exc:
        raise MalformedVenueMessage(
            str(exc), venue=VENUE, symbol=symbol, message_type="match"
        ) from exc


def parse_message(
    message: dict[str, Any], received_ts: Millis
) -> OrderBookSnapshot | BookDelta | TradeEvent | None:
    try:
        kind = message.get("type")
    except AttributeError as exc:
        raise MalformedVenueMessage(str(exc), venue=VENUE) from exc
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
