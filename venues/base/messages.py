"""Normalised messages emitted by venue adapters."""

from __future__ import annotations

from pydantic import Field

from core.models.common import Base, Millis, StrEnum
from core.models.market import OrderBookSnapshot, PriceLevel, TradeEvent


class BookDelta(Base):
    """An incremental L2 update.

    A level with ``size == 0`` removes that price.  ``prev_sequence`` lets
    TIDAL detect gaps: if it does not match the book's current sequence the
    book is marked unsynchronised and a resynchronisation is requested rather
    than silently applying a corrupt update.
    """

    venue: str
    symbol: str
    exchange_ts: Millis
    received_ts: Millis
    sequence: int | None = None
    prev_sequence: int | None = None
    bids: list[PriceLevel] = Field(default_factory=list)
    asks: list[PriceLevel] = Field(default_factory=list)


class VenueStatusKind(StrEnum):
    CONNECTED = "CONNECTED"
    DISCONNECTED = "DISCONNECTED"
    RESUBSCRIBED = "RESUBSCRIBED"
    HEARTBEAT = "HEARTBEAT"
    ERROR = "ERROR"


class VenueStatus(Base):
    venue: str
    kind: VenueStatusKind
    received_ts: Millis
    detail: str = ""
    symbol: str | None = None


class RawMessage(Base):
    """The unparsed venue payload, recorded when ``storage.record_raw`` is on."""

    venue: str
    received_ts: Millis
    payload: str
    channel: str | None = None


VenueMessage = OrderBookSnapshot | BookDelta | TradeEvent | VenueStatus

__all__ = [
    "BookDelta",
    "OrderBookSnapshot",
    "RawMessage",
    "TradeEvent",
    "VenueMessage",
    "VenueStatus",
    "VenueStatusKind",
]
