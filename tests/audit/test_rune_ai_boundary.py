"""Phase 5 — H13: RUNE-AI cannot affect the verdict, the size, or any gate.

The claim is absolute, so the test has to be too. It is not enough that a
maximally-alarmed AI fails to reject an approved trade; the whole decision —
verdict, approved notional, every gate's result, observation and limit, and
the deterministic reason codes — must be byte-identical with and without
commentary, in every direction.

The only thing commentary may add is metadata: ``ai_commentary``,
``ai_concern_level`` and ``AI:``-prefixed reason codes, none of which any
consumer treats as a gate.
"""

from __future__ import annotations

import inspect

import pytest

from agents.lumen.provider import IntelligenceProvider, IntelligenceRequest, IntelligenceResponse
from agents.rune.agent import Rune
from agents.rune.ai import RiskCommentary, RuneAI
from agents.rune.core import RuneCore
from core.bus import InMemoryEventBus
from core.clock import ManualClock
from core.config import Settings, load_settings, simulated_venues
from core.health import HealthRegistry
from core.models.ops import KillSwitchState
from core.models.risk import RiskVerdict
from tests.audit.rune_fixtures import context, intent
from tests.conftest import START_MS


def settings() -> Settings:
    return load_settings().model_copy(update={"venues": simulated_venues()})


def build_rune(ai: RuneAI | None = None) -> Rune:
    clock = ManualClock(START_MS)
    return Rune(
        bus=InMemoryEventBus(raise_on_handler_error=True),
        clock=clock,
        settings=settings(),
        health=HealthRegistry(clock=clock),
        ai=ai,
    )


class ScriptedProvider(IntelligenceProvider):
    """Returns whatever it is told, including malformed payloads."""

    name = "audit-scripted"

    def __init__(self, payload: dict | None = None, *, ok: bool = True,
                 error: str | None = None):
        self.payload = payload or {}
        self.ok = ok
        self.error = error
        self.calls = 0

    async def analyze(self, request: IntelligenceRequest) -> IntelligenceResponse:
        self.calls += 1
        return IntelligenceResponse(
            ok=self.ok,
            task=request.task,
            provider=self.name,
            data=self.payload,
            error=self.error,
            unavailable=not self.ok,
        )


class ExplodingProvider(IntelligenceProvider):
    name = "audit-exploding"

    async def analyze(self, request: IntelligenceRequest) -> IntelligenceResponse:
        raise RuntimeError("provider exploded")


def commentary(concern: float, *, codes: list[str] | None = None) -> RiskCommentary:
    return RiskCommentary(
        concern_level=concern,
        regime="DISLOCATED",
        commentary="the model is worried" if concern > 0.5 else "all quiet",
        reason_codes=codes if codes is not None else ["MODEL_CODE"],
        contradictions=[],
        ok=True,
    )


def deterministic_fingerprint(decision) -> dict:
    """Everything the deterministic layer owns.

    Deliberately excludes ``decision_id`` (minted per call) and the AI
    metadata fields, which are the only things commentary may write.
    """
    return {
        "verdict": decision.verdict.value,
        "approved_notional": decision.approved_notional,
        "requested_notional": decision.requested_notional,
        "intent_id": decision.intent_id,
        "strategy": decision.strategy,
        "symbol": decision.symbol,
        "created_at": decision.created_at,
        "source_data_timestamp": decision.source_data_timestamp,
        "gates": [
            (g.name, g.result.value, g.mandatory, g.observed, g.limit)
            for g in decision.gates
        ],
        "deterministic_reason_codes": [
            c for c in decision.reason_codes if not c.startswith("AI:")
        ],
    }


APPROVE_CASE = ({}, {})
REJECT_CASES = [
    ({"kill_switch": KillSwitchState(halt_new_trades=True)}, {}),
    ({}, {"expected_net_edge_bps": 0.0}),
    ({"open_orders": 20}, {}),
    ({"health": None}, {}),
    ({"max_economical_notional": None}, {}),
]
REDUCE_CASE = ({"max_economical_notional": 1_500.0}, {"notional": 5_000.0})


class TestTheCoreNeverSeesCommentary:
    def test_rune_core_evaluate_takes_no_ai_input(self):
        params = list(inspect.signature(RuneCore.evaluate).parameters)
        assert params == ["self", "intent", "ctx", "now_ms"]

    def test_commentary_is_applied_after_the_verdict_exists(self):
        source = inspect.getsource(Rune.evaluate)
        verdict_at = source.index("decision = self.core.evaluate(")
        ai_at = source.index("commentary")
        assert verdict_at < ai_at, (
            "commentary must be read only after the deterministic verdict is "
            "already computed"
        )

    def test_commentary_writes_only_metadata_fields(self):
        source = inspect.getsource(Rune.evaluate)
        assigned = {
            line.split("=")[0].strip()
            for line in source.splitlines()
            if line.strip().startswith("decision.") and "=" in line
        }
        assert assigned <= {
            "decision.ai_commentary",
            "decision.ai_concern_level",
            "decision.reason_codes",
        }, f"commentary writes to {assigned}"


async def compare_with_and_without_ai(
    ai: RuneAI, ctx_kwargs: dict, intent_kwargs: dict, *, now_ms: int = START_MS
):
    """Evaluate ONE intent against ONE context, with and without commentary.

    The single shared ``proposed`` and ``ctx`` are the whole point.
    ``TradeIntent.intent_id`` is minted by a ``default_factory``, so building
    a second ``intent(...)`` for the comparison run would change the input as
    well as the AI presence — and ``deterministic_fingerprint`` includes
    ``intent_id``, correctly, because a decision that named a different intent
    would be a different decision. Reusing the object isolates the one
    variable this file is about.

    ``RuneCore.evaluate`` does not mutate either argument (pinned in
    ``test_rune_gate_invariants.py::TestGatesArePureFunctions``), so sharing
    them across the two runs is sound as well as stronger.
    """
    proposed = intent(**intent_kwargs)
    ctx = context(**ctx_kwargs)
    baseline = await build_rune().evaluate(proposed, ctx, now_ms)
    withai = await build_rune(ai).evaluate(proposed, ctx, now_ms)
    return baseline, withai


class TestAlarmedAiCannotRejectAnApproval:
    async def test_maximum_concern_leaves_an_approval_untouched(self):
        ctx_kwargs, intent_kwargs = APPROVE_CASE
        ai = RuneAI(ScriptedProvider({}))
        ai.latest = commentary(1.0, codes=["CATASTROPHE", "STOP", "DANGER"])
        baseline, withai = await compare_with_and_without_ai(
            ai, ctx_kwargs, intent_kwargs
        )

        assert baseline.verdict is RiskVerdict.APPROVED
        assert deterministic_fingerprint(withai) == deterministic_fingerprint(baseline)
        assert withai.verdict is RiskVerdict.APPROVED
        assert withai.approved_notional == pytest.approx(baseline.approved_notional)

    async def test_maximum_concern_cannot_shrink_an_approved_size(self):
        ai = RuneAI(ScriptedProvider({}))
        ai.latest = commentary(1.0)
        baseline, withai = await compare_with_and_without_ai(ai, {}, {})
        assert withai.approved_notional == pytest.approx(baseline.approved_notional)
        assert withai.requested_notional == pytest.approx(baseline.requested_notional)

    async def test_maximum_concern_cannot_change_a_reduced_size(self):
        ctx_kwargs, intent_kwargs = REDUCE_CASE
        ai = RuneAI(ScriptedProvider({}))
        ai.latest = commentary(1.0)
        baseline, withai = await compare_with_and_without_ai(
            ai, ctx_kwargs, intent_kwargs
        )

        assert baseline.verdict is RiskVerdict.APPROVED_REDUCED
        assert deterministic_fingerprint(withai) == deterministic_fingerprint(baseline)


class TestCalmAiCannotApproveARejection:
    @pytest.mark.parametrize(("ctx_kwargs", "intent_kwargs"), REJECT_CASES)
    async def test_zero_concern_leaves_a_rejection_untouched(
        self, ctx_kwargs, intent_kwargs
    ):
        ai = RuneAI(ScriptedProvider({}))
        ai.latest = commentary(0.0, codes=["ALL_CLEAR", "SAFE_TO_TRADE"])
        baseline, withai = await compare_with_and_without_ai(
            ai, ctx_kwargs, intent_kwargs
        )

        assert baseline.verdict is RiskVerdict.REJECTED
        assert deterministic_fingerprint(withai) == deterministic_fingerprint(baseline)
        assert withai.verdict is RiskVerdict.REJECTED
        assert withai.approved_notional == 0.0
        assert not withai.approved


class TestDegradedAiIsNoAi:
    async def test_an_unavailable_provider_changes_nothing(self):
        ai = RuneAI(ScriptedProvider(None, ok=False, error="provider down"))
        await ai.assess({})
        assert ai.latest is not None and not ai.latest.ok

        baseline, withai = await compare_with_and_without_ai(ai, {}, {})
        assert deterministic_fingerprint(withai) == deterministic_fingerprint(baseline)
        assert withai.ai_commentary is None, (
            "an unavailable commentary must not be attached as if it were one"
        )

    async def test_a_malformed_payload_changes_nothing(self):
        ai = RuneAI(ScriptedProvider({"concern_level": "not-a-number"}))
        await ai.assess({})
        baseline, withai = await compare_with_and_without_ai(ai, {}, {})
        assert deterministic_fingerprint(withai) == deterministic_fingerprint(baseline)

    async def test_a_missing_provider_changes_nothing(self):
        proposed = intent()
        ctx = context()
        withai = await build_rune(None).evaluate(proposed, ctx, START_MS)
        baseline = await build_rune().evaluate(proposed, ctx, START_MS)
        assert deterministic_fingerprint(withai) == deterministic_fingerprint(baseline)

    async def test_stale_commentary_is_attached_but_inert(self):
        """``Rune.evaluate`` reads ``ai.latest`` — whatever it happens to be —
        rather than calling the provider on the decision path. That keeps the
        network off the fast loop; the commentary may therefore be arbitrarily
        old and must remain metadata.

        The TRADE has to stay temporally valid while the commentary ages, or
        the test measures the deadline and data-age gates instead of the AI
        boundary. So the later evaluation uses a freshly-timed intent: the
        commentary is an hour old, the market observation and the deadline are
        not.
        """
        ai = RuneAI(ScriptedProvider({}))
        ai.latest = commentary(0.9)

        # A valid trade now, with commentary that is already stale.
        first = await build_rune(ai).evaluate(intent(), context(), START_MS)
        assert first.verdict is RiskVerdict.APPROVED

        later = START_MS + 3_600_000
        later_intent_kwargs = {
            "created_at": later,
            "source_data_timestamp": later - 50,
            "deadline_ms": later + 2_000,
        }
        baseline, withai = await compare_with_and_without_ai(
            ai, {}, later_intent_kwargs, now_ms=later
        )

        assert baseline.verdict is RiskVerdict.APPROVED, (
            "the later trade must itself be valid, or this test is measuring "
            f"INTENT_NOT_EXPIRED rather than the AI boundary: "
            f"{baseline.reason_codes}"
        )
        assert deterministic_fingerprint(withai) == deterministic_fingerprint(baseline)
        assert withai.ai_concern_level == pytest.approx(0.9), (
            "hour-old commentary is still attached as metadata"
        )
        assert first.ai_concern_level == withai.ai_concern_level

    async def test_the_decision_path_never_calls_the_provider(self):
        provider = ScriptedProvider({})
        ai = RuneAI(provider)
        ai.latest = commentary(1.0)
        await build_rune(ai).evaluate(intent(), context(), START_MS)
        assert provider.calls == 0, (
            "a provider call on the risk path would put a network round trip "
            "inside the hard gate"
        )

    async def test_an_aged_commentary_does_not_call_the_provider_either(self):
        """Complements the stale test: nothing refreshes commentary on the
        decision path, however old it is."""
        provider = ScriptedProvider({})
        ai = RuneAI(provider)
        ai.latest = commentary(0.9)
        later = START_MS + 3_600_000
        await build_rune(ai).evaluate(
            intent(
                created_at=later,
                source_data_timestamp=later - 50,
                deadline_ms=later + 2_000,
            ),
            context(),
            later,
        )
        assert provider.calls == 0


class TestCommentaryMetadataIsBounded:
    async def test_at_most_three_ai_reason_codes_are_attached(self):
        ai = RuneAI(ScriptedProvider({}))
        ai.latest = commentary(0.5, codes=[f"CODE_{i}" for i in range(50)])
        decision = await build_rune(ai).evaluate(intent(), context(), START_MS)
        ai_codes = [c for c in decision.reason_codes if c.startswith("AI:")]
        assert len(ai_codes) <= 3

    async def test_ai_codes_are_prefixed_so_they_cannot_be_mistaken_for_gates(self):
        ai = RuneAI(ScriptedProvider({}))
        ai.latest = commentary(0.5, codes=["MAX_GROSS_EXPOSURE", "ALL_GATES_PASSED"])
        decision = await build_rune(ai).evaluate(intent(), context(), START_MS)
        gate_names = {g.name for g in decision.gates}
        impostors = [
            c for c in decision.reason_codes
            if c in gate_names and c not in {g.name for g in decision.gates if g.blocking}
        ]
        assert impostors == [], (
            f"AI-supplied codes collided with gate names: {impostors}"
        )
        assert "AI:MAX_GROSS_EXPOSURE" in decision.reason_codes

    async def test_the_deterministic_reason_codes_come_first(self):
        ai = RuneAI(ScriptedProvider({}))
        ai.latest = commentary(0.5, codes=["X"])
        decision = await build_rune(ai).evaluate(intent(), context(), START_MS)
        assert decision.reason_codes[0] == "ALL_GATES_PASSED"

    async def test_concern_level_is_clamped_to_zero_one(self):
        for raw, expected in ((5.0, 1.0), (-3.0, 0.0), (0.4, 0.4)):
            ai = RuneAI(ScriptedProvider({"concern_level": raw, "regime": "CALM"}))
            result = await ai.assess({})
            assert result.concern_level == pytest.approx(expected)


class TestAiFailureDoesNotSuspendRune:
    def test_rune_stays_healthy_when_its_ai_layer_fails(self):
        """A required component going unhealthy suspends the strategy. The AI
        layer is advisory, so its failure must not do that."""
        from core.models.ops import HealthStatus

        ai = RuneAI(ScriptedProvider(None, ok=False))
        ai.calls = 10
        ai.failures = 10
        rune = build_rune(ai)
        rune.heartbeat()
        state = rune.health.snapshot(START_MS).components["RUNE"]
        assert state.status is HealthStatus.HEALTHY
        assert "RUNE-AI failing" in state.detail

    async def test_an_exploding_provider_surfaces_as_a_failure_not_a_crash(self):
        ai = RuneAI(ExplodingProvider())
        with pytest.raises(RuntimeError):
            await ai.assess({})
        # The decision path is untouched: ``latest`` was never written.
        assert ai.latest is None
        decision = await build_rune(ai).evaluate(intent(), context(), START_MS)
        assert decision.verdict is RiskVerdict.APPROVED
        assert decision.ai_commentary is None
