"""Phase 7 audit — reconciliation run and discrepancy registry."""

from __future__ import annotations

from core.models.ops import Mismatch, MismatchKind, ReconciliationResult, Severity
from core.models.reconciliation import (
    DiscrepancyEntityType,
    DiscrepancyStatus,
    ReconciliationDiscrepancy,
    ReconciliationRunStatus,
    ReconciliationSourceKind,
    ResolutionAction,
    ResolutionStatus,
    discrepancy_identity,
)
from tests.audit.marin_fixtures import T0, build_marin


def mismatch(amount: float = 10.0) -> Mismatch:
    return Mismatch(
        kind=MismatchKind.CASH_MISMATCH,
        severity=Severity.CRITICAL,
        key="cash",
        expected=100.0,
        actual=100.0 + amount,
        difference=amount,
    )


class TestDiscrepancyIdentity:
    def test_source_pair_is_commutative(self):
        a = discrepancy_identity(
            MismatchKind.CASH_MISMATCH,
            DiscrepancyEntityType.CASH,
            "cash",
            ReconciliationSourceKind.EXECUTION,
            ReconciliationSourceKind.ACCOUNT,
        )
        b = discrepancy_identity(
            MismatchKind.CASH_MISMATCH,
            DiscrepancyEntityType.CASH,
            "cash",
            ReconciliationSourceKind.ACCOUNT,
            ReconciliationSourceKind.EXECUTION,
        )
        assert a == b

    def test_repeated_discrepancy_updates_in_place(self, bus, clock, health):
        marin = build_marin(bus=bus, clock=clock, health=health)
        one = ReconciliationResult(created_at=T0, ok=False, mismatches=[mismatch(10)])
        two = ReconciliationResult(created_at=T0 + 1, ok=False, mismatches=[mismatch(15)])

        marin.mirror_result(one, T0)
        marin.mirror_result(two, T0 + 1)

        found = marin.open_discrepancies()
        assert len(found) == 1
        assert found[0].occurrences == 2
        assert found[0].difference == 15.0
        assert found[0].last_seen_at == T0 + 1

    def test_different_entities_do_not_collide(self, bus, clock, health):
        marin = build_marin(bus=bus, clock=clock, health=health)
        run = marin.registry.register_run(T0)
        for key in ("cash-a", "cash-b"):
            marin.registry.add_mismatch(
                Mismatch(
                    kind=MismatchKind.CASH_MISMATCH,
                    severity=Severity.CRITICAL,
                    key=key,
                ),
                run_id=run.run_id,
                now_ms=T0,
            )
        assert len(marin.registry.all_discrepancies()) == 2

    def test_resolved_discrepancy_reopens_if_seen_again(self, bus, clock, health):
        marin = build_marin(bus=bus, clock=clock, health=health)
        result = ReconciliationResult(created_at=T0, ok=False, mismatches=[mismatch()])
        record = marin.mirror_result(result, T0)
        discrepancy = marin.discrepancies_for_run(record.run_id)[0]
        marin.registry.set_discrepancy_status(
            discrepancy.discrepancy_id, DiscrepancyStatus.RESOLVED, T0 + 1
        )

        later = ReconciliationResult(
            created_at=T0 + 2, ok=False, mismatches=[mismatch(20)]
        )
        marin.mirror_result(later, T0 + 2)

        reopened = marin.registry.get_discrepancy(discrepancy.discrepancy_id)
        assert reopened is not None
        assert reopened.status is DiscrepancyStatus.OPEN
        assert reopened.occurrences == 2


class TestRunLifecycle:
    def test_begin_reconciliation_stops_at_capturing(self, bus, clock, health):
        marin = build_marin(bus=bus, clock=clock, health=health, attach_local=True)

        record = marin.begin_reconciliation(T0)

        assert record.status is ReconciliationRunStatus.CAPTURING
        assert marin.last_result is None

    def test_mirror_result_preserves_severity_and_result(self, bus, clock, health):
        marin = build_marin(bus=bus, clock=clock, health=health)
        result = ReconciliationResult(created_at=T0, ok=False, mismatches=[mismatch()])
        before = result.model_dump()

        record = marin.mirror_result(result, T0)

        assert result.model_dump() == before
        discrepancy = marin.discrepancies_for_run(record.run_id)[0]
        assert discrepancy.severity is Severity.CRITICAL
        assert record.status is ReconciliationRunStatus.DISCREPANCIES_FOUND

    def test_repeated_status_write_does_not_double_count(self, bus, clock, health):
        marin = build_marin(bus=bus, clock=clock, health=health)
        record = marin.registry.register_run(T0)
        marin.registry.set_run_status(record.run_id, ReconciliationRunStatus.CLEAN, T0)
        first = marin.registry.metrics()
        marin.registry.set_run_status(record.run_id, ReconciliationRunStatus.CLEAN, T0 + 1)
        second = marin.registry.metrics()

        assert second.clean_runs == first.clean_runs
        assert second.runs_completed == first.runs_completed


class TestRetention:
    def _open_discrepancy(self, marin):
        record = marin.registry.register_run(T0)
        d = ReconciliationDiscrepancy(
            discrepancy_id="d-open",
            run_id=record.run_id,
            kind=MismatchKind.CASH_MISMATCH,
            severity=Severity.CRITICAL,
            entity_type=DiscrepancyEntityType.CASH,
            entity_id="cash",
            source_a=ReconciliationSourceKind.EXECUTION,
            source_b=ReconciliationSourceKind.ACCOUNT,
            first_seen_at=T0,
            last_seen_at=T0,
        )
        marin.registry.add_discrepancy(d, T0)
        marin.registry.set_run_status(
            record.run_id, ReconciliationRunStatus.RESOLVED, T0
        )
        return record, d

    def test_open_discrepancy_prevents_run_compaction(self, bus, clock, health):
        marin = build_marin(bus=bus, clock=clock, health=health)
        record, _ = self._open_discrepancy(marin)

        released = marin.registry.compact(keep_terminal=False)

        assert released == 0
        assert marin.registry.get_run(record.run_id) is not None

    def test_pending_resolution_prevents_run_compaction(self, bus, clock, health):
        marin = build_marin(bus=bus, clock=clock, health=health)
        record, d = self._open_discrepancy(marin)
        marin.registry.set_discrepancy_status(
            d.discrepancy_id, DiscrepancyStatus.RESOLVED, T0
        )
        resolution = marin.propose_resolution(
            d.discrepancy_id,
            T0,
            action=ResolutionAction.ESCALATE_OPERATOR,
        )
        assert resolution.status is ResolutionStatus.PROPOSED

        released = marin.registry.compact(keep_terminal=False)

        assert released == 0
        assert marin.registry.get_run(record.run_id) is not None

    def test_terminal_settled_run_compacts_and_metrics_survive(self, bus, clock, health):
        marin = build_marin(bus=bus, clock=clock, health=health)
        record = marin.registry.register_run(T0)
        marin.registry.set_run_status(record.run_id, ReconciliationRunStatus.CLEAN, T0)
        before = marin.registry.metrics()

        released = marin.registry.compact(keep_terminal=False)
        after = marin.registry.metrics()

        assert released == 1
        assert marin.registry.get_run(record.run_id) is None
        assert after.runs_started == before.runs_started
        assert after.runs_completed == before.runs_completed
        assert marin.registry.archived.count == 1
