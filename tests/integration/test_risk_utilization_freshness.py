"""Phase 1 polish: risk utilization must reflect the CURRENT portfolio.

``state.risk_utilization`` used to be set only inside ``_risk_check()`` --
called only while evaluating a *new* trade intent -- so it described the
portfolio as of the last new-trade risk evaluation, not the portfolio that
exists now. A position that closed left stale nonzero exposure on the
dashboard indefinitely, until the next new opportunity happened to trigger
another risk check (which, with no capital at risk, might never happen
again).

``Orchestrator._refresh_risk_utilization`` now runs unconditionally every
tick, right after the portfolio is marked, through the same canonical
``RuneCore.utilization()`` calculation ``_risk_check`` itself uses -- one
formula, two call sites.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from core.models.common import Side
from core.models.execution import FillEvent
from tests.conftest import START_MS

VENUE = "VENUE_A"
SYMBOL = "BTC-USDT"


def open_fill(quantity=1.0, price=100.0, fee=1.0):
    return FillEvent(
        created_at=START_MS,
        client_order_id="order-open",
        venue=VENUE,
        symbol=SYMBOL,
        side=Side.BUY,
        quantity=quantity,
        price=price,
        fee=fee,
    )


def close_fill(quantity=1.0, price=105.0, fee=1.0):
    return FillEvent(
        created_at=START_MS,
        client_order_id="order-close",
        venue=VENUE,
        symbol=SYMBOL,
        side=Side.SELL,
        quantity=quantity,
        price=price,
        fee=fee,
    )


class TestRiskUtilizationReflectsAnOpenPosition:
    async def test_risk_utilization_shows_nonzero_exposure_while_a_position_is_open(
        self, platform
    ):
        await platform.start(record=False)
        platform.account.apply_fill(open_fill())
        await platform.orchestrator.tick()

        util = platform.state.risk_utilization
        assert util is not None
        assert util.gross_exposure > 0
        assert util.gross_exposure == pytest.approx(platform.account.snapshot().gross_exposure)


class TestRiskUtilizationRefreshesWhenThePositionCloses:
    async def test_next_tick_after_a_close_shows_zero_portfolio_exposure(self, platform):
        await platform.start(record=False)
        platform.account.apply_fill(open_fill())
        await platform.orchestrator.tick()
        assert platform.account.snapshot().gross_exposure > 0

        platform.account.apply_fill(close_fill())
        portfolio = platform.account.snapshot()
        assert portfolio.gross_exposure == pytest.approx(0.0)

    async def test_next_tick_also_shows_zero_risk_exposure_with_no_new_trade(self, platform):
        """The refresh must happen from the tick alone -- no new opportunity,
        no new risk evaluation, is triggered here at all.
        """
        await platform.start(record=False)
        platform.account.apply_fill(open_fill())
        await platform.orchestrator.tick()
        assert platform.state.risk_utilization.gross_exposure > 0

        platform.account.apply_fill(close_fill())
        await platform.orchestrator.tick()

        util = platform.state.risk_utilization
        assert util.gross_exposure == pytest.approx(0.0), (
            "a closed position must not leave stale nonzero exposure on the "
            "published risk-utilization snapshot"
        )
        assert util.net_exposure == pytest.approx(0.0)


class TestUnhedgedAmountUpdatesAsPositionChanges:
    async def test_unhedged_notional_tracks_the_live_position(self, platform):
        await platform.start(record=False)
        platform.account.apply_fill(open_fill(quantity=2.0))
        await platform.orchestrator.tick()
        opened = platform.state.risk_utilization.unhedged_notional

        platform.account.apply_fill(close_fill(quantity=1.0))
        await platform.orchestrator.tick()
        half_closed = platform.state.risk_utilization.unhedged_notional

        platform.account.apply_fill(close_fill(quantity=1.0))
        await platform.orchestrator.tick()
        fully_closed = platform.state.risk_utilization.unhedged_notional

        assert opened > half_closed >= 0
        assert fully_closed == pytest.approx(0.0)


class TestDashboardApiReturnsCurrentRiskValues:
    async def test_the_state_endpoint_matches_the_refreshed_utilization(self, platform):
        from apps.api.app import create_app

        await platform.start(record=False)
        platform.account.apply_fill(open_fill())
        await platform.orchestrator.tick()
        platform.account.apply_fill(close_fill())
        await platform.orchestrator.tick()

        with TestClient(create_app(platform)) as client:
            body = client.get("/api/state").json()

        risk = body["risk"]
        util = platform.state.risk_utilization
        assert risk["gross_exposure"] == pytest.approx(util.gross_exposure)
        assert risk["gross_exposure"] == pytest.approx(0.0)


class TestNoRegressionInRuneGates:
    async def test_risk_limits_are_never_breached_over_a_longer_run(self, platform):
        """Unchanged from the existing invariant suite -- the refresh must
        not perturb RUNE's actual gating decisions, only the freshness of
        the published snapshot.
        """
        from tests.conftest import run_platform

        limits = platform.settings.risk
        await run_platform(platform, 300)
        snapshot = platform.account.snapshot()
        assert snapshot.gross_exposure <= limits.max_gross_exposure * 1.001
        assert snapshot.drawdown <= limits.max_drawdown * 1.001
