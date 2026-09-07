"""Intelligence vocabulary — where information came from, not whether it is true.

WHAT THIS MODULE IS FOR
=======================
LUMEN already reads unstructured information and returns structure. It gathers
recent headlines and a coarse market summary, asks a provider for a read, and
converts a successful response into one weighted ``AgentOpinion``.

What the platform has never had is a way to answer, afterwards: *what did LUMEN
see, who did it ask, what came back, and which opinion did that produce?* A
model's judgement is only as inspectable as the inputs behind it, and until now
those inputs existed for the length of one call and then went away.

Every model here is that record.

PROVENANCE, NOT TRUTH
=====================
This module answers "where did this input come from, and when?". It does not
answer "is it correct?", and nothing here should be read as saying it does.
:class:`IntelligenceEvidence` records a source, a title and two timestamps. It
asserts nothing about accuracy, authority or good faith — a headline recorded
here is a headline that arrived, no more. Misinformation, duplicated stories
and conflicting stories all record cleanly and are all still unsolved problems.

THE LINE THIS MODULE MUST NOT CROSS
===================================
* :class:`IntelligenceAnalysisRecord` carries the provider's raw readings —
  sentiment, attention, shock, direction, confidence — as the provider returned
  them. It does **not** recompute LUMEN's turbulence formula or its published
  signal. Two implementations of that formula could disagree, and the one
  nobody tested would eventually be read as the answer.
* :class:`IntelligenceProviderCapabilities` is metadata. No provider is
  selected, skipped, retried or failed over on the strength of it.
* :class:`LumenReadiness` reports. LUMEN is optional by construction — it is
  not in ``required_agents`` — so an unready LUMEN must not stop anything, and
  nothing here gates on one.
* No credential, key, header or client object appears in any model here, and
  none may be added.

NO CLOCK, NO IMPORTS OUTWARD
============================
Every timestamp is supplied by the caller. Nothing here imports from ``apps/``,
``agents/`` or ``execution/``.
"""

from __future__ import annotations

from typing import Any

from pydantic import Field

from core.models.common import AgentId, Base, Millis, StrEnum, new_id

# ======================================================================
# where information comes from
# ======================================================================


class IntelligenceSourceKind(StrEnum):
    """What kind of information an item is.

    Three of these are reachable today. ``HEADLINE`` is what
    ``Lumen.add_headline`` accepts, ``MARKET_CONTEXT`` is the coarse market
    summary ``_context`` assembles, and ``PROVIDER`` is the model's own reading.

    The rest — news articles, announcements, social posts, research, recorded
    replays — are vocabulary. **No adapter exists for any of them**, and adding
    one means adding a network client, a credential and a trust question, none
    of which this phase may introduce.
    """

    HEADLINE = "HEADLINE"
    NEWS_ARTICLE = "NEWS_ARTICLE"
    ANNOUNCEMENT = "ANNOUNCEMENT"
    MARKET_CONTEXT = "MARKET_CONTEXT"
    SOCIAL = "SOCIAL"
    RESEARCH = "RESEARCH"
    PROVIDER = "PROVIDER"
    RECORDED = "RECORDED"


class EvidenceFreshness(StrEnum):
    """How old a piece of evidence is, as a label.

    Observational. LUMEN's existing one-hour headline window in ``_context``
    is unchanged and remains the filter that decides what the provider
    actually sees; this vocabulary describes items after the fact rather than
    selecting them.

    ``UNKNOWN`` is the honest answer for an item with no publication time, and
    is deliberately not folded into ``STALE``: "we do not know how old this is"
    and "this is old" are different facts.
    """

    FRESH = "FRESH"
    STALE = "STALE"
    UNKNOWN = "UNKNOWN"


class IntelligenceEvidence(Base):
    """One piece of information the platform was given.

    Provenance only: where it came from, when it was published, when we
    captured it, and enough of the body to recognise it. It asserts nothing
    about whether the information is accurate or the source trustworthy.

    ``body_excerpt`` is an excerpt on purpose — a record that retained whole
    articles would be sized by how much news happened rather than by how much
    the platform looked at.

    **No secrets, no fetching.** Constructing one of these performs no network
    call, and nothing in this model may carry a key, a token or a header.
    """

    evidence_id: str = Field(default_factory=lambda: new_id("evid"))

    kind: IntelligenceSourceKind = IntelligenceSourceKind.HEADLINE

    #: Who published it, as reported. Not authenticated.
    source: str = ""
    title: str = ""

    published_at: Millis | None = None
    #: When this platform first saw it. Supplied by the caller.
    captured_at: Millis | None = None

    symbol: str = ""

    body_excerpt: str = ""

    #: The publisher's own identifier, where one exists. Used for identity, not
    #: trusted for anything else.
    external_id: str | None = None
    #: A reference string, not a fetch instruction. Nothing dereferences it.
    uri_reference: str | None = None

    freshness: EvidenceFreshness = EvidenceFreshness.UNKNOWN

    metadata: dict[str, str] = Field(default_factory=dict)


def evidence_identity(evidence: IntelligenceEvidence) -> str:
    """A stable key for "is this the same item we already have?".

    Deliberately shallow. When the publisher supplies an id, that is the
    identity. Otherwise it falls back to source, publication time and title,
    which catches the same item arriving twice down the same pipe.

    It will **not** catch the same story reported by two outlets, the same
    story re-headlined, or a story updated in place. Semantic deduplication is
    a judgement about meaning, and a wrong merge silently deletes information
    the platform was given. Exact correctness here is VALIDATION DEFERRED.
    """
    if evidence.external_id:
        return f"{evidence.source}:{evidence.external_id}"
    return f"{evidence.source}:{evidence.published_at}:{evidence.title}"


class IntelligenceMarketContext(Base):
    """The coarse market summary LUMEN is given.

    Exactly the four values ``Lumen._context`` already puts in its payload, in
    a typed form. **Deliberately narrow**: LUMEN sees the information
    environment and a summary of price, and it must never see balances,
    positions, orders, risk limits, RUNE decisions, wallets or credentials.
    Widening this model is how that boundary would erode.
    """

    created_at: Millis
    symbol: str

    reference_price: float | None = None
    venues_quoting: int = 0
    max_cross_venue_deviation_bps: float | None = None
    short_vol_bps: float = 0.0

    source_data_timestamp: Millis | None = None


class IntelligenceEvidenceBundle(Base):
    """Everything one analysis had available to it.

    Describes what information was present. It does not decide what the
    information means — that is the provider's job, and interpreting it here
    would put a second reader beside the one the platform actually asked.

    ``complete`` means the capture finished, not that the evidence is
    sufficient or representative. No model here can tell whether the platform
    saw the story that mattered.
    """

    bundle_id: str = Field(default_factory=lambda: new_id("ibundle"))
    created_at: Millis

    symbol: str = ""

    evidence: list[IntelligenceEvidence] = Field(default_factory=list)
    market_context: IntelligenceMarketContext | None = None

    source_count: int = 0
    latest_published_at: Millis | None = None

    #: The capture ran to completion. Says nothing about sufficiency.
    complete: bool = False

    notes: list[str] = Field(default_factory=list)


class IntelligenceSourceHealth(Base):
    """Whether one information source answered.

    Reporting only. No retry, no backoff, no network — a source that failed is
    recorded as having failed and nothing here does anything about it.
    """

    source: str
    kind: IntelligenceSourceKind = IntelligenceSourceKind.HEADLINE

    available: bool = False
    #: The capture returned everything it was asked for.
    complete: bool = False

    captured_at: Millis | None = None
    last_success_at: Millis | None = None

    items_captured: int = 0
    error: str = ""


# ======================================================================
# asking a provider
# ======================================================================


class IntelligenceProviderCapabilities(Base):
    """What a provider is like. **Descriptive, never dispatched on.**

    Nothing reads these to choose a provider, skip one, retry against another
    or decide whether a response can be trusted. ``build_provider`` still
    selects by configured name, exactly as before.

    They are written down because the differences matter to a reader:
    ``replay_safe`` and ``deterministic`` are the reason ``ScriptedProvider``
    exists, and ``networked`` is the reason ``ClaudeProvider`` is the only
    provider that can fail for reasons outside the platform.
    """

    structured_output: bool = False
    supports_json_schema: bool = False
    networked: bool = False
    deterministic: bool = False
    #: Safe to invoke during a replay. See :class:`IntelligenceReplayProvenance`
    #: for why the question is currently moot: replay reinvokes no provider.
    replay_safe: bool = False
    supports_close: bool = False


class IntelligenceProviderDescriptor(Base):
    """Which provider is configured, and what it is.

    **No API key, no header, no client object, and none may be added.** The
    platform's credential handling stays exactly where it is; a descriptor that
    carried a secret would put one into every snapshot that embeds it.
    """

    name: str
    model: str | None = None

    capabilities: IntelligenceProviderCapabilities = Field(
        default_factory=IntelligenceProviderCapabilities
    )

    #: Whether the provider has what it needs to run. Not whether it works.
    configured: bool = False

    description: str = ""


class IntelligenceProviderRequestRecord(Base):
    """That a provider was asked, and roughly what for.

    A *summary* of the request, not a second copy of it. The system prompt is
    a module constant and the schema is a module constant; storing either per
    request would make the record's size a function of how often LUMEN ran.

    ``payload_summary`` describes the shape of what was sent — how many
    headlines, whether a market summary was present — rather than reproducing
    it. The bundle referenced by ``analysis_id`` holds the content.
    """

    request_id: str = Field(default_factory=lambda: new_id("ireq"))
    created_at: Millis

    analysis_id: str | None = None

    task: str = ""
    provider: str = ""
    model: str | None = None

    max_tokens: int = 0
    timeout_s: float = 0.0

    #: Shape, not content. No secrets.
    payload_summary: dict[str, str] = Field(default_factory=dict)

    schema_name: str = ""


class IntelligenceProviderResponseRecord(Base):
    """What came back, mirrored from ``IntelligenceResponse``.

    The provider's own return type stays authoritative and unchanged. This is a
    copy of its outcome fields, kept so a later reader can see that a call was
    made and how it went without the response object still being alive.

    ``unavailable`` preserves the distinction the provider already draws: a
    provider that declined or could not be reached is a different fact from one
    that returned something unparseable, and collapsing them would make an
    outage look like a bug.
    """

    response_id: str = Field(default_factory=lambda: new_id("iresp"))
    created_at: Millis

    request_id: str | None = None
    analysis_id: str | None = None

    ok: bool = False
    provider: str = ""
    model: str | None = None

    latency_ms: float = 0.0

    #: Declined or unreachable, as opposed to unparseable.
    unavailable: bool = False

    error: str = ""

    #: The readings that came back, summarised. Never the raw payload, and
    #: never anything the provider was configured with.
    data_summary: dict[str, str] = Field(default_factory=dict)


# ======================================================================
# analysis lifecycle
# ======================================================================


class IntelligenceRunStatus(StrEnum):
    """Where one analysis got to.

    Deliberately short. LUMEN's flow is capture, ask, convert — and a lifecycle
    with more states than the code has steps invites someone to implement a
    behaviour to justify one.

    ``UNAVAILABLE`` and ``FAILED`` are kept apart because the platform already
    keeps them apart: a provider that could not be reached is an outage, and a
    response that could not be parsed is a defect somewhere. ``PUBLISHED``
    means an opinion actually reached the bus, which ``COMPLETED`` alone does
    not imply — a malformed response completes the call and publishes nothing.
    """

    CREATED = "CREATED"
    CAPTURING = "CAPTURING"
    REQUESTING = "REQUESTING"
    COMPLETED = "COMPLETED"
    PUBLISHED = "PUBLISHED"
    UNAVAILABLE = "UNAVAILABLE"
    FAILED = "FAILED"


class PublishedOpinionRef(Base):
    """A reference to the ``AgentOpinion`` an analysis produced.

    Not the opinion itself, and no invented identifier. ``AgentOpinion`` has no
    id field of its own, so the reference is the tuple that actually
    identifies one — agent, symbol, creation instant, model version — plus the
    correlation id when the opinion carries one.

    Minting a synthetic id here would create something that looks like a key
    and matches nothing.
    """

    agent_id: AgentId = AgentId.LUMEN
    symbol: str = ""
    created_at: Millis | None = None
    expires_at: Millis | None = None
    model_version: str = ""
    correlation_id: str | None = None

    #: Copied from the opinion. Carried so a reader can see what was published
    #: without the opinion object; never recomputed from the readings below.
    signal: float = 0.0
    confidence: float = 0.0


class IntelligenceAnalysisRecord(Base):
    """One pass of LUMEN's slow loop over one symbol, and its provenance.

    **Observability. The authority is LUMEN.** The readings on this record —
    sentiment, attention, shock, direction, confidence — are copied from the
    provider's response exactly as it returned them. LUMEN's turbulence
    formula and the published signal it produces are **not** recomputed here;
    the published signal appears only as it was published, on
    ``published_opinion_ref``.

    That separation is the point. A record that re-derived the signal would be
    a second implementation of the one thing LUMEN exists to compute, and the
    untested copy would eventually be read as the answer.
    """

    analysis_id: str = Field(default_factory=lambda: new_id("intel"))

    created_at: Millis
    updated_at: Millis
    completed_at: Millis | None = None

    symbol: str = ""

    status: IntelligenceRunStatus = IntelligenceRunStatus.CREATED

    task: str = ""

    evidence_bundle_id: str | None = None

    provider: str = ""
    model: str | None = None

    request_id: str | None = None
    response_id: str | None = None

    provider_latency_ms: float = 0.0

    response_ok: bool = False
    provider_unavailable: bool = False
    #: The provider answered but the payload could not be read. Distinct from
    #: unavailable, exactly as ``_to_opinion`` already distinguishes them.
    malformed_response: bool = False

    #: The provider's raw readings, copied. Not interpreted here.
    sentiment: float | None = None
    attention: float | None = None
    information_shock: bool | None = None
    direction: float | None = None
    confidence: float | None = None

    ttl_seconds: int | None = None

    reason_codes: list[str] = Field(default_factory=list)

    #: Present only when an opinion actually reached the bus.
    published_opinion_ref: PublishedOpinionRef | None = None

    error: str = ""
    notes: list[str] = Field(default_factory=list)

    @property
    def is_terminal(self) -> bool:
        return self.status in (
            IntelligenceRunStatus.COMPLETED,
            IntelligenceRunStatus.PUBLISHED,
            IntelligenceRunStatus.UNAVAILABLE,
            IntelligenceRunStatus.FAILED,
        )

    @property
    def published(self) -> bool:
        return self.published_opinion_ref is not None


class LumenContextSnapshot(Base):
    """What LUMEN saw, for one analysis.

    The record that makes "why did LUMEN say that?" answerable after the fact.
    It holds references — a bundle id, the market context, the ids of the
    headlines in scope — rather than re-assembling the payload, so it cannot
    describe a different input set than the one the provider was sent.
    """

    created_at: Millis
    symbol: str

    analysis_id: str | None = None
    evidence_bundle_id: str | None = None

    market_context: IntelligenceMarketContext | None = None
    headline_ids: list[str] = Field(default_factory=list)

    #: How many headlines the existing window admitted. LUMEN's own selection
    #: -- last ten, published within the hour -- is unchanged.
    headlines_in_scope: int = 0


class IntelligenceReplayProvenance(Base):
    """How a recorded analysis behaves under replay.

    Phase 2 settled this and Phase 10 does not reopen it: **LUMEN opinions are
    exogenous inputs.** Replay republishes the recorded ``AGENT_OPINION``
    events and does not call the provider again — it could not do so
    deterministically, and a replay that re-asked a non-deterministic model
    would not be a replay of anything.

    So for every analysis this build produces:

    * ``recorded`` is True — the opinion went onto the bus and was persisted;
    * ``replayed_as_external_input`` is True — replay reads it back as data;
    * ``provider_reinvoked`` is False, always.

    This is a model. It describes the replay engine's behaviour; it does not
    configure it, and the replay engine is untouched by this phase.
    """

    analysis_id: str | None = None
    opinion_reference: PublishedOpinionRef | None = None

    provider: str = ""
    model: str | None = None

    recorded: bool = True
    replayed_as_external_input: bool = True
    #: False for every path this build has. Never set True by anything here.
    provider_reinvoked: bool = False


# ======================================================================
# the slow loop
# ======================================================================


class IntelligenceLoopStatus(StrEnum):
    """Where the slow loop is.

    Observability. ``Lumen.run_forever`` is unchanged — it still runs a pass,
    logs and counts any exception, and sleeps ``poll_interval_s`` — and no
    scheduler, supervisor or backoff is introduced by naming these states.
    """

    IDLE = "IDLE"
    RUNNING = "RUNNING"
    SLEEPING = "SLEEPING"
    DEGRADED = "DEGRADED"
    STOPPED = "STOPPED"


class IntelligenceLoopSnapshot(Base):
    """One pass of the slow loop, described.

    ``next_run_due_at`` is derived by whoever captures the snapshot from the
    configured interval. It is a description of when the existing sleep is
    expected to end, not a schedule anything obeys.
    """

    created_at: Millis

    status: IntelligenceLoopStatus = IntelligenceLoopStatus.IDLE
    last_run_at: Millis | None = None
    next_run_due_at: Millis | None = None

    poll_interval_s: float = 0.0

    symbols_total: int = 0
    symbols_processed: int = 0

    analyses_started: int = 0
    analyses_completed: int = 0


# ======================================================================
# metrics, readiness and the snapshot
# ======================================================================


class IntelligenceMetrics(Base):
    """Counters. No thresholds, no rates, no adaptive anything.

    Explicitly not an input to anything: no provider is switched, no
    confidence adjusted, no weight moved on the strength of these. A platform
    that down-weighted a provider because it had failed lately would be making
    a calibration decision out of its own history, which is a much later
    question and a much harder one.
    """

    evidence_bundles: int = 0

    analyses_started: int = 0
    analyses_completed: int = 0

    provider_requests: int = 0
    provider_successes: int = 0
    provider_failures: int = 0
    provider_unavailable: int = 0

    opinions_published: int = 0
    malformed_responses: int = 0

    total_latency_ms: float = 0.0


class LumenReadiness(Base):
    """Whether the intelligence layer is in a fit state. **Reporting only.**

    LUMEN IS OPTIONAL, and this model must never be read as making it
    otherwise. It is not in ``required_agents``; a missing LUMEN opinion does
    not make a consensus incomplete; and ``ready=False`` here **must not stop
    trading**. With the shipped ``NullProvider`` configuration LUMEN is
    permanently unready and the platform trades normally — that is the
    designed state, not a fault.

    Provider health is separate and unchanged: ``Lumen._heartbeat`` still
    reports OFFLINE past the failure threshold, DEGRADED below it, HEALTHY
    otherwise, and this model changes none of that.
    """

    ready: bool = False
    created_at: Millis | None = None

    provider_configured: bool = False
    #: The last call succeeded, or none has failed yet. Not a live probe.
    provider_available: bool = False

    evidence_available: bool = False
    market_context_available: bool = False
    recent_analysis_available: bool = False

    consecutive_failures: int = 0

    #: Always True. Stated on the record so a reader cannot mistake an unready
    #: LUMEN for a blocked platform.
    optional: bool = True

    reason_codes: list[str] = Field(default_factory=list)
    detail: str = ""


class LumenSnapshot(Base):
    """One serializable view of the intelligence layer.

    Compact metadata: counters, the provider descriptor, and *ids* for recent
    analyses. A snapshot embedding every analysis with its evidence would be
    sized by how much news the platform had ingested rather than by what is
    currently happening.

    ``created_at`` is supplied by the caller and never read from a clock.
    """

    created_at: Millis

    provider: IntelligenceProviderDescriptor | None = None

    last_call_ms: Millis | None = None
    calls: int = 0
    failures: int = 0
    consecutive_failures: int = 0
    mean_latency_ms: float = 0.0

    headline_count: int = 0

    pending_analyses: int = 0
    recent_analysis_ids: list[str] = Field(default_factory=list)

    latest_analysis_by_symbol: dict[str, str] = Field(default_factory=dict)
    latest_opinion_by_symbol: dict[str, PublishedOpinionRef] = Field(
        default_factory=dict
    )

    loop: IntelligenceLoopSnapshot | None = None

    metrics: IntelligenceMetrics = Field(default_factory=IntelligenceMetrics)
    readiness: LumenReadiness | None = None


# ======================================================================
# control plane
# ======================================================================


class IntelligenceControlKind(StrEnum):
    """Operator actions on the intelligence layer.

    **Vocabulary only. Nothing here is executable**, and ``CLEAR_EVIDENCE``
    least of all: a dispatchable control that deletes the platform's record of
    what it was told would be a way to erase provenance, built before anyone
    decided who may do that or what it must preserve.

    There is no dispatcher, no route and no handler, and adding one is a later,
    separate decision.
    """

    PAUSE = "PAUSE"
    RESUME = "RESUME"
    RUN_NOW = "RUN_NOW"
    CLEAR_EVIDENCE = "CLEAR_EVIDENCE"


def summarize_response_data(data: dict[str, Any]) -> dict[str, str]:
    """Flatten a provider payload into a string summary for the record.

    Values are stringified and truncated. This keeps the response record from
    carrying an arbitrary nested payload, and keeps it from being mistaken for
    the response itself — the readings that matter are typed fields on
    :class:`IntelligenceAnalysisRecord`, copied individually.
    """
    return {str(key): str(value)[:120] for key, value in sorted(data.items())}


__all__ = [
    "EvidenceFreshness",
    "IntelligenceAnalysisRecord",
    "IntelligenceControlKind",
    "IntelligenceEvidence",
    "IntelligenceEvidenceBundle",
    "IntelligenceLoopSnapshot",
    "IntelligenceLoopStatus",
    "IntelligenceMarketContext",
    "IntelligenceMetrics",
    "IntelligenceProviderCapabilities",
    "IntelligenceProviderDescriptor",
    "IntelligenceProviderRequestRecord",
    "IntelligenceProviderResponseRecord",
    "IntelligenceReplayProvenance",
    "IntelligenceRunStatus",
    "IntelligenceSourceHealth",
    "IntelligenceSourceKind",
    "LumenContextSnapshot",
    "LumenReadiness",
    "LumenSnapshot",
    "PublishedOpinionRef",
    "evidence_identity",
    "summarize_response_data",
]
