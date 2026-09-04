"""Phase 2 Batch 1.4: every economic threshold is decided on SUPPLIED time.

These are the adversarial cases. Each sits exactly on a threshold where one
millisecond changes the answer, and in each the live clock is deliberately
moved somewhere else entirely before the call. If any of these decisions
still consulted the clock, the original run and its replay would land on
opposite sides of the boundary -- which is precisely how P2-14 and P2-15
produced different trades from an identical recorded market.

Covered thresholds:

A. market data age (TIDAL DataQuality)
B. opportunity expiry
C. opinion expiry
D. degraded-opinion grace window
E. RUNE data-age limit
F. RUNE intent deadline
G. health DEGRADED threshold
H. health OFFLINE threshold
"""

from __future__ import annotations

import pytest

from agents.rune.core import RiskContext, RuneCore
from core.clock import ManualClock
from core.config import RiskLimits
from core.health import HealthRegistry
from core.models.agent import AgentOpinion
from core.models.common import AgentId, DataQuality, Side
from core.models.opportunity import (
    CostBreakdown,
    Opportunity,
    OpportunityKind,
    OpportunityLeg,
    TradeIntent,
)
from core.models.ops import HealthState, HealthStatus, KillSwitchState, SystemHealth
from core.models.portfolio import PortfolioState
from core.models.risk import GateResult, RiskVerdict
from core.state import SystemState
from tests.conftest import START_MS

REQUIRED = ["TIDAL", "NORO", "ZEPHR", "RUNE", "VESKA", "MARIN"]

#: Where the live clock is parked during every call below. Far from every
#: boundary under test, so any decision that consulted it would be obvious.
ELSEWHERE = START_MS + 10_000_000


def _healthy_snapshot() -> SystemHealth:
    return SystemHealth(
        created_at=START_MS,
        components={
            name: HealthState(
                service=name,
                status=HealthStatus.HEALTHY,
                last_heartbeat_ms=START_MS,
            )
            for name in REQUIRED
        },
    )


def _intent(**overrides) -> TradeIntent:
    defaults = dict(
        created_at=START_MS,
        source_data_timestamp=START_MS,
        opportunity_id="opp-1",
        strategy="cross_venue",
        symbol="BTC-USD",
        legs=[
            OpportunityLeg(
                venue="VENUE_A", symbol="BTC-USD", side=Side.BUY, reference_price=100.0
            ),
            OpportunityLeg(
                venue="VENUE_B", symbol="BTC-USD", side=Side.SELL, reference_price=101.0
            ),
        ],
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


def _context(**overrides) -> RiskContext:
    defaults = dict(
        portfolio=PortfolioState(
            created_at=START_MS,
            initial_balance=100_000.0,
            cash=100_000.0,
            peak_equity=100_000.0,
        ),
        kill_switch=KillSwitchState(),
        health=_healthy_snapshot(),
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


def _gate(decision, name):
    return next((g for g in decision.gates if g.name == name), None)


@pytest.fixture
def parked_clock() -> ManualClock:
    """A clock sitting far away from every boundary under test."""
    return ManualClock(ELSEWHERE)


# --------------------------------------------------------------------------
# E / F: RUNE
# --------------------------------------------------------------------------


class TestRuneDataAgeBoundary:
    """E: source data exactly inside / at / one past the age limit."""

    @pytest.fixture
    def rune(self, parked_clock) -> RuneCore:
        return RuneCore(RiskLimits(), parked_clock)

    def _verdict_at(self, rune, *, age: int):
        limits = RiskLimits()
        now = START_MS + limits.max_data_age_ms + age
        return rune.evaluate(
            _intent(source_data_timestamp=START_MS, deadline_ms=now + 10_000),
            _context(),
            now,
        )

    def test_one_ms_inside_the_age_limit_is_approved(self, rune):
        decision = self._verdict_at(rune, age=-1)
        assert decision.verdict is RiskVerdict.APPROVED

    def test_exactly_at_the_age_limit_is_approved(self, rune):
        decision = self._verdict_at(rune, age=0)
        assert decision.verdict is RiskVerdict.APPROVED

    def test_one_ms_past_the_age_limit_is_rejected(self, rune):
        decision = self._verdict_at(rune, age=1)
        assert decision.verdict is RiskVerdict.REJECTED
        assert _gate(decision, "MARKET_DATA_FRESH").result is GateResult.FAIL

    def test_the_verdict_ignores_the_live_clock_entirely(self, rune, parked_clock):
        """The same call, with the clock moved even further away, is
        unchanged -- the answer depends only on the supplied time."""
        first = self._verdict_at(rune, age=0).verdict
        parked_clock.advance(5_000_000)
        assert self._verdict_at(rune, age=0).verdict is first
        assert self._verdict_at(rune, age=1).verdict is RiskVerdict.REJECTED


class TestRuneDeadlineBoundary:
    """F: intent deadline minus one / exactly / plus one."""

    @pytest.fixture
    def rune(self, parked_clock) -> RuneCore:
        return RuneCore(RiskLimits(), parked_clock)

    def _verdict_at(self, rune, *, offset: int):
        deadline = START_MS + 1_000
        now = deadline + offset
        return rune.evaluate(
            _intent(source_data_timestamp=now, deadline_ms=deadline),
            _context(),
            now,
        )

    def test_one_ms_before_the_deadline_is_approved(self, rune):
        assert self._verdict_at(rune, offset=-1).verdict is RiskVerdict.APPROVED

    def test_exactly_at_the_deadline_is_approved(self, rune):
        assert self._verdict_at(rune, offset=0).verdict is RiskVerdict.APPROVED

    def test_one_ms_past_the_deadline_is_rejected(self, rune):
        decision = self._verdict_at(rune, offset=1)
        assert decision.verdict is RiskVerdict.REJECTED
        assert _gate(decision, "INTENT_NOT_EXPIRED").result is GateResult.FAIL


# --------------------------------------------------------------------------
# C / D: opinion expiry and the degraded grace window
# --------------------------------------------------------------------------


def _opinion(agent: AgentId, subject: str, *, expires_at: int) -> AgentOpinion:
    # ``subject`` is derived from correlation_id for per-opportunity agents.
    return AgentOpinion(
        created_at=START_MS,
        expires_at=expires_at,
        correlation_id=subject,
        agent_id=agent,
        symbol="BTC-USD",
        signal=0.5,
        confidence=0.8,
        model_version="test-1",
    )


class TestOpinionQualityBoundary:
    """C and D, through the SystemState API a tick actually uses."""

    GRACE = 500

    @pytest.fixture
    def state(self, parked_clock) -> SystemState:
        s = SystemState(clock=parked_clock)
        s.put_opinion(_opinion(AgentId.TIDAL, "opp-1", expires_at=START_MS + 1_000))
        return s

    @pytest.mark.parametrize(
        ("offset", "expected"),
        [
            (-1, DataQuality.FRESH),
            (0, DataQuality.FRESH),
            (1, DataQuality.DEGRADED),
            (GRACE, DataQuality.DEGRADED),
            (GRACE + 1, DataQuality.STALE),
        ],
    )
    def test_quality_is_decided_on_the_supplied_time(self, state, offset, expected):
        now = START_MS + 1_000 + offset
        slot = state.opinion(
            "opp-1", AgentId.TIDAL, degraded_grace_ms=self.GRACE, now_ms=now
        )
        assert slot is not None
        assert slot.quality is expected

    @pytest.mark.parametrize(
        ("offset", "expected"),
        [
            (0, DataQuality.FRESH),
            (1, DataQuality.DEGRADED),
            (GRACE + 1, DataQuality.STALE),
        ],
    )
    def test_opinions_for_agrees_with_opinion(self, state, offset, expected):
        now = START_MS + 1_000 + offset
        slots = state.opinions_for("opp-1", degraded_grace_ms=self.GRACE, now_ms=now)
        assert slots[AgentId.TIDAL].quality is expected

    def test_two_reads_in_one_logical_tick_cannot_disagree(self, state, parked_clock):
        """The defect this closes: the live clock crossing an expiry between
        two reads inside a single tick, silently changing consensus weight.
        """
        tick_time = START_MS + 1_000
        first = state.opinions_for("opp-1", degraded_grace_ms=0, now_ms=tick_time)
        parked_clock.advance(10_000_000)
        second = state.opinions_for("opp-1", degraded_grace_ms=0, now_ms=tick_time)
        assert first[AgentId.TIDAL].quality is second[AgentId.TIDAL].quality
        assert first[AgentId.TIDAL].quality is DataQuality.FRESH


# --------------------------------------------------------------------------
# B: opportunity expiry
# --------------------------------------------------------------------------


class TestOpportunityExpiryBoundary:
    def _opportunity(self) -> Opportunity:
        return Opportunity(
            created_at=START_MS,
            expires_at=START_MS + 1_000,
            kind=OpportunityKind.CROSS_VENUE_DISLOCATION,
            strategy="cross_venue",
            symbol="BTC-USD",
            legs=[
                OpportunityLeg(
                    venue="VENUE_A",
                    symbol="BTC-USD",
                    side=Side.BUY,
                    reference_price=100.0,
                ),
                OpportunityLeg(
                    venue="VENUE_B",
                    symbol="BTC-USD",
                    side=Side.SELL,
                    reference_price=101.0,
                ),
            ],
            gross_edge_bps=30.0,
        )

    @pytest.mark.parametrize(
        ("offset", "valid"), [(-1, True), (0, True), (1, False)]
    )
    def test_validity_is_decided_on_the_supplied_time(self, offset, valid):
        opportunity = self._opportunity()
        assert opportunity.is_valid_at(START_MS + 1_000 + offset) is valid

    def test_add_opportunity_stamps_the_opportunitys_own_birth_time(
        self, parked_clock
    ):
        """Scheduling delay must not move a state-machine timestamp."""
        state = SystemState(clock=parked_clock)
        record = state.add_opportunity(self._opportunity())
        assert record.updated_at == START_MS


# --------------------------------------------------------------------------
# G / H: health thresholds
# --------------------------------------------------------------------------


class TestHealthAgeBoundaries:
    """G and H: a heartbeat keeps its true arrival time; how OLD it is when
    a decision asks is judged at the supplied time."""

    @pytest.fixture
    def registry(self, parked_clock) -> HealthRegistry:
        reg = HealthRegistry(clock=ManualClock(START_MS))
        reg.register("TIDAL", "v1")
        reg.heartbeat("TIDAL", status=HealthStatus.HEALTHY, version="v1")
        # From here on, the registry's own clock is the parked one: any
        # decision that consulted it would report a wildly stale component.
        reg.clock = parked_clock
        return reg

    def _degraded_after(self, registry) -> int:
        return registry.policy.degraded_after_ms

    def test_g_one_ms_before_degraded_threshold_is_healthy(self, registry):
        now = START_MS + self._degraded_after(registry) - 1
        assert registry.status_of("TIDAL", now) is HealthStatus.HEALTHY

    def test_g_at_degraded_threshold_is_degraded(self, registry):
        now = START_MS + self._degraded_after(registry)
        assert registry.status_of("TIDAL", now) is HealthStatus.DEGRADED

    def test_h_one_ms_before_offline_threshold_is_not_offline(self, registry):
        now = START_MS + registry.policy.offline_after_ms - 1
        assert registry.status_of("TIDAL", now) is not HealthStatus.OFFLINE

    def test_h_at_offline_threshold_is_offline(self, registry):
        now = START_MS + registry.policy.offline_after_ms
        assert registry.status_of("TIDAL", now) is HealthStatus.OFFLINE

    def test_a_heartbeat_from_after_the_evaluation_instant_is_not_stale(
        self, registry
    ):
        """Defined semantics, not a silent clamp.

        A component heartbeats off the live clock, which a concurrent feed
        can push past the canonical time of the tick currently asking. Such
        a heartbeat is newer than the question, so the component cannot be
        stale at that instant -- and, critically, the resulting STATUS is
        the same whether it landed just before or just after, which is what
        keeps the original run and its replay in agreement.
        """
        future = START_MS + 5_000
        registry.heartbeat("NORO", status=HealthStatus.HEALTHY, version="v1")
        registry._states["NORO"].last_heartbeat_ms = future

        asked_at = START_MS
        assert registry.status_of("NORO", asked_at) is HealthStatus.HEALTHY
        # The true arrival time is preserved, not rewritten.
        assert registry.snapshot(asked_at).components["NORO"].last_heartbeat_ms == future
        # And it agrees with the same heartbeat landing exactly on the instant.
        registry._states["NORO"].last_heartbeat_ms = asked_at
        assert registry.status_of("NORO", asked_at) is HealthStatus.HEALTHY

    def test_snapshot_and_all_healthy_use_the_same_supplied_time(self, registry):
        now = START_MS + self._degraded_after(registry) - 1
        snapshot = registry.snapshot(now)
        assert snapshot.components["TIDAL"].status is HealthStatus.HEALTHY
        ok, _bad = registry.all_healthy(["TIDAL"], now)
        assert ok

        later = START_MS + registry.policy.offline_after_ms
        assert registry.snapshot(later).components["TIDAL"].status is (
            HealthStatus.OFFLINE
        )
        ok, bad = registry.all_healthy(["TIDAL"], later)
        assert not ok and "TIDAL" in bad
