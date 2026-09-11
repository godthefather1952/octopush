"""Phase 10 audit: registry persistence, detachment and retention."""

from __future__ import annotations

from agents.lumen.providers import IntelligenceProviderDirectory, describe_provider
from agents.lumen.registry import IntelligenceRegistry, IntelligenceStore
from core.models.intelligence import (
    IntelligenceAnalysisRecord,
    IntelligenceEvidence,
    IntelligenceEvidenceBundle,
    IntelligenceProviderDescriptor,
    IntelligenceRunStatus,
    PublishedOpinionRef,
)
from tests.audit.phase10_fixtures import (
    FailingIntelligenceStore,
    RecordingProvider,
    build_lumen,
)


class AnalysisWriteFailStore(IntelligenceStore):
    def put_analysis(self, record: IntelligenceAnalysisRecord) -> None:
        raise RuntimeError("analysis store unavailable")

    def put_evidence_bundle(self, bundle: IntelligenceEvidenceBundle) -> None:
        return None

    def get_analysis(self, analysis_id: str) -> IntelligenceAnalysisRecord | None:
        return None


def test_optional_bundle_store_failure_is_observational_only():
    registry = IntelligenceRegistry(store=FailingIntelligenceStore())
    evidence = IntelligenceEvidence(
        source="wire",
        title="headline",
        published_at=90,
        captured_at=100,
        symbol="BTC-USD",
    )

    bundle = registry.register_evidence_bundle(
        "BTC-USD",
        100,
        evidence=[evidence],
        complete=True,
    )

    assert bundle.bundle_id in registry.bundles
    assert registry.evidence_bundles == 1


def test_optional_analysis_store_failure_is_observational_only():
    registry = IntelligenceRegistry(store=AnalysisWriteFailStore())

    record = registry.begin_analysis("BTC-USD", 100, provider="recording")

    assert record.analysis_id in registry.analyses
    assert registry.analyses_started == 1


async def test_store_failure_cannot_suppress_an_otherwise_valid_lumen_read(settings):
    provider = RecordingProvider()
    lumen = build_lumen(settings, provider=provider)
    lumen.intel_registry.store = FailingIntelligenceStore()

    opinion = await lumen.evaluate(settings.symbols[0])

    assert provider.calls == 1
    assert opinion is not None


def test_get_analysis_returns_a_detached_historical_record():
    registry = IntelligenceRegistry()
    record = registry.begin_analysis("BTC-USD", 100)

    observed = registry.get_analysis(record.analysis_id)
    assert observed is not None
    observed.error = "reader mutation"

    assert registry.analyses[record.analysis_id].error == ""


def test_recent_and_all_analyses_return_detached_records():
    registry = IntelligenceRegistry()
    record = registry.begin_analysis("BTC-USD", 100)

    registry.all_analyses()[0].status = IntelligenceRunStatus.FAILED
    registry.recent_analyses(1)[0].error = "changed"

    held = registry.analyses[record.analysis_id]
    assert held.status is IntelligenceRunStatus.REQUESTING
    assert held.error == ""


def test_evidence_bundle_detaches_caller_owned_items_on_registration():
    registry = IntelligenceRegistry()
    evidence = IntelligenceEvidence(
        source="wire",
        title="original",
        published_at=90,
        captured_at=100,
        symbol="BTC-USD",
    )

    bundle = registry.register_evidence_bundle("BTC-USD", 100, evidence=[evidence])
    evidence.title = "mutated later"

    assert bundle.evidence[0].title == "original"
    assert registry.bundles[bundle.bundle_id].evidence[0].title == "original"


def test_get_bundle_returns_a_detached_bundle():
    registry = IntelligenceRegistry()
    evidence = IntelligenceEvidence(
        source="wire",
        title="original",
        published_at=90,
        captured_at=100,
        symbol="BTC-USD",
    )
    bundle = registry.register_evidence_bundle("BTC-USD", 100, evidence=[evidence])

    observed = registry.get_bundle(bundle.bundle_id)
    assert observed is not None
    observed.evidence[0].title = "reader mutation"

    assert registry.bundles[bundle.bundle_id].evidence[0].title == "original"


def test_evidence_bundle_list_returns_detached_bundles():
    registry = IntelligenceRegistry()
    evidence = IntelligenceEvidence(
        source="wire",
        title="original",
        captured_at=100,
        symbol="BTC-USD",
    )
    bundle = registry.register_evidence_bundle("BTC-USD", 100, evidence=[evidence])

    registry.evidence_bundle_list(1)[0].complete = False

    assert registry.bundles[bundle.bundle_id].complete is True


def test_latest_opinion_queries_do_not_expose_resident_mutable_refs():
    registry = IntelligenceRegistry()
    record = registry.begin_analysis("BTC-USD", 100)
    ref = PublishedOpinionRef(
        symbol="BTC-USD",
        created_at=110,
        expires_at=210,
        model_version="lumen-0.1",
        signal=-0.5,
        confidence=0.8,
    )
    registry.link_opinion(record.analysis_id, ref, 120)

    observed = registry.latest_opinion_for_symbol("BTC-USD")
    assert observed is not None
    observed.signal = 1.0

    assert registry._latest_opinion["BTC-USD"].signal == -0.5


def test_provider_directory_copies_descriptors_in_and_out():
    directory = IntelligenceProviderDirectory()
    descriptor = IntelligenceProviderDescriptor(name="custom", configured=True)
    directory.register(descriptor, active=True)
    descriptor.configured = False

    active = directory.active()
    assert active is not None
    assert active.configured is True

    active.description = "reader mutation"
    assert directory.active() is not None
    assert directory.active().description == ""


def test_describe_provider_does_not_expose_provider_private_state():
    provider = RecordingProvider()
    descriptor = describe_provider(provider)

    assert descriptor.name == provider.name
    assert "requests" not in descriptor.model_dump()
    assert "data" not in descriptor.model_dump()


def test_compaction_never_drops_failed_or_unavailable_analyses():
    registry = IntelligenceRegistry()
    failed = registry.begin_analysis("BTC-USD", 100)
    registry.mark_failed(failed.analysis_id, 110, error="bad")
    unavailable = registry.begin_analysis("ETH-USD", 120)
    registry.mark_unavailable(unavailable.analysis_id, 130, error="offline")

    released = registry.compact(keep_all=False)

    assert released == 0
    assert failed.analysis_id in registry.analyses
    assert unavailable.analysis_id in registry.analyses


def test_compaction_keeps_latest_analysis_and_its_bundle():
    registry = IntelligenceRegistry()
    old_bundle = registry.register_evidence_bundle("BTC-USD", 90)
    old = registry.begin_analysis(
        "BTC-USD", 100, evidence_bundle_id=old_bundle.bundle_id
    )
    registry.complete_analysis(old.analysis_id, 110)

    new_bundle = registry.register_evidence_bundle("BTC-USD", 190)
    latest = registry.begin_analysis(
        "BTC-USD", 200, evidence_bundle_id=new_bundle.bundle_id
    )
    registry.complete_analysis(latest.analysis_id, 210)

    released = registry.compact(keep_all=False)

    assert released == 1
    assert old.analysis_id not in registry.analyses
    assert old_bundle.bundle_id not in registry.bundles
    assert latest.analysis_id in registry.analyses
    assert new_bundle.bundle_id in registry.bundles


def test_pending_analysis_is_never_compacted():
    registry = IntelligenceRegistry()
    record = registry.begin_analysis("BTC-USD", 100)

    released = registry.compact(keep_all=False)

    assert released == 0
    assert record.analysis_id in registry.analyses
