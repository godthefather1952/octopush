"""Venue A wire-format parsing (Binance-style public streams).

Pure functions: a payload dict in, normalised messages out.  Keeping the
parsing free of I/O is what makes the venue layer testable against captured
payloads without a network.

Every parse function here raises :class:`MalformedVenueMessage` rather than
letting a bad field (a missing key, an unparseable number, a NaN/Infinity
price) propagate as a bare ``KeyError``/``ValueError``/pydantic
``ValidationError``. The wrapping happens *inside* each function so the
exception can carry whatever the parser already knew — venue, symbol, message
type — before the failure, which is what lets the adapter decide what a
parse failure means for book continuity (TIDAL-M4) without re-parsing or
guessing.
"""

from __future__ import annotations

from typing import Any

from core.models.common import Millis, Side
from core.models.market import OrderBookSnapshot, PriceLevel, TradeEvent
from venues.base.messages import BookDelta, MalformedVenueMessage
from venues.base.symbols import normalize

VENUE = "VENUE_A"
#: Binance spells symbols with no separator. The quote is carried through
#: unchanged: BTC-USDT -> BTCUSDT, BTC-USD -> BTCUSD.
SYMBOL_STYLE = "concat"

#: Raised by ``float()`` on a bad literal, by a pydantic model on an invalid
#: or non-finite value (``ValidationError`` subclasses ``ValueError``), by a
#: missing/mistyped field, or by treating a non-dict payload (a bare number,
#: a list) as one. Caught uniformly wherever a parse function wraps its own
#: body in :class:`MalformedVenueMessage`.
_PARSE_ERRORS = (ValueError, TypeError, KeyError, IndexError, AttributeError)


def _levels(raw: list[Any], *, descending: bool) -> list[PriceLevel]:
    """Parse ``[["price", "qty"], ...]`` and sort best-first.

    Zero-quantity entries are deletions in an incremental update and are kept
    so the book applier can remove the level.

    Every entry must produce a valid, finite ``PriceLevel`` or the whole
    message is malformed — not just that one level. Silently dropping a
    single bad level used to be preferred here, but a depth message is one
    atomic statement of "the book changed this way"; discarding part of it
    while applying the rest asserts a book state the venue never actually
    sent (TIDAL-M3/M4).
    """
    out: list[PriceLevel] = []
    for entry in raw:
        out.append(PriceLevel(price=float(entry[0]), size=float(entry[1])))
    out.sort(key=lambda level: level.price, reverse=descending)
    return out


def parse_depth_update(data: dict[str, Any], received_ts: Millis) -> BookDelta:
    """``depthUpdate`` -> :class:`BookDelta`.

    ``U`` and ``u`` are the first and last update ids the message covers — a
    span, not a point. Both are carried through so the book can apply the
    documented rule against the whole span. This used to collapse to
    ``prev_sequence = U - 1``, which asserted the span was always exactly one
    id wide and made the post-snapshot handshake unsatisfiable (TIDAL-H1).
    """
    symbol: str | None = None
    try:
        symbol = normalize(data["s"])
        return BookDelta(
            venue=VENUE,
            symbol=symbol,
            exchange_ts=int(data.get("E") or data.get("T") or received_ts),
            received_ts=received_ts,
            first_sequence=int(data["U"]),
            sequence=int(data["u"]),
            bids=_levels(data.get("b", []), descending=True),
            asks=_levels(data.get("a", []), descending=False),
        )
    except _PARSE_ERRORS as exc:
        raise MalformedVenueMessage(
            str(exc), venue=VENUE, symbol=symbol, message_type="depthUpdate"
        ) from exc


def parse_depth_snapshot(
    data: dict[str, Any], symbol: str, received_ts: Millis
) -> OrderBookSnapshot:
    """REST ``/api/v3/depth`` -> a checkpoint snapshot."""
    canonical: str | None = None
    try:
        canonical = normalize(symbol)
        return OrderBookSnapshot(
            venue=VENUE,
            symbol=canonical,
            exchange_ts=int(data.get("E") or received_ts),
            received_ts=received_ts,
            sequence=int(data["lastUpdateId"]),
            bids=_levels(data.get("bids", []), descending=True),
            asks=_levels(data.get("asks", []), descending=False),
            is_checkpoint=True,
        )
    except _PARSE_ERRORS as exc:
        raise MalformedVenueMessage(
            str(exc), venue=VENUE, symbol=canonical, message_type="depthSnapshot"
        ) from exc


def parse_trade(data: dict[str, Any], received_ts: Millis) -> TradeEvent:
    """``trade`` -> :class:`TradeEvent`.

    ``m`` is "buyer is the maker", so ``m == True`` means the aggressor sold.
    """
    symbol: str | None = None
    try:
        symbol = normalize(data["s"])
        return TradeEvent(
            venue=VENUE,
            symbol=symbol,
            exchange_ts=int(data.get("T") or data.get("E") or received_ts),
            received_ts=received_ts,
            price=float(data["p"]),
            size=float(data["q"]),
            aggressor=Side.SELL if data.get("m") else Side.BUY,
            trade_id=str(data.get("t")) if data.get("t") is not None else None,
        )
    except _PARSE_ERRORS as exc:
        raise MalformedVenueMessage(
            str(exc), venue=VENUE, symbol=symbol, message_type="trade"
        ) from exc


def parse_message(
    message: dict[str, Any], received_ts: Millis
) -> BookDelta | TradeEvent | None:
    """Dispatch a combined-stream envelope. Unknown types are ignored."""
    try:
        data = message.get("data", message)
        event = data.get("e")
    except AttributeError as exc:
        # Valid JSON that isn't the object this format requires — a bare
        # number, a list, null. There is no envelope to identify a type from.
        raise MalformedVenueMessage(str(exc), venue=VENUE) from exc
    if event == "depthUpdate":
        return parse_depth_update(data, received_ts)
    if event == "trade":
        return parse_trade(data, received_ts)
    return None


def stream_names(symbols: list[str]) -> list[str]:
    """Combined-stream subscription names for the canonical symbols."""
    from venues.base.symbols import denormalize

    names: list[str] = []
    for symbol in symbols:
        venue_symbol = denormalize(symbol, SYMBOL_STYLE).lower()
        names.append(f"{venue_symbol}@depth@100ms")
        names.append(f"{venue_symbol}@trade")
    return names
