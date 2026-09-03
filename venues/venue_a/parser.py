"""Venue A wire-format parsing (Binance-style public streams).

Pure functions: a payload dict in, normalised messages out.  Keeping the
parsing free of I/O is what makes the venue layer testable against captured
payloads without a network.
"""

from __future__ import annotations

from typing import Any

from core.models.common import Millis, Side
from core.models.market import OrderBookSnapshot, PriceLevel, TradeEvent
from venues.base.messages import BookDelta
from venues.base.symbols import normalize

VENUE = "VENUE_A"
SYMBOL_STYLE = "concat_usdt"


def _levels(raw: list[Any], *, descending: bool) -> list[PriceLevel]:
    """Parse ``[["price", "qty"], ...]`` and sort best-first.

    Zero-quantity entries are deletions in an incremental update and are kept
    so the book applier can remove the level.
    """
    out: list[PriceLevel] = []
    for entry in raw:
        price = float(entry[0])
        size = float(entry[1])
        if price <= 0:
            continue
        out.append(PriceLevel(price=price, size=size))
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
    return BookDelta(
        venue=VENUE,
        symbol=normalize(data["s"]),
        exchange_ts=int(data.get("E") or data.get("T") or received_ts),
        received_ts=received_ts,
        first_sequence=int(data["U"]),
        sequence=int(data["u"]),
        bids=_levels(data.get("b", []), descending=True),
        asks=_levels(data.get("a", []), descending=False),
    )


def parse_depth_snapshot(
    data: dict[str, Any], symbol: str, received_ts: Millis
) -> OrderBookSnapshot:
    """REST ``/api/v3/depth`` -> a checkpoint snapshot."""
    return OrderBookSnapshot(
        venue=VENUE,
        symbol=normalize(symbol),
        exchange_ts=int(data.get("E") or received_ts),
        received_ts=received_ts,
        sequence=int(data["lastUpdateId"]),
        bids=_levels(data.get("bids", []), descending=True),
        asks=_levels(data.get("asks", []), descending=False),
        is_checkpoint=True,
    )


def parse_trade(data: dict[str, Any], received_ts: Millis) -> TradeEvent:
    """``trade`` -> :class:`TradeEvent`.

    ``m`` is "buyer is the maker", so ``m == True`` means the aggressor sold.
    """
    return TradeEvent(
        venue=VENUE,
        symbol=normalize(data["s"]),
        exchange_ts=int(data.get("T") or data.get("E") or received_ts),
        received_ts=received_ts,
        price=float(data["p"]),
        size=float(data["q"]),
        aggressor=Side.SELL if data.get("m") else Side.BUY,
        trade_id=str(data.get("t")) if data.get("t") is not None else None,
    )


def parse_message(
    message: dict[str, Any], received_ts: Millis
) -> BookDelta | TradeEvent | None:
    """Dispatch a combined-stream envelope. Unknown types are ignored."""
    data = message.get("data", message)
    event = data.get("e")
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
