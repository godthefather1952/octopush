"""Phase 6 — H3, H4, H5: does time-in-force mean what it says?

``TimeInForce`` declares GTC, IOC, FOK and POST_ONLY, and the router emits two
of them: IOC for every aggressive entry leg, POST_ONLY for the low-urgency
passive path. All four are accepted by ``PlannedOrder`` and reachable through a
persisted or replayed plan.

``PaperExecutor.poll`` contains no reference to ``TimeInForce`` at all. The one
place the enum is read in the execution path is
``execution.paper.simulator.is_marketable``, which uses it to choose between
``fill_marketable`` and ``fill_passive`` — a routing question, not a lifetime
one. So the questions here are:

* **H3** — does an IOC order get exactly one immediate executable attempt, or
  does it rest until ``ttl_ms`` like a GTC?
* **H4** — can a FOK order end up partially filled?
* **H5** — can a POST_ONLY order take liquidity and still be booked as a maker?

None of these is about whether the simulator is pessimistic enough. A resting
IOC is *optimistic* — it gives the platform fill opportunities a real venue
would never grant — and a post-only that crosses is optimistic twice over,
because it takes at the maker fee.
"""

from __future__ import annotations

import inspect

import pytest

from core.models.common import Liquidity, OrderType, TimeInForce
from core.models.execution import OrderStatus
from execution.paper import simulator as sim
from execution.paper.executor import PaperExecutor
from tests.audit.veska_fixtures import (
    VENUE_A_LATENCY_MS,
    audit_settings,
    deterministic_execution,
    one_venue_market,
    plan,
    planned,
    rig,
)
from tests.conftest import START_MS

CERTAIN = audit_settings(**deterministic_execution())
ACK_AT = START_MS + VENUE_A_LATENCY_MS

#: A book whose ask is far above any limit used below, so nothing is fillable.
UNREACHABLE = 60_000.0
#: A book whose ask is below every limit used below.
REACHABLE = 30_000.0


class TestTheExecutorReadsTimeInForceAtAll:
    """Structural premise for everything below."""

    def test_the_poll_loop_never_inspects_time_in_force(self):
        source = inspect.getsource(PaperExecutor.poll)
        assert "time_in_force" not in source and "TimeInForce" not in source, (
            "poll now reads time-in-force; the lifetime hypotheses below need "
            "rechecking against whatever it does with it"
        )

    def test_the_only_reader_is_the_marketable_helper(self):
        """``is_marketable`` uses TIF to pick a fill model, not a lifetime."""
        source = inspect.getsource(sim.is_marketable)
        assert "TimeInForce.IOC" in source and "TimeInForce.FOK" in source
        assert "expires_at" not in source and "ttl" not in source


class TestIocSemantics:
    """H3. Immediate-or-cancel means one attempt, then terminate."""

    async def test_an_ioc_with_no_reachable_liquidity_does_not_survive(self):
        """A. Nothing executable at arrival: the remainder must terminate."""
        built = rig(
            settings=CERTAIN, market_state=one_venue_market(mid=UNREACHABLE)
        )
        await built.executor.submit(
            plan(planned(time_in_force=TimeInForce.IOC, limit_price=40_100.0)),
            START_MS,
        )
        order = built.only_order()

        await built.executor.poll(ACK_AT)

        assert order.is_terminal, (
            "an IOC order that found no executable liquidity on arrival is "
            f"still {order.status.value}; IOC grants one immediate attempt, "
            "not a working order"
        )

    async def test_an_ioc_remainder_must_not_fill_from_later_liquidity(self):
        """C. The property that actually costs money.

        The order arrives into a book it cannot cross. One tick later the
        market comes to it. A real venue cancelled the remainder on arrival, so
        this fill cannot happen — but it is exactly the fill a resting order
        would take.
        """
        built = rig(
            settings=CERTAIN, market_state=one_venue_market(mid=UNREACHABLE)
        )
        await built.executor.submit(
            plan(planned(time_in_force=TimeInForce.IOC, limit_price=40_100.0)),
            START_MS,
        )
        order = built.only_order()
        await built.executor.poll(ACK_AT)

        built.executor.update_market(one_venue_market(mid=REACHABLE))
        fills = await built.executor.poll(ACK_AT + 100)

        assert not fills, (
            "an IOC order filled from liquidity that appeared 100ms after its "
            f"single permitted attempt: {[(f.quantity, f.price) for f in fills]}"
        )

    async def test_an_ioc_partial_fill_terminates_its_remainder(self):
        """B. Partially filled at arrival, remainder cancelled immediately."""
        # One level of 0.02 against an order for 0.10.
        built = rig(
            settings=CERTAIN,
            market_state=one_venue_market(mid=REACHABLE, levels=1, size=0.02),
        )

        await built.executor.submit(
            plan(
                planned(
                    time_in_force=TimeInForce.IOC,
                    quantity=0.10,
                    limit_price=40_100.0,
                )
            ),
            START_MS,
        )
        order = built.only_order()

        await built.executor.poll(ACK_AT)

        assert order.filled_quantity == pytest.approx(0.02, abs=1e-6)
        assert order.is_terminal, (
            "a partially-filled IOC left "
            f"{order.remaining_quantity} working in {order.status.value}; the "
            "unfilled remainder of an IOC is cancelled, not worked"
        )

    async def test_an_ioc_does_not_simply_become_a_five_second_gtc(self):
        """D. The specific degradation: IOC lifetime equal to the GTC TTL.

        Measured in logical polls rather than from ``terminal_at``, because
        that field is stamped from the OMS clock rather than from the poll's
        instant (H1) and would answer a different question.
        """
        ttl = 5_000
        built = rig(
            settings=CERTAIN, market_state=one_venue_market(mid=UNREACHABLE)
        )
        await built.executor.submit(
            plan(
                planned(
                    time_in_force=TimeInForce.IOC,
                    limit_price=40_100.0,
                    ttl_ms=ttl,
                )
            ),
            START_MS,
        )
        order = built.only_order()

        alive_at: list[int] = []
        for offset in (0, 1_000, 2_000, 3_000, 4_000, ttl):
            await built.executor.poll(ACK_AT + offset)
            if not order.is_terminal:
                alive_at.append(offset)

        assert alive_at == [], (
            "an IOC order was still working "
            f"{alive_at}ms after its arrival, terminating only at "
            f"{order.status.value}; that is the GTC time-to-live, not an "
            "immediate cancel"
        )

    async def test_a_gtc_order_may_legitimately_rest(self):
        """The control. GTC is the TIF that is supposed to behave this way."""
        built = rig(
            settings=CERTAIN, market_state=one_venue_market(mid=UNREACHABLE)
        )
        await built.executor.submit(
            plan(
                planned(
                    time_in_force=TimeInForce.GTC,
                    order_type=OrderType.LIMIT,
                    limit_price=40_100.0,
                    ttl_ms=5_000,
                )
            ),
            START_MS,
        )
        order = built.only_order()
        await built.executor.poll(ACK_AT)
        assert order.status is OrderStatus.OPEN


class TestFokSemantics:
    """H4. Fill-or-kill: the whole quantity, or nothing at all."""

    def test_fok_is_accepted_by_the_schema_and_routed_as_marketable(self):
        """The premise. FOK is reachable through any persisted plan."""
        assert TimeInForce.FOK in set(TimeInForce)
        source = inspect.getsource(sim.is_marketable)
        assert "TimeInForce.FOK" in source

    async def test_a_fok_order_can_never_be_left_partially_filled(self):
        """Insufficient depth inside the limit: kill, do not partial."""
        built = rig(
            settings=CERTAIN,
            market_state=one_venue_market(mid=REACHABLE, levels=1, size=0.02),
        )

        await built.executor.submit(
            plan(
                planned(
                    time_in_force=TimeInForce.FOK,
                    quantity=0.10,
                    limit_price=40_100.0,
                )
            ),
            START_MS,
        )
        order = built.only_order()

        await built.executor.poll(ACK_AT)

        assert order.filled_quantity in (0.0, pytest.approx(0.10, abs=1e-6)), (
            f"a FOK order filled {order.filled_quantity} of {order.quantity}: "
            "fill-or-kill admits no third outcome"
        )

    async def test_a_capped_partial_fraction_cannot_partial_a_fok(self):
        """``max_partial_fraction`` is applied inside ``fill_marketable``.

        It is a taker-side realism knob, and FOK is routed through the taker
        path, so a configuration that caps partials also caps a FOK — turning
        an all-or-nothing instruction into a guaranteed partial.
        """
        capped = audit_settings(**deterministic_execution(max_partial_fraction=0.10))
        built = rig(settings=capped, market_state=one_venue_market(mid=REACHABLE))

        await built.executor.submit(
            plan(
                planned(
                    time_in_force=TimeInForce.FOK,
                    quantity=0.10,
                    limit_price=40_100.0,
                )
            ),
            START_MS,
        )
        order = built.only_order()

        await built.executor.poll(ACK_AT)

        assert order.filled_quantity in (0.0, pytest.approx(0.10, abs=1e-6)), (
            f"max_partial_fraction produced a {order.filled_quantity} fill on a "
            f"FOK order for {order.quantity}"
        )

    def test_fok_is_either_supported_or_explicitly_refused(self):
        """If FOK is not implemented it should be rejected, not accepted with
        IOC-like semantics. Either answer is defensible; silence is not."""
        import execution.oms as oms_module

        executor_source = inspect.getsource(PaperExecutor)
        oms_source = inspect.getsource(oms_module)
        refused = "FOK" in executor_source or "FOK" in oms_source
        implemented = "FOK" in inspect.getsource(PaperExecutor.poll)
        assert refused or implemented, (
            "FOK is accepted by PlannedOrder and PaperOrder but appears nowhere "
            "in the executor or the OMS: it is neither implemented nor refused"
        )


class TestPostOnlySemantics:
    """H5. A post-only order must never take liquidity."""

    def test_the_router_emits_post_only_on_the_patient_path(self):
        """The premise: this TIF is on the shipped path, not hypothetical."""
        from execution.router import VenueRouter

        source = inspect.getsource(VenueRouter.route)
        assert "TimeInForce.POST_ONLY" in source
        assert "is_maker=True" in source

    def test_a_post_only_order_is_not_treated_as_marketable(self):
        """Which is correct — and is also why it reaches ``fill_passive``."""
        from core.models.execution import PaperOrder
        from core.models.common import Side

        order = PaperOrder(
            created_at=START_MS,
            venue="VENUE_A",
            symbol="BTC-USD",
            side=Side.BUY,
            order_type=OrderType.LIMIT,
            time_in_force=TimeInForce.POST_ONLY,
            quantity=0.10,
            limit_price=40_100.0,
            expected_price=40_000.0,
        )
        assert not sim.is_marketable(order)

    async def test_a_post_only_order_may_rest_while_it_is_passive(self):
        """A. The control: still non-crossing at arrival, so resting is right."""
        built = rig(
            settings=CERTAIN, market_state=one_venue_market(mid=UNREACHABLE)
        )
        await built.executor.submit(
            plan(
                planned(
                    time_in_force=TimeInForce.POST_ONLY,
                    limit_price=40_000.0,
                    expected_price=40_000.0,
                )
            ),
            START_MS,
        )
        order = built.only_order()
        await built.executor.poll(ACK_AT)
        assert order.status is OrderStatus.OPEN and order.filled_quantity == 0.0

    async def test_a_post_only_order_that_crosses_must_not_take(self):
        """B. The finding's core.

        The order was passive when it was sent. By the time it arrives the
        market has moved through its limit. A real venue rejects or reprices
        such an order precisely so it cannot take; it must not simply trade.
        """
        built = rig(
            settings=CERTAIN, market_state=one_venue_market(mid=REACHABLE)
        )
        await built.executor.submit(
            plan(
                planned(
                    time_in_force=TimeInForce.POST_ONLY,
                    # Limit above the ask: the order crosses on arrival.
                    limit_price=40_100.0,
                    expected_price=40_000.0,
                )
            ),
            START_MS,
        )
        order = built.only_order()

        fills = await built.executor.poll(ACK_AT)

        assert not fills, (
            "a POST_ONLY order crossed the spread and traded: "
            f"{[(f.quantity, f.price, f.liquidity.value) for f in fills]} — a "
            "post-only order that would take must be rejected or repriced, "
            "never executed"
        )

    async def test_a_crossing_post_only_fill_is_never_booked_as_maker(self):
        """C. The fee half of the same finding.

        Even granting the fill, pricing it at the posted limit and charging the
        maker fee claims a rebate for liquidity the order removed.
        """
        built = rig(
            settings=CERTAIN, market_state=one_venue_market(mid=REACHABLE)
        )
        await built.executor.submit(
            plan(
                planned(
                    time_in_force=TimeInForce.POST_ONLY,
                    limit_price=40_100.0,
                    expected_price=40_000.0,
                )
            ),
            START_MS,
        )

        fills = await built.executor.poll(ACK_AT)

        maker = [f for f in fills if f.liquidity is Liquidity.MAKER]
        assert not maker, (
            "a post-only order that crossed the spread was booked as MAKER at "
            f"{[(f.price, f.fee) for f in maker]}; the market came through its "
            "limit, so this fill removed liquidity"
        )

    def test_the_passive_model_treats_a_crossed_book_as_a_full_probability_fill(self):
        """Structural evidence for the two assertions above."""
        source = inspect.getsource(sim.FillSimulator.fill_passive)
        assert "crossed" in source
        assert "probability = self.config.maker_fill_probability" in source
        assert "price = order.limit_price" in source
        assert "Liquidity.MAKER" in source
