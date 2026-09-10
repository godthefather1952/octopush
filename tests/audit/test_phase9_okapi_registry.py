"""Phase 9 audit: hedge/target registry integrity, persistence and retention."""

from __future__ import annotations

from agents.okapi.registry import HedgeRegistry
from agents.okapi.targets import HedgeTargetRegistry
from core.models.common import Side
from core.models.hedging import HedgeOutcomeSummary, HedgeRequestStatus
from tests.audit.phase9_fixtures import (
    CapturingHedgeStore,
    ExplodingHedgeStore,
    build_okapi,
    make_intent,
    make_market,
    make_portfolio,
)
from tests.conftest import START_MS
from core.models.common import DataQuality


def test_register_request_is_idempotent_by_hedge_intent_id():
    registry = HedgeRegistry()
    intent = make_intent()

    first = registry.register_request(intent, START_MS)
    second = registry.register_request(intent, START_MS + 10)

    assert second.hedge_id == first.hedge_id
    assert registry.resident_hedges == 1
    assert registry.hedges_proposed == 1
    assert registry.residuals_detected == 1
    assert registry.requested_notional_total == intent.notional


def test_optional_store_failure_cannot_block_okapi_build_hedges(
    bus, clock, settings, health
):
    """A mirror/persistence backend must never become hedge control flow."""
    cfg = settings.model_copy(update={"hedge_tolerance_notional": 100.0})
    okapi = build_okapi(
        bus=bus,
        clock=clock,
        settings=cfg,
        health=health,
        store=ExplodingHedgeStore(),
    )
    okapi.set_desired_delta("BTC-USD", 0.0)
    market = make_market(
        ("VENUE_A", "BTC-USD", 100.0, START_MS, DataQuality.FRESH),
        ("VENUE_B", "BTC-USD", 101.0, START_MS, DataQuality.FRESH),
    )

    intents = okapi.build_hedges(
        make_portfolio(("VENUE_A", "BTC-USD", 10.0, 100.0)), market, START_MS
    )

    assert len(intents) == 1
    assert intents[0].side is Side.SELL


def test_optional_store_failure_cannot_block_registry_lifecycle():
    registry = HedgeRegistry()
    record = registry.register_request(make_intent(), START_MS)
    registry.store = ExplodingHedgeStore()

    changed = registry.set_status(
        record.hedge_id, HedgeRequestStatus.SUBMITTING, START_MS + 1
    )

    assert changed is not None
    assert changed.status is HedgeRequestStatus.SUBMITTING


def test_successful_store_gets_lifecycle_write_through():
    store = CapturingHedgeStore()
    registry = HedgeRegistry(store=store)
    record = registry.register_request(make_intent(), START_MS)
    registry.set_status(record.hedge_id, HedgeRequestStatus.SUBMITTING, START_MS + 1)

    assert len(store.writes) == 2
    assert store.writes[0].status is HedgeRequestStatus.PROPOSED
    assert store.writes[1].status is HedgeRequestStatus.SUBMITTING


def test_outcome_accounting_is_idempotent_by_hedge_id():
    registry = HedgeRegistry()
    record = registry.register_request(make_intent(), START_MS)
    outcome = HedgeOutcomeSummary(
        hedge_id=record.hedge_id,
        requested_notional=1_000.0,
        filled_notional=800.0,
        completed_at=START_MS + 10,
    )

    registry.record_outcome(outcome)
    registry.record_outcome(outcome)

    assert registry.completed_notional_total == 800.0
    assert registry.outcome(record.hedge_id) is not None


def test_replacing_outcome_adjusts_lifetime_notional_instead_of_double_counting():
    registry = HedgeRegistry()
    record = registry.register_request(make_intent(), START_MS)
    registry.record_outcome(
        HedgeOutcomeSummary(hedge_id=record.hedge_id, filled_notional=500.0)
    )
    registry.record_outcome(
        HedgeOutcomeSummary(hedge_id=record.hedge_id, filled_notional=700.0)
    )

    assert registry.completed_notional_total == 700.0
    assert registry.outcome(record.hedge_id).filled_notional == 700.0


def test_hedge_registry_read_cannot_mutate_resident_history():
    registry = HedgeRegistry()
    record = registry.register_request(make_intent(), START_MS)

    observed = registry.get(record.hedge_id)
    assert observed is not None
    observed.status = HedgeRequestStatus.COMPLETE
    observed.reason_codes.append("MUTATED_BY_READER")

    held = registry.hedges[record.hedge_id]
    assert held.status is HedgeRequestStatus.PROPOSED
    assert "MUTATED_BY_READER" not in held.reason_codes


def test_target_registry_read_cannot_mutate_resident_mirror():
    registry = HedgeTargetRegistry()
    target = registry.set_target("BTC-USD", 0.0, START_MS)

    observed = registry.get_target("BTC-USD")
    assert observed is not None
    observed.target_notional = 999_999.0
    observed.reason_codes.append("MUTATED_BY_READER")

    held = registry._targets["BTC-USD"]
    assert held.target_id == target.target_id
    assert held.target_notional == 0.0
    assert "MUTATED_BY_READER" not in held.reason_codes


def test_reapplying_same_status_does_not_double_count():
    registry = HedgeRegistry()
    record = registry.register_request(make_intent(), START_MS)

    registry.set_status(record.hedge_id, HedgeRequestStatus.COMPLETE, START_MS + 1)
    first_terminal = record.terminal_at
    registry.set_status(record.hedge_id, HedgeRequestStatus.COMPLETE, START_MS + 2)

    assert registry.hedges_completed == 1
    assert record.terminal_at == first_terminal


def test_compaction_preserves_unknown_active_and_reconciliation_linked_records():
    registry = HedgeRegistry()
    unknown = registry.register_request(make_intent(hedge_id="hdg-unknown"), START_MS)
    active = registry.register_request(make_intent(hedge_id="hdg-active"), START_MS)
    linked = registry.register_request(make_intent(hedge_id="hdg-linked"), START_MS)
    terminal = registry.register_request(make_intent(hedge_id="hdg-terminal"), START_MS)

    registry.set_status(unknown.hedge_id, HedgeRequestStatus.UNKNOWN, START_MS + 1)
    registry.set_status(active.hedge_id, HedgeRequestStatus.WORKING, START_MS + 1)
    registry.set_status(linked.hedge_id, HedgeRequestStatus.COMPLETE, START_MS + 1)
    registry.link_reconciliation_run(linked.hedge_id, "rec-1", START_MS + 2)
    registry.set_status(terminal.hedge_id, HedgeRequestStatus.COMPLETE, START_MS + 1)

    released = registry.compact(keep_terminal=False)

    assert released == 1
    assert registry.get(unknown.hedge_id) is not None
    assert registry.get(active.hedge_id) is not None
    assert registry.get(linked.hedge_id) is not None
    assert registry.get(terminal.hedge_id) is None
