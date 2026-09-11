"""Provider metadata — a description of who can be asked, not a chooser.

WHAT THIS IS
============
The platform has three providers: ``NullProvider`` (the default, always
unavailable), ``ScriptedProvider`` (deterministic, for tests and replay) and
``ClaudeProvider`` (networked). They differ in ways that matter to a reader —
one reaches the network and two do not, one is non-deterministic and two are
not — and nothing has ever written those differences down.

:class:`IntelligenceProviderDirectory` holds descriptors for them.

WHAT IT IS NOT
==============
**Not a selector, and not a fallback.** ``build_provider(kind, ...)`` still
chooses by configured name, exactly as before, and ``Lumen.provider`` is still
whatever wiring handed it. This directory:

* never switches provider on failure — automatic failover would silently change
  which model produced a reading, and a comparison across a session would then
  be comparing two different analysts;
* never skips a provider on the strength of its capabilities;
* never retries. ``ClaudeProvider`` has its own ``max_retries``, unchanged, and
  no second retry layer is added above it.

**No credentials.** A descriptor carries a name, a model string and a handful
of booleans. It must never carry an API key, a header or a client object — a
descriptor with a secret in it would put one into every snapshot that embeds it.
"""

from __future__ import annotations

from agents.lumen.provider import (
    ClaudeProvider,
    IntelligenceProvider,
    NullProvider,
    ScriptedProvider,
)
from core.models.intelligence import (
    IntelligenceProviderCapabilities,
    IntelligenceProviderDescriptor,
)

#: What each shipped provider is like. Descriptive: nothing dispatches on any
#: of these values, and a wrong entry changes no behaviour, only a display.
NULL_CAPABILITIES = IntelligenceProviderCapabilities(
    structured_output=False,
    supports_json_schema=False,
    networked=False,
    deterministic=True,
    replay_safe=True,
    supports_close=True,
)

SCRIPTED_CAPABILITIES = IntelligenceProviderCapabilities(
    structured_output=True,
    supports_json_schema=False,
    networked=False,
    deterministic=True,
    replay_safe=True,
    supports_close=True,
)

CLAUDE_CAPABILITIES = IntelligenceProviderCapabilities(
    structured_output=True,
    supports_json_schema=True,
    networked=True,
    deterministic=False,
    #: False, and the point is moot: replay republishes recorded LUMEN
    #: opinions as exogenous inputs and reinvokes no provider at all.
    replay_safe=False,
    supports_close=True,
)


def describe_provider(provider: IntelligenceProvider) -> IntelligenceProviderDescriptor:
    """Describe a constructed provider.

    Reads the provider's own ``name`` and, where it has one, its model. It
    calls nothing on the provider and touches no credential — in particular it
    does not read ``ClaudeProvider._api_key``, and must never learn to.

    ``configured`` says the provider has what it needs to be asked. For
    ``NullProvider`` that is False by construction: being permanently
    unavailable is what it is for, and reporting it as configured would hide
    the platform's default state.
    """
    if isinstance(provider, NullProvider):
        return IntelligenceProviderDescriptor(
            name=provider.name,
            capabilities=NULL_CAPABILITIES,
            configured=False,
            description=(
                "No intelligence provider. The platform's default, so that "
                "running without an intelligence layer is the tested "
                "behaviour rather than an untried edge case."
            ),
        )
    if isinstance(provider, ScriptedProvider):
        return IntelligenceProviderDescriptor(
            name=provider.name,
            model="scripted",
            capabilities=SCRIPTED_CAPABILITIES,
            configured=True,
            description=(
                "Deterministic queued responses, for tests and replay."
            ),
        )
    if isinstance(provider, ClaudeProvider):
        return IntelligenceProviderDescriptor(
            name=provider.name,
            model=provider.model,
            capabilities=CLAUDE_CAPABILITIES,
            configured=True,
            description=(
                "Claude-backed. Sees the information environment and a coarse "
                "market summary; never orders, balances, credentials or risk "
                "limits."
            ),
        )
    return IntelligenceProviderDescriptor(
        name=getattr(provider, "name", "unknown"),
        model=getattr(provider, "model", None),
        capabilities=IntelligenceProviderCapabilities(),
        configured=True,
        description="Provider registered outside the shipped set.",
    )


class IntelligenceProviderDirectory:
    """A registry of provider descriptors.

    Registration order is preserved; re-registering a name replaces its
    descriptor in place rather than appending a duplicate.
    """

    def __init__(self) -> None:
        self._providers: dict[str, IntelligenceProviderDescriptor] = {}
        self._active: str | None = None

    def register(
        self,
        descriptor: IntelligenceProviderDescriptor,
        *,
        active: bool = False,
    ) -> IntelligenceProviderDescriptor:
        """Record a descriptor.

        ``active=True`` marks which provider LUMEN was actually wired with. It
        records a fact about the composition root; it does not cause any
        provider to be used, and nothing reads it to choose one.
        """
        held = descriptor.model_copy(deep=True)
        self._providers[held.name] = held
        if active:
            self._active = held.name
        return held.model_copy(deep=True)

    def register_provider(
        self, provider: IntelligenceProvider, *, active: bool = False
    ) -> IntelligenceProviderDescriptor:
        """Describe and record a constructed provider."""
        return self.register(describe_provider(provider), active=active)

    def get(self, name: str) -> IntelligenceProviderDescriptor | None:
        descriptor = self._providers.get(name)
        return descriptor.model_copy(deep=True) if descriptor is not None else None

    def all(self) -> list[IntelligenceProviderDescriptor]:
        return [
            descriptor.model_copy(deep=True)
            for descriptor in self._providers.values()
        ]

    def active(self) -> IntelligenceProviderDescriptor | None:
        """The descriptor for the provider LUMEN is wired with, if recorded."""
        descriptor = self._providers.get(self._active) if self._active else None
        return descriptor.model_copy(deep=True) if descriptor is not None else None

    def names(self) -> list[str]:
        return list(self._providers.keys())

    def __len__(self) -> int:
        return len(self._providers)


__all__ = [
    "CLAUDE_CAPABILITIES",
    "NULL_CAPABILITIES",
    "SCRIPTED_CAPABILITIES",
    "IntelligenceProviderDirectory",
    "describe_provider",
]
