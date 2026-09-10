"""Phase 7 audit — legacy reconciliation and compaction edges."""

from __future__ import annotations

from core.models.common import MONEY_EPSILON
from core.models.execution import OrderStatus
from core.models.ops import MismatchKind, Severity
from tests.audit.marin_fixtures import T0, build_marin, make_unknown, matched_trade


def kinds(result):
    return {m.kind for m in result.mismatches}


class TestToleranceEdges:
    def test_cash_noise_at_tolerance_does_not_fire(self, bus, clock, health):
        marin = build_marin(bus=bus, clock=clock, health=health)
        matched_trade(marin)
        marin.account.cash += MONEY_EPSILON
        assert marin.reconcile(T0).ok

    def test_cash_noise_above_tolerance_is_critical(self, bus, clock, health):
        marin = build_marin(bus=bus, clock=clock, health=health)
        matched_trade(marin)
        marin.account.cash += MONEY_EPSILON * 2

        result = marin.reconcile(T0)

        assert result.ok is False
        mismatch = next(
            m for m in result.mismatches if m.kind is MismatchKind.CASH_MISMATCH
        )
        assert mismatch.severity is Severity.CRITICAL


class TestHistoricalDetection:
    def test_lifetime_fill_count_detects_loss_after_resident_sets_match(
        self, bus, clock, health
    ):
        marin = build_marin(bus=bus, clock=clock, health=health)
        _, fill = matched_trade(marin)
        marin.account.fills_applied += 1

        result = marin.reconcile(T0)

        assert result.ok is False
        assert MismatchKind.MISSING_FILL in kinds(result)
        assert any(m.key == "fills_applied" for m in result.mismatches)
        assert fill.fill_id in {f.fill_id for f in marin.oms.all_fills()}

    def test_unknown_remains_visible_as_warning(self, bus, clock, health):
        marin = build_marin(bus=bus, clock=clock, health=health)
        order = make_unknown(marin)

        result = marin.reconcile(T0)

        unknown = [
            m
            for m in result.mismatches
            if m.key == order.client_order_id
            and m.kind is MismatchKind.ORDER_STATE_MISMATCH
        ]
        assert unknown
        assert unknown[0].severity is Severity.WARNING
        assert result.ok is True


class TestCompactionSafety:
    async def test_critical_run_never_compacts_fill_history(self, bus, clock, health):
        marin = build_marin(bus=bus, clock=clock, health=health)
        matched_trade(marin)
        marin.account.retained_fills = 0
        marin.account.cash += 100.0
        before_log = list(marin.account.fill_log)
        before_orders = set(marin.oms.orders)

        result = await marin.run(T0)

        assert result.ok is False
        assert marin.account.fill_log == before_log
        assert set(marin.oms.orders) == before_orders

    def test_seal_boundary_does_not_include_live_order(self, bus, clock, health):
        marin = build_marin(bus=bus, clock=clock, health=health)
        order, _ = matched_trade(marin, quantity=1.0)
        order.quantity = 2.0
        order.status = OrderStatus.PARTIALLY_FILLED

        assert marin._seal_boundary() == 0
