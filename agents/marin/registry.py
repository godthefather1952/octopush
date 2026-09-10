"""The reconciliation registry — runs, discrepancies and resolutions.

WHAT THIS IS
============
Durable-shaped memory for the reconciliation workflow: which runs happened, what
they disagreed about, and what anyone proposed doing about it. It is what lets
"is this the same cash discrepancy we saw an hour ago?" have an answer.

WHAT THIS IS NOT
================
It takes no safety action. It does not call the kill switch, mutate the ledger,
resolve an order, query a venue, or decide that a disagreement is acceptable.
Nothing in the platform consults it to decide whether trading may continue.

Every mutation takes ``now_ms`` explicitly. No clock is read here, for the same
reason none is read in the executor or the execution registry: a record stamped
from a clock read is a record replay cannot reconstruct (P2-14).

RETENTION
=========
``compact`` exists and releases nothing by default. Runs may one day be
archived to :class:`ArchivedReconciliationRuns` aggregates the way orders are;
an OPEN discrepancy, an unresolved resolution and any run still holding one are
never eligible, whatever the arguments say. Discarding an unresolved
disagreement is how a platform decides a problem stopped existing because it
stopped looking.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from core.models.common import Millis
from core.models.ops import Mismatch, Severity
from core.models.reconciliation import (
    ArchivedReconciliationRuns,
    DiscrepancyStatus,
    ReconciliationDiscrepancy,
    ReconciliationMetrics,
    ReconciliationResolution,
    ReconciliationRunRecord,
    ReconciliationRunStatus,
    ReconciliationSourceKind,
    ReconciliationTrigger,
    ResolutionAction,
    ResolutionStatus,
    discrepancy_from_mismatch,
)

log = logging.getLogger(__name__)


class ReconciliationStore(ABC):
    """The persistence seam, so MARIN is not tied to memory forever.

    **No implementation exists, and none is built here.** An in-memory registry
    is sufficient for a paper session, and adding a SQLite or Postgres backend
    now would be inventing a schema before anyone knows which queries matter.

    The seam is real rather than decorative: :class:`ReconciliationRegistry`
    accepts one and writes through when present, so a later pass adds a class
    rather than restructuring the registry. Reads still come from memory —
    making the registry a cache with an invalidation story is a design decision
    that belongs with whoever implements the backend.
    """

    @abstractmethod
    def put_run(self, record: ReconciliationRunRecord) -> None: ...

    @abstractmethod
    def put_discrepancy(self, discrepancy: ReconciliationDiscrepancy) -> None: ...

    @abstractmethod
    def put_resolution(self, resolution: ReconciliationResolution) -> None: ...

    @abstractmethod
    def get_run(self, run_id: str) -> ReconciliationRunRecord | None: ...

    @abstractmethod
    def open_discrepancies(self) -> list[ReconciliationDiscrepancy]: ...


@dataclass
class ReconciliationRegistry:
    """Runs, discrepancies and resolutions, in memory."""

    runs: dict[str, ReconciliationRunRecord] = field(default_factory=dict)
    discrepancies: dict[str, ReconciliationDiscrepancy] = field(default_factory=dict)
    resolutions: dict[str, ReconciliationResolution] = field(default_factory=dict)

    #: Optional write-through persistence. See :class:`ReconciliationStore`.
    store: ReconciliationStore | None = None

    archived: ArchivedReconciliationRuns = field(
        default_factory=ArchivedReconciliationRuns
    )

    #: Lifetime counters, unaffected by any future compaction.
    runs_started: int = 0
    runs_completed: int = 0
    clean_runs: int = 0
    runs_with_discrepancies: int = 0
    discrepancies_resolved: int = 0
    critical_seen: int = 0
    warnings_seen: int = 0
    resolution_requests: int = 0
    resolution_applied: int = 0
    resolution_failed: int = 0
    snapshots_captured: int = 0
    unknown_orders_seen: int = 0

    # -- runs --------------------------------------------------------------

    def register_run(
        self,
        now_ms: Millis,
        *,
        trigger: ReconciliationTrigger = ReconciliationTrigger.PERIODIC,
        reason: str = "",
        run_id: str | None = None,
    ) -> ReconciliationRunRecord:
        """Open a run record. Idempotent by ``run_id`` when one is supplied.

        Re-registering an existing id returns the record already held rather
        than replacing it. A run id names one reconciliation, and starting a
        second under the same name would discard the discrepancies the first
        one attached.
        """
        if run_id is not None and run_id in self.runs:
            return self.runs[run_id]

        fields = {
            "created_at": now_ms,
            "updated_at": now_ms,
            "trigger": trigger,
            "reason": reason,
        }
        if run_id is not None:
            fields["run_id"] = run_id
        record = ReconciliationRunRecord(**fields)
        self.runs[record.run_id] = record
        self.runs_started += 1
        self._persist_run(record)
        return record

    def set_run_status(
        self,
        run_id: str,
        status: ReconciliationRunStatus,
        now_ms: Millis,
        *,
        note: str = "",
    ) -> ReconciliationRunRecord | None:
        record = self.runs.get(run_id)
        if record is None:
            log.warning("set_run_status for unregistered run %s", run_id)
            return None

        previous = record.status
        record.status = status
        record.updated_at = now_ms
        if status in (
            ReconciliationRunStatus.CLEAN,
            ReconciliationRunStatus.RESOLVED,
            ReconciliationRunStatus.FAILED,
        ):
            record.completed_at = now_ms
        if note:
            record.notes = [*record.notes, note]

        if previous is not status:
            if status is ReconciliationRunStatus.CLEAN:
                self.clean_runs += 1
                self.runs_completed += 1
            elif status is ReconciliationRunStatus.DISCREPANCIES_FOUND:
                self.runs_with_discrepancies += 1
            elif status in (
                ReconciliationRunStatus.RESOLVED,
                ReconciliationRunStatus.FAILED,
            ):
                self.runs_completed += 1
        self._persist_run(record)
        return record

    def attach_snapshot(
        self,
        run_id: str,
        now_ms: Millis,
        *,
        snapshot_id: str | None = None,
        execution_snapshot_id: str | None = None,
        account_snapshot_id: str | None = None,
        venue_snapshot_ids: list[str] | None = None,
        venue_snapshot_id: str | None = None,
        recorded_snapshot_id: str | None = None,
        source_kinds: list[ReconciliationSourceKind] | None = None,
        missing_source_kinds: list[ReconciliationSourceKind] | None = None,
    ) -> ReconciliationRunRecord | None:
        """Attach captured truth identities without collapsing venue history.

        ``venue_snapshot_ids`` is canonical. ``venue_snapshot_id`` remains a
        compatibility input for older single-venue callers and is mirrored into
        the canonical list rather than replacing it.
        """
        record = self.runs.get(run_id)
        if record is None:
            return None
        if snapshot_id is not None:
            record.snapshot_id = snapshot_id
        if execution_snapshot_id is not None:
            record.execution_snapshot_id = execution_snapshot_id
        if account_snapshot_id is not None:
            record.account_snapshot_id = account_snapshot_id
        if venue_snapshot_ids is not None:
            record.venue_snapshot_ids = list(venue_snapshot_ids)
            record.venue_snapshot_id = (
                record.venue_snapshot_ids[0] if record.venue_snapshot_ids else None
            )
        elif venue_snapshot_id is not None:
            record.venue_snapshot_ids = [venue_snapshot_id]
            record.venue_snapshot_id = venue_snapshot_id
        if recorded_snapshot_id is not None:
            record.recorded_snapshot_id = recorded_snapshot_id
        if source_kinds is not None:
            record.source_kinds = list(source_kinds)
        if missing_source_kinds is not None:
            record.missing_source_kinds = list(missing_source_kinds)
        record.updated_at = now_ms
        self.snapshots_captured += 1
        self._persist_run(record)
        return record

    def note_run(self, run_id: str, text: str, now_ms: Millis) -> None:
        record = self.runs.get(run_id)
        if record is None:
            return
        record.notes = [*record.notes, text]
        record.updated_at = now_ms
        self._persist_run(record)

    # -- discrepancies -----------------------------------------------------

    def add_discrepancy(
        self, discrepancy: ReconciliationDiscrepancy, now_ms: Millis
    ) -> ReconciliationDiscrepancy:
        """Record a disagreement, or update the one already tracking it.

        Identity comes from
        :func:`~core.models.reconciliation.discrepancy_identity`, so the same
        disagreement seen on a later run increments ``occurrences`` and moves
        ``last_seen_at`` rather than creating a second record. A registry that
        appended every sighting would report a persistent problem as hundreds
        of separate ones and make "how long has this been wrong?" unanswerable.

        A discrepancy that had been marked RESOLVED and is then seen again is
        reopened: something concluded it was fixed, and it was not.
        """
        existing = self.discrepancies.get(discrepancy.discrepancy_id)
        if existing is None:
            self.discrepancies[discrepancy.discrepancy_id] = discrepancy
            self._count_severity(discrepancy.severity)
            self._attach_to_run(discrepancy, now_ms)
            self._persist_discrepancy(discrepancy)
            return discrepancy

        existing.last_seen_at = now_ms
        existing.occurrences += 1
        existing.expected = discrepancy.expected
        existing.actual = discrepancy.actual
        existing.difference = discrepancy.difference
        existing.detail = discrepancy.detail or existing.detail
        if existing.status is DiscrepancyStatus.RESOLVED:
            existing.status = DiscrepancyStatus.OPEN
        self._count_severity(discrepancy.severity)
        self._attach_to_run(discrepancy, now_ms, target=existing)
        self._persist_discrepancy(existing)
        return existing

    def add_mismatch(
        self,
        mismatch: Mismatch,
        *,
        run_id: str,
        now_ms: Millis,
        source_a: ReconciliationSourceKind = ReconciliationSourceKind.EXECUTION,
        source_b: ReconciliationSourceKind = ReconciliationSourceKind.ACCOUNT,
    ) -> ReconciliationDiscrepancy:
        """Track a measurement produced by the existing algorithm.

        The adapter is pure and carries kind, severity, key and numbers across
        untouched — no comparison logic moves here, and no severity is
        reinterpreted.
        """
        return self.add_discrepancy(
            discrepancy_from_mismatch(
                mismatch,
                run_id=run_id,
                now_ms=now_ms,
                source_a=source_a,
                source_b=source_b,
            ),
            now_ms,
        )

    def touch_discrepancy(
        self, discrepancy_id: str, now_ms: Millis
    ) -> ReconciliationDiscrepancy | None:
        """Record that an already-tracked disagreement was seen again."""
        existing = self.discrepancies.get(discrepancy_id)
        if existing is None:
            return None
        existing.last_seen_at = now_ms
        existing.occurrences += 1
        self._persist_discrepancy(existing)
        return existing

    def set_discrepancy_status(
        self,
        discrepancy_id: str,
        status: DiscrepancyStatus,
        now_ms: Millis,
        *,
        note: str = "",
    ) -> ReconciliationDiscrepancy | None:
        """Move a discrepancy through its workflow.

        Only an explicit caller reaches this. Nothing in the platform
        acknowledges, resolves or ignores a disagreement on its own.
        """
        existing = self.discrepancies.get(discrepancy_id)
        if existing is None:
            log.warning("set_discrepancy_status for unknown %s", discrepancy_id)
            return None
        previous = existing.status
        existing.status = status
        existing.last_seen_at = now_ms
        if note:
            existing.evidence = {**existing.evidence, "note": note}
        if (
            status is DiscrepancyStatus.RESOLVED
            and previous is not DiscrepancyStatus.RESOLVED
        ):
            self.discrepancies_resolved += 1
        self._persist_discrepancy(existing)
        return existing

    # -- resolutions -------------------------------------------------------

    def register_resolution(
        self, resolution: ReconciliationResolution, now_ms: Millis
    ) -> ReconciliationResolution:
        """Record a proposed response. Proposing does nothing on its own."""
        self.resolutions[resolution.resolution_id] = resolution
        self.resolution_requests += 1
        discrepancy = self.discrepancies.get(resolution.discrepancy_id)
        if discrepancy is not None:
            discrepancy.resolution_ids = [
                *discrepancy.resolution_ids,
                resolution.resolution_id,
            ]
            discrepancy.last_seen_at = now_ms
            self._persist_discrepancy(discrepancy)
        if resolution.run_id is not None:
            record = self.runs.get(resolution.run_id)
            if record is not None:
                record.resolution_ids = [
                    *record.resolution_ids,
                    resolution.resolution_id,
                ]
                record.updated_at = now_ms
                self._persist_run(record)
        self._persist_resolution(resolution)
        return resolution

    def set_resolution_status(
        self,
        resolution_id: str,
        status: ResolutionStatus,
        now_ms: Millis,
        *,
        note: str = "",
    ) -> ReconciliationResolution | None:
        resolution = self.resolutions.get(resolution_id)
        if resolution is None:
            log.warning("set_resolution_status for unknown %s", resolution_id)
            return None
        previous = resolution.status
        resolution.status = status
        resolution.updated_at = now_ms
        if status is ResolutionStatus.APPLIED:
            resolution.applied_at = now_ms
        if note:
            resolution.notes = [*resolution.notes, note]
        if previous is not status:
            if status is ResolutionStatus.APPLIED:
                self.resolution_applied += 1
            elif status is ResolutionStatus.FAILED:
                self.resolution_failed += 1
        self._persist_resolution(resolution)
        return resolution

    # -- queries -----------------------------------------------------------

    def get_run(self, run_id: str) -> ReconciliationRunRecord | None:
        return self.runs.get(run_id)

    def all_runs(self) -> list[ReconciliationRunRecord]:
        return list(self.runs.values())

    def active_runs(self) -> list[ReconciliationRunRecord]:
        return [r for r in self.runs.values() if not r.is_terminal]

    def latest_run(self) -> ReconciliationRunRecord | None:
        if not self.runs:
            return None
        return max(self.runs.values(), key=lambda r: r.created_at)

    def get_discrepancy(
        self, discrepancy_id: str
    ) -> ReconciliationDiscrepancy | None:
        return self.discrepancies.get(discrepancy_id)

    def all_discrepancies(self) -> list[ReconciliationDiscrepancy]:
        return list(self.discrepancies.values())

    def open_discrepancies(self) -> list[ReconciliationDiscrepancy]:
        return [d for d in self.discrepancies.values() if d.is_open]

    def open_critical(self) -> list[ReconciliationDiscrepancy]:
        return [
            d
            for d in self.discrepancies.values()
            if d.is_open and d.severity is Severity.CRITICAL
        ]

    def open_warnings(self) -> list[ReconciliationDiscrepancy]:
        return [
            d
            for d in self.discrepancies.values()
            if d.is_open and d.severity is Severity.WARNING
        ]

    def discrepancies_for_run(self, run_id: str) -> list[ReconciliationDiscrepancy]:
        record = self.runs.get(run_id)
        if record is None:
            return []
        found = [self.discrepancies.get(d) for d in record.discrepancy_ids]
        return [d for d in found if d is not None]

    def discrepancies_for_entity(
        self, entity_id: str
    ) -> list[ReconciliationDiscrepancy]:
        return [d for d in self.discrepancies.values() if d.entity_id == entity_id]

    def get_resolution(self, resolution_id: str) -> ReconciliationResolution | None:
        return self.resolutions.get(resolution_id)

    def resolutions_for_discrepancy(
        self, discrepancy_id: str
    ) -> list[ReconciliationResolution]:
        return [
            r
            for r in self.resolutions.values()
            if r.discrepancy_id == discrepancy_id
        ]

    def pending_resolutions(self) -> list[ReconciliationResolution]:
        return [r for r in self.resolutions.values() if not r.is_terminal]

    def metrics(self) -> ReconciliationMetrics:
        return ReconciliationMetrics(
            runs_started=self.runs_started,
            runs_completed=self.runs_completed,
            clean_runs=self.clean_runs,
            runs_with_discrepancies=self.runs_with_discrepancies,
            discrepancies_open=len(self.open_discrepancies()),
            discrepancies_resolved=self.discrepancies_resolved,
            critical_seen=self.critical_seen,
            warnings_seen=self.warnings_seen,
            unknown_orders_seen=self.unknown_orders_seen,
            resolution_requests=self.resolution_requests,
            resolution_applied=self.resolution_applied,
            resolution_failed=self.resolution_failed,
            snapshots_captured=self.snapshots_captured,
        )

    # -- retention ---------------------------------------------------------

    def compact(self, *, keep_terminal: bool = True) -> int:
        """Release finished runs. Conservative by construction.

        With ``keep_terminal`` at its default nothing is released — the
        framework supplies the hook and the safety rule and leaves the policy
        to a later pass that has measured what retention costs.

        Even when asked to release, a run holding an OPEN discrepancy or a
        pending resolution is never eligible, and no discrepancy or resolution
        is ever deleted. Dropping an unresolved disagreement is how a platform
        decides a problem stopped existing because it stopped looking.

        Returns the number of run records released.
        """
        if keep_terminal:
            return 0
        doomed = [
            record
            for record in self.runs.values()
            if record.is_terminal and self._run_is_settled(record)
        ]
        for record in doomed:
            self.archived.absorb(record)
            del self.runs[record.run_id]
        return len(doomed)

    def _run_is_settled(self, record: ReconciliationRunRecord) -> bool:
        for discrepancy_id in record.discrepancy_ids:
            discrepancy = self.discrepancies.get(discrepancy_id)
            if discrepancy is not None and discrepancy.is_open:
                return False
        for resolution_id in record.resolution_ids:
            resolution = self.resolutions.get(resolution_id)
            if resolution is not None and not resolution.is_terminal:
                return False
        return True

    @property
    def resident_runs(self) -> int:
        return len(self.runs)

    # -- internals ---------------------------------------------------------

    def _count_severity(self, severity: Severity) -> None:
        if severity is Severity.CRITICAL:
            self.critical_seen += 1
        elif severity is Severity.WARNING:
            self.warnings_seen += 1

    def _attach_to_run(
        self,
        discrepancy: ReconciliationDiscrepancy,
        now_ms: Millis,
        *,
        target: ReconciliationDiscrepancy | None = None,
    ) -> None:
        tracked = target if target is not None else discrepancy
        record = self.runs.get(discrepancy.run_id)
        if record is None:
            return
        if tracked.discrepancy_id not in record.discrepancy_ids:
            record.discrepancy_ids = [
                *record.discrepancy_ids,
                tracked.discrepancy_id,
            ]
        if tracked.severity is Severity.CRITICAL:
            record.critical_count += 1
        elif tracked.severity is Severity.WARNING:
            record.warning_count += 1
        else:
            record.info_count += 1
        record.updated_at = now_ms
        self._persist_run(record)

    def _persist_run(self, record: ReconciliationRunRecord) -> None:
        if self.store is not None:
            self.store.put_run(record)

    def _persist_discrepancy(self, discrepancy: ReconciliationDiscrepancy) -> None:
        if self.store is not None:
            self.store.put_discrepancy(discrepancy)

    def _persist_resolution(self, resolution: ReconciliationResolution) -> None:
        if self.store is not None:
            self.store.put_resolution(resolution)


__all__ = [
    "ReconciliationRegistry",
    "ReconciliationStore",
    "ResolutionAction",
    "ResolutionStatus",
]
