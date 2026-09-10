"""Phase 9 audit: optional hedge-store seam must remain observational."""

from __future__ import annotations

from agents.okapi.registry import HedgeRegistry
from core.models.hedging import HedgeRequestStatus
from tests.audit.phase9_fixtures import CapturingHedgeStore, make_intent
from tests.conftest import START_MS


def test_store_receives_linkage_updates():
    store = CapturingHedgeStore()
    registry = HedgeRegistry(store=store)
    record = registry.register_request(make_intent(), START_MS)

    registry.attach_trade_intent(record.hedge_id, "trade-1", START_MS + 1)
    registry.attach_execution_plan(record.hedge_id, "plan-1", START_MS + 2)
    registry.attach_orders(record.hedge_id, ["order-1", "order-2"], START_MS + 3)

    assert store.writes[-1].trade_intent_id == "trade-1"
    assert store.writes[-1].execution_plan_ids == ["plan-1"]
    assert store.writes[-1].order_ids == ["order-1", "order-2"]


def test_store_write_snapshot_does_not_alias_later_registry_mutation():
    store = CapturingHedgeStore()
    registry = HedgeRegistry(store=store)
    record = registry.register_request(make_intent(), START_MS)
    first_write = store.writes[0]

    registry.set_status(record.hedge_id, HedgeRequestStatus.WORKING, START_MS + 1)

    assert first_write.status is HedgeRequestStatus.PROPOSED
    assert store.writes[-1].status is HedgeRequestStatus.WORKING
