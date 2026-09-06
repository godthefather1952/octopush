"""Shared builders for the Phase 5 RUNE hard-risk audit.

Audit-only. Nothing here is production code and nothing here may be wired
into production.

Everything is constructed explicitly rather than derived from a running
platform, because the audit's job is to vary exactly one risk dimension at a
time and observe what the final authorization boundary does. A scenario built
by driving the simulator would couple the answer to whatever the market
happened to do.

The builders deliberately mirror ``tests/unit/test_risk.py``'s shapes so an
audit result can be read against the existing unit suite without translating
between two sets of fixtures. They are duplicated rather than imported: the
unit suite is a validated baseline this pass must not disturb, and an audit
that reaches into it would make the two move together.
"""

from __future__ import annotations

from dataclasses import dataclass

from agents.rune.core import RiskContext, RuneCore
from core.clock import ManualClock
from core.config import RiskLimits
from core.models.common import Side
from core.models.opportunity import CostBreakdown, OpportunityLeg, TradeIntent
from core.models.ops import HealthState, HealthStatus, KillSwitchState, SystemHealth
from core.models.portfolio import PortfolioState, PositionState
from core.models.risk import GateCheck, RiskDecision
from tests.conftest import START_MS

#: The strategy's own required-component list, restated so the audit does not
#: depend on an import from the strategy package it is auditing.
REQUIRED = ["TIDAL", "NORO", "ZEPHR", "RUNE", "VESKA", "MARIN"]

SYMBOL = "BTC-USD"
VENUE_A = "VENUE_A"
VENUE_B = "VENUE_B"
VENUE_C = "VENUE_C"


# ======================================================================
# builders
# ======================================================================


def health(
    *,
    veska: HealthStatus = HealthStatus.HEALTHY,
    statuses: dict[str, HealthStatus] | None = None,
    drop: tuple[str, ...] = (),
    heartbeat_ms: int = START_MS,
) -> SystemHealth:
    """A health snapshot with every required component present and HEALTHY.

    ``drop`` removes a component entirely (never heartbeat); ``statuses``
    overrides individual ones.
    """
    overrides = statuses or {}
    components = {
        name: HealthState(
            service=name,
            status=overrides.get(name, veska if name == "VESKA" else HealthStatus.HEALTHY),
            last_heartbeat_ms=heartbeat_ms,
        )
        for name in REQUIRED
        if name not in drop
    }
    return SystemHealth(created_at=START_MS, components=components)


def portfolio(**overrides) -> PortfolioState:
    defaults = dict(
        created_at=START_MS,
        initial_balance=100_000.0,
        cash=100_000.0,
        peak_equity=100_000.0,
    )
    defaults.update(overrides)
    return PortfolioState(**defaults)


def position(
    venue: str,
    symbol: str = SYMBOL,
    *,
    quantity: float,
    price: float = 100.0,
) -> PositionState:
    """A marked position whose ``notional`` is exactly ``|quantity| * price``."""
    return PositionState(
        venue=venue,
        symbol=symbol,
        quantity=quantity,
        average_entry_price=price,
        mark_price=price,
        updated_at=START_MS,
    )


def portfolio_with(*positions: PositionState, **overrides) -> PortfolioState:
    return portfolio(
        positions={p.key: p for p in positions},
        **overrides,
    )


def leg(venue: str, side: Side, symbol: str = SYMBOL, price: float = 100.0):
    return OpportunityLeg(venue=venue, symbol=symbol, side=side, reference_price=price)


#: The ordinary two-leg cross-venue shape: buy the cheap venue, sell the rich
#: one. This is what the platform actually trades, so it is the default.
CROSS_VENUE_LEGS = (
    leg(VENUE_A, Side.BUY),
    leg(VENUE_B, Side.SELL),
)


def intent(**overrides) -> TradeIntent:
    defaults = dict(
        created_at=START_MS,
        source_data_timestamp=START_MS - 50,
        opportunity_id="opp-1",
        strategy="cross_venue",
        symbol=SYMBOL,
        legs=list(CROSS_VENUE_LEGS),
        notional=5_000.0,
        gross_edge_bps=30.0,
        costs=CostBreakdown(fees_bps=10.0),
        expected_net_edge_bps=20.0,
        consensus_score=0.8,
        consensus_agreement=0.8,
        max_slippage_bps=10.0,
        deadline_ms=START_MS + 2_000,
    )
    defaults.update(overrides)
    return TradeIntent(**defaults)


def context(**overrides) -> RiskContext:
    defaults = dict(
        portfolio=portfolio(),
        kill_switch=KillSwitchState(),
        health=health(),
        consensus_threshold=0.6,
        consensus_complete=True,
        required_components=REQUIRED,
        open_orders=0,
        error_rate=0.0,
        unhedged_notional=0.0,
        strategy_exposure=0.0,
        max_economical_notional=25_000.0,
        hedge_available=True,
    )
    defaults.update(overrides)
    return RiskContext(**defaults)


def core(limits: RiskLimits | None = None, *, start_ms: int = START_MS) -> RuneCore:
    """A RuneCore on a stationary manual clock.

    Every audit call passes ``now_ms`` explicitly, so the clock exists only to
    satisfy the constructor -- and a stationary one makes an accidental live
    read visible as a wrong answer rather than as a plausible one.
    """
    return RuneCore(limits or RiskLimits(), ManualClock(start_ms))


def gate_named(decision: RiskDecision, name: str) -> GateCheck:
    return next(g for g in decision.gates if g.name == name)


def has_gate(decision: RiskDecision, name: str) -> bool:
    return any(g.name == name for g in decision.gates)


def blocking_names(decision: RiskDecision) -> list[str]:
    return [g.name for g in decision.gates if g.blocking]


# ======================================================================
# audit-only comparators
# ======================================================================


@dataclass(frozen=True)
class ExposureAccounting:
    """What each exposure dimension WOULD be if every leg actually filled.

    An audit-only reference implementation, written from the economic
    definition rather than from production's formula, so the two can be
    compared instead of production being compared with itself.

    ``per_leg_notional`` is the quote notional each leg trades. The platform's
    ``TradeIntent.notional`` is per-leg -- VESKA sizes every leg as
    ``notional / expected_price`` -- so a two-leg trade puts ``notional`` on
    each of two venues and contributes ``2 * notional`` of gross exposure.
    That is the unit this whole audit measures in.
    """

    per_leg_notional: float
    leg_count: int

    @property
    def gross_contribution(self) -> float:
        """Gross exposure the trade adds, assuming every leg opens."""
        return self.per_leg_notional * self.leg_count

    @property
    def strategy_contribution(self) -> float:
        """What one opportunity consumes of a strategy's gross budget.

        Identical to :attr:`gross_contribution`: a strategy's exposure is the
        gross notional its open trades hold, and there is nothing about the
        strategy label that halves a leg.
        """
        return self.gross_contribution


def strategy_exposure_as_gate_sees_it(
    working_notional: dict[str, float], intent_notional: float, leg_count: int
) -> float:
    """Reproduce production's MAX_STRATEGY_EXPOSURE projection exactly.

    Diagnostic only -- the audit asserts the *safety* property, not this
    number. It exists so a failure message can show both sides.

    Production computes ``ctx.strategy_exposure + intent.notional * legs``
    where ``ctx.strategy_exposure`` is
    ``Orchestrator._current_strategy_exposure()``.

    Since the P5-2 remediation each ``working_notional`` entry is one
    opportunity's ``approved_notional * len(legs)`` -- GROSS across its legs --
    so passing a gross-valued map here makes this agree with
    :func:`true_strategy_exposure`. Before the fix the entries were per-leg and
    the two diverged by a factor approaching the leg count, which is exactly
    what this pair of comparators was built to expose. Keeping both means a
    future regression to per-leg storage is still detectable rather than
    silently self-consistent.
    """
    return sum(working_notional.values()) + intent_notional * leg_count


def true_strategy_exposure(
    working: dict[str, tuple[float, int]], intent_notional: float, leg_count: int
) -> float:
    """The same quantity in consistent units.

    ``working`` maps opportunity id -> (per-leg notional, leg count), so an
    already-working two-leg trade contributes twice its per-leg notional,
    exactly as the incoming intent does.
    """
    reserved = sum(notional * legs for notional, legs in working.values())
    return reserved + intent_notional * leg_count


__all__ = [
    "CROSS_VENUE_LEGS",
    "REQUIRED",
    "SYMBOL",
    "VENUE_A",
    "VENUE_B",
    "VENUE_C",
    "ExposureAccounting",
    "blocking_names",
    "context",
    "core",
    "gate_named",
    "has_gate",
    "health",
    "intent",
    "leg",
    "portfolio",
    "portfolio_with",
    "position",
    "strategy_exposure_as_gate_sees_it",
    "true_strategy_exposure",
]
