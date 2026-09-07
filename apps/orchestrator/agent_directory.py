"""Who participates — a phone book, not a switchboard.

WHAT THIS IS
============
The platform has always known which agents exist, but only implicitly: they are
constructed in ``wiring.py``, they subscribe to the bus, and
``ConsensusConfig.required_agents`` names three of them. Nothing anywhere
answers the plain question "what participants does this platform have, what
does each one look at, and how often?"

:class:`AgentDirectory` answers exactly that and nothing more. It holds
:class:`~core.models.orchestration.AgentDescriptor` metadata — an id, a service
name, a version, a subject scope, a cadence, a mirrored weight. It holds no
callables, no addresses, no credentials and no transport. You cannot invoke an
agent through this object, which is the point: a directory that could dispatch
would be a second message path competing with the bus.

WHAT DECIDES, AND WHAT MERELY DESCRIBES
=======================================
``ConsensusConfig.required_agents`` decides who is required. The directory
mirrors that list into ``AgentDescriptor.required_by_default`` so a reader can
see it in one place, and :meth:`AgentDirectory.required` reports what the
directory was told. Neither is consulted by the consensus engine, the barrier,
or the orchestrator's entry path. If the two ever disagree, the configuration
is right and the directory is stale — never the other way around.

Registration is metadata only, so a missing registration cannot break trading:
an unregistered agent still publishes opinions, is still tracked by the
barrier, and is still weighed by the consensus engine. It is simply absent from
a display.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from core.models.common import AgentId, Millis
from core.models.orchestration import (
    AgentCadence,
    AgentDescriptor,
    AgentDirectorySnapshot,
    AgentSubjectScope,
)


class AgentDirectory:
    """An ordered registry of agent metadata.

    Registration order is preserved, because the order agents are wired in is
    the order a reader expects to see them listed. Re-registering the same
    ``agent_id`` replaces the descriptor in place rather than appending a
    duplicate: wiring may legitimately run twice in a test, and a directory
    that grew a second TIDAL each time would be reporting a fiction.
    """

    def __init__(self) -> None:
        self._agents: dict[AgentId, AgentDescriptor] = {}

    # -- registration ------------------------------------------------------

    def register(
        self,
        agent_id: AgentId,
        *,
        service: str = "",
        version: str = "",
        scope: AgentSubjectScope = AgentSubjectScope.UNSPECIFIED,
        cadence: AgentCadence = AgentCadence.UNSPECIFIED,
        required_by_default: bool = False,
        weight: float | None = None,
        description: str = "",
    ) -> AgentDescriptor:
        """Record what is known about one participant.

        ``required_by_default`` and ``weight`` are mirrors of configuration.
        Passing them here changes nothing about consensus; it only makes the
        configured values visible alongside the rest of the metadata.
        """
        descriptor = AgentDescriptor(
            agent_id=agent_id,
            service=service,
            version=version,
            scope=scope,
            cadence=cadence,
            required_by_default=required_by_default,
            weight=weight,
            description=description,
        )
        self._agents[agent_id] = descriptor
        return descriptor

    def register_descriptor(self, descriptor: AgentDescriptor) -> AgentDescriptor:
        """Register a descriptor built elsewhere."""
        self._agents[descriptor.agent_id] = descriptor
        return descriptor

    def forget(self, agent_id: AgentId) -> None:
        """Drop one registration. Nothing about trading changes."""
        self._agents.pop(agent_id, None)

    # -- queries -----------------------------------------------------------

    def get(self, agent_id: AgentId) -> AgentDescriptor | None:
        """The descriptor for one agent, or ``None`` if it was never registered.

        ``None`` means "not described here". It does not mean the agent is
        absent, unhealthy, or excluded from consensus — the directory has no
        opinion on any of those.
        """
        return self._agents.get(agent_id)

    def all(self) -> list[AgentDescriptor]:
        """Every registered descriptor, in registration order."""
        return list(self._agents.values())

    def ids(self) -> list[AgentId]:
        """Every registered agent id, in registration order."""
        return list(self._agents.keys())

    def required(self) -> list[AgentDescriptor]:
        """Those registered as required by the shipped configuration.

        This reports what the directory was told at registration time. The
        consensus engine reads ``ConsensusConfig.required_agents`` and is
        unaffected by anything here.
        """
        return [d for d in self._agents.values() if d.required_by_default]

    def by_scope(self, scope: AgentSubjectScope) -> list[AgentDescriptor]:
        """Registered agents that look at a given kind of subject."""
        return [d for d in self._agents.values() if d.scope is scope]

    def by_cadence(self, cadence: AgentCadence) -> list[AgentDescriptor]:
        """Registered agents that run at a given rhythm."""
        return [d for d in self._agents.values() if d.cadence is cadence]

    def contains(self, agent_id: AgentId) -> bool:
        return agent_id in self._agents

    def __len__(self) -> int:
        return len(self._agents)

    def __iter__(self) -> Iterable[AgentDescriptor]:
        return iter(list(self._agents.values()))

    # -- capture -----------------------------------------------------------

    def snapshot(
        self,
        now_ms: Millis,
        *,
        required_agents: Sequence[AgentId] | None = None,
    ) -> AgentDirectorySnapshot:
        """Capture the directory at a caller-supplied instant.

        ``required_agents`` is the authoritative list from configuration. When
        given it is mirrored into the snapshot verbatim; when omitted the
        snapshot falls back to what registration recorded, which may be stale.
        The snapshot never reconciles the two — a display that quietly
        "corrected" configuration would hide precisely the drift worth seeing.
        """
        if required_agents is None:
            mirrored = [d.agent_id for d in self._agents.values() if d.required_by_default]
        else:
            mirrored = list(required_agents)
        return AgentDirectorySnapshot(
            created_at=now_ms,
            agents=self.all(),
            required_agents=mirrored,
        )


__all__ = ["AgentDirectory"]
