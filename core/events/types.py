"""Event topics and the envelope carried on the bus.

Every component publishes and subscribes by topic; nothing calls another
component's methods across a boundary.
"""

from __future__ import annotations

from typing import Any, ClassVar

from pydantic import Field

from core.models.common import Base, Millis, StrEnum, new_id


class EventType(StrEnum):
    # --- market data ------------------------------------------------------
    MARKET_UPDATE = "MARKET_UPDATE"
    BOOK_SNAPSHOT = "BOOK_SNAPSHOT"
    BOOK_DELTA = "BOOK_DELTA"
    TRADE_PRINT = "TRADE_PRINT"
    MARKET_STATE = "MARKET_STATE"
    VENUE_CONNECTED = "VENUE_CONNECTED"
    VENUE_DISCONNECTED = "VENUE_DISCONNECTED"
    VENUE_SEQUENCE_GAP = "VENUE_SEQUENCE_GAP"
    #: TIDAL asking whoever owns a feed to re-establish one book from a fresh
    #: checkpoint. TIDAL holds no adapter reference, so recovery has to travel
    #: the same way everything else does — as an event.
    BOOK_RESYNC_REQUESTED = "BOOK_RESYNC_REQUESTED"
    FEED_STALE = "FEED_STALE"

    # --- analysis ---------------------------------------------------------
    OPPORTUNITY_DETECTED = "OPPORTUNITY_DETECTED"
    OPPORTUNITY_EXPIRED = "OPPORTUNITY_EXPIRED"
    AGENT_OPINION = "AGENT_OPINION"
    CONSENSUS_UPDATED = "CONSENSUS_UPDATED"

    # --- risk -------------------------------------------------------------
    RISK_EVALUATION_REQUEST = "RISK_EVALUATION_REQUEST"
    RISK_PASS = "RISK_PASS"
    RISK_FAIL = "RISK_FAIL"

    # --- execution --------------------------------------------------------
    TRADE_INTENT = "TRADE_INTENT"
    EXECUTION_PLAN = "EXECUTION_PLAN"
    PAPER_ORDER_CREATED = "PAPER_ORDER_CREATED"
    PAPER_ORDER_UPDATED = "PAPER_ORDER_UPDATED"
    PAPER_FILL = "PAPER_FILL"
    EXECUTION_REPORT = "EXECUTION_REPORT"

    # --- portfolio --------------------------------------------------------
    POSITION_UPDATED = "POSITION_UPDATED"
    PORTFOLIO_STATE = "PORTFOLIO_STATE"

    # --- hedging & reconciliation ----------------------------------------
    DELTA_REPORT = "DELTA_REPORT"
    HEDGE_INTENT = "HEDGE_INTENT"
    RECONCILIATION_COMPLETE = "RECONCILIATION_COMPLETE"
    RECONCILIATION_MISMATCH = "RECONCILIATION_MISMATCH"

    # --- lifecycle & ops --------------------------------------------------
    STRATEGY_STATE_CHANGED = "STRATEGY_STATE_CHANGED"
    TRADE_ATTRIBUTION = "TRADE_ATTRIBUTION"
    HEALTH_HEARTBEAT = "HEALTH_HEARTBEAT"
    KILL_SWITCH_TRIGGERED = "KILL_SWITCH_TRIGGERED"
    KILL_SWITCH_CLEARED = "KILL_SWITCH_CLEARED"
    SYSTEM_EVENT = "SYSTEM_EVENT"
    ERROR = "ERROR"

    # --- replay control -----------------------------------------------------
    #: A durable marker for one original orchestrator tick's exact position
    #: in the recorded timeline. Recorded like any other event (the Recorder
    #: is bus middleware) but deliberately excluded from
    #: ``MARKET_INPUT_TYPES``: it carries no market data and must never be
    #: fed back into the pipeline as if it were an input. Its sole purpose is
    #: letting replay recover *when* each tick happened relative to the
    #: market events around it, rather than inferring a tick cadence from the
    #: number of market events -- which depends on feed/network timing, not
    #: the platform's own decision cadence.
    ORCHESTRATOR_TICK = "ORCHESTRATOR_TICK"


#: Topics whose payloads are replayed as market inputs by the replay engine.
MARKET_INPUT_TYPES: frozenset[EventType] = frozenset(
    {
        EventType.BOOK_SNAPSHOT,
        EventType.BOOK_DELTA,
        EventType.TRADE_PRINT,
        EventType.VENUE_CONNECTED,
        EventType.VENUE_DISCONNECTED,
        EventType.VENUE_SEQUENCE_GAP,
    }
)


class Event(Base):
    """The bus envelope.

    ``payload`` is always the JSON form of a model from ``core.models``; the
    producing component's schema is named in ``schema_name`` so consumers can
    validate rather than duck-type.

    Two fields exist for reading a recording back rather than for running:
    ``schema_version`` says which envelope shape wrote it, and
    ``causation_id`` says which event produced it.
    """

    #: Envelope shape this build writes. Bump when a change would make an
    #: older recording invalid, and handle the older shape on read.
    CURRENT_SCHEMA_VERSION: ClassVar[int] = 1

    id: str = Field(default_factory=lambda: new_id("bus"))
    type: EventType
    #: Logical publication time, from the clock (real or replay).
    ts_ms: Millis
    source: str
    payload: dict[str, Any] = Field(default_factory=dict)
    schema_name: str | None = None
    #: Envelope version at the time of writing. Without it a payload shape
    #: change breaks replay of older sessions silently: the recording either
    #: fails validation somewhere confusing, or — if the change was additive
    #: — validates and behaves differently from the run it recorded.
    schema_version: int = Field(default=CURRENT_SCHEMA_VERSION, ge=1)
    #: Groups every event belonging to one opportunity.
    correlation_id: str | None = None
    #: The event that produced this one. Correlation answers "which events
    #: belong together"; only causation answers "what did this come from",
    #: which is the question attribution and post-mortems actually ask.
    causation_id: str | None = None
    #: Monotonic sequence assigned by the recorder; drives deterministic
    #: replay ordering when several events share a timestamp.
    sequence: int | None = None

    @property
    def is_readable(self) -> bool:
        """Whether this build understands the envelope that wrote this event.

        A newer version is not a warning to proceed past: validating a
        payload against a model that does not describe it either errors
        somewhere unhelpful or, when the change was additive, quietly
        produces different behaviour from the recorded run.
        """
        return self.schema_version <= self.CURRENT_SCHEMA_VERSION

    @classmethod
    def caused_by(cls, cause: Event, **fields: Any) -> Event:
        """Build an event descending from ``cause``.

        Correlation is inherited unless given explicitly, so a caller cannot
        link causation and forget the grouping.
        """
        fields.setdefault("correlation_id", cause.correlation_id)
        return cls(causation_id=cause.id, **fields)

    def sort_key(self) -> tuple[int, int, str]:
        return (self.ts_ms, self.sequence if self.sequence is not None else 0, self.id)
