"""RUNE-AI — advisory risk commentary.

Reads the same portfolio and market summary a human risk officer would, and
returns a concern level plus reasoning.  It runs on the slow loop, off the
execution path.

RUNE-AI can *tighten* nothing and *loosen* nothing.  Its output is attached to
the RiskDecision for the record and shown on the dashboard; the verdict is
RUNE-CORE's alone.  A provider outage costs the platform commentary and
nothing else.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agents.lumen.provider import IntelligenceProvider, IntelligenceRequest

VERSION = "rune-ai-0.1"

SYSTEM_PROMPT = (
    "You are the risk-commentary layer of an automated paper-trading platform. "
    "You are given a structured summary of portfolio state, market conditions "
    "and recent agent signals. Identify abnormal conditions, contradictory "
    "signals, unexpected correlations and signs of strategy degradation. "
    "You do not authorise or block trades; deterministic code does that. "
    "Report only what the data supports."
)

RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["concern_level", "regime", "commentary", "reason_codes"],
    "properties": {
        "concern_level": {
            "type": "number",
            "minimum": 0,
            "maximum": 1,
            "description": "0 = nothing unusual, 1 = severe abnormality",
        },
        "regime": {
            "type": "string",
            "enum": ["CALM", "TRENDING", "VOLATILE", "DISLOCATED", "ILLIQUID", "UNKNOWN"],
        },
        "commentary": {"type": "string", "maxLength": 600},
        "reason_codes": {"type": "array", "items": {"type": "string"}},
        "contradictions": {"type": "array", "items": {"type": "string"}},
    },
}


@dataclass
class RiskCommentary:
    concern_level: float
    regime: str
    commentary: str
    reason_codes: list[str]
    contradictions: list[str]
    ok: bool
    error: str | None = None

    @classmethod
    def unavailable(cls, error: str) -> RiskCommentary:
        return cls(
            concern_level=0.0,
            regime="UNKNOWN",
            commentary="",
            reason_codes=["RUNE_AI_UNAVAILABLE"],
            contradictions=[],
            ok=False,
            error=error,
        )


class RuneAI:
    def __init__(self, provider: IntelligenceProvider, *, max_tokens: int = 800) -> None:
        self.provider = provider
        self.max_tokens = max_tokens
        self.latest: RiskCommentary | None = None
        self.calls = 0
        self.failures = 0

    async def assess(self, context: dict[str, Any]) -> RiskCommentary:
        self.calls += 1
        response = await self.provider.analyze(
            IntelligenceRequest(
                task="risk_assessment",
                system=SYSTEM_PROMPT,
                payload=context,
                response_schema=RESPONSE_SCHEMA,
                max_tokens=self.max_tokens,
            )
        )
        if not response.ok:
            self.failures += 1
            self.latest = RiskCommentary.unavailable(response.error or "unavailable")
            return self.latest

        data = response.data
        try:
            commentary = RiskCommentary(
                concern_level=max(0.0, min(1.0, float(data.get("concern_level", 0.0)))),
                regime=str(data.get("regime", "UNKNOWN")),
                commentary=str(data.get("commentary", ""))[:600],
                reason_codes=[str(code) for code in data.get("reason_codes", [])][:10],
                contradictions=[str(c) for c in data.get("contradictions", [])][:10],
                ok=True,
            )
        except (TypeError, ValueError) as exc:
            self.failures += 1
            commentary = RiskCommentary.unavailable(f"malformed response: {exc}")
        self.latest = commentary
        return commentary
