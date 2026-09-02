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

import math
from enum import StrEnum as _StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from core.ids import new_id as _mint

# Milliseconds since the Unix epoch.
Millis = int

# Currency assumption
# -------------------
# Every monetary field in this codebase — `notional`, `cash`, `price`, `fee`,
# `realized_pnl`, every `*_notional` risk limit — is denominated in the single
# quote currency, and none of them says so in its name.
#
# That is a deliberate simplification and it is only safe because the symbol
# layer already collapses quote currencies: USDT and USDC markets are
# normalised to a `-USD` symbol (see venues/base/symbols.py), which is an
# explicit modelling decision that carries a residual basis risk the platform
# does not currently model.
#
# The assumption holds while the universe is USD-quoted. It breaks the moment
# a second quote currency is admitted — a EUR or a BTC-quoted pair — at which
# point summing `notional` across venues stops being meaningful and every
# money field needs a currency alongside it. Adding that pair is therefore not
# a configuration change; it is a modelling change, and this comment is here
# so that is discovered before the fact rather than after.

#: Tolerance used when comparing two independently derived monetary amounts.
#: Prices and sizes are carried as float64; reconciliation therefore compares
#: with an epsilon rather than for exact equality.
MONEY_EPSILON = 1e-6

#: Tolerance used when comparing quantities (base-asset units).
QTY_EPSILON = 1e-9

#: Below this a position counts as flat. Deliberately tighter than
#: :data:`QTY_EPSILON`: that one asks "did two paths agree", this one asks
#: "is anything left", and rounding a real residual to flat would strand
#: exposure the risk limits then stop seeing.
FLAT_EPSILON = 1e-12


def sanitize_json(value: Any) -> Any:
    """Recursively replace non-finite floats with ``None``.

    Applied at the serialisation boundary so that ``inf`` remains usable in
    intermediate arithmetic (an exhausted book really does have unbounded
    market impact) while never reaching the wire or the store.
    """
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {k: sanitize_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitize_json(v) for v in value]
    return value


def new_id(prefix: str) -> str:
    """Mint an identifier in the ``prefix`` namespace.

    Delegates to the generator installed in :mod:`core.ids`, so that replay can
    swap in reproducible identifiers without every call site knowing. Live runs
    get full-width random ids.
    """
    return _mint(prefix)


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
        # Non-finite floats serialise as JSON null, never as the bare
        # ``Infinity`` / ``NaN`` literals, which are not valid RFC 8259 and
        # which PostgreSQL JSONB rejects outright. This covers model_dump_json;
        # to_json_dict() sanitises the dict path explicitly below, because
        # pydantic applies this setting only to its own JSON serialiser.
        ser_json_inf_nan="null",
    )

    def to_json_dict(self) -> dict[str, Any]:
        """Strictly JSON-safe dict.

        Every event payload in the system passes through here, so this is the
        one place that has to guarantee RFC 8259 compliance. ``model_dump``
        happily returns ``inf``/``nan`` floats even in json mode, and
        ``json.dumps`` then writes bare ``Infinity``/``NaN`` — accepted by
        SQLite (which stores text) and rejected by PostgreSQL JSONB, so a
        payload that worked on the default backend would fail on the
        production one.

        Non-finite values become ``None``. Producers that need the distinction
        to survive should publish an explicit status field alongside the value
        rather than relying on the sentinel.
        """
        return sanitize_json(self.model_dump(mode="json"))


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
