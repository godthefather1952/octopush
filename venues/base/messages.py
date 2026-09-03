"""Normalised messages emitted by venue adapters."""

from __future__ import annotations

from pydantic import Field

from core.models.common import Base, Millis, StrEnum
from core.models.market import OrderBookSnapshot, PriceLevel, TradeEvent


class BookDelta(Base):
    """An incremental L2 update.

    A level with ``size == 0`` removes that price.

    Two different sequencing conventions live here, because two different
    kinds of feed exist and collapsing them into one number is what produced
    TIDAL-H1.

    ``prev_sequence`` is the *point* convention: "this update sits directly on
    top of sequence N". It suits a feed that numbers updates one at a time.

    ``first_sequence`` with ``sequence`` is the *range* convention: one message
    covers update ids ``first_sequence..sequence`` inclusive — Binance's ``U``
    and ``u``. A range cannot be expressed as a point without losing the span,
    and the span is exactly what the documented post-snapshot handshake
    (``U <= lastUpdateId + 1 <= u``) tests. Encoding it as
    ``prev_sequence = U - 1`` silently narrowed that rule to ``U == L + 1`` and
    rejected the majority of legitimate first events.

    A delta carries one convention or the other, never both.
    """

    venue: str
    symbol: str
    exchange_ts: Millis
    received_ts: Millis
    #: Final update id covered by this message (Binance ``u``).
    sequence: int | None = None
    #: Point convention: the sequence this update must sit directly on top of.
    prev_sequence: int | None = None
    #: Range convention: first update id covered by this message (Binance ``U``).
    first_sequence: int | None = None
    bids: list[PriceLevel] = Field(default_factory=list)
    asks: list[PriceLevel] = Field(default_factory=list)

    @property
    def covers_range(self) -> bool:
        """Whether this delta spans a range of update ids rather than a point."""
        return self.first_sequence is not None and self.sequence is not None


class ResyncRequest(Base):
    """A request to re-establish one book from a fresh venue checkpoint.

    Carries the venue and symbol only. It is a request for market *data*, and
    there is deliberately no field by which it could ask a venue for anything
    else.
    """

    venue: str
    symbol: str
    requested_at: Millis
    reason: str = ""


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
    "ResyncRequest",
    "TradeEvent",
    "VenueMessage",
    "VenueStatus",
    "VenueStatusKind",
]
