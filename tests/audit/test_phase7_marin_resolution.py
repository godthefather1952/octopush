"""Phase 7 audit — resolution authority and UNKNOWN safety."""

from __future__ import annotations

import pytest

from agents.marin import Marin
from agents.marin.policy import is_resolvable_automatically, suggested_action
from core.models.common import TimeInForce
from core.models.execution import OrderStatus
from core.models.ops import MismatchKind, Severity
from core.models.reconciliation import (
    DiscrepancyEntityType,
    ReconciliationDiscrepancy,
    ReconciliationSourceKind,
    ResolutionStatus,
)
from tests.audit.marin_fixtures import (
    T0,
    ResolutionVeska,
    build_marin,
    make_unknown,
)
from tests.audit.veska_fixtures import T0 as V0
from tests.audit.veska_fixtures import (
    build_harness,
    execution_plan,
    market_state,
    planned_order,
    price_levels,
    venue_state,
)


def discrepancy() -> ReconciliationDiscrepancy:
    return ReconciliationDiscrepancy(
        discrepancy_id="d-order",
        run_id="run-order",
        kind=MismatchKind.ORDER_STATE_MISMATCH,
        severity=Severity.CRITICAL,
        entity_type=DiscrepancyEntityType.ORDER,
        entity_id="o-unknown",
        source_a=ReconciliationSourceKind.EXECUTION,
        source_b=ReconciliationSourceKind.ACCOUNT,
        first_seen_at=T0,
        last_seen_at=T0,
    )


class TestConservativePolicy:
    def test_nothing_is_automatically_resolvable(self):
        assert is_resolvable_automatically(discrepancy()) is False

    def test_suggested_action_is_pure(self):
        d = discrepancy()
        before = d.model_dump()
        action = suggested_action(d)
        assert action is not None
        assert d.model_dump() == before


class TestResolutionAuthority:
    async def test_internal_account_source_cannot_authorize_unknown_resolution(
        self, bus, clock, health
    ):
        """H7-43: internal truth must not be promoted to authoritative evidence."""
        marin = build_marin(bus=bus, clock=clock, health=health)
        order = make_unknown(marin)
        veska = ResolutionVeska(accepted=True)

        result = await marin.apply_order_resolution(
            veska,
            order.client_order_id,
            OrderStatus.CANCELLED,
            T0 + 1,
            source=ReconciliationSourceKind.ACCOUNT,
            evidence={"account": "internal opinion"},
        )

        assert result.accepted is False
        assert veska.calls == []
        resolution = next(iter(marin.registry.resolutions.values()))
        assert resolution.status is ResolutionStatus.REJECTED

    async def test_authoritative_source_requires_explicit_evidence(
        self, bus, clock, health
    ):
        marin = build_marin(bus=bus, clock=clock, health=health)
        order = make_unknown(marin)
        veska = ResolutionVeska(accepted=True)

        result = await marin.apply_order_resolution(
            veska,
            order.client_order_id,
            OrderStatus.CANCELLED,
            T0 + 1,
            source=ReconciliationSourceKind.OPERATOR,
        )

        assert result.accepted is False
        assert veska.calls == []
        resolution = next(iter(marin.registry.resolutions.values()))
        assert resolution.status is ResolutionStatus.REJECTED

    async def test_resolution_exception_becomes_terminal_failed_record(
        self, bus, clock, health
    ):
        """H7-44: an exception must not strand a resolution in APPLYING."""
        marin = build_marin(bus=bus, clock=clock, health=health)
        order = make_unknown(marin)
        veska = ResolutionVeska(explode=True)

        with pytest.raises(RuntimeError, match="audit veska resolution failure"):
            await marin.apply_order_resolution(
                veska,
                order.client_order_id,
                OrderStatus.CANCELLED,
                T0 + 1,
                source=ReconciliationSourceKind.OPERATOR,
                evidence={"ticket": "operator-confirmed"},
            )

        resolutions = list(marin.registry.resolutions.values())
        assert len(resolutions) == 1
        assert resolutions[0].status is ResolutionStatus.FAILED
        assert any("RuntimeError" in note for note in resolutions[0].notes)

    async def test_normal_rejection_is_recorded_failed(self, bus, clock, health):
        marin = build_marin(bus=bus, clock=clock, health=health)
        order = make_unknown(marin)
        veska = ResolutionVeska(accepted=False)

        result = await marin.apply_order_resolution(
            veska,
            order.client_order_id,
            OrderStatus.CANCELLED,
            T0 + 1,
            source=ReconciliationSourceKind.OPERATOR,
            evidence={"ticket": "operator-confirmed"},
        )

        assert result.accepted is False
        resolution = next(iter(marin.registry.resolutions.values()))
        assert resolution.status is ResolutionStatus.FAILED


class TestStatusOnlyFilledResolution:
    async def test_filled_resolution_requires_fill_economics(self):
        """H7-45: FILLED cannot be established by status-only evidence."""
        harness = build_harness()
        harness.update_market(
            market_state(
                venue_state(
                    bids=price_levels((90.0, 10.0)),
                    asks=price_levels((110.0, 10.0)),
                )
            )
        )
        plan = execution_plan(
            planned_order(
                time_in_force=TimeInForce.GTC,
                limit_price=95.0,
                client_order_id="p7-unknown",
            ),
            created_at=V0,
        )
        await harness.veska.execute(plan, V0)
        harness.executor.inject_timeout("p7-unknown")
        await harness.veska.poll(
            V0 + harness.settings.venue("VENUE_A").latency_ms
        )

        marin = Marin(
            bus=harness.bus,
            clock=harness.clock,
            health=harness.health,
            oms=harness.oms,
            account=harness.account,
        )
        result = await marin.apply_order_resolution(
            harness.veska,
            "p7-unknown",
            OrderStatus.FILLED,
            V0 + 100,
            source=ReconciliationSourceKind.OPERATOR,
            evidence={"statement": "venue says filled"},
        )
        order = harness.oms.get("p7-unknown")
        assert order is not None

        assert result.accepted is False
        assert order.status is OrderStatus.UNKNOWN
        assert order.filled_quantity == 0.0
        resolution = next(iter(marin.registry.resolutions.values()))
        assert resolution.status is ResolutionStatus.REJECTED
        assert any("fill economics" in note.lower() for note in resolution.notes)
