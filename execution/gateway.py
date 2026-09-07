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

The transport-neutral result models a venue adapter would normalise into live
in :mod:`core.models.venue_execution` and are re-exported here for
compatibility. They moved in Phase 8 so that ``core/`` no longer depends on
``execution/``; the interface below stays, because an interface is not a value
type.

THE PAPER BOUNDARY IS UNCHANGED
===============================
``Veska.__init__`` still refuses any executor whose ``is_paper`` is False, and
this module gives nobody a way around that. Building a live executor would
require deliberate, separate work — which is exactly the intent.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from core.models.common import Millis, OrderType, Side, TimeInForce
from core.models.venue_execution import (
    VenueBalanceSnapshot,
    VenueFillSnapshot,
    VenueGatewayCapabilities,
    VenueOrderAck,
    VenueOrderSnapshot,
    VenuePositionSnapshot,
)

# ======================================================================
# transport-neutral venue truth
# ======================================================================
#
# The value models moved to ``core.models.venue_execution`` in Phase 8 and are
# re-exported here, so ``from execution.gateway import VenueOrderSnapshot``
# keeps working. There is one canonical definition of each class, in core; this
# module defines none of them.
#
# They moved because reconciliation needed them too, and importing them from
# ``execution/`` made ``core/`` depend on ``execution/`` -- backwards in a
# repository where the dependency has always run the other way. The abstract
# gateway below stays here: it is an execution-layer interface, not a value
# type.


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
