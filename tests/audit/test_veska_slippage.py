"""Phase 6 — H13, H14: the slippage budget, and how latency is charged.

**H13.** ``VenueRouter.route``'s docstring makes a hard claim: an aggressive
leg crosses "but always with a limit, so the realised price can never be worse
than the modelled one by more than the configured slippage budget". The router
sets ``expected_price = touch`` and ``limit = touch * (1 ± budget)``, and
``FillSimulator.fill_marketable`` filters the book to levels at or better than
the limit. If both hold, signed slippage is capped by construction. This file
proves it rather than assuming it, and looks for the shapes where it would not
hold: a MARKET order (no limit to filter on), and a plan whose limit and
expected price were not derived from one another.

**H14.** The same venue latency is used twice. ``submit`` sets
``ack_at = now + latency``, so an order cannot fill until that much market
evolution has already happened; then ``fill_marketable`` calls
``latency_adjusted_levels(..., latency)`` and drifts the arrival book
adversely by that same latency again.

That is a question, not an accusation. In a static market the wait costs
nothing and the drift is the only charge, which is exactly what the simulator
documents. In a moving market both apply. The tests below measure each case so
the report can classify rather than assert.
"""

from __future__ import annotations

import inspect

import pytest

from core.models.common import Liquidity, OrderType, Side, TimeInForce
from execution.paper.simulator import FillSimulator
from tests.audit.veska_fixtures import (
    VENUE_A_LATENCY_MS,
    audit_settings,
    deterministic_execution,
    one_venue_market,
    plan,
    planned,
    rig,
    signed_slippage_bps,
)
from tests.conftest import START_MS

#: No vanishing liquidity and no drift: slippage is then purely the book walk.
CLEAN = audit_settings(**deterministic_execution())
#: Shipped drift, everything else pinned: for the H14 comparison.
DRIFTING = audit_settings(**deterministic_execution(latency_drift_bps_per_100ms=0.4))

ACK_AT = START_MS + VENUE_A_LATENCY_MS
BUDGET_BPS = 10.0


def budgeted(side: Side, touch: float, *, quantity: float = 0.10, **overrides):
    """An order shaped exactly as the router builds one."""
    budget = BUDGET_BPS / 10_000
    limit = touch * (1 + budget) if side is Side.BUY else touch * (1 - budget)
    fields = {
        "side": side,
        "quantity": quantity,
        "order_type": OrderType.LIMIT,
        "time_in_force": TimeInForce.IOC,
        "expected_price": touch,
        "limit_price": limit,
    }
    fields.update(overrides)
    return planned(**fields)


class TestTheRouterBuildsTheCapIn:
    """The premise H13 rests on."""

    def test_the_limit_is_the_expected_price_plus_the_budget(self):
        from execution.router import VenueRouter

        source = inspect.getsource(VenueRouter.route)
        assert "budget = max_slippage_bps / 10_000" in source
        assert "touch * (1 + budget)" in source and "touch * (1 - budget)" in source
        assert "expected_price=touch" in source

    def test_the_router_never_emits_a_market_order(self):
        from execution.router import VenueRouter

        source = inspect.getsource(VenueRouter.route)
        assert "OrderType.MARKET" not in source

    def test_the_simulator_filters_the_book_to_the_limit(self):
        source = inspect.getsource(FillSimulator.fill_marketable)
        assert "level.price <= order.limit_price" in source
        assert "level.price >= order.limit_price" in source


class TestSlippageIsCapped:
    """H13. Every taker fill, on every shape of book."""

    @pytest.mark.parametrize("side", [Side.BUY, Side.SELL])
    @pytest.mark.parametrize("levels", [1, 2, 8])
    @pytest.mark.parametrize("size", [0.01, 0.05, 5.0])
    async def test_a_router_shaped_order_never_exceeds_its_budget(
        self, side, levels, size
    ):
        """Single level, a deep walk, and a book too thin to fill."""
        built = rig(
            settings=CLEAN,
            market_state=one_venue_market(levels=levels, size=size),
        )
        state = built.executor.market.venue_state("VENUE_A", "BTC-USD")
        touch = state.metrics.best_ask if side is Side.BUY else state.metrics.best_bid

        await built.executor.submit(
            plan(budgeted(side, touch, quantity=1.0)), START_MS
        )
        fills = await built.executor.poll(ACK_AT)

        for fill in fills:
            observed = signed_slippage_bps(fill, touch)
            assert observed <= BUDGET_BPS + 1e-6, (
                f"a {side.value} fill at {fill.price} against an expected "
                f"{touch} is {observed:.4f} bps of slippage, past the "
                f"{BUDGET_BPS} bps budget the limit was built from"
            )

    @pytest.mark.parametrize("side", [Side.BUY, Side.SELL])
    async def test_the_reported_slippage_matches_the_realised_price(self, side):
        """``FillEvent.slippage_bps`` must not be a separate story."""
        built = rig(settings=CLEAN, market_state=one_venue_market())
        state = built.executor.market.venue_state("VENUE_A", "BTC-USD")
        touch = state.metrics.best_ask if side is Side.BUY else state.metrics.best_bid

        await built.executor.submit(plan(budgeted(side, touch)), START_MS)
        fills = await built.executor.poll(ACK_AT)

        assert fills
        for fill in fills:
            assert fill.slippage_bps == pytest.approx(
                signed_slippage_bps(fill, touch), abs=1e-6
            )
            assert fill.slippage_bps <= BUDGET_BPS + 1e-6

    async def test_insufficient_depth_inside_the_limit_partials_rather_than_overpays(
        self,
    ):
        """The only honest response to a thin book."""
        built = rig(
            settings=CLEAN,
            market_state=one_venue_market(levels=1, size=0.01, spread=2.0),
        )
        state = built.executor.market.venue_state("VENUE_A", "BTC-USD")
        touch = state.metrics.best_ask

        await built.executor.submit(
            plan(budgeted(Side.BUY, touch, quantity=1.0)), START_MS
        )
        fills = await built.executor.poll(ACK_AT)

        order = built.only_order()
        assert order.filled_quantity < order.quantity
        for fill in fills:
            assert fill.price <= touch * (1 + BUDGET_BPS / 10_000) + 1e-9

    async def test_a_cancel_pending_fill_is_still_capped(self):
        """The one path that fills from a non-OPEN status."""
        built = rig(settings=CLEAN, market_state=one_venue_market())
        state = built.executor.market.venue_state("VENUE_A", "BTC-USD")
        touch = state.metrics.best_ask
        await built.executor.submit(
            plan(budgeted(Side.BUY, touch, limit_price=touch - 500.0)), START_MS
        )
        order = built.only_order()
        await built.executor.poll(ACK_AT)
        await built.executor.cancel(order.client_order_id, ACK_AT)

        built.executor.update_market(one_venue_market(mid=39_000.0))
        fills = await built.executor.poll(ACK_AT + 1_000)

        for fill in fills:
            assert fill.price <= order.limit_price + 1e-9

    async def test_a_market_order_has_no_cap_at_all(self):
        """The shape the router never emits, and the reason it does not.

        A MARKET order skips the limit filter entirely, so it walks whatever
        book it finds. Recorded as a boundary property of the executor rather
        than as a defect in the shipped path.
        """
        built = rig(
            settings=CLEAN,
            market_state=one_venue_market(levels=8, size=0.02, tick=50.0),
        )
        await built.executor.submit(
            plan(
                planned(
                    order_type=OrderType.MARKET,
                    time_in_force=TimeInForce.IOC,
                    quantity=1.0,
                    expected_price=40_001.0,
                    limit_price=None,
                )
            ),
            START_MS,
        )
        fills = await built.executor.poll(ACK_AT)
        worst = max((f.slippage_bps for f in fills), default=0.0)
        assert worst >= 0.0
        assert all(f.liquidity is Liquidity.TAKER for f in fills)


class TestLatencyIsChargedOnce:
    """H14. Measurement, then classification — no verdict asserted here."""

    def test_the_same_latency_reaches_both_the_arrival_and_the_book(self):
        """The premise, from the source."""
        from execution.paper.executor import PaperExecutor

        submit_source = inspect.getsource(PaperExecutor.submit)
        fill_source = inspect.getsource(PaperExecutor._attempt_fill)
        assert "ack_at=now + self._latency(order.venue)" in submit_source
        assert "latency = float(self._latency(order.venue))" in fill_source
        assert "latency," in fill_source

    def test_the_drift_is_applied_to_whatever_book_it_is_given(self):
        source = inspect.getsource(FillSimulator.latency_adjusted_levels)
        assert "latency_drift_bps_per_100ms" in source
        assert "latency_ms / 100.0" in source

    async def test_case_a_a_static_market_charges_the_drift_once(self):
        """The documented interpretation: the book has not moved, so the only
        adverse effect is the synthetic drift. Nothing is double-counted."""
        static = one_venue_market()
        built = rig(settings=DRIFTING, market_state=static)
        touch = static.venue_state("VENUE_A", "BTC-USD").metrics.best_ask

        await built.executor.submit(plan(budgeted(Side.BUY, touch)), START_MS)
        fills = await built.executor.poll(ACK_AT)

        assert fills
        drifted = touch * (
            1 + DRIFTING.execution.latency_drift_bps_per_100ms
            * (VENUE_A_LATENCY_MS / 100.0)
            / 10_000
        )
        assert fills[0].price == pytest.approx(drifted, rel=1e-9), (
            "in a static market the realised price should be the touch moved "
            f"by exactly one latency's drift ({drifted}), got {fills[0].price}"
        )

    async def test_case_b_a_market_that_already_moved_is_charged_again(self):
        """The measurement that decides the classification.

        Between submission and arrival the market moves adversely by exactly
        the modelled drift — the elapsed evolution the wait to ``ack_at``
        represents. The simulator then applies the same drift on top.
        """
        drift_fraction = (
            DRIFTING.execution.latency_drift_bps_per_100ms
            * (VENUE_A_LATENCY_MS / 100.0)
            / 10_000
        )
        start = one_venue_market()
        touch = start.venue_state("VENUE_A", "BTC-USD").metrics.best_ask
        built = rig(settings=DRIFTING, market_state=start)
        await built.executor.submit(plan(budgeted(Side.BUY, touch)), START_MS)

        # The market has already moved adversely by one latency's worth.
        moved_mid = 40_000.0 * (1 + drift_fraction)
        built.executor.update_market(one_venue_market(mid=moved_mid))
        fills = await built.executor.poll(ACK_AT)

        assert fills
        charged_once = touch * (1 + drift_fraction)
        charged_twice = touch * (1 + drift_fraction) ** 2
        assert fills[0].price == pytest.approx(charged_once, rel=1e-6), (
            "the arrival book had already moved by one latency's drift; the "
            f"fill landed at {fills[0].price}, against {charged_once} for a "
            f"single charge and {charged_twice} for two"
        )

    async def test_the_double_charge_is_always_adverse_never_favourable(self):
        """The safety direction, whatever the classification.

        Charging latency twice makes paper fills worse than reality, never
        better. It cannot manufacture an unsafe position; it understates P&L
        and suppresses fills.
        """
        for side in (Side.BUY, Side.SELL):
            static = one_venue_market()
            state = static.venue_state("VENUE_A", "BTC-USD")
            touch = (
                state.metrics.best_ask if side is Side.BUY else state.metrics.best_bid
            )
            built = rig(settings=DRIFTING, market_state=static)
            await built.executor.submit(plan(budgeted(side, touch)), START_MS)
            fills = await built.executor.poll(ACK_AT)
            for fill in fills:
                assert signed_slippage_bps(fill, touch) >= -1e-9, (
                    f"the latency model produced a {side.value} fill better "
                    f"than the touch: {fill.price} vs {touch}"
                )

    async def test_zero_configured_drift_removes_the_second_charge_entirely(self):
        """A control, isolating which of the two charges is which."""
        static = one_venue_market()
        touch = static.venue_state("VENUE_A", "BTC-USD").metrics.best_ask
        built = rig(settings=CLEAN, market_state=static)
        await built.executor.submit(plan(budgeted(Side.BUY, touch)), START_MS)
        fills = await built.executor.poll(ACK_AT)
        assert fills and fills[0].price == pytest.approx(touch, rel=1e-9)
