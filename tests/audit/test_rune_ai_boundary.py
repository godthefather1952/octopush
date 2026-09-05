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


class TestAlarmedAiCannotRejectAnApproval:
    async def test_maximum_concern_leaves_an_approval_untouched(self):
        ctx_kwargs, intent_kwargs = APPROVE_CASE
        baseline = await build_rune().evaluate(
            intent(**intent_kwargs), context(**ctx_kwargs), START_MS
        )
        assert baseline.verdict is RiskVerdict.APPROVED

        ai = RuneAI(ScriptedProvider({}))
        ai.latest = commentary(1.0, codes=["CATASTROPHE", "STOP", "DANGER"])
        withai = await build_rune(ai).evaluate(
            intent(**intent_kwargs), context(**ctx_kwargs), START_MS
        )
        assert deterministic_fingerprint(withai) == deterministic_fingerprint(baseline)
        assert withai.verdict is RiskVerdict.APPROVED
        assert withai.approved_notional == pytest.approx(baseline.approved_notional)

    async def test_maximum_concern_cannot_shrink_an_approved_size(self):
        ai = RuneAI(ScriptedProvider({}))
        ai.latest = commentary(1.0)
        withai = await build_rune(ai).evaluate(intent(), context(), START_MS)
        baseline = await build_rune().evaluate(intent(), context(), START_MS)
        assert withai.approved_notional == pytest.approx(baseline.approved_notional)

    async def test_maximum_concern_cannot_change_a_reduced_size(self):
        ctx_kwargs, intent_kwargs = REDUCE_CASE
        baseline = await build_rune().evaluate(
            intent(**intent_kwargs), context(**ctx_kwargs), START_MS
        )
        assert baseline.verdict is RiskVerdict.APPROVED_REDUCED

        ai = RuneAI(ScriptedProvider({}))
        ai.latest = commentary(1.0)
        withai = await build_rune(ai).evaluate(
            intent(**intent_kwargs), context(**ctx_kwargs), START_MS
        )
        assert deterministic_fingerprint(withai) == deterministic_fingerprint(baseline)


class TestCalmAiCannotApproveARejection:
    @pytest.mark.parametrize(("ctx_kwargs", "intent_kwargs"), REJECT_CASES)
    async def test_zero_concern_leaves_a_rejection_untouched(
        self, ctx_kwargs, intent_kwargs
    ):
        baseline = await build_rune().evaluate(
            intent(**intent_kwargs), context(**ctx_kwargs), START_MS
        )
        assert baseline.verdict is RiskVerdict.REJECTED

        ai = RuneAI(ScriptedProvider({}))
        ai.latest = commentary(0.0, codes=["ALL_CLEAR", "SAFE_TO_TRADE"])
        withai = await build_rune(ai).evaluate(
            intent(**intent_kwargs), context(**ctx_kwargs), START_MS
        )
        assert deterministic_fingerprint(withai) == deterministic_fingerprint(baseline)
        assert withai.verdict is RiskVerdict.REJECTED
        assert withai.approved_notional == 0.0
        assert not withai.approved


class TestDegradedAiIsNoAi:
    async def test_an_unavailable_provider_changes_nothing(self):
        ai = RuneAI(ScriptedProvider(None, ok=False, error="provider down"))
        await ai.assess({})
        assert ai.latest is not None and not ai.latest.ok

        withai = await build_rune(ai).evaluate(intent(), context(), START_MS)
        baseline = await build_rune().evaluate(intent(), context(), START_MS)
        assert deterministic_fingerprint(withai) == deterministic_fingerprint(baseline)
        assert withai.ai_commentary is None, (
            "an unavailable commentary must not be attached as if it were one"
        )

    async def test_a_malformed_payload_changes_nothing(self):
        ai = RuneAI(ScriptedProvider({"concern_level": "not-a-number"}))
        await ai.assess({})
        withai = await build_rune(ai).evaluate(intent(), context(), START_MS)
        baseline = await build_rune().evaluate(intent(), context(), START_MS)
        assert deterministic_fingerprint(withai) == deterministic_fingerprint(baseline)

    async def test_a_missing_provider_changes_nothing(self):
        withai = await build_rune(None).evaluate(intent(), context(), START_MS)
        baseline = await build_rune().evaluate(intent(), context(), START_MS)
        assert deterministic_fingerprint(withai) == deterministic_fingerprint(baseline)

    async def test_stale_commentary_is_attached_but_inert(self):
        """``Rune.evaluate`` reads ``ai.latest`` — whatever it happens to be —
        rather than calling the provider on the decision path. That keeps the
        network off the fast loop; the commentary may therefore be arbitrarily
        old and must remain metadata."""
        ai = RuneAI(ScriptedProvider({}))
        ai.latest = commentary(0.9)
        rune = build_rune(ai)
        first = await rune.evaluate(intent(), context(), START_MS)
        second = await rune.evaluate(intent(), context(), START_MS + 3_600_000)
        assert first.ai_concern_level == second.ai_concern_level
        assert first.verdict is second.verdict

    async def test_the_decision_path_never_calls_the_provider(self):
        provider = ScriptedProvider({})
        ai = RuneAI(provider)
        ai.latest = commentary(1.0)
        await build_rune(ai).evaluate(intent(), context(), START_MS)
        assert provider.calls == 0, (
            "a provider call on the risk path would put a network round trip "
            "inside the hard gate"
        )


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
