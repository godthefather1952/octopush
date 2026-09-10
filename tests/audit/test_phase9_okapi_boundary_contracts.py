"""Phase 9 audit: conservative policy and authority-boundary contracts."""

from __future__ import annotations

from agents.okapi.policy import needs_hedge, side_for_residual
from core.models.common import Side
from core.models.ops import DeltaReport
from tests.conftest import START_MS


def _report(residual: float, *, within: bool) -> DeltaReport:
    return DeltaReport(
        created_at=START_MS,
        symbol="BTC-USD",
        desired_delta=0.0,
        actual_delta=residual,
        unhedged_delta=residual,
        within_tolerance=within,
        tolerance=100.0,
    )


def test_zero_residual_has_no_hedge_side():
    assert side_for_residual(0.0) is None


def test_positive_and_negative_residual_side_helpers_match_agent_contract():
    assert side_for_residual(1.0) is Side.SELL
    assert side_for_residual(-1.0) is Side.BUY


def test_needs_hedge_respects_tolerance_and_zero():
    assert needs_hedge(_report(101.0, within=False)) is True
    assert needs_hedge(_report(100.0, within=True)) is False
    assert needs_hedge(_report(0.0, within=False)) is False
