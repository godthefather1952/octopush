"""Intelligence provider abstraction.

No part of the trading system knows which model produced a piece of
intelligence.  Components ask an :class:`IntelligenceProvider` for structured
analysis and receive a validated dict; swapping Claude for another provider is
a wiring change, not a redesign.

Providers are also allowed to fail.  A failure is reported, never raised into
the trading path, and never silently converted into a neutral opinion.
"""

from __future__ import annotations

import json
import logging
import time
from abc import ABC, abstractmethod
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

log = logging.getLogger(__name__)


class IntelligenceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: Short identifier for the kind of analysis, used for metrics.
    task: str
    system: str
    #: Structured facts. Never free text assembled elsewhere in the system.
    payload: dict[str, Any] = Field(default_factory=dict)
    #: JSON schema the response must satisfy.
    response_schema: dict[str, Any] = Field(default_factory=dict)
    max_tokens: int = 1024
    timeout_s: float = 20.0

    def prompt(self) -> str:
        return (
            f"{self.system}\n\n"
            "Analyse the following structured market context and respond with "
            "JSON only, matching this schema:\n"
            f"{json.dumps(self.response_schema, indent=2)}\n\n"
            "Context:\n"
            f"{json.dumps(self.payload, indent=2, default=str)}"
        )


class IntelligenceResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool
    task: str
    provider: str
    model: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)
    latency_ms: float = 0.0
    error: str | None = None
    #: True when the provider declined or was unavailable, as opposed to
    #: returning something unparseable.
    unavailable: bool = False


class IntelligenceProvider(ABC):
    name: str = "abstract"

    @abstractmethod
    async def analyze(self, request: IntelligenceRequest) -> IntelligenceResponse: ...

    async def aclose(self) -> None:
        """Release provider resources. Providers with none do nothing."""
        return None


class NullProvider(IntelligenceProvider):
    """Always unavailable.

    The default.  It exists so that the platform's behaviour with no
    intelligence layer at all is the *tested* behaviour, not an untried edge
    case discovered when an API goes down.
    """

    name = "null"

    async def analyze(self, request: IntelligenceRequest) -> IntelligenceResponse:
        return IntelligenceResponse(
            ok=False,
            task=request.task,
            provider=self.name,
            unavailable=True,
            error="no intelligence provider configured",
        )


class ScriptedProvider(IntelligenceProvider):
    """Deterministic provider for tests and replay.

    Returns queued responses in order; when the queue is exhausted it returns
    the last response, or becomes unavailable if configured to.
    """

    name = "scripted"

    def __init__(
        self,
        responses: list[dict[str, Any]] | None = None,
        *,
        fail_after: int | None = None,
        latency_ms: float = 5.0,
    ) -> None:
        self._responses = list(responses or [])
        self._index = 0
        self._fail_after = fail_after
        self._latency_ms = latency_ms
        self.calls = 0

    async def analyze(self, request: IntelligenceRequest) -> IntelligenceResponse:
        self.calls += 1
        if self._fail_after is not None and self.calls > self._fail_after:
            return IntelligenceResponse(
                ok=False,
                task=request.task,
                provider=self.name,
                unavailable=True,
                error="scripted failure",
                latency_ms=self._latency_ms,
            )
        if not self._responses:
            return IntelligenceResponse(
                ok=False,
                task=request.task,
                provider=self.name,
                unavailable=True,
                error="no scripted responses",
            )
        data = self._responses[min(self._index, len(self._responses) - 1)]
        self._index += 1
        return IntelligenceResponse(
            ok=True,
            task=request.task,
            provider=self.name,
            model="scripted",
            data=dict(data),
            latency_ms=self._latency_ms,
        )


class ClaudeProvider(IntelligenceProvider):
    """Claude-backed implementation.

    Claude is used for exactly what it is good at — reading unstructured
    information and returning structured judgement.  It has no access to
    orders, balances, credentials, risk limits or position sizing, and its
    output reaches the trading path only as one weighted opinion among
    several.
    """

    name = "claude"

    def __init__(
        self,
        *,
        model: str = "claude-opus-5",
        api_key: str | None = None,
        max_retries: int = 1,
    ) -> None:
        self.model = model
        self._api_key = api_key
        self._max_retries = max_retries
        self._client: Any = None
        self.failures = 0
        self.calls = 0

    def _get_client(self) -> Any:  # pragma: no cover - needs the SDK
        if self._client is None:
            from anthropic import AsyncAnthropic

            self._client = AsyncAnthropic(
                api_key=self._api_key, max_retries=self._max_retries
            )
        return self._client

    @staticmethod
    def _extract_json(text: str) -> dict[str, Any]:
        """Pull the JSON object out of a model response."""
        stripped = text.strip()
        if stripped.startswith("```"):
            stripped = stripped.split("```")[1]
            if stripped.startswith("json"):
                stripped = stripped[4:]
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start == -1 or end == -1:
            raise ValueError("no JSON object in response")
        return json.loads(stripped[start : end + 1])

    async def analyze(self, request: IntelligenceRequest) -> IntelligenceResponse:
        started = time.perf_counter()
        self.calls += 1
        try:  # pragma: no cover - network path
            client = self._get_client()
            message = await client.messages.create(
                model=self.model,
                max_tokens=request.max_tokens,
                system=request.system,
                messages=[{"role": "user", "content": request.prompt()}],
                timeout=request.timeout_s,
            )
            text = "".join(
                block.text for block in message.content if getattr(block, "type", "") == "text"
            )
            data = self._extract_json(text)
            return IntelligenceResponse(
                ok=True,
                task=request.task,
                provider=self.name,
                model=self.model,
                data=data,
                latency_ms=(time.perf_counter() - started) * 1000,
            )
        except Exception as exc:
            self.failures += 1
            log.warning(
                "intelligence provider failed",
                extra={"provider": self.name, "task": request.task, "error": str(exc)},
            )
            return IntelligenceResponse(
                ok=False,
                task=request.task,
                provider=self.name,
                model=self.model,
                error=str(exc),
                unavailable=True,
                latency_ms=(time.perf_counter() - started) * 1000,
            )

    async def aclose(self) -> None:  # pragma: no cover - network path
        if self._client is not None:
            await self._client.close()
            self._client = None


def build_provider(
    kind: str, *, model: str = "claude-opus-5", **kwargs: Any
) -> IntelligenceProvider:
    if kind in ("null", "none", "disabled"):
        return NullProvider()
    if kind == "scripted":
        return ScriptedProvider(**kwargs)
    if kind == "claude":
        return ClaudeProvider(model=model, **kwargs)
    raise ValueError(f"unknown intelligence provider: {kind}")
