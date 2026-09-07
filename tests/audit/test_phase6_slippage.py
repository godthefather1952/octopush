"""H13, H14, H22 — the slippage bound, the latency model, and numeric safety.

H13: WHERE THE BOUND ACTUALLY COMES FROM
========================================
Nothing in the fill path compares a realised slippage against
``plan.max_slippage_bps``. The bound is enforced *upstream and indirectly*, by
``VenueRouter.route``::

    budget = max_slippage_bps / 10_000
    limit = touch * (1 + budget) if BUY else touch * (1 - budget)

and then by ``FillSimulator.fill_marketable``, which filters the book to levels
at or better than ``limit_price`` before walking it. So a router-built order is
bounded because its limit price encodes the budget — not because anything
checks the outcome.

That distinction matters: a plan whose ``limit_price`` was set by anything
other than the router carries no bound at all, and a MARKET order
(``limit_price is None``) skips the filter entirely.

H14: LATENCY, POSSIBLY TWICE
============================
``poll`` will not act on an order until ``now_ms >= pending.ack_at``, where
``ack_at = submitted_at + venue.latency_ms``. Then ``_attempt_fill`` passes the
*same* ``latency_ms`` into ``latency_adjusted_levels``, which moves the book
adversely by ``latency_drift_bps_per_100ms * latency/100``.

So the order both waits out the latency — during which the real book has
already moved by whatever the market did — and then has a synthetic adverse
drift applied on top. Whether that is a double count or a deliberate stress
model is a design question; the tests below isolate the two effects so the
answer is evidence rather than argument.
"""

from __future__ import annotations

import inspect

import pytest

from core.models.common import Liquidity, OrderType, Side, TimeInForce
from core.models.execution import FillEvent, PaperOrder
from core.models.opportunity import ExecutionPlan, PlannedOrder
from execution.costs import walk_book
from tests.audit.veska_fixtures import (
    T0,
    VENUE_A,
    build_harness,
    deterministic_settings,
    execution_plan,
    market_state,
    planned_order,
    price_levels,
    venue_state,
)

#: A tiny tolerance for float arithmetic. Anything larger would let a real
#: breach hide inside the epsilon.
EPS_BPS = 1e-6


def _latency(harness, venue: str = VENUE_A) -> int:
    return harness.settings.venue(venue).latency_ms


class TestTheBoundIsUpstream:
    """Static evidence for where the limit is, and is not."""

    def test_no_fill_path_compares_against_max_slippage_bps(self):
        from execution.paper import executor as executor_module
        from execution.paper import simulator as simulator_module

        for module in (executor_module, simulator_module):
            source = inspect.getsource(module)
            assert "max_slippage_bps" not in source, (
                f"{module.__name__} now reads max_slippage_bps; the bound may "
                "have moved into the fill path"
            )

    def test_the_router_encodes_the_budget_in_the_limit_price(self):
        from execution.router import VenueRouter

        source = inspect.getsource(VenueRouter.route)
        assert "budget = max_slippage_bps / 10_000" in source
        assert "touch * (1 + budget)" in source

    def test_the_marketable_path_filters_by_limit_price(self):
        from execution.paper.simulator import FillSimulator

        source = inspect.getsource(FillSimulator.fill_marketable)
        assert "order.limit_price is not None" in source
        assert "level.price <= order.limit_price" in source

    def test_a_market_order_has_no_price_filter_at_all(self):
        """Reachability note: the router never emits one, but ``execute`` would."""
        from execution.paper.simulator import FillSimulator

        source = inspect.getsource(FillSimulator.fill_marketable)
        guard = source[: source.index("if not levels:")]
        assert "OrderType.LIMIT" in guard, (
            "the level filter is no longer conditional on LIMIT; a MARKET "
            "order's exposure has changed"
        )


class TestRealisedSlippageIsBounded:
    """H13 — a taker fill must never beat the plan's budget."""

    @pytest.mark.parametrize("side", [Side.BUY, Side.SELL])
    @pytest.mark.parametrize(
        "book",
        [
            "single_level",
            "multi_level",
            "partial_depth",
            "deep_and_dear",
        ],
    )
    async def test_no_taker_fill_exceeds_the_plans_slippage_budget(
        self, side: Side, book: str
    ):
        """Swept across sides and book shapes.

        The order is built through the router so the limit price carries the
        budget, which is the production path. A breach here would mean the
        encoded limit does not in fact bound the realised price.
        """
        harness = build_harness()
        budget = 50.0
        touch = 100.0

        shapes = {
            "single_level": ([(touch, 100.0)], [(touch, 100.0)]),
            "multi_level": (
                [(touch, 1.0), (touch * 0.999, 1.0), (touch * 0.99, 100.0)],
                [(touch, 1.0), (touch * 1.001, 1.0), (touch * 1.01, 100.0)],
            ),
            "partial_depth": ([(touch, 0.05)], [(touch, 0.05)]),
            "deep_and_dear": (
                [(touch, 0.05), (touch * 0.5, 1_000.0)],
                [(touch, 0.05), (touch * 2.0, 1_000.0)],
            ),
        }
        bids_spec, asks_spec = shapes[book]
        harness.update_market(
            market_state(
                venue_state(
                    venue=VENUE_A,
                    bids=price_levels(*bids_spec),
                    asks=price_levels(*asks_spec),
                )
            )
        )

        limit = (
            touch * (1 + budget / 10_000)
            if side is Side.BUY
            else touch * (1 - budget / 10_000)
        )
        plan = execution_plan(
            planned_order(
                side=side,
                quantity=10.0,
                time_in_force=TimeInForce.IOC,
                limit_price=limit,
                expected_price=touch,
            ),
            created_at=T0,
            max_slippage_bps=budget,
        )
        await harness.veska.execute(plan, T0)
        fills = await harness.veska.poll(T0 + _latency(harness))

        for fill in fills:
            assert fill.slippage_bps <= budget + EPS_BPS, (
                f"{side.value} on a {book} book realised "
                f"{fill.slippage_bps:.4f} bps against a budget of {budget}"
            )

    async def test_insufficient_depth_fills_partially_rather_than_beyond_the_limit(
        self,
    ):
        """Depth runs out inside the budget: partial, never a worse price."""
        harness = build_harness()
        touch = 100.0
        budget = 10.0
        limit = touch * (1 + budget / 10_000)
        harness.update_market(
            market_state(
                venue_state(
                    venue=VENUE_A,
                    bids=price_levels((99.0, 100.0)),
                    # 0.1 inside the budget, then a wall far outside it.
                    asks=price_levels((touch, 0.1), (touch * 1.5, 1_000.0)),
                )
            )
        )
        plan = execution_plan(
            planned_order(
                quantity=5.0,
                time_in_force=TimeInForce.IOC,
                limit_price=limit,
                expected_price=touch,
            ),
            created_at=T0,
            max_slippage_bps=budget,
        )
        await harness.veska.execute(plan, T0)
        fills = await harness.veska.poll(T0 + _latency(harness))

        order = harness.orders_of(plan.plan_id)[0]
        assert order.filled_quantity < 5.0
        for fill in fills:
            assert fill.price <= limit + 1e-9, (
                f"filled at {fill.price} through a limit of {limit}"
            )
            assert fill.slippage_bps <= budget + EPS_BPS

    async def test_vanishing_liquidity_does_not_produce_a_worse_price(self):
        """With the vanish probability at its shipped default, sweep it.

        ``apply_vanishing_liquidity`` drops whole levels. Dropping the touch
        would leave a worse level as the best available — which must still be
        refused by the limit filter.
        """
        settings = deterministic_settings(
            execution={"liquidity_vanish_probability": 0.5}
        )
        touch = 100.0
        budget = 5.0
        limit = touch * (1 + budget / 10_000)
        for seed in range(12):
            harness = build_harness(settings=settings, seed=seed)
            harness.update_market(
                market_state(
                    venue_state(
                        venue=VENUE_A,
                        bids=price_levels((99.0, 100.0)),
                        asks=price_levels(
                            (touch, 5.0), (touch * 1.02, 5.0), (touch * 1.05, 5.0)
                        ),
                    )
                )
            )
            plan = execution_plan(
                planned_order(
                    quantity=5.0,
                    time_in_force=TimeInForce.IOC,
                    limit_price=limit,
                    expected_price=touch,
                ),
                created_at=T0,
                max_slippage_bps=budget,
                plan_id=f"plan-vanish-{seed}",
            )
            await harness.veska.execute(plan, T0)
            fills = await harness.veska.poll(T0 + _latency(harness))
            for fill in fills:
                assert fill.slippage_bps <= budget + EPS_BPS, (
                    f"seed {seed}: {fill.slippage_bps:.4f} bps against {budget}"
                )

    async def test_a_fill_while_cancel_pending_is_still_bounded(self):
        """The cancel-race path reaches ``_attempt_fill`` too."""
        harness = build_harness()
        touch = 100.0
        budget = 20.0
        limit = touch * (1 + budget / 10_000)
        harness.update_market(
            market_state(
                venue_state(
                    venue=VENUE_A,
                    bids=price_levels((99.0, 100.0)),
                    asks=price_levels((touch, 100.0)),
                )
            )
        )
        plan = execution_plan(
            planned_order(
                quantity=1.0,
                time_in_force=TimeInForce.GTC,
                limit_price=limit,
                expected_price=touch,
                ttl_ms=600_000,
            ),
            created_at=T0,
            max_slippage_bps=budget,
        )
        await harness.veska.execute(plan, T0)
        latency = _latency(harness)
        await harness.veska.poll(T0 + latency)
        order = harness.orders_of(plan.plan_id)[0]
        await harness.veska.cancel(order.client_order_id, T0 + latency + 1)

        cancel_latency = harness.settings.venue(VENUE_A).cancel_latency_ms
        fills = await harness.veska.poll(T0 + latency + cancel_latency)

        for fill in fills:
            assert fill.slippage_bps <= budget + EPS_BPS


class TestWalkBookArithmetic:
    """The primitive every taker fill is built on."""

    def test_a_buyer_walking_up_records_positive_slippage(self):
        result = walk_book(
            price_levels((100.0, 1.0), (101.0, 1.0)), notional=150.0, side=Side.BUY
        )
        assert result.average_price > 100.0
        assert result.slippage_bps > 0

    def test_a_seller_walking_down_records_positive_slippage(self):
        result = walk_book(
            price_levels((100.0, 1.0), (99.0, 1.0)), notional=150.0, side=Side.SELL
        )
        assert result.average_price < 100.0
        assert result.slippage_bps > 0

    def test_an_empty_book_fills_nothing_and_reports_exhausted(self):
        result = walk_book([], notional=100.0, side=Side.BUY)
        assert result.filled_quantity == 0.0
        assert result.exhausted

    def test_zero_notional_fills_nothing(self):
        result = walk_book(
            price_levels((100.0, 1.0)), notional=0.0, side=Side.BUY
        )
        assert result.filled_quantity == 0.0

    def test_walking_the_whole_book_reports_exhausted(self):
        result = walk_book(
            price_levels((100.0, 0.1)), notional=1_000_000.0, side=Side.BUY
        )
        assert result.exhausted
        assert result.filled_quantity == pytest.approx(0.1)


class TestLatencyModel:
    """H14 — is the latency counted once or twice?"""

    def test_the_wait_and_the_drift_share_one_latency_value(self):
        """Static evidence: both come from ``_latency(order.venue)``."""
        from execution.paper.executor import PaperExecutor

        submit = inspect.getsource(PaperExecutor.submit)
        attempt = inspect.getsource(PaperExecutor._attempt_fill)
        assert "ack_at=now + self._latency(order.venue)" in submit
        assert "latency = float(self._latency(order.venue))" in attempt
        assert "latency," in attempt

    def test_the_drift_is_applied_to_the_book_at_arrival(self):
        """The book handed to the simulator is already the post-wait one.

        ``poll`` runs at ``now_ms >= ack_at``, and ``_book_view`` reads
        ``self.market`` — whatever the caller last supplied. So the drift is
        applied on top of however far the market actually moved during the
        wait.
        """
        from execution.paper.executor import PaperExecutor

        source = inspect.getsource(PaperExecutor.poll)
        assert "if now_ms < pending.ack_at:" in source
        assert "view = self._book_view(order)" in source

    async def test_a_static_book_still_produces_adverse_slippage(self):
        """Isolation A: the market did not move, and the fill is still worse.

        With a static book the only source of an adverse price is the
        synthetic drift. This measures it.
        """
        harness = build_harness()
        touch = 100.0
        harness.update_market(
            market_state(
                venue_state(
                    venue=VENUE_A,
                    bids=price_levels((99.0, 100.0)),
                    asks=price_levels((touch, 100.0)),
                )
            )
        )
        plan = execution_plan(
            planned_order(
                quantity=1.0,
                time_in_force=TimeInForce.IOC,
                limit_price=touch * 1.05,
                expected_price=touch,
            ),
            created_at=T0,
            max_slippage_bps=500.0,
        )
        await harness.veska.execute(plan, T0)
        fills = await harness.veska.poll(T0 + _latency(harness))

        assert fills, "no fill; the comparison below is untested"
        drift_bps = (
            harness.settings.execution.latency_drift_bps_per_100ms
            * _latency(harness)
            / 100.0
        )
        assert fills[0].slippage_bps == pytest.approx(drift_bps, abs=1e-6), (
            "on a book that did not move, the realised slippage is "
            f"{fills[0].slippage_bps:.4f} bps, which is exactly the "
            f"{drift_bps:.4f} bps of synthetic latency drift"
        )

    async def test_an_already_moved_book_adds_the_drift_on_top(self):
        """Isolation B: the market moved, and the drift is applied again.

        This is the double-count question stated as a measurement. If the
        drift were compensating for a wait during which the book is assumed
        static, applying it to a book that has already moved counts the same
        latency twice.
        """
        harness = build_harness()
        touch = 100.0
        latency = _latency(harness)
        drift_bps = (
            harness.settings.execution.latency_drift_bps_per_100ms * latency / 100.0
        )
        moved_touch = touch * (1 + drift_bps / 10_000)

        harness.update_market(
            market_state(
                venue_state(
                    venue=VENUE_A,
                    bids=price_levels((99.0, 100.0)),
                    asks=price_levels((moved_touch, 100.0)),
                    as_of=T0 + latency,
                ),
                created_at=T0 + latency,
            )
        )
        plan = execution_plan(
            planned_order(
                quantity=1.0,
                time_in_force=TimeInForce.IOC,
                limit_price=touch * 1.05,
                expected_price=touch,
            ),
            created_at=T0,
            max_slippage_bps=500.0,
        )
        await harness.veska.execute(plan, T0)
        fills = await harness.veska.poll(T0 + latency)

        assert fills, "no fill; the comparison below is untested"
        realised = fills[0].slippage_bps
        assert realised == pytest.approx(drift_bps, abs=1e-6), (
            "the book had already moved by the full latency drift "
            f"({drift_bps:.4f} bps) and the fill realised {realised:.4f} bps: "
            "the same latency was charged twice"
        )


class TestNumericSafety:
    """H22 — malformed execution objects must fail rather than trade."""

    @pytest.mark.parametrize("bad", [0.0, -1.0, float("nan"), float("inf")])
    def test_a_planned_order_rejects_a_non_positive_or_non_finite_quantity(
        self, bad: float
    ):
        with pytest.raises(Exception):
            PlannedOrder(
                venue=VENUE_A,
                symbol="BTC-USD",
                side=Side.BUY,
                quantity=bad,
                order_type=OrderType.LIMIT,
                time_in_force=TimeInForce.IOC,
                expected_price=100.0,
                expected_fee_bps=6.0,
            )

    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
    def test_a_planned_order_rejects_a_non_finite_expected_price(self, bad: float):
        with pytest.raises(Exception):
            PlannedOrder(
                venue=VENUE_A,
                symbol="BTC-USD",
                side=Side.BUY,
                quantity=1.0,
                order_type=OrderType.LIMIT,
                time_in_force=TimeInForce.IOC,
                expected_price=bad,
                expected_fee_bps=6.0,
            )

    @pytest.mark.parametrize("bad", [float("nan"), float("inf")])
    def test_a_planned_order_rejects_a_non_finite_limit_price(self, bad: float):
        with pytest.raises(Exception):
            PlannedOrder(
                venue=VENUE_A,
                symbol="BTC-USD",
                side=Side.BUY,
                quantity=1.0,
                order_type=OrderType.LIMIT,
                time_in_force=TimeInForce.IOC,
                limit_price=bad,
                expected_price=100.0,
                expected_fee_bps=6.0,
            )

    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
    def test_an_execution_plan_rejects_a_non_finite_notional(self, bad: float):
        with pytest.raises(Exception):
            ExecutionPlan(
                created_at=T0,
                intent_id="i",
                strategy="s",
                symbol="BTC-USD",
                orders=[planned_order()],
                deadline_ms=T0 + 1_000,
                max_slippage_bps=10.0,
                notional=bad,
            )

    @pytest.mark.parametrize("bad", [float("nan"), float("inf")])
    def test_an_execution_plan_rejects_a_non_finite_slippage_budget(
        self, bad: float
    ):
        with pytest.raises(Exception):
            ExecutionPlan(
                created_at=T0,
                intent_id="i",
                strategy="s",
                symbol="BTC-USD",
                orders=[planned_order()],
                deadline_ms=T0 + 1_000,
                max_slippage_bps=bad,
                notional=100.0,
            )

    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
    def test_a_fill_event_rejects_a_non_finite_fee(self, bad: float):
        with pytest.raises(Exception):
            FillEvent(
                created_at=T0,
                client_order_id="o",
                venue=VENUE_A,
                symbol="BTC-USD",
                side=Side.BUY,
                quantity=1.0,
                price=100.0,
                fee=bad,
                liquidity=Liquidity.TAKER,
            )

    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
    def test_a_fill_event_rejects_a_non_finite_slippage(self, bad: float):
        with pytest.raises(Exception):
            FillEvent(
                created_at=T0,
                client_order_id="o",
                venue=VENUE_A,
                symbol="BTC-USD",
                side=Side.BUY,
                quantity=1.0,
                price=100.0,
                fee=0.0,
                liquidity=Liquidity.TAKER,
                slippage_bps=bad,
            )

    @pytest.mark.parametrize("bad", [0.0, -1.0])
    def test_a_paper_order_rejects_a_non_positive_quantity(self, bad: float):
        with pytest.raises(Exception):
            PaperOrder(
                created_at=T0,
                venue=VENUE_A,
                symbol="BTC-USD",
                side=Side.BUY,
                quantity=bad,
                order_type=OrderType.LIMIT,
                time_in_force=TimeInForce.IOC,
                expected_price=100.0,
            )

    def test_a_planned_order_rejects_a_negative_ttl(self):
        """A negative TTL would set ``expires_at`` before ``created_at``."""
        with pytest.raises(Exception):
            PlannedOrder(
                venue=VENUE_A,
                symbol="BTC-USD",
                side=Side.BUY,
                quantity=1.0,
                order_type=OrderType.LIMIT,
                time_in_force=TimeInForce.IOC,
                expected_price=100.0,
                expected_fee_bps=6.0,
                ttl_ms=-1,
            )
