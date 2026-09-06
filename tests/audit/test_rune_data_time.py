"""Phase 5 — P5-9: data age, future timestamps, deadlines, and health decay.

``gate_data_age`` computes ``age = now_ms - source_data_timestamp``. It used to
pass whenever ``age <= max_data_age_ms``, and every negative number satisfies
that — so a timestamp in the FUTURE read as maximally fresh, and the further
wrong it was, the fresher the gate called it.

TIDAL already refuses to publish a venue whose exchange timestamp leads local
receipt by more than ``max_clock_skew_ms`` (TIDAL-H3), so upstream was meant to
make this unreachable. Remediation E2 gave RUNE — the final hard boundary — its
own lower bound rather than relying on that: the permitted interval is now
``-max_clock_skew_ms <= age <= max_data_age_ms``, reusing the tolerance the
platform already configures for exactly this question instead of inventing a
second number. Both defences are asserted below.
"""

from __future__ import annotations

import inspect

import pytest

from core.config import RiskLimits
from core.models.risk import GateResult, RiskVerdict
from risk import limits as gates
from tests.audit.rune_fixtures import (
    blocking_names,
    context,
    core,
    gate_named,
    health,
    intent,
)
from tests.conftest import START_MS

LIMITS = RiskLimits()
MAX_AGE = LIMITS.max_data_age_ms      # 2,000
MAX_SKEW = LIMITS.max_clock_skew_ms   # 2,000


def age_check(offset_ms: int):
    """Evaluate an intent whose source data is ``offset_ms`` old.

    A negative offset puts the observation in the future.
    """
    decision = core(LIMITS).evaluate(
        intent(source_data_timestamp=START_MS - offset_ms), context(), START_MS
    )
    return decision, gate_named(decision, "MARKET_DATA_FRESH")


class TestOrdinaryAges:
    @pytest.mark.parametrize("age", [0, 1, 500, MAX_AGE - 1, MAX_AGE])
    def test_data_at_or_within_the_limit_passes(self, age):
        decision, check = age_check(age)
        assert check.result is GateResult.PASS
        assert check.observed == pytest.approx(float(age))
        assert decision.verdict is RiskVerdict.APPROVED

    @pytest.mark.parametrize("age", [MAX_AGE + 1, MAX_AGE * 2, 10_000_000])
    def test_data_past_the_limit_blocks(self, age):
        decision, check = age_check(age)
        assert check.result is GateResult.FAIL
        assert "MARKET_DATA_FRESH" in blocking_names(decision)

    def test_the_boundary_is_inclusive(self):
        """``age <= max_data_age_ms``: exactly at the limit is fresh enough."""
        assert age_check(MAX_AGE)[1].result is GateResult.PASS
        assert age_check(MAX_AGE + 1)[1].result is GateResult.FAIL

    def test_a_missing_timestamp_is_unknown_and_blocks(self):
        decision = core(LIMITS).evaluate(
            intent(source_data_timestamp=None), context(), START_MS
        )
        check = gate_named(decision, "MARKET_DATA_FRESH")
        assert check.result is GateResult.UNKNOWN
        assert check.blocking


class TestFutureTimestamps:
    """H6. A source observation stamped ahead of the deciding instant."""

    def test_a_future_timestamp_produces_a_negative_age(self):
        """The premise, before any judgement about it."""
        _decision, check = age_check(-500)
        assert check.observed == pytest.approx(-500.0)

    @pytest.mark.parametrize("lead_ms", [1, 500, MAX_SKEW - 1, MAX_SKEW])
    def test_ordinary_clock_drift_is_tolerated(self, lead_ms):
        """A control, not a finding.

        Two independently-synced machines disagree by small amounts all the
        time, and ``max_clock_skew_ms`` is the platform's own statement of how
        much of that is ordinary. Accepting a lead inside that tolerance is
        correct behaviour and is pinned here so the finding below is scoped to
        leads the platform itself calls implausible.
        """
        decision, _check = age_check(-lead_ms)
        assert decision.verdict is RiskVerdict.APPROVED

    @pytest.mark.parametrize(
        "lead_ms",
        [
            MAX_SKEW + 1,
            60_000,
            86_400_000,       # a day ahead
            31_536_000_000,   # a year ahead
        ],
    )
    def test_implausibly_fresh_data_must_not_authorise_a_trade(self, lead_ms):
        """A market observation cannot have happened long after the decision.

        Beyond ``max_clock_skew_ms`` — the platform's own definition of how far
        a timestamp may lead before it stops being ordinary clock drift — an
        observation from the future is not fresh data, it is broken data. The
        deterministic hard boundary should refuse it rather than treat "age is
        very negative" as "age is very small".
        """
        decision, check = age_check(-lead_ms)
        assert decision.verdict is RiskVerdict.REJECTED, (
            "a source observation stamped "
            f"{lead_ms}ms in the future was accepted as fresh market data: "
            f"observed_age={check.observed} limit={check.limit} "
            f"max_clock_skew_ms={MAX_SKEW} "
            f"blocking={blocking_names(decision)}"
        )

    def test_the_gate_now_has_a_lower_bound(self):
        """Structural: records exactly what the comparison is, so a future
        change is visible.

        The bound is ``max_clock_skew_ms``, not a hardcoded zero: honest data
        from a machine whose clock differs by a few milliseconds must still
        trade, and the tolerance for that is already configured.
        """
        source = inspect.getsource(gates.gate_data_age)
        assert "lower <= age <= limits.max_data_age_ms" in source
        assert "lower = -limits.max_clock_skew_ms" in source

    def test_the_gate_still_reads_no_clock(self):
        """``now_ms`` is supplied, never sampled — otherwise the same intent
        would reach different verdicts on replay."""
        source = inspect.getsource(gates.gate_data_age)
        body = source.split('"""')[-1]
        for sampled in ("time.time", "datetime", "monotonic", "clock.now", ".now()"):
            assert sampled not in body

    def test_the_detail_names_the_permitted_interval(self):
        """A rejection must say what would have been accepted; "age -500ms"
        against "limit 2000ms" reads like a pass."""
        _decision, check = age_check(-(MAX_SKEW + 1))
        assert f"[-{MAX_SKEW}, {MAX_AGE}]" in (check.detail or "")

    def test_the_lower_boundary_is_inclusive(self):
        """``-max_clock_skew_ms <= age``: exactly at the tolerance is drift."""
        assert age_check(-MAX_SKEW)[1].result is GateResult.PASS
        assert age_check(-(MAX_SKEW + 1))[1].result is GateResult.FAIL

    def test_the_observed_value_is_still_the_signed_age(self):
        """The lower bound changed the verdict, not the reported quantity."""
        _decision, check = age_check(-(MAX_SKEW + 1))
        assert check.observed == pytest.approx(-float(MAX_SKEW + 1))
        assert check.limit == pytest.approx(float(MAX_AGE))

    def test_upstream_is_where_skew_is_currently_caught(self):
        """Records the defence that does exist, so the finding is scoped.

        TIDAL's ``_quality`` refuses a book whose exchange timestamp leads
        local receipt beyond tolerance, so a future ``source_data_timestamp``
        cannot normally reach RUNE through the ordinary pipeline.
        """
        from agents.tidal.agent import Tidal

        source = inspect.getsource(Tidal._quality)
        assert "max_clock_skew_ms" in source
        assert "DataQuality.UNAVAILABLE" in source


class TestDeadline:
    """§21 — an expired intent must be unrescuable."""

    def test_before_the_deadline_passes(self):
        decision = core(LIMITS).evaluate(
            intent(deadline_ms=START_MS + 1), context(), START_MS
        )
        assert gate_named(decision, "INTENT_NOT_EXPIRED").result is GateResult.PASS

    def test_exactly_at_the_deadline_passes(self):
        """``now_ms <= deadline_ms``: the boundary is inclusive."""
        decision = core(LIMITS).evaluate(
            intent(deadline_ms=START_MS), context(), START_MS
        )
        assert gate_named(decision, "INTENT_NOT_EXPIRED").result is GateResult.PASS

    def test_one_millisecond_past_the_deadline_blocks(self):
        decision = core(LIMITS).evaluate(
            intent(deadline_ms=START_MS - 1), context(), START_MS
        )
        assert "INTENT_NOT_EXPIRED" in blocking_names(decision)
        assert decision.verdict is RiskVerdict.REJECTED

    @pytest.mark.parametrize(
        "rescue",
        [
            {"consensus_agreement": 1.0, "consensus_score": 1.0},
            {"expected_net_edge_bps": 10_000.0},
            {"notional": 250.0},
            {"urgency": 1.0},
        ],
    )
    def test_nothing_on_the_intent_rescues_an_expired_one(self, rescue):
        decision = core(LIMITS).evaluate(
            intent(deadline_ms=START_MS - 1, **rescue), context(), START_MS
        )
        assert decision.verdict is RiskVerdict.REJECTED
        assert "INTENT_NOT_EXPIRED" in blocking_names(decision)

    def test_abundant_liquidity_does_not_rescue_an_expired_intent(self):
        decision = core(LIMITS).evaluate(
            intent(deadline_ms=START_MS - 1),
            context(max_economical_notional=10_000_000.0),
            START_MS,
        )
        assert decision.verdict is RiskVerdict.REJECTED

    def test_the_core_has_no_channel_through_which_ai_could_rescue_one(self):
        """``RuneCore`` cannot see commentary at all: it is applied by
        ``Rune.evaluate`` after the verdict exists. Pinned here alongside the
        other rescue attempts because "expired" must be final."""
        from agents.rune.core import RiskContext, RuneCore

        params = set(inspect.signature(RuneCore.evaluate).parameters)
        assert params == {"self", "intent", "ctx", "now_ms"}
        fields = set(RiskContext.__dataclass_fields__)
        assert not [f for f in fields if f == "ai" or f.startswith("ai_")]
        assert "commentary" not in " ".join(fields)


class TestSystemHealthDecay:
    """§26 — a component whose heartbeat is old must not stay healthy forever.

    ``HealthRegistry._aged`` applies the decay, and ``SystemHealth.required_ok``
    reads whatever status the snapshot already carries. RUNE receives a
    snapshot, so the decay must have been applied before it arrives.
    """

    def test_required_ok_reads_the_status_on_the_snapshot(self):
        from core.models.ops import SystemHealth

        source = inspect.getsource(SystemHealth.required_ok)
        assert "status is not HealthStatus.HEALTHY" in source
        assert "name not in self.components" in source

    def test_a_stale_heartbeat_decays_before_rune_sees_it(self):
        from core.clock import ManualClock
        from core.health import HealthRegistry
        from core.models.ops import HealthStatus

        clock = ManualClock(START_MS)
        registry = HealthRegistry(clock=clock)
        for name in ("TIDAL", "NORO", "ZEPHR", "RUNE", "VESKA", "MARIN"):
            registry.register(name, "v")
            registry.heartbeat(name, status=HealthStatus.HEALTHY)

        fresh = registry.snapshot(START_MS)
        assert fresh.required_ok(["VESKA"])[0]

        # Far past ``offline_after_ms``.
        stale = registry.snapshot(START_MS + 3_600_000)
        assert not stale.required_ok(["VESKA"])[0]

        decision = core(LIMITS).evaluate(intent(), context(health=stale), START_MS)
        assert "SYSTEM_HEALTHY" in blocking_names(decision)

    def test_a_component_that_never_heartbeat_is_not_healthy(self):
        from core.clock import ManualClock
        from core.health import HealthRegistry

        clock = ManualClock(START_MS)
        registry = HealthRegistry(clock=clock)
        registry.register("VESKA", "v")
        snapshot = registry.snapshot(START_MS)
        assert not snapshot.required_ok(["VESKA"])[0]

    def test_an_absent_component_blocks_rather_than_being_ignored(self):
        decision = core(LIMITS).evaluate(
            intent(), context(health=health(drop=("ZEPHR",))), START_MS
        )
        assert "SYSTEM_HEALTHY" in blocking_names(decision)

    def test_execution_health_reads_veska_specifically(self):
        from core.models.ops import HealthStatus

        decision = core(LIMITS).evaluate(
            intent(), context(health=health(veska=HealthStatus.DEGRADED)), START_MS
        )
        assert "EXECUTION_HEALTHY" in blocking_names(decision)
        # And the general health gate agrees, because VESKA is required.
        assert "SYSTEM_HEALTHY" in blocking_names(decision)
