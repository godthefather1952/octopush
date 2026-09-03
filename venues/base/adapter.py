"""Venue adapter interface — public market data only.

This is a deliberate structural boundary, not a runtime flag.  The interface
declares *no* method capable of submitting, amending or cancelling an order on
a real exchange, and no adapter in this repository holds credentials, signs
requests, or touches an authenticated endpoint.  Adding live trading requires
writing a new interface and a new implementation, which is exactly the amount
of friction the design intends (see README, "Paper mode security boundary").
"""

from __future__ import annotations

import asyncio
import contextlib
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from core.clock import Clock
from core.config import VenueConfig
from venues.base.messages import MalformedVenueMessage, RawMessage, VenueMessage

Emit = Callable[[VenueMessage], Awaitable[None]]
EmitRaw = Callable[[RawMessage], Awaitable[None]]


@dataclass(frozen=True)
class VenueCapabilities:
    """What an adapter is allowed to do. Trading is not among them."""

    public_market_data: bool = True
    order_books: bool = True
    trades: bool = True
    funding: bool = False
    open_interest: bool = False
    #: Always False in this codebase. Present so that the absence is explicit
    #: and testable rather than merely implied.
    order_submission: bool = False
    authenticated: bool = False


@dataclass(frozen=True)
class MalformedContext:
    """What a malformed-message containment decision actually did.

    Exists so an operator (or a test) can answer, without re-reading a raw
    payload: which venue and symbol, what kind of message, why it was
    rejected, and whether that rejection escalated into invalidating a book
    or requesting a resync/reconnect (TIDAL-M4's error-visibility
    requirement). Bounded and structured on purpose — no raw frame content.
    """

    venue: str
    symbol: str | None
    message_type: str | None
    detail: str
    book_invalidated: bool
    resync_requested: bool


class ContinuityUncertain(RuntimeError):
    """Raised to force a reconnect when a malformed message leaves local book
    state impossible to trust and there is no narrower recovery available.

    Coinbase's public feed has no per-symbol resubscribe that reliably yields
    a fresh snapshot (see the Batch 3 report) — a full reconnect is the only
    verified way to re-establish one. Raising this from ``handle_payload``
    lets it propagate out of ``_session()`` into the existing, already-tested
    reconnect machinery in :meth:`WebSocketAdapter.run` rather than
    duplicating disconnect/backoff/resubscribe logic at the call site.
    """


@dataclass
class ConnectionStats:
    connected: bool = False
    connects: int = 0
    reconnects: int = 0
    messages: int = 0
    sequence_gaps: int = 0
    errors: int = 0
    last_message_ts: int | None = None
    last_error: str | None = None
    #: The most recent malformed-message containment decision. ``None`` until
    #: the first one occurs.
    last_malformed: MalformedContext | None = None

    def record_malformed(
        self, exc: MalformedVenueMessage, *, book_invalidated: bool, resync_requested: bool
    ) -> None:
        self.errors += 1
        self.last_error = str(exc)[:300]
        self.last_malformed = MalformedContext(
            venue=exc.venue or "",
            symbol=exc.symbol,
            message_type=exc.message_type,
            detail=str(exc)[:300],
            book_invalidated=book_invalidated,
            resync_requested=resync_requested,
        )


class VenueAdapter(ABC):
    """Streams normalised public market data for one venue."""

    capabilities = VenueCapabilities()

    def __init__(self, config: VenueConfig, clock: Clock, symbols: list[str]) -> None:
        self.config = config
        self.clock = clock
        self.symbols = list(symbols)
        self.stats = ConnectionStats()
        self._emit: Emit | None = None
        self._emit_raw: EmitRaw | None = None
        self._task: asyncio.Task[None] | None = None
        self._stopping = False

    @property
    def name(self) -> str:
        return self.config.name

    def bind(self, emit: Emit, emit_raw: EmitRaw | None = None) -> None:
        self._emit = emit
        self._emit_raw = emit_raw

    async def emit(self, message: VenueMessage) -> None:
        self.stats.messages += 1
        self.stats.last_message_ts = self.clock.now_ms()
        if self._emit is not None:
            await self._emit(message)

    async def emit_raw(self, payload: str, channel: str | None = None) -> None:
        if self._emit_raw is None:
            return
        await self._emit_raw(
            RawMessage(
                venue=self.name,
                received_ts=self.clock.now_ms(),
                payload=payload,
                channel=channel,
            )
        )

    @abstractmethod
    async def run(self) -> None:
        """Stream until cancelled, reconnecting on failure."""

    async def request_resync(self, symbol: str, reason: str = "") -> None:
        """Re-establish one symbol's book from a fresh public checkpoint.

        The default does nothing, which is the correct behaviour for a feed
        that carries its own snapshots (it recovers on resubscribe) and for
        one with no checkpoint endpoint at all.

        This asks a venue for public market data and nothing else. It takes a
        symbol, returns nothing, and has no counterpart that could act on an
        account — the interface still declares no way to submit, amend or
        cancel an order.
        """
        # Concrete and doing nothing, not abstract: a feed with no checkpoint
        # endpoint has nothing to implement, and forcing it to write an empty
        # override would say less than this does.
        return None

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stopping = False
        self._task = asyncio.create_task(self.run(), name=f"venue-{self.name}")

    async def stop(self) -> None:
        self._stopping = True
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        self.stats.connected = False


@dataclass
class ReconnectPolicy:
    """Exponential backoff with a ceiling, used by every network adapter."""

    initial_s: float = 1.0
    factor: float = 2.0
    max_s: float = 30.0
    attempt: int = field(default=0, repr=False)

    def next_delay(self) -> float:
        delay = min(self.max_s, self.initial_s * (self.factor**self.attempt))
        self.attempt += 1
        return delay

    def reset(self) -> None:
        self.attempt = 0
