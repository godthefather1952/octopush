"""The intelligence registry — what LUMEN saw, asked, and got back.

WHAT THIS IS
============
``Lumen.evaluate`` gathers context, calls a provider once, and converts a
successful response into an ``AgentOpinion``. The context existed for the
length of that call. The response existed for the length of that call. Neither
survives, so "why did LUMEN say that?" has never had an answer.

:class:`IntelligenceRegistry` is that answer. One record per analysis, holding
the evidence bundle it was built from, a summary of the request, the outcome of
the response, the provider's raw readings, and a reference to the opinion — if
any — that reached the bus.

WHAT IT IS NOT
==============
* **No provider calls.** ``Lumen.evaluate`` calls the provider exactly once,
  and this registry never calls one. Recording provenance must not cost a
  second request — that would double the platform's spend and, against a
  non-deterministic model, produce a "record" of a different answer than the
  one that was used.
* **No signal calculation.** LUMEN's turbulence formula and the signal it
  publishes stay in ``_to_opinion``. The registry stores the provider's raw
  readings and the published signal as published, and derives neither.
* **No consensus.** Nothing here reaches the consensus engine, and nothing in
  the consensus path reads this.
* **No event publication.** The registry writes to memory. ``run_once`` still
  publishes ``AGENT_OPINION``, exactly as it did.
* **Nothing reads it to decide.** Whether an opinion is produced depends on the
  provider response and ``_to_opinion``, precisely as before. Deleting this
  registry would leave LUMEN's behaviour identical.

Every mutation takes ``now_ms`` explicitly. Nothing in this module reads a
clock.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from core.models.common import Millis
from core.models.intelligence import (
    IntelligenceAnalysisRecord,
    IntelligenceEvidence,
    IntelligenceEvidenceBundle,
    IntelligenceMarketContext,
    IntelligenceMetrics,
    IntelligenceProviderRequestRecord,
    IntelligenceProviderResponseRecord,
    IntelligenceRunStatus,
    PublishedOpinionRef,
    evidence_identity,
    summarize_response_data,
)

log = logging.getLogger(__name__)


class IntelligenceStore(ABC):
    """The persistence seam for intelligence provenance.

    **No implementation exists, and none is built here.** In-memory is right
    for a paper session, and choosing a schema now would fix the shape of
    queries nobody has written yet.

    The seam is real rather than decorative: :class:`IntelligenceRegistry`
    accepts one and writes through when present, so a later pass adds a class
    instead of restructuring the registry. Reads still come from memory.
    """

    @abstractmethod
    def put_analysis(self, record: IntelligenceAnalysisRecord) -> None: ...

    @abstractmethod
    def put_evidence_bundle(self, bundle: IntelligenceEvidenceBundle) -> None: ...

    @abstractmethod
    def get_analysis(self, analysis_id: str) -> IntelligenceAnalysisRecord | None: ...


@dataclass
class IntelligenceRegistry:
    """Analyses, evidence bundles and provider provenance, in memory."""

    analyses: dict[str, IntelligenceAnalysisRecord] = field(default_factory=dict)
    bundles: dict[str, IntelligenceEvidenceBundle] = field(default_factory=dict)
    requests: dict[str, IntelligenceProviderRequestRecord] = field(
        default_factory=dict
    )
    responses: dict[str, IntelligenceProviderResponseRecord] = field(
        default_factory=dict
    )

    #: Optional write-through persistence. See :class:`IntelligenceStore`.
    store: IntelligenceStore | None = None

    #: Registration order, so "the last N analyses" is a slice.
    _order: list[str] = field(default_factory=list)
    #: symbol -> analysis_id of the most recent analysis for it.
    _latest_by_symbol: dict[str, str] = field(default_factory=dict)
    #: symbol -> the most recent opinion actually published for it.
    _latest_opinion: dict[str, PublishedOpinionRef] = field(default_factory=dict)

    #: Lifetime counters, unaffected by any future compaction.
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

    # ------------------------------------------------------------------
    # evidence
    # ------------------------------------------------------------------

    def register_evidence_bundle(
        self,
        symbol: str,
        now_ms: Millis,
        *,
        evidence: list[IntelligenceEvidence] | None = None,
        market_context: IntelligenceMarketContext | None = None,
        complete: bool = True,
    ) -> IntelligenceEvidenceBundle:
        """Record what information was available for one analysis.

        ``complete`` says the capture ran to completion. It does **not** say
        the evidence was sufficient or representative — no model here can tell
        whether the platform saw the story that mattered.

        ``latest_published_at`` is the newest publication instant among the
        items, or ``None`` when none of them carries one. ``None`` is not
        folded into "old": not knowing when something was published is a
        different fact from knowing it is stale.
        """
        items = [item.model_copy(deep=True) for item in (evidence or [])]
        published = [item.published_at for item in items if item.published_at is not None]
        bundle = IntelligenceEvidenceBundle(
            created_at=now_ms,
            symbol=symbol,
            evidence=items,
            market_context=(
                market_context.model_copy(deep=True)
                if market_context is not None
                else None
            ),
            source_count=len({item.source for item in items if item.source}),
            latest_published_at=max(published) if published else None,
            complete=complete,
        )
        self.bundles[bundle.bundle_id] = bundle
        self.evidence_bundles += 1
        if self.store is not None:
            try:
                self.store.put_evidence_bundle(bundle)
            except Exception:
                log.exception(
                    "optional intelligence bundle persistence failed",
                    extra={"bundle_id": bundle.bundle_id},
                )
        return bundle.model_copy(deep=True)

    def get_bundle(self, bundle_id: str) -> IntelligenceEvidenceBundle | None:
        bundle = self.bundles.get(bundle_id)
        return bundle.model_copy(deep=True) if bundle is not None else None

    def evidence_bundle_list(self, limit: int = 20) -> list[IntelligenceEvidenceBundle]:
        """The most recently registered bundles, newest last."""
        if limit <= 0:
            return []
        return [
            bundle.model_copy(deep=True)
            for bundle in list(self.bundles.values())[-limit:]
        ]

    def known_evidence_identities(self) -> set[str]:
        """Identity keys for every item in every resident bundle.

        The deduplication seam. A future news adapter can ask whether it has
        already recorded an item before adding it again. Identity is shallow by
        design — see ``evidence_identity`` — and it will not catch the same
        story reported by two outlets.
        """
        return {
            evidence_identity(item)
            for bundle in self.bundles.values()
            for item in bundle.evidence
        }

    # ------------------------------------------------------------------
    # analysis lifecycle
    # ------------------------------------------------------------------

    def begin_analysis(
        self,
        symbol: str,
        now_ms: Millis,
        *,
        task: str = "",
        provider: str = "",
        model: str | None = None,
        evidence_bundle_id: str | None = None,
    ) -> IntelligenceAnalysisRecord:
        """Open a record for one pass over one symbol."""
        record = IntelligenceAnalysisRecord(
            created_at=now_ms,
            updated_at=now_ms,
            symbol=symbol,
            status=IntelligenceRunStatus.REQUESTING,
            task=task,
            evidence_bundle_id=evidence_bundle_id,
            provider=provider,
            model=model,
        )
        self.analyses[record.analysis_id] = record
        self._order.append(record.analysis_id)
        self._latest_by_symbol[symbol] = record.analysis_id
        self.analyses_started += 1
        self._persist(record)
        return record

    def attach_request(
        self,
        analysis_id: str,
        now_ms: Millis,
        *,
        task: str = "",
        provider: str = "",
        model: str | None = None,
        max_tokens: int = 0,
        timeout_s: float = 0.0,
        payload_summary: dict[str, str] | None = None,
        schema_name: str = "",
    ) -> IntelligenceProviderRequestRecord | None:
        """Record that the provider was asked, and roughly what for.

        A summary, not a second copy of the request. The system prompt and the
        response schema are module constants; storing either per call would
        make the record's size a function of how often LUMEN ran.

        **This does not send anything.** The call itself is made once, by
        ``Lumen.evaluate``, exactly as before.
        """
        record = self.analyses.get(analysis_id)
        if record is None:
            return None
        request = IntelligenceProviderRequestRecord(
            created_at=now_ms,
            analysis_id=analysis_id,
            task=task or record.task,
            provider=provider or record.provider,
            model=model if model is not None else record.model,
            max_tokens=max_tokens,
            timeout_s=timeout_s,
            payload_summary=dict(payload_summary or {}),
            schema_name=schema_name,
        )
        self.requests[request.request_id] = request
        record.request_id = request.request_id
        record.updated_at = now_ms
        self.provider_requests += 1
        self._persist(record)
        return request

    def attach_response(
        self,
        analysis_id: str,
        now_ms: Millis,
        *,
        ok: bool,
        provider: str = "",
        model: str | None = None,
        latency_ms: float = 0.0,
        unavailable: bool = False,
        error: str = "",
        data: dict[str, Any] | None = None,
    ) -> IntelligenceProviderResponseRecord | None:
        """Mirror the outcome of the one provider call that was made.

        Every field is copied from the ``IntelligenceResponse`` the provider
        returned, which stays the authoritative object. The readings are copied
        onto the analysis record individually and are **not** interpreted:
        LUMEN's turbulence formula and its published signal stay in
        ``_to_opinion``.
        """
        record = self.analyses.get(analysis_id)
        if record is None:
            return None
        payload = dict(data or {})
        response = IntelligenceProviderResponseRecord(
            created_at=now_ms,
            request_id=record.request_id,
            analysis_id=analysis_id,
            ok=ok,
            provider=provider or record.provider,
            model=model if model is not None else record.model,
            latency_ms=latency_ms,
            unavailable=unavailable,
            error=error,
            data_summary=summarize_response_data(payload),
        )
        self.responses[response.response_id] = response

        record.response_id = response.response_id
        record.response_ok = ok
        record.provider_unavailable = unavailable
        record.provider_latency_ms = latency_ms
        record.updated_at = now_ms
        if error:
            record.error = error

        self.total_latency_ms += latency_ms
        if ok:
            self.provider_successes += 1
            self._copy_readings(record, payload)
        else:
            self.provider_failures += 1
            if unavailable:
                self.provider_unavailable += 1

        self._persist(record)
        return response

    @staticmethod
    def _copy_readings(
        record: IntelligenceAnalysisRecord, data: dict[str, Any]
    ) -> None:
        """Copy the provider's raw readings onto the record.

        Copied, never derived. A reading the provider did not supply, or
        supplied unusably, stays ``None`` rather than being defaulted — a
        record that filled in a plausible zero would be inventing evidence.
        Whether the payload is usable at all is settled by ``_to_opinion``,
        which is unchanged and remains the only reader that matters.
        """
        for name in ("sentiment", "attention", "direction", "confidence"):
            value = data.get(name)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                setattr(record, name, float(value))
        shock = data.get("information_shock")
        if isinstance(shock, bool):
            record.information_shock = shock
        ttl = data.get("ttl_seconds")
        if isinstance(ttl, int) and not isinstance(ttl, bool):
            record.ttl_seconds = ttl
        codes = data.get("reason_codes")
        if isinstance(codes, list):
            record.reason_codes = [str(code) for code in codes][:8]

    def complete_analysis(
        self, analysis_id: str, now_ms: Millis
    ) -> IntelligenceAnalysisRecord | None:
        """Close the analysis as COMPLETED.

        COMPLETED means the call finished, not that an opinion was published.
        A malformed response completes the call and publishes nothing —
        :meth:`mark_malformed` records that case, and
        :attr:`IntelligenceAnalysisRecord.published` is the field that answers
        whether anything reached the bus.
        """
        record = self.analyses.get(analysis_id)
        if record is None:
            return None
        if record.is_terminal:
            return record.model_copy(deep=True)
        record.status = IntelligenceRunStatus.COMPLETED
        record.completed_at = now_ms
        record.updated_at = now_ms
        self.analyses_completed += 1
        self._persist(record)
        return record.model_copy(deep=True)

    def mark_unavailable(
        self, analysis_id: str, now_ms: Millis, *, error: str = ""
    ) -> IntelligenceAnalysisRecord | None:
        """The provider declined or could not be reached.

        Kept distinct from :meth:`mark_failed`. An outage and a defect are
        different facts, and collapsing them makes an API being down look like
        a bug in the platform.
        """
        record = self.analyses.get(analysis_id)
        if record is None:
            return None
        if record.is_terminal:
            return record.model_copy(deep=True)
        record.status = IntelligenceRunStatus.UNAVAILABLE
        record.provider_unavailable = True
        record.completed_at = now_ms
        record.updated_at = now_ms
        if error:
            record.error = error
        self._persist(record)
        return record.model_copy(deep=True)

    def mark_failed(
        self, analysis_id: str, now_ms: Millis, *, error: str = ""
    ) -> IntelligenceAnalysisRecord | None:
        record = self.analyses.get(analysis_id)
        if record is None:
            return None
        if record.is_terminal:
            return record.model_copy(deep=True)
        record.status = IntelligenceRunStatus.FAILED
        record.completed_at = now_ms
        record.updated_at = now_ms
        if error:
            record.error = error
        self._persist(record)
        return record.model_copy(deep=True)

    def mark_malformed(
        self, analysis_id: str, now_ms: Millis, *, error: str = ""
    ) -> IntelligenceAnalysisRecord | None:
        """The provider answered, but the payload could not be read.

        Recorded *after* ``_to_opinion`` has already returned ``None`` and
        LUMEN has already published nothing. The registry never invents a
        neutral opinion to fill the gap — a fabricated neutral vote is a lie,
        and the platform's existing choice to publish nothing is the honest
        one.
        """
        record = self.analyses.get(analysis_id)
        if record is None:
            return None
        if record.is_terminal:
            return record.model_copy(deep=True)
        record.malformed_response = True
        record.status = IntelligenceRunStatus.FAILED
        record.completed_at = now_ms
        record.updated_at = now_ms
        if error:
            record.error = error
        self.malformed_responses += 1
        self._persist(record)
        return record.model_copy(deep=True)

    def link_opinion(
        self, analysis_id: str, ref: PublishedOpinionRef, now_ms: Millis
    ) -> IntelligenceAnalysisRecord | None:
        """Attach the reference to the opinion this analysis produced.

        The reference is the tuple that actually identifies an
        ``AgentOpinion`` — agent, symbol, creation instant, model version. No
        synthetic id is minted: ``AgentOpinion`` has none, and inventing one
        would create something that looks like a key and matches nothing.
        """
        record = self.analyses.get(analysis_id)
        if record is None:
            return None
        if record.status is IntelligenceRunStatus.PUBLISHED:
            return record.model_copy(deep=True)
        if record.status in (
            IntelligenceRunStatus.UNAVAILABLE,
            IntelligenceRunStatus.FAILED,
        ):
            return record.model_copy(deep=True)
        held_ref = ref.model_copy(deep=True)
        record.published_opinion_ref = held_ref
        record.status = IntelligenceRunStatus.PUBLISHED
        record.updated_at = now_ms
        if held_ref.symbol:
            self._latest_opinion[held_ref.symbol] = held_ref.model_copy(deep=True)
        self.opinions_published += 1
        self._persist(record)
        return record.model_copy(deep=True)

    # ------------------------------------------------------------------
    # queries
    # ------------------------------------------------------------------

    def get_analysis(self, analysis_id: str) -> IntelligenceAnalysisRecord | None:
        record = self.analyses.get(analysis_id)
        return record.model_copy(deep=True) if record is not None else None

    def all_analyses(self) -> list[IntelligenceAnalysisRecord]:
        """Every resident analysis, oldest first."""
        return [
            self.analyses[aid].model_copy(deep=True)
            for aid in self._order
            if aid in self.analyses
        ]

    def recent_analyses(self, limit: int = 20) -> list[IntelligenceAnalysisRecord]:
        """The most recent analyses, newest last."""
        if limit <= 0:
            return []
        return [
            self.analyses[aid].model_copy(deep=True)
            for aid in self._order[-limit:]
            if aid in self.analyses
        ]

    def analyses_for_symbol(self, symbol: str) -> list[IntelligenceAnalysisRecord]:
        return [
            record.model_copy(deep=True)
            for record in self.analyses.values()
            if record.symbol == symbol
        ]

    def latest_for_symbol(self, symbol: str) -> IntelligenceAnalysisRecord | None:
        analysis_id = self._latest_by_symbol.get(symbol)
        record = self.analyses.get(analysis_id) if analysis_id else None
        return record.model_copy(deep=True) if record is not None else None

    def latest_opinion_for_symbol(self, symbol: str) -> PublishedOpinionRef | None:
        ref = self._latest_opinion.get(symbol)
        return ref.model_copy(deep=True) if ref is not None else None

    def latest_opinions(self) -> dict[str, PublishedOpinionRef]:
        return {
            symbol: ref.model_copy(deep=True)
            for symbol, ref in self._latest_opinion.items()
        }

    def latest_analysis_ids(self) -> dict[str, str]:
        return dict(self._latest_by_symbol)

    def pending(self) -> list[IntelligenceAnalysisRecord]:
        """Analyses that have not reached a terminal state."""
        return [
            record.model_copy(deep=True)
            for record in self.analyses.values()
            if not record.is_terminal
        ]

    def unavailable(self) -> list[IntelligenceAnalysisRecord]:
        """Analyses whose provider declined or could not be reached."""
        return [
            record.model_copy(deep=True)
            for record in self.analyses.values()
            if record.status is IntelligenceRunStatus.UNAVAILABLE
        ]

    def metrics(self) -> IntelligenceMetrics:
        """Counters, for display. Nothing reads these to decide anything."""
        return IntelligenceMetrics(
            evidence_bundles=self.evidence_bundles,
            analyses_started=self.analyses_started,
            analyses_completed=self.analyses_completed,
            provider_requests=self.provider_requests,
            provider_successes=self.provider_successes,
            provider_failures=self.provider_failures,
            provider_unavailable=self.provider_unavailable,
            opinions_published=self.opinions_published,
            malformed_responses=self.malformed_responses,
            total_latency_ms=self.total_latency_ms,
        )

    # ------------------------------------------------------------------
    # retention
    # ------------------------------------------------------------------

    def compact(self, *, keep_all: bool = True) -> int:
        """Release finished analyses. Conservative by construction.

        With the default **nothing is released.** The framework supplies the
        hook and the safety rules and leaves the policy to a later pass that
        has measured what retention costs. There is no arbitrary count limit,
        because no measurement supports choosing one. LUMEN's own 50-item
        headline cap is separate and unchanged.

        Even when asked to release, these are absolute:

        * a pending analysis is never released;
        * a failed or unavailable analysis is never released — the record of a
          provider outage is exactly what a later reader will want, and no
          aging policy exists yet to say when it stops mattering;
        * an analysis whose opinion is the latest for its symbol is never
          released, nor is its evidence bundle;
        * no evidence bundle referenced by a resident analysis is released.

        Dropping any of those is how a platform loses the record of where its
        information came from. Lifetime counters are unaffected.

        Returns the number of analysis records released.
        """
        if keep_all:
            return 0

        latest_ids = set(self._latest_by_symbol.values())
        doomed = [
            record
            for record in self.all_analyses()
            if record.is_terminal
            and record.status
            not in (IntelligenceRunStatus.FAILED, IntelligenceRunStatus.UNAVAILABLE)
            and record.analysis_id not in latest_ids
        ]
        for record in doomed:
            self.analyses.pop(record.analysis_id, None)
            if record.request_id is not None:
                self.requests.pop(record.request_id, None)
            if record.response_id is not None:
                self.responses.pop(record.response_id, None)
        self._order = [aid for aid in self._order if aid in self.analyses]

        referenced = {
            record.evidence_bundle_id
            for record in self.analyses.values()
            if record.evidence_bundle_id is not None
        }
        for bundle_id in [bid for bid in self.bundles if bid not in referenced]:
            self.bundles.pop(bundle_id, None)
        return len(doomed)

    @property
    def resident_analyses(self) -> int:
        return len(self.analyses)

    @property
    def resident_bundles(self) -> int:
        return len(self.bundles)

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _persist(self, record: IntelligenceAnalysisRecord) -> None:
        if self.store is None:
            return
        try:
            self.store.put_analysis(record)
        except Exception:
            log.exception(
                "optional intelligence analysis persistence failed",
                extra={"analysis_id": record.analysis_id},
            )


__all__ = ["IntelligenceRegistry", "IntelligenceStore"]
