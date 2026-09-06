"""Risk decision schemas.

RUNE-CORE is deterministic code. Its verdict is expressed here; nothing —
including any AI-produced opinion — may overturn a failed mandatory gate.
"""

from __future__ import annotations

from pydantic import Field

from core.models.common import Base, Envelope, StrEnum


class GateResult(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    #: The gate could not be evaluated. Treated as FAIL for mandatory gates:
    #: an unevaluable mandatory gate is never an implicit pass.
    UNKNOWN = "UNKNOWN"


class GateCheck(Base):
    name: str
    result: GateResult
    mandatory: bool = True
    detail: str = ""
    observed: float | None = None
    limit: float | None = None

    @property
    def blocking(self) -> bool:
        return self.mandatory and self.result is not GateResult.PASS


class RiskVerdict(StrEnum):
    APPROVED = "APPROVED"
    #: Approved but for less notional than requested.
    APPROVED_REDUCED = "APPROVED_REDUCED"
    REJECTED = "REJECTED"


class RiskDecision(Envelope):
    """The output of RUNE. No trade proceeds without one."""

    decision_id: str
    intent_id: str
    strategy: str
    symbol: str
    verdict: RiskVerdict
    #: Notional RUNE authorises, which may be below the requested notional.
    approved_notional: float = 0.0
    requested_notional: float = 0.0
    gates: list[GateCheck] = Field(default_factory=list)
    reason_codes: list[str] = Field(default_factory=list)
    #: Advisory commentary from RUNE-AI. Never affects the verdict.
    ai_commentary: str | None = None
    ai_concern_level: float | None = None

    @property
    def approved(self) -> bool:
        return self.verdict in (RiskVerdict.APPROVED, RiskVerdict.APPROVED_REDUCED)

    @property
    def failed_gates(self) -> list[GateCheck]:
        return [g for g in self.gates if g.blocking]


class CommittedExposure(Base):
    """Exposure the platform has committed to but that has not yet filled.

    THE WINDOW THIS CLOSES
    ======================
    Every exposure gate reads :class:`~core.models.portfolio.PortfolioState`,
    and a portfolio only moves when a fill lands. Between authorisation and
    fill a trade is real risk — its orders are live and can execute at any
    moment — yet it was invisible to MAX_GROSS_EXPOSURE, MAX_NET_EXPOSURE,
    MAX_LEVERAGE, MAX_VENUE_EXPOSURE and MAX_POSITION_NOTIONAL. The
    orchestrator authorises opportunities back to back inside one ``_seek``
    pass with no settlement between them, so two trades could each be
    authorised against the same empty book and together breach a limit
    neither of them individually approached (P5-1).

    A DERIVED SNAPSHOT, NOT A LEDGER
    ================================
    This is a *value* computed from authoritative order state on demand, never
    a second mutable ledger that has to be incremented on submit, decremented
    on partial fill, and released on cancel/reject/expiry. A ledger like that
    can drift from the order lifecycle it is supposed to mirror; a snapshot
    recomputed from :attr:`core.state.SystemState.orders` cannot.
    ``Orchestrator._current_committed_exposure`` is the one place that builds
    it.

    UNITS
    =====
    Quote notional, in the same unit as ``PortfolioState.gross_exposure`` —
    i.e. already summed across legs, NOT a per-leg figure. Each contributing
    order reserves ``remaining_quantity * expected_price``, which reconstructs
    the per-leg ``approved_notional`` for an untouched order and shrinks as it
    fills, so the reservation hands over to the position it becomes rather
    than double-counting alongside it.
    """

    #: Unsigned quote notional still working, summed over every reserved order.
    gross_exposure: float = 0.0
    #: The same amount signed by side (+BUY / -SELL), so it can be added
    #: directly to ``PortfolioState.net_exposure``.
    net_exposure: float = 0.0
    #: Venue -> unsigned committed notional on that venue.
    venue_exposure: dict[str, float] = Field(default_factory=dict)
    #: ``"venue:symbol"`` -> unsigned committed notional on that position, keyed
    #: exactly as ``PortfolioState.positions`` is.
    position_exposure: dict[str, float] = Field(default_factory=dict)

    @property
    def is_zero(self) -> bool:
        """Whether anything at all is reserved. Used only to keep gate
        ``detail`` strings quiet when there is nothing to explain."""
        return (
            self.gross_exposure == 0.0
            and self.net_exposure == 0.0
            and not self.venue_exposure
            and not self.position_exposure
        )


class RiskUtilization(Base):
    """Current consumption of each deterministic limit, for the dashboard."""

    #: Filled PLUS committed, so the dashboard shows the same exposure the
    #: gates judge against rather than only what has settled.
    gross_exposure: float = 0.0
    max_gross_exposure: float = 0.0
    net_exposure: float = 0.0
    max_net_exposure: float = 0.0
    day_loss: float = 0.0
    max_day_loss: float = 0.0
    drawdown: float = 0.0
    max_drawdown: float = 0.0
    unhedged_notional: float = 0.0
    max_unhedged_notional: float = 0.0
    venue_exposure: dict[str, float] = Field(default_factory=dict)
    max_venue_exposure: float = 0.0
    strategy_exposure: dict[str, float] = Field(default_factory=dict)
    max_strategy_exposure: float = 0.0
    #: The committed-but-unfilled share of the three figures above, so a
    #: reader can tell a number that moved because a fill landed from one that
    #: moved because an order was submitted. Reported, never subtracted: the
    #: totals already include it.
    committed_gross_exposure: float = 0.0
    committed_net_exposure: float = 0.0

    @staticmethod
    def _pct(used: float, limit: float) -> float:
        if limit <= 0:
            return 0.0
        return min(1.0, max(0.0, used / limit))

    def worst_utilization(self) -> float:
        return max(
            self._pct(self.gross_exposure, self.max_gross_exposure),
            self._pct(abs(self.net_exposure), self.max_net_exposure),
            self._pct(self.day_loss, self.max_day_loss),
            self._pct(self.drawdown, self.max_drawdown),
            self._pct(self.unhedged_notional, self.max_unhedged_notional),
        )
