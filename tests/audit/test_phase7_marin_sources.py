"""Phase 7 audit — truth-source capture and readiness."""

from __future__ import annotations

from core.models.ops import ReconciliationResult
from core.models.reconciliation import ReconciliationSourceKind
from tests.audit.marin_fixtures import (
    T0,
    build_marin,
    empty_account_source,
    empty_execution_source,
    make_unknown,
    venue_source,
)


class TestCaptureHealth:
    def test_source_exception_is_visible_as_unavailable(self, bus, clock, health):
        marin = build_marin(bus=bus, clock=clock, health=health)
        marin.attach_sources(venues=[venue_source("A", explode=True)])

        snapshot = marin.capture_snapshot(T0)

        assert snapshot.venues == []
        assert snapshot.venue is None
        assert len(snapshot.sources) == 1
        source = snapshot.sources[0]
        assert source.kind is ReconciliationSourceKind.VENUE
        assert source.available is False
        assert source.complete is False
        assert "RuntimeError" in source.detail

    def test_empty_venue_truth_is_not_the_same_as_missing(self, bus, clock, health):
        marin = build_marin(bus=bus, clock=clock, health=health)
        marin.attach_sources(
            venues=[venue_source("A"), venue_source("B", available=False)]
        )

        snapshot = marin.capture_snapshot(T0)

        health_by_name = {s.name: s for s in snapshot.sources}
        assert health_by_name["venue-A"].available is True
        assert health_by_name["venue-B"].available is False
        assert [venue.venue for venue in snapshot.venues] == ["A"]
        assert snapshot.venue is snapshot.venues[0]

    def test_capture_uses_supplied_logical_time(self, bus, clock, health):
        marin = build_marin(bus=bus, clock=clock, health=health)
        marin.attach_sources(venues=[venue_source("A")])
        logical_now = clock.now_ms()
        clock.set(logical_now + 999_999)

        snapshot = marin.capture_snapshot(logical_now)

        assert snapshot.created_at == logical_now
        assert len(snapshot.venues) == 1
        assert snapshot.venues[0].created_at == logical_now
        assert snapshot.sources[0].captured_at == logical_now


class TestMultiVenueTruth:
    def test_all_configured_venue_snapshots_survive_one_capture(self, bus, clock, health):
        """H7-18: no authoritative venue may overwrite another."""
        marin = build_marin(bus=bus, clock=clock, health=health)
        marin.attach_sources(venues=[venue_source("A"), venue_source("B")])

        snapshot = marin.capture_snapshot(T0)

        assert [venue.venue for venue in snapshot.venues] == ["A", "B"]
        assert snapshot.venue is snapshot.venues[0]
        assert len({venue.snapshot_id for venue in snapshot.venues}) == 2

    def test_healthy_and_unavailable_venue_health_both_survive(self, bus, clock, health):
        marin = build_marin(bus=bus, clock=clock, health=health)
        marin.attach_sources(
            venues=[venue_source("A"), venue_source("B", available=False)]
        )

        snapshot = marin.capture_snapshot(T0)

        assert {s.name for s in snapshot.sources} == {"venue-A", "venue-B"}
        assert ReconciliationSourceKind.VENUE in snapshot.missing_sources
        assert [venue.venue for venue in snapshot.venues] == ["A"]


class TestReadiness:
    def test_no_reconciliation_is_never_ready(self, bus, clock, health):
        marin = build_marin(bus=bus, clock=clock, health=health, attach_local=True)

        readiness = marin.readiness(T0)

        assert readiness.ready is False
        assert "NO_RECONCILIATION_YET" in readiness.reason_codes

    def test_missing_execution_and_account_sources_are_explicit(self, bus, clock, health):
        marin = build_marin(bus=bus, clock=clock, health=health)
        marin.last_result = ReconciliationResult(created_at=T0, ok=True)

        readiness = marin.readiness(T0)

        assert "NO_EXECUTION_SOURCE" in readiness.reason_codes
        assert "NO_ACCOUNT_SOURCE" in readiness.reason_codes
        assert readiness.ready is False

    def test_configured_sources_must_have_been_captured(self, bus, clock, health):
        marin = build_marin(bus=bus, clock=clock, health=health, attach_local=True)
        marin.last_result = ReconciliationResult(created_at=T0, ok=True)

        readiness = marin.readiness(T0)

        assert readiness.ready is False
        assert "NO_EXECUTION_CAPTURE" in readiness.reason_codes
        assert "NO_ACCOUNT_CAPTURE" in readiness.reason_codes
        assert readiness.execution_source_available is False
        assert readiness.account_source_available is False

    def test_unknown_order_prevents_ready(self, bus, clock, health):
        marin = build_marin(bus=bus, clock=clock, health=health, attach_local=True)
        marin.capture_snapshot(T0)
        marin.last_result = ReconciliationResult(created_at=T0, ok=True)
        make_unknown(marin)

        readiness = marin.readiness(T0)

        assert readiness.ready is False
        assert "UNRESOLVED_ORDERS" in readiness.reason_codes

    def test_configured_but_unavailable_source_is_not_ready(self, bus, clock, health):
        """H7-27: source configuration is not proof that capture is usable."""
        marin = build_marin(bus=bus, clock=clock, health=health)
        marin.attach_sources(
            execution=empty_execution_source(available=False),
            account=empty_account_source(),
        )
        marin.last_result = ReconciliationResult(created_at=T0, ok=True)
        marin.capture_snapshot(T0)

        readiness = marin.readiness(T0)

        assert readiness.ready is False
        assert "EXECUTION_SOURCE_UNUSABLE" in readiness.reason_codes
        assert readiness.execution_source_available is False
        assert readiness.account_source_available is True

    def test_configured_but_incomplete_account_source_is_not_ready(
        self, bus, clock, health
    ):
        marin = build_marin(bus=bus, clock=clock, health=health)
        marin.attach_sources(
            execution=empty_execution_source(),
            account=empty_account_source(complete=False),
        )
        marin.last_result = ReconciliationResult(created_at=T0, ok=True)
        marin.capture_snapshot(T0)

        readiness = marin.readiness(T0)

        assert readiness.ready is False
        assert "ACCOUNT_SOURCE_UNUSABLE" in readiness.reason_codes
        assert readiness.execution_source_available is True
        assert readiness.account_source_available is False

    def test_current_paper_readiness_does_not_require_venue_source(self, bus, clock, health):
        """Policy measurement: startup has a stronger venue requirement."""
        marin = build_marin(bus=bus, clock=clock, health=health, attach_local=True)
        marin.capture_snapshot(T0)
        marin.last_result = ReconciliationResult(created_at=T0, ok=True)

        readiness = marin.readiness(T0)

        assert readiness.ready is True
        assert readiness.execution_source_available is True
        assert readiness.account_source_available is True
        assert readiness.venue_sources_available == 0

    def test_startup_request_requires_venue_truth(self, bus, clock, health):
        marin = build_marin(bus=bus, clock=clock, health=health, attach_local=True)

        request = marin.prepare_startup_reconciliation(T0)

        assert ReconciliationSourceKind.VENUE in request.required_sources
        assert ReconciliationSourceKind.VENUE in request.missing_required
        assert request.sources_satisfied is False
