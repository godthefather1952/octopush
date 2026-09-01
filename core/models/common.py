"""Primitive types, enums and the base envelope shared by every schema.

Everything that crosses a component boundary in the trading floor is a Pydantic
model defined in ``core.models``.  No component is allowed to depend on another
component's free-form text output.

Time is represented everywhere as integer milliseconds since the Unix epoch
(``ts_ms``).  Wall-clock time is never read directly by domain code; it is
always obtained from a :class:`core.clock.Clock` so that replay is
deterministic.
"""

from __future__ import annotations

import uuid
from enum import StrEnum as _StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

# Milliseconds since the Unix epoch.
Millis = int

#: Tolerance used when comparing two independently derived monetary amounts.
#: Prices and sizes are carried as float64; reconciliation therefore compares
#: with an epsilon rather than for exact equality.
MONEY_EPSILON = 1e-6

#: Tolerance used when comparing quantities (base-asset units).
QTY_EPSILON = 1e-9


def new_id(prefix: str) -> str:
    """Return a short, human-greppable unique identifier."""
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


class StrEnum(_StrEnum):
    """Base for every enum in the system.

    Aliased so that the whole codebase imports one name and the stdlib
    dependency stays in one place.
    """


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"

    @property
    def sign(self) -> int:
        """+1 for BUY, -1 for SELL. Used for signed position arithmetic."""
        return 1 if self is Side.BUY else -1

    @property
    def opposite(self) -> Side:
        return Side.SELL if self is Side.BUY else Side.BUY


class OrderType(StrEnum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"


class TimeInForce(StrEnum):
    GTC = "GTC"
    IOC = "IOC"
    FOK = "FOK"
    POST_ONLY = "POST_ONLY"


class Liquidity(StrEnum):
    """Whether a fill added or removed liquidity — drives the fee applied."""

    MAKER = "MAKER"
    TAKER = "TAKER"


class DataQuality(StrEnum):
    """Freshness classification for any derived value.

    A stale signal must never silently degrade into a neutral signal; it
    degrades into one of the non-``FRESH`` states instead and consumers are
    required to branch on it.
    """

    FRESH = "FRESH"
    DEGRADED = "DEGRADED"
    STALE = "STALE"
    UNAVAILABLE = "UNAVAILABLE"

    @property
    def is_usable(self) -> bool:
        return self is DataQuality.FRESH


class AgentId(StrEnum):
    TIDAL = "TIDAL"
    NORO = "NORO"
    ZEPHR = "ZEPHR"
    LUMEN = "LUMEN"
    OKAPI = "OKAPI"
    MARIN = "MARIN"
    RUNE = "RUNE"
    VESKA = "VESKA"
    ORCHESTRATOR = "ORCHESTRATOR"


class TradingMode(StrEnum):
    """The only supported mode in this codebase is :attr:`PAPER`.

    There is deliberately no ``LIVE`` member: paper mode is a structural
    property of the build, not a runtime flag (see ``docs``/README section on
    the paper-mode security boundary).
    """

    PAPER = "PAPER"


class Base(BaseModel):
    """Base class for every schema in the system."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=False,
        use_enum_values=False,
        validate_assignment=True,
        ser_json_inf_nan="constants",
    )

    def to_json_dict(self) -> dict[str, Any]:
        """JSON-safe dict (enums as values, floats as floats)."""
        return self.model_dump(mode="json")


class Envelope(Base):
    """Common header carried by every analytical or lifecycle payload."""

    event_id: str = Field(default_factory=lambda: new_id("evt"))
    #: When the producing component emitted this record.
    created_at: Millis
    #: Timestamp of the newest market data this record was derived from.
    #: ``None`` for records that are not derived from market data.
    source_data_timestamp: Millis | None = None
    #: Correlation id linking every record produced for one opportunity.
    correlation_id: str | None = None

    @property
    def data_age_ms(self) -> int | None:
        if self.source_data_timestamp is None:
            return None
        return self.created_at - self.source_data_timestamp
