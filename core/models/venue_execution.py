"""Transport-neutral venue value types.

WHAT A VENUE SAYS, IN SHAPES NOTHING VENUE-SPECIFIC LEAKS THROUGH
================================================================
These are the models a venue adapter normalises its own private replies *into*,
so that nothing above the adapter ever learns a venue-specific field name. That
is what makes reconciling against more than one venue possible: two adapters
with entirely different wire formats produce the same five shapes, and
everything downstream compares like with like.

They are pure value types. There is no transport here, no authentication, no
connection, and no behaviour — the abstract gateway that would *use* them is an
execution-layer interface and stays in :mod:`execution.gateway`.

WHY THEY LIVE IN CORE
=====================
Phase 6 defined them in ``execution/gateway.py``, which was the natural place
at the time. Phase 7 then needed them for
:class:`~core.models.reconciliation.VenueTruthSnapshot`, and importing them
there made ``core/`` depend on ``execution/`` — backwards, in a repository
where ``execution`` imports ``core`` throughout and ``core`` had never imported
``execution`` once.

Phase 8 corrects it by moving the value types here and leaving the interface
where it belongs. ``execution.gateway`` re-exports every name, so
``from execution.gateway import VenueOrderSnapshot`` still works; there is one
canonical definition of each class and no duplicate. Nothing about their
contents changed.
"""

from __future__ import annotations

from pydantic import Field

from core.models.common import Base, Liquidity, Millis, OrderType, Side, TimeInForce
from core.models.execution import OrderStatus


class VenueOrderAck(Base):
    """A venue's acknowledgement that it has accepted (or refused) an order."""

    accepted: bool
    client_order_id: str
    #: The venue's own identifier, where it issues one.
    venue_order_id: str | None = None
    status: OrderStatus = OrderStatus.SUBMITTING
    #: The venue's timestamp for the acknowledgement, normalised to millis.
    at_ms: Millis | None = None
    reason: str = ""


class VenueOrderSnapshot(Base):
    """What a venue says one order currently is.

    The authoritative answer an UNKNOWN order is waiting for. Deliberately
    minimal: quantities, a status, and the venue's own time — anything richer
    would start encoding one venue's model.
    """

    client_order_id: str
    venue_order_id: str | None = None
    venue: str
    symbol: str
    side: Side
    order_type: OrderType
    time_in_force: TimeInForce
    status: OrderStatus
    quantity: float
    filled_quantity: float = 0.0
    average_price: float | None = None
    limit_price: float | None = None
    at_ms: Millis | None = None


class VenueFillSnapshot(Base):
    """One execution as the venue reports it."""

    fill_id: str
    client_order_id: str
    venue: str
    symbol: str
    side: Side
    quantity: float
    price: float
    fee: float = 0.0
    #: Venues that do not report this leave it unset rather than guessing.
    liquidity: Liquidity | None = None
    at_ms: Millis | None = None


class VenuePositionSnapshot(Base):
    """A position as the venue holds it, in base-asset units."""

    venue: str
    symbol: str
    #: Signed: positive is long, negative is short.
    quantity: float
    average_entry_price: float | None = None
    at_ms: Millis | None = None


class VenueBalanceSnapshot(Base):
    """A balance as the venue holds it."""

    venue: str
    asset: str
    total: float
    available: float
    at_ms: Millis | None = None


class VenueGatewayCapabilities(Base):
    """What a venue adapter would claim its exchange supports.

    Separate from ``ExecutorCapabilities``: that describes what an executor
    implements, this would describe what a venue permits. A future live
    executor's capabilities would be the intersection of the two.
    """

    supports_market: bool = False
    supports_limit: bool = True
    supports_ioc: bool = False
    supports_fok: bool = False
    supports_post_only: bool = False
    supports_gtc: bool = True
    supports_cancel_all: bool = False
    supports_order_lookup: bool = False
    supports_position_query: bool = False
    supports_balance_query: bool = False
    #: Venue-declared limits, where the venue publishes them.
    min_order_notional: float | None = None
    max_order_notional: float | None = None
    notes: list[str] = Field(default_factory=list)


__all__ = [
    "VenueBalanceSnapshot",
    "VenueFillSnapshot",
    "VenueGatewayCapabilities",
    "VenueOrderAck",
    "VenueOrderSnapshot",
    "VenuePositionSnapshot",
]
