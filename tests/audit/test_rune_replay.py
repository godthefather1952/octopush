"""Phase 5 — H15: RUNE decisions must be reproducible.

A risk decision that cannot be reproduced from its recorded inputs cannot be
audited after the fact, and a replay that reaches a different verdict from the
run it replays is not a replay.

The determinism here is stronger than "no randomness": ``RuneCore.evaluate`` is
a pure function of ``(intent, ctx, now_ms)``. Everything nondeterministic —
the decision id, the wall clock — is either supplied explicitly or handled by
the platform's existing deterministic-id mechanism.
"""

from __future__ import annotations

import inspect

import pytest

from agents.rune.core import RuneCore
from core.config import RiskLimits
from core.models.ops import KillSwitchState
from core.models.risk import GateResult, RiskVerdict
from tests.audit.rune_fixtures import (
    VENUE_A,
    context,
    core,
    intent,
    portfolio,
    portfolio_with,
    position,
)
from tests.conftest import START_MS


def fingerprint(decision) -> dict:
    """Everything a replay must reproduce.

    ``decision_id`` is excluded: it is minted through ``core.ids``, which is
    random live and deterministic under replay by design, so comparing the
    literal value would be asserting on something nobody claims matches.
    """
    return {
        "verdict": decision.verdict.value,
        "approved_notional": decision.approved_notional,
        "requested_notional": decision.requested_notional,
        "reason_codes": list(decision.reason_codes),
        "created_at": decision.created_at,
        "source_data_timestamp": decision.source_data_timestamp,
        "intent_id": decision.intent_id,
        "correlation_id": decision.correlation_id,
        "strategy": decision.strategy,
        "symbol": decision.symbol,
        "gates": [
            (g.name, g.result.value, g.mandatory, g.observed, g.limit, g.detail)
            for g in decision.gates
        ],
    }


#: One case per verdict the audit must be able to reproduce.
CASES = {
    "approval": ({}, {}, RiskVerdict.APPROVED),
    "reduced": (
        {"max_economical_notional": 1_500.0},
        {"notional": 5_000.0},
        RiskVerdict.APPROVED_REDUCED,
    ),
    "hard_rejection": (
        {"kill_switch": KillSwitchState(halt_new_trades=True)},
        {},
        RiskVerdict.REJECTED,
    ),
    "size_rejection": (
        {"portfolio": portfolio_with(position(VENUE_A, quantity=2_000.0))},
        {},
        RiskVerdict.REJECTED,
    ),
    "unknown_rejection": ({"health": None}, {}, RiskVerdict.REJECTED),
}


class TestDecisionsAreReproducible:
    @pytest.mark.parametrize("case", sorted(CASES))
    def test_the_same_inputs_reproduce_the_same_decision(self, case):
        ctx_kwargs, intent_kwargs, expected = CASES[case]
        proposed = intent(**intent_kwargs)

        first = core().evaluate(proposed, context(**ctx_kwargs), START_MS)
        second = core().evaluate(proposed, context(**ctx_kwargs), START_MS)

        assert first.verdict is expected
        assert fingerprint(first) == fingerprint(second)

    @pytest.mark.parametrize("case", sorted(CASES))
    def test_a_fresh_engine_reproduces_an_earlier_engines_decision(self, case):
        """Replay builds a new ``RuneCore``; it must not depend on counters or
        anything else accumulated by the original run."""
        ctx_kwargs, intent_kwargs, _expected = CASES[case]
        proposed = intent(**intent_kwargs)

        warmed = core()
        for _ in range(20):
            warmed.evaluate(intent(), context(), START_MS)
        after_warmup = warmed.evaluate(proposed, context(**ctx_kwargs), START_MS)

        cold = core().evaluate(proposed, context(**ctx_kwargs), START_MS)
        assert fingerprint(after_warmup) == fingerprint(cold)

    @pytest.mark.parametrize("case", sorted(CASES))
    def test_repeating_a_decision_ten_times_is_stable(self, case):
        ctx_kwargs, intent_kwargs, _expected = CASES[case]
        proposed = intent(**intent_kwargs)
        engine = core()
        results = [
            fingerprint(engine.evaluate(proposed, context(**ctx_kwargs), START_MS))
            for _ in range(10)
        ]
        assert all(r == results[0] for r in results)


class TestNothingNondeterministicIsReachable:
    def test_the_core_reads_no_wall_clock_when_given_a_time(self):
        source = inspect.getsource(RuneCore.evaluate)
        assert "self.clock.now_ms() if now_ms is None else now_ms" in source, (
            "the clock is the fallback, never the primary source"
        )

    #: Constructs that would make a gate nondeterministic. Bare substrings are
    #: safe here because none of them also names a configuration value.
    NONDETERMINISTIC = (
        "random",
        "time.time",
        "datetime.now",
        "uuid",
        "requests",
        "httpx",
    )

    #: How a module *reads* a clock, as opposed to merely naming one. The bare
    #: word "clock" is deliberately absent — see
    #: ``test_configured_clock_tolerance_is_not_a_clock_read``.
    CLOCK_READS = (
        "from core.clock",
        "import core.clock",
        "self.clock",
        "clock.now",
        "SystemClock",
        "ManualClock",
        "now_ms()",
    )

    #: Modules whose import would put a live clock, randomness or the network
    #: inside the gates, checked as import statements rather than as text.
    FORBIDDEN_IMPORTS = frozenset(
        {
            "random",
            "time",
            "datetime",
            "uuid",
            "secrets",
            "requests",
            "httpx",
            "socket",
            "urllib",
            "core.clock",
        }
    )

    def _forbidden_module(self, module: str) -> bool:
        parts = module.split(".")
        return any(
            ".".join(parts[: index + 1]) in self.FORBIDDEN_IMPORTS
            for index in range(len(parts))
        )

    def test_no_gate_imports_a_clock_randomness_or_the_network(self):
        """Imports, read from the AST rather than from the text.

        An import is what actually creates the dependency, and ``import time as
        t`` would slip past any substring scan. This is the load-bearing half
        of the invariant; the source scan below catches the rest.
        """
        import ast

        import risk.limits as gates

        imported: set[str] = set()
        for node in ast.walk(ast.parse(inspect.getsource(gates))):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        offenders = sorted(m for m in imported if self._forbidden_module(m))
        assert not offenders, (
            f"the deterministic gate module imports {offenders}; a gate's "
            "verdict must depend on its arguments alone"
        )

    def test_no_gate_reads_a_clock_randomness_or_the_network(self):
        """The same invariant stated over the source, for constructs an import
        scan cannot see (an attribute access on something passed in)."""
        import risk.limits as gates

        source = inspect.getsource(gates)
        for forbidden in self.NONDETERMINISTIC + self.CLOCK_READS:
            assert forbidden not in source, (
                f"{forbidden!r} appears in the deterministic gate module"
            )

    def test_configured_clock_tolerance_is_not_a_clock_read(self):
        """The premise of the narrowed ban above, stated so it cannot drift.

        This audit used to reject the bare substring ``"clock"`` anywhere in
        ``risk.limits``. P5-9 gave ``gate_data_age`` a lower bound of
        ``-limits.max_clock_skew_ms``, and the substring scan called that a
        nondeterminism finding. It is not one: ``max_clock_skew_ms`` is an
        integer loaded from configuration, identical on every evaluation and in
        every replay of the same recorded config. Reading it is no more a clock
        access than reading ``max_data_age_ms``, and the prose explaining why
        the bound exists necessarily says "clock skew".

        The property that matters is that the module takes no reading of the
        current time — which the two tests above assert directly.
        """
        import risk.limits as gates

        source = inspect.getsource(gates)
        assert "max_clock_skew_ms" in source, (
            "P5-9's lower bound is gone; the gate would again treat a "
            "timestamp from the future as maximally fresh"
        )
        assert "clock" in source, (
            "premise: the letters are present, which is exactly why a bare "
            "substring ban was the wrong test"
        )
        for read in self.CLOCK_READS:
            assert read not in source

    def test_gate_data_age_takes_its_instant_as_an_argument(self):
        """P5-9 touched the one gate that reasons about time at all, so it is
        pinned on its own rather than only through the module-wide scan."""
        from risk.limits import gate_data_age

        assert list(inspect.signature(gate_data_age).parameters) == [
            "intent",
            "limits",
            "now_ms",
        ]
        source = inspect.getsource(gate_data_age)
        for read in self.CLOCK_READS:
            assert read not in source, (
                f"{read!r} appears in gate_data_age; its instant must be "
                "supplied, never sampled"
            )

    def test_gate_data_age_is_a_pure_function_of_its_arguments(self):
        """Behavioural, not documentary.

        Same intent, same limits, same ``now_ms`` — same ``GateCheck``, at
        every boundary of the interval P5-9 established. And the verdict moves
        with the supplied instant rather than with anything ambient.
        """
        from risk.limits import gate_data_age

        limits = RiskLimits()
        boundaries = (
            0,
            limits.max_data_age_ms,
            limits.max_data_age_ms + 1,
            -limits.max_clock_skew_ms,
            -(limits.max_clock_skew_ms + 1),
        )
        for offset in boundaries:
            proposed = intent(source_data_timestamp=START_MS - offset)
            checks = [
                gate_data_age(proposed, limits, START_MS).model_dump()
                for _ in range(10)
            ]
            assert all(check == checks[0] for check in checks), (
                f"gate_data_age varied across repeated calls at age {offset}ms"
            )

        stable = intent(source_data_timestamp=START_MS)
        assert gate_data_age(stable, limits, START_MS).result is GateResult.PASS
        assert (
            gate_data_age(
                stable, limits, START_MS + limits.max_data_age_ms + 1
            ).result
            is GateResult.FAIL
        )

    def test_the_core_module_imports_nothing_nondeterministic(self):
        import agents.rune.core as rune_core

        source = inspect.getsource(rune_core)
        for forbidden in ("import random", "import time", "httpx", "requests"):
            assert forbidden not in source

    def test_the_decision_id_is_the_only_minted_value(self):
        """One intent, evaluated twice.

        Building a second ``intent()`` would mint a second ``intent_id`` as
        well, so the comparison would vary two things at once and the
        fingerprint — which includes ``intent_id``, correctly — would differ
        for a reason that has nothing to do with the decision. Reusing the
        input isolates what is actually being claimed: across two evaluations
        of the SAME input, ``decision_id`` is the only field that moves.
        """
        proposed = intent()
        ctx = context()
        first = core().evaluate(proposed, ctx, START_MS)
        second = core().evaluate(proposed, ctx, START_MS)
        assert first.decision_id != second.decision_id, (
            "premise: ids are minted per decision, which is why the "
            "fingerprint excludes them"
        )
        assert fingerprint(first) == fingerprint(second)
        assert first.intent_id == second.intent_id

    def test_the_decision_id_goes_through_the_platform_id_mechanism(self):
        source = inspect.getsource(RuneCore)
        assert 'new_id("risk")' in source
        assert "uuid" not in source


class TestGateOrderIsStable:
    """A replay diff compares gates positionally, so the order must not depend
    on anything that varies between runs."""

    def test_the_gate_sequence_is_identical_across_verdicts(self):
        approved = core().evaluate(intent(), context(), START_MS)
        rejected = core().evaluate(intent(), context(open_orders=20), START_MS)
        assert [g.name for g in approved.gates] == [g.name for g in rejected.gates]

    def test_the_gate_sequence_does_not_depend_on_portfolio_contents(self):
        empty = core().evaluate(intent(), context(), START_MS)
        held = core().evaluate(
            intent(),
            context(portfolio=portfolio_with(position(VENUE_A, quantity=10.0))),
            START_MS,
        )
        assert [g.name for g in empty.gates] == [g.name for g in held.gates]

    def test_reason_codes_are_ordered_by_the_gate_sequence(self):
        decision = core().evaluate(
            intent(consensus_agreement=0.0, expected_net_edge_bps=0.0),
            context(open_orders=20, error_rate=1.0),
            START_MS,
        )
        gate_order = [g.name for g in decision.gates]
        positions = [gate_order.index(code) for code in decision.reason_codes]
        assert positions == sorted(positions)


class TestSerialisationRoundTrip:
    """A recorded decision must read back as the decision that was made."""

    @pytest.mark.parametrize("case", sorted(CASES))
    def test_a_decision_survives_json_serialisation(self, case):
        from core.models.risk import RiskDecision

        ctx_kwargs, intent_kwargs, _expected = CASES[case]
        decision = core().evaluate(
            intent(**intent_kwargs), context(**ctx_kwargs), START_MS
        )
        payload = decision.to_json_dict()
        restored = RiskDecision.model_validate(payload)
        assert fingerprint(restored) == fingerprint(decision)

    def test_every_gate_observation_survives_the_round_trip(self):
        from core.models.risk import RiskDecision

        decision = core().evaluate(intent(), context(), START_MS)
        restored = RiskDecision.model_validate(decision.to_json_dict())
        for original, copy in zip(decision.gates, restored.gates, strict=True):
            assert original.observed == copy.observed
            assert original.limit == copy.limit
            assert original.result is copy.result

    def test_the_approved_flag_is_derived_not_stored(self):
        """``approved`` is a property over ``verdict``, so a recorded decision
        cannot disagree with itself."""
        from core.models.risk import RiskDecision

        assert isinstance(RiskDecision.approved, property)
        assert "approved" not in RiskDecision.model_fields


class TestConfigurationEntersTheFingerprint:
    """Two different limit sets must be distinguishable from the decision
    alone, or an audit cannot tell which configuration produced it."""

    def test_the_limit_a_gate_enforced_travels_on_the_decision(self):
        strict = core(RiskLimits(max_order_notional=1_000.0,
                                 max_position_notional=50_000.0))
        loose = core(RiskLimits(max_order_notional=25_000.0))
        proposed = intent(notional=900.0)

        strict_check = next(
            g for g in strict.evaluate(proposed, context(), START_MS).gates
            if g.name == "MAX_ORDER_NOTIONAL"
        )
        loose_check = next(
            g for g in loose.evaluate(proposed, context(), START_MS).gates
            if g.name == "MAX_ORDER_NOTIONAL"
        )
        assert strict_check.limit == pytest.approx(1_000.0)
        assert loose_check.limit == pytest.approx(25_000.0)

    def test_a_changed_limit_changes_the_fingerprint(self):
        proposed = intent(notional=900.0)
        a = core(RiskLimits(max_order_notional=1_000.0,
                            max_position_notional=50_000.0)).evaluate(
            proposed, context(), START_MS
        )
        b = core(RiskLimits(max_order_notional=25_000.0)).evaluate(
            proposed, context(), START_MS
        )
        assert fingerprint(a) != fingerprint(b)


class TestUtilizationIsDeterministic:
    def test_the_same_portfolio_produces_the_same_utilization(self):
        engine = core()
        book = portfolio_with(position(VENUE_A, quantity=100.0))
        first = engine.utilization(book, 500.0, {"cross_venue": 1_000.0})
        second = engine.utilization(book, 500.0, {"cross_venue": 1_000.0})
        assert first.model_dump() == second.model_dump()

    def test_utilization_reads_no_clock(self):
        source = inspect.getsource(RuneCore.utilization)
        assert "self.clock" not in source
        assert "now_ms" not in source

    def test_utilization_uses_absolute_unhedged_notional(self):
        engine = core()
        positive = engine.utilization(portfolio(), 500.0, {})
        negative = engine.utilization(portfolio(), -500.0, {})
        assert positive.unhedged_notional == negative.unhedged_notional
