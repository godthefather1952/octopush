"""Phase 9 audit: lifecycle counters and outcome/status consistency."""

from __future__ import annotations

from agents.okapi.registry import HedgeRegistry
from core.models.hedging import HedgeOutcomeSummary, HedgeRequestStatus
from tests.audit.phase9_fixtures import make_intent
from tests.conftest import START_MS


def test_terminal_counter_advances_once_per_transition():
    registry = HedgeRegistry()
    record = registry.register_request(make_intent(), START_MS)

    registry.set_status(record.hedge_id, HedgeRequestStatus.COMPLETE, START_MS + 1)
    registry.set_status(record.hedge_id, HedgeRequestStatus.COMPLETE, START_MS + 2)

    assert registry.hedges_completed == 1


def test_unknown_counter_advances_once_per_transition():
    registry = HedgeRegistry()
    record = registry.register_request(make_intent(), START_MS)

    registry.set_status(record.hedge_id, HedgeRequestStatus.UNKNOWN, START_MS + 1)
    registry.set_status(record.hedge_id, HedgeRequestStatus.UNKNOWN, START_MS + 2)

    assert registry.hedges_unknown == 1


def test_outcome_for_unknown_hedge_does_not_make_it_terminal():
    registry = HedgeRegistry()
    record = registry.register_request(make_intent(), START_MS)
    registry.set_status(record.hedge_id, HedgeRequestStatus.UNKNOWN, START_MS + 1)

    registry.record_outcome(
        HedgeOutcomeSummary(
            hedge_id=record.hedge_id,
            requested_notional=1_000.0,
            filled_notional=100.0,
            completed_at=START_MS + 2,
        )
    )

    assert registry.get(record.hedge_id).status is HedgeRequestStatus.UNKNOWN
    assert registry.get(record.hedge_id).is_outstanding is True


def test_lifetime_metrics_survive_compaction():
    registry = HedgeRegistry()
    record = registry.register_request(make_intent(), START_MS)
    registry.set_status(record.hedge_id, HedgeRequestStatus.COMPLETE, START_MS + 1)
    before = registry.metrics()

    released = registry.compact(keep_terminal=False)
    after = registry.metrics()

    assert released == 1
    assert after.hedges_proposed == before.hedges_proposed
    assert after.hedges_completed == before.hedges_completed
    assert after.requested_notional == before.requested_notional
