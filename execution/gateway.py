"""The venue-gateway seam — an interface, and nothing behind it.

WHAT THIS FILE IS FOR
=====================
This build is paper-only and has no way to reach a real exchange. That is a
structural property, enforced at the composition root, and this file does not
change it.

What it does is name the shape a future authenticated venue adapter would have
to take, so that the architecture makes the answer to "where would live
execution go?" obvious rather than a matter of opinion:

    VESKA
      └── Executor
            ├── PaperExecutor          <- the only implementation today
            └── (a future live executor)
                  └── ExecutionVenueGateway
                        ├── (a future venue adapter)
                        └── ...

WHAT IS DELIBERATELY ABSENT
===========================
There is no implementation, no subclass, no construction site, and no import of
this module anywhere in the running platform. There is no authentication, no
HTTP, no WebSocket, no signing, no key handling, no exchange SDK — not stubbed,
not commented out, not "for later". A seam that carries a half-written
credential path is not a seam, it is a liability.

The result models below are transport-neutral on purpose. A venue adapter's job
would be to normalise its own private truth *into* these shapes, so that
nothing above it ever learns a venue-specific field name. That is what makes
reconciliation possible against more than one venue at a time.

THE PAPER BOUNDARY IS UNCHANGED
===============================
``Veska.__init__`` still refuses any executor whose ``is_paper`` is False, and
this module gives nobody a way around that. Building a live executor would
require deliberate, separate work — which is exactly the intent.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from pydantic import Field

from core.models.common import (
    Base,
    Liquidity,
    Millis,
    OrderType,
    Side,
    TimeInForce,
)
from core.models.execution import OrderStatus


# ======================================================================
# transport-neutral venue truth
# ======================================================================


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


# ======================================================================
# the interface
# ======================================================================


class ExecutionVenueGateway(ABC):
    """What a future authenticated venue adapter must provide.

    **Nothing implements this, and nothing constructs it.** It exists so that
    the obligations of a live venue connector are written down before anyone
    writes one, and so that a reader can see at a glance which of them the
    paper path already satisfies internally.

    Every method takes ``now_ms`` for the same reason the executor does: a
    logical instant supplied by the caller is reconstructible under replay, and
    a clock read taken inside the call is not.

    A future implementation of this interface would additionally have to answer
    questions this build has never had to ask — how credentials are supplied,
    how a rejected signature is surfaced, what happens when the venue's clock
    disagrees with ours, and how a partial network failure is distinguished
    from a rejection. None of those is answered here, and none should be
    guessed at.
    """

    #: A gateway is by definition able to reach a real venue. The composition
    #: root's paper guard is what keeps one from ever being wired in.
    is_paper: bool = False

    @property
    @abstractmethod
    def venue(self) -> str:
        """The venue this gateway speaks for."""

    @property
    @abstractmethod
    def capabilities(self) -> VenueGatewayCapabilities:
        """What this venue permits, as the adapter understands it."""

    # -- commands ----------------------------------------------------------

    @abstractmethod
    async def submit_order(
        self,
        *,
        client_order_id: str,
        symbol: str,
        side: Side,
        order_type: OrderType,
        time_in_force: TimeInForce,
        quantity: float,
        limit_price: float | None,
        now_ms: Millis,
    ) -> VenueOrderAck:
        """Place one order and return the venue's acknowledgement."""

    @abstractmethod
    async def cancel_order(
        self, client_order_id: str, now_ms: Millis
    ) -> VenueOrderAck:
        """Request cancellation of one order."""

    @abstractmethod
    async def cancel_all(self, now_ms: Millis) -> list[VenueOrderAck]:
        """Request cancellation of every order this gateway has working."""

    # -- queries -----------------------------------------------------------

    @abstractmethod
    async def get_order(
        self, client_order_id: str, now_ms: Millis
    ) -> VenueOrderSnapshot | None:
        """The venue's authoritative view of one order.

        This is the call that resolves an UNKNOWN: the platform asks the venue
        what actually happened rather than assuming.
        """

    @abstractmethod
    async def list_open_orders(self, now_ms: Millis) -> list[VenueOrderSnapshot]:
        """Every order the venue currently considers working."""

    @abstractmethod
    async def list_recent_fills(
        self, since_ms: Millis, now_ms: Millis
    ) -> list[VenueFillSnapshot]:
        """Executions the venue has reported since ``since_ms``."""

    @abstractmethod
    async def list_positions(self, now_ms: Millis) -> list[VenuePositionSnapshot]:
        """Positions as the venue holds them."""

    @abstractmethod
    async def get_balances(self, now_ms: Millis) -> list[VenueBalanceSnapshot]:
        """Balances as the venue holds them."""


__all__ = [
    "ExecutionVenueGateway",
    "VenueBalanceSnapshot",
    "VenueFillSnapshot",
    "VenueGatewayCapabilities",
    "VenueOrderAck",
    "VenueOrderSnapshot",
    "VenuePositionSnapshot",
]
