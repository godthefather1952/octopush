"""Phase 5 — the gate inventory, and the rules that bind every gate.

RUNE is the last thing between a wanted trade and a submitted order. These
tests pin the structural properties that make it a boundary rather than a
suggestion: every gate runs, one failure is enough, an unevaluable mandatory
gate blocks, and nothing downstream can rewrite a verdict.
"""

from __future__ import annotations

import inspect

import pytest

from agents.rune.core import RuneCore
from core.config import RiskLimits
from core.models.common import Side
from core.models.ops import HealthStatus, KillSwitchState
from core.models.risk import GateResult, RiskVerdict
from risk import limits as gates
from tests.audit.rune_fixtures import (
    REQUIRED,
    VENUE_A,
    blocking_names,
    context,
    core,
    gate_named,
    has_gate,
    health,
    intent,
    portfolio,
)
from tests.conftest import START_MS

#: Every gate RUNE-CORE is expected to run on an entry intent, read off
#: ``RuneCore._gate``. Stated here as data so a gate silently disappearing
#: from the list is a test failure rather than an absence nobody notices.
EXPECTED_GATES = [
    "KILL_SWITCH_CLEAR",
    "SYSTEM_HEALTHY",
    "EXECUTION_HEALTHY",
    "CONSENSUS_THRESHOLD",
    "MARKET_DATA_FRESH",
    "INTENT_NOT_EXPIRED",
    "MIN_EXPECTED_EDGE",
    "LIQUIDITY_SUFFICIENT",
    "HEDGE_AVAILABLE",
    "MAX_ORDER_NOTIONAL",
    "MAX_POSITION_NOTIONAL",
    "MAX_GROSS_EXPOSURE",
    "MAX_NET_EXPOSURE",
    "MAX_LEVERAGE",
    "MAX_VENUE_EXPOSURE",
    "MAX_STRATEGY_EXPOSURE",
    "MAX_DAILY_LOSS",
    "MAX_DRAWDOWN",
    "MAX_UNHEDGED_EXPOSURE",
    "MAX_OPEN_ORDERS",
    "MAX_ERROR_RATE",
]

#: ``gate_consensus`` emits one of two names depending on completeness, so the
#: inventory above lists the complete-consensus spelling and this is the other.
CONSENSUS_INCOMPLETE_GATE = "CONSENSUS_COMPLETE"


class TestGateInventory:
    def test_every_expected_gate_runs_on_a_clean_intent(self):
        decision = core().evaluate(intent(), context(), START_MS)
        names = [g.name for g in decision.gates]
        assert names == EXPECTED_GATES, (
            "the gate list changed; a gate added or removed from RuneCore._gate "
            "changes what the final authorization boundary actually checks"
        )

    def test_no_gate_is_evaluated_twice(self):
        decision = core().evaluate(intent(), context(), START_MS)
        names = [g.name for g in decision.gates]
        assert len(names) == len(set(names))

    def test_every_gate_is_mandatory(self):
        """An advisory hard-risk gate is a contradiction. If one is ever added,
        this test is where the decision has to be made deliberately."""
        decision = core().evaluate(intent(), context(), START_MS)
        advisory = [g.name for g in decision.gates if not g.mandatory]
        assert not advisory, f"non-mandatory gates in the hard-risk path: {advisory}"

    def test_the_incomplete_consensus_spelling_is_reachable(self):
        decision = core().evaluate(
            intent(), context(consensus_complete=False), START_MS
        )
        assert has_gate(decision, CONSENSUS_INCOMPLETE_GATE)
        assert not has_gate(decision, "CONSENSUS_THRESHOLD")
        assert decision.verdict is RiskVerdict.REJECTED

    def test_every_gate_function_exported_by_risk_limits_is_used(self):
        """A gate that exists but is never called is not a gate.

        ``risk.limits.__all__`` is the module's own statement of what it
        provides; ``RuneCore._gate`` is what actually runs. They must agree.
        """
        exported = {n for n in gates.__all__ if n.startswith("gate_")}
        source = inspect.getsource(RuneCore._gate)
        unused = sorted(name for name in exported if f"gates.{name}(" not in source)
        assert not unused, f"exported but never run by RuneCore._gate: {unused}"


class TestOneFailureIsEnough:
    """A single blocking gate rejects, regardless of what else passed."""

    @pytest.mark.parametrize(
        ("name", "ctx_kwargs", "intent_kwargs"),
        [
            ("KILL_SWITCH_CLEAR", {"kill_switch": KillSwitchState(halt_new_trades=True)}, {}),
            ("SYSTEM_HEALTHY", {"health": health(statuses={"NORO": HealthStatus.OFFLINE})}, {}),
            ("EXECUTION_HEALTHY", {"health": health(veska=HealthStatus.DEGRADED)}, {}),
            ("CONSENSUS_THRESHOLD", {}, {"consensus_agreement": 0.1}),
            ("MARKET_DATA_FRESH", {}, {"source_data_timestamp": START_MS - 999_999}),
            ("INTENT_NOT_EXPIRED", {}, {"deadline_ms": START_MS - 1}),
            ("MIN_EXPECTED_EDGE", {}, {"expected_net_edge_bps": 0.0}),
            ("HEDGE_AVAILABLE", {"hedge_available": False}, {}),
            ("MAX_OPEN_ORDERS", {"open_orders": 20}, {}),
            ("MAX_ERROR_RATE", {"error_rate": 1.0}, {}),
        ],
    )
    def test_a_single_blocking_gate_rejects(self, name, ctx_kwargs, intent_kwargs):
        decision = core().evaluate(
            intent(**intent_kwargs), context(**ctx_kwargs), START_MS
        )
        assert decision.verdict is RiskVerdict.REJECTED
        assert name in blocking_names(decision)
        assert name in decision.reason_codes
        assert decision.approved_notional == 0.0

    def test_an_impossible_unhedged_state_also_rejects(self):
        """MAX_UNHEDGED_EXPOSURE left the parametrisation above in Remediation
        D, and could not stay in it.

        Once a gate is mirrored in ``_headroom`` it can no longer be reached as
        a *blocking* gate: either the reduction makes it pass, or headroom is
        zero and ``evaluate`` short-circuits on MIN_TRADE_NOTIONAL first. That
        is true of every size-sensitive gate here — which is why the list above
        contains none of them — and MAX_UNHEDGED_EXPOSURE became size-sensitive
        when its worst intermediate leg risk was accounted for (P5-18).

        The safety property is unchanged and asserted on both halves: the
        decision authorises nothing, and the gate itself still fails the state.
        """
        from risk import limits as gates

        decision = core().evaluate(
            intent(), context(unhedged_notional=10_000_000.0), START_MS
        )
        assert decision.verdict is RiskVerdict.REJECTED
        assert decision.approved_notional == 0.0
        assert decision.reason_codes == ["MIN_TRADE_NOTIONAL"]
        assert gates.gate_unhedged(
            intent(), 10_000_000.0, core().limits
        ).blocking

    def test_reason_codes_name_exactly_the_blocking_gates(self):
        """Not a superset and not a subset: an operator reading reason codes is
        reading the list of things that must change for the trade to happen."""
        decision = core().evaluate(
            intent(consensus_agreement=0.1, expected_net_edge_bps=0.0),
            context(open_orders=20),
            START_MS,
        )
        assert sorted(decision.reason_codes) == sorted(blocking_names(decision))
        assert set(decision.reason_codes) == {
            "CONSENSUS_THRESHOLD",
            "MIN_EXPECTED_EDGE",
            "MAX_OPEN_ORDERS",
        }

    def test_a_rejection_reports_every_gate_it_ran(self):
        """The full gate list travels on the decision even when one fails, so
        a rejection is diagnosable without re-running risk."""
        decision = core().evaluate(intent(), context(open_orders=20), START_MS)
        assert [g.name for g in decision.gates] == EXPECTED_GATES

    def test_a_later_passing_gate_cannot_clear_an_earlier_failure(self):
        """Ordering must not matter. The first gate in the list failing and the
        last gate in the list failing are the same outcome."""
        first = core().evaluate(
            intent(), context(kill_switch=KillSwitchState(halt_new_trades=True)), START_MS
        )
        last = core().evaluate(intent(), context(error_rate=1.0), START_MS)
        assert first.verdict is last.verdict is RiskVerdict.REJECTED
        assert first.approved_notional == last.approved_notional == 0.0


class TestUnknownBlocks:
    """An unevaluable mandatory gate has not been satisfied."""

    @pytest.mark.parametrize(
        ("name", "ctx_kwargs", "intent_kwargs"),
        [
            ("SYSTEM_HEALTHY", {"health": None}, {}),
            ("EXECUTION_HEALTHY", {"health": None}, {}),
            ("EXECUTION_HEALTHY", {"health": health(drop=("VESKA",))}, {}),
            ("MARKET_DATA_FRESH", {}, {"source_data_timestamp": None}),
            ("LIQUIDITY_SUFFICIENT", {"max_economical_notional": None}, {}),
        ],
    )
    def test_unknown_is_blocking(self, name, ctx_kwargs, intent_kwargs):
        decision = core().evaluate(
            intent(**intent_kwargs), context(**ctx_kwargs), START_MS
        )
        check = gate_named(decision, name)
        assert check.result is GateResult.UNKNOWN
        assert check.blocking
        assert decision.verdict is RiskVerdict.REJECTED
        assert decision.approved_notional == 0.0

    def test_a_missing_required_component_blocks(self):
        decision = core().evaluate(
            intent(), context(health=health(drop=("MARIN",))), START_MS
        )
        assert "SYSTEM_HEALTHY" in blocking_names(decision)

    def test_gate_result_unknown_is_not_pass(self):
        """The property the whole fail-closed model rests on."""
        from core.models.risk import GateCheck

        check = GateCheck(name="X", result=GateResult.UNKNOWN, mandatory=True)
        assert check.blocking is True


class TestApprovalIsPositive:
    def test_an_approved_decision_reports_all_gates_passed(self):
        decision = core().evaluate(intent(), context(), START_MS)
        assert decision.verdict is RiskVerdict.APPROVED
        assert decision.reason_codes == ["ALL_GATES_PASSED"]
        assert all(g.result is GateResult.PASS for g in decision.gates)

    def test_approval_never_exceeds_the_request(self):
        decision = core().evaluate(intent(notional=5_000.0), context(), START_MS)
        assert decision.approved_notional <= decision.requested_notional

    def test_the_decision_carries_the_intents_own_identity(self):
        """Attribution and the orchestrator both key off these. A decision that
        names the wrong intent authorises the wrong trade."""
        proposed = intent()
        decision = core().evaluate(proposed, context(), START_MS)
        assert decision.intent_id == proposed.intent_id
        assert decision.correlation_id == proposed.correlation_id
        assert decision.strategy == proposed.strategy
        assert decision.symbol == proposed.symbol
        assert decision.source_data_timestamp == proposed.source_data_timestamp

    def test_a_reduced_decision_still_names_the_original_intent(self):
        proposed = intent(notional=24_000.0)
        decision = core(
            RiskLimits(max_order_notional=10_000.0, max_position_notional=50_000.0)
        ).evaluate(proposed, context(), START_MS)
        assert decision.verdict is RiskVerdict.APPROVED_REDUCED
        assert decision.intent_id == proposed.intent_id
        assert decision.requested_notional == pytest.approx(24_000.0)


class TestGateObservabilityMatchesEnforcement:
    """``observed`` and ``limit`` are what an operator reads to understand a
    rejection. They must describe the comparison the gate actually made."""

    def test_order_notional_reports_the_sized_notional_not_the_request(self):
        decision = core(
            RiskLimits(max_order_notional=10_000.0, max_position_notional=50_000.0)
        ).evaluate(intent(notional=24_000.0), context(), START_MS)
        check = gate_named(decision, "MAX_ORDER_NOTIONAL")
        assert check.observed == pytest.approx(decision.approved_notional)
        assert check.limit == pytest.approx(10_000.0)

    def test_every_numeric_gate_reports_a_limit(self):
        decision = core().evaluate(intent(), context(), START_MS)
        numeric = [g for g in decision.gates if g.observed is not None]
        missing = [g.name for g in numeric if g.limit is None]
        assert not missing, f"gates reporting an observation with no limit: {missing}"

    def test_a_passing_gates_observation_actually_satisfies_its_limit(self):
        """Guards against a gate that computes one number and compares another."""
        decision = core().evaluate(intent(), context(), START_MS)
        for check in decision.gates:
            if check.observed is None or check.limit is None:
                continue
            if check.name in {"LIQUIDITY_SUFFICIENT", "MIN_EXPECTED_EDGE", "CONSENSUS_THRESHOLD"}:
                # These are floors: observed must be at or above the limit.
                assert check.observed >= check.limit - 1e-9, check.name
            elif check.name == "INTENT_NOT_EXPIRED":
                assert check.observed <= check.limit, check.name
            else:
                assert check.observed <= check.limit + 1e-9, check.name


class TestRuneCoreTakesExplicitTime:
    """RUNE is on the economic fast loop: its verdict must depend on the tick's
    logical instant, never on when the call happened to run."""

    def test_the_same_inputs_at_the_same_instant_give_the_same_verdict(self):
        engine = core()
        first = engine.evaluate(intent(), context(), START_MS)
        second = engine.evaluate(intent(), context(), START_MS)
        assert first.verdict is second.verdict
        assert first.approved_notional == second.approved_notional
        assert [g.name for g in first.gates] == [g.name for g in second.gates]

    def test_advancing_the_clock_cannot_change_a_verdict_taken_at_a_fixed_instant(self):
        engine = core()
        before = engine.evaluate(intent(), context(), START_MS)
        engine.clock.advance(9_000_000)
        after = engine.evaluate(intent(), context(), START_MS)
        assert before.verdict is after.verdict
        assert before.created_at == after.created_at == START_MS

    def test_data_age_and_deadline_are_measured_from_the_supplied_instant(self):
        engine = core()
        proposed = intent(
            source_data_timestamp=START_MS - 100, deadline_ms=START_MS + 500
        )
        assert engine.evaluate(proposed, context(), START_MS).verdict is (
            RiskVerdict.APPROVED
        )
        # Same intent, a later logical tick: now expired.
        late = engine.evaluate(proposed, context(), START_MS + 501)
        assert "INTENT_NOT_EXPIRED" in blocking_names(late)


class TestExposureGatesReadTheRightPortfolio:
    def test_venue_exposure_is_keyed_by_venue_not_by_position_key(self):
        from tests.audit.rune_fixtures import portfolio_with, position

        book = portfolio_with(
            position(VENUE_A, "BTC-USD", quantity=100.0, price=100.0),
            position(VENUE_A, "ETH-USD", quantity=100.0, price=100.0),
        )
        assert book.exposure_by_venue()[VENUE_A] == pytest.approx(20_000.0)
        decision = core(
            RiskLimits(max_venue_exposure=25_000.0, max_gross_exposure=1_000_000.0)
        ).evaluate(intent(notional=10_000.0), context(portfolio=book), START_MS)
        # Both symbols count against VENUE_A, leaving 5,000 of venue headroom,
        # so a 10,000 request is cut to fit rather than being measured against
        # only the larger of the two positions.
        assert decision.verdict is RiskVerdict.APPROVED_REDUCED
        assert decision.approved_notional == pytest.approx(5_000.0), (
            "both symbols on VENUE_A must count against that venue's limit"
        )
        check = gate_named(decision, "MAX_VENUE_EXPOSURE")
        assert check.observed == pytest.approx(25_000.0)
        assert check.limit == pytest.approx(25_000.0)

    def test_position_exposure_is_keyed_by_venue_and_symbol(self):
        from tests.audit.rune_fixtures import portfolio_with, position

        book = portfolio_with(position(VENUE_A, "ETH-USD", quantity=100.0, price=100.0))
        decision = core().evaluate(
            intent(notional=5_000.0), context(portfolio=book), START_MS
        )
        check = gate_named(decision, "MAX_POSITION_NOTIONAL")
        assert check.observed == pytest.approx(5_000.0), (
            "an unrelated symbol on the same venue is a different position"
        )


class TestSideSignIsTheDirectionalContract:
    """``net_exposure`` projection multiplies by ``side.sign``; the rest of the
    audit depends on that meaning what it says."""

    def test_buy_is_positive_and_sell_is_negative(self):
        assert Side.BUY.sign == 1
        assert Side.SELL.sign == -1

    def test_a_balanced_two_leg_trade_projects_zero_net_delta(self):
        decision = core().evaluate(intent(notional=5_000.0), context(), START_MS)
        check = gate_named(decision, "MAX_NET_EXPOSURE")
        assert check.observed == pytest.approx(0.0)

    def test_a_one_sided_trade_projects_its_full_notional(self):
        from tests.audit.rune_fixtures import leg

        decision = core().evaluate(
            intent(legs=[leg(VENUE_A, Side.BUY)], notional=5_000.0),
            context(),
            START_MS,
        )
        check = gate_named(decision, "MAX_NET_EXPOSURE")
        assert check.observed == pytest.approx(5_000.0)


class TestGatesArePureFunctions:
    """A gate that mutates its inputs would make the order of the gate list
    economically significant."""

    def test_evaluating_does_not_mutate_the_intent(self):
        proposed = intent(notional=5_000.0)
        before = proposed.model_dump()
        core().evaluate(proposed, context(), START_MS)
        assert proposed.model_dump() == before

    def test_evaluating_does_not_mutate_the_portfolio(self):
        book = portfolio()
        before = book.model_dump()
        core().evaluate(intent(), context(portfolio=book), START_MS)
        assert book.model_dump() == before

    def test_reducing_the_size_does_not_mutate_the_original_intent(self):
        proposed = intent(notional=24_000.0)
        core(
            RiskLimits(max_order_notional=10_000.0, max_position_notional=50_000.0)
        ).evaluate(proposed, context(), START_MS)
        assert proposed.notional == pytest.approx(24_000.0)


class TestRequiredComponentList:
    def test_an_empty_required_list_still_runs_the_gate(self):
        """Passing no required components is a configuration statement, not a
        reason to skip the check."""
        decision = core().evaluate(
            intent(), context(required_components=[], health=health()), START_MS
        )
        assert has_gate(decision, "SYSTEM_HEALTHY")

    def test_the_strategys_required_components_are_all_checked(self):
        for name in REQUIRED:
            decision = core().evaluate(
                intent(),
                context(health=health(statuses={name: HealthStatus.OFFLINE})),
                START_MS,
            )
            assert "SYSTEM_HEALTHY" in blocking_names(decision), name
