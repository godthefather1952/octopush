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


class RiskUtilization(Base):
    """Current consumption of each deterministic limit, for the dashboard."""

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
