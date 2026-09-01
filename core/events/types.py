"""Event topics and the envelope carried on the bus.

Every component publishes and subscribes by topic; nothing calls another
component's methods across a boundary.
"""

from __future__ import annotations

from typing import Any

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
    """

    id: str = Field(default_factory=lambda: new_id("bus"))
    type: EventType
    #: Logical publication time, from the clock (real or replay).
    ts_ms: Millis
    source: str
    payload: dict[str, Any] = Field(default_factory=dict)
    schema_name: str | None = None
    correlation_id: str | None = None
    #: Monotonic sequence assigned by the recorder; drives deterministic
    #: replay ordering when several events share a timestamp.
    sequence: int | None = None

    def sort_key(self) -> tuple[int, int, str]:
        return (self.ts_ms, self.sequence if self.sequence is not None else 0, self.id)
