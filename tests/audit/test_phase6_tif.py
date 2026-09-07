"""H3, H4, H5, H31 — time-in-force semantics, against the platform's own definitions.

THE ORACLE IS IN THE REPOSITORY
===============================
``execution/veska/policy.py`` is not this audit's opinion about what IOC, FOK
and POST_ONLY mean. It is the platform's own canonical statement, written in
Phase 6 and explicitly *not* wired into the fill loop:

    "It is **not** an enforcement layer, and this construction pass
     deliberately does not wire it into ``PaperExecutor``'s fill loop. Whether
     the executor's behaviour matches these definitions is exactly what a later
     validation pass exists to determine."

This is that pass. Every assertion below compares the executor against
``policy``'s own predicates, so a disagreement is the repository contradicting
itself rather than the audit imposing a preference.

WHAT ``is_marketable`` ACTUALLY DOES
====================================
``execution/paper/simulator.py``::

    def is_marketable(order):
        if order.order_type is OrderType.MARKET:
            return True
        return order.time_in_force in (TimeInForce.IOC, TimeInForce.FOK)

Three consequences follow, and each is a hypothesis below:

* **IOC** takes the marketable path but nothing terminates its remainder, so
  it rests until its TTL — the GTC behaviour ``policy.can_rest`` denies it.
* **FOK** takes the same path, which partially fills. ``policy.requires_full_fill``
  says a partial fill is an illegal outcome for FOK, and
  ``PAPER_CAPABILITIES.supports_fok`` is False — yet nothing rejects one.
* **POST_ONLY** takes the *passive* path, which fills at the order's limit
  price with ``Liquidity.MAKER`` even when the book is crossed against it.
  ``policy.must_not_take`` says such an order must never remove liquidity.
"""

from __future__ import annotations

from core.models.common import Liquidity, OrderType, Side, TimeInForce
from core.models.execution import OrderStatus
from execution.paper.executor import PAPER_CAPABILITIES
from execution.paper.simulator import is_marketable
from execution.veska import policy
from execution.veska.preflight import preflight_plan
from tests.audit.veska_fixtures import (
    T0,
    VENUE_A,
    build_harness,
    execution_plan,
    market_state,
    planned_order,
    price_levels,
    venue_state,
)


def _latency(harness, venue: str = VENUE_A) -> int:
    return harness.settings.venue(venue).latency_ms


def _crossed_for_buy(created_at: int = T0):
    """A book whose ask is below our limit: a buy here would take liquidity."""
    return market_state(
        venue_state(
            venue=VENUE_A,
            bids=price_levels((99.0, 10.0)),
            asks=price_levels((100.0, 10.0)),
            as_of=created_at,
        ),
        created_at=created_at,
    )


def _away_for_buy(created_at: int = T0):
    """A book the market has not come to: our bid is well below the ask."""
    return market_state(
        venue_state(
            venue=VENUE_A,
            bids=price_levels((90.0, 10.0)),
            asks=price_levels((110.0, 10.0)),
            as_of=created_at,
        ),
        created_at=created_at,
    )


class TestPolicyIsTheOracle:
    """Pin what the platform says these instructions mean."""

    def test_ioc_and_fok_are_immediate(self):
        assert policy.is_immediate(TimeInForce.IOC)
        assert policy.is_immediate(TimeInForce.FOK)

    def test_ioc_may_not_rest(self):
        assert not policy.can_rest(TimeInForce.IOC)
        assert not policy.can_rest(TimeInForce.FOK)

    def test_gtc_and_post_only_may_rest(self):
        assert policy.can_rest(TimeInForce.GTC)
        assert policy.can_rest(TimeInForce.POST_ONLY)

    def test_fok_forbids_a_partial_fill(self):
        assert policy.requires_full_fill(TimeInForce.FOK)

    def test_post_only_must_not_take(self):
        assert policy.must_not_take(TimeInForce.POST_ONLY)
        assert policy.expects_maker_fee(TimeInForce.POST_ONLY)

    def test_gtc_does_not_guarantee_a_maker_fee(self):
        """Its liquidity flag must come from what happened, not the instruction."""
        assert not policy.expects_maker_fee(TimeInForce.GTC)


class TestMarketabilityClassification:
    """``is_marketable`` decides which fill path an order takes."""

    def test_ioc_and_fok_take_the_marketable_path(self):
        for tif in (TimeInForce.IOC, TimeInForce.FOK):
            order = _order_with(tif)
            assert is_marketable(order)

    def test_post_only_takes_the_passive_path(self):
        assert not is_marketable(_order_with(TimeInForce.POST_ONLY))

    def test_a_crossing_gtc_limit_is_classified_passive(self):
        """The classification ignores price entirely.

        A GTC limit priced through the book is an aggressive order in every
        venue that exists. Here it is routed to ``fill_passive``, which prices
        it at its own limit and stamps it ``Liquidity.MAKER``.
        """
        order = _order_with(TimeInForce.GTC)
        assert not is_marketable(order), (
            "a GTC limit is classified passive regardless of whether its "
            "price crosses; the audit's premise for the maker-fee finding "
            "below has changed"
        )


def _order_with(tif: TimeInForce):
    from core.models.execution import PaperOrder

    return PaperOrder(
        created_at=T0,
        venue=VENUE_A,
        symbol="BTC-USD",
        side=Side.BUY,
        quantity=1.0,
        order_type=OrderType.LIMIT,
        time_in_force=tif,
        expected_price=100.0,
        limit_price=101.0,
    )


class TestIOC:
    """H3 — immediate or cancel."""

    async def test_an_ioc_with_no_liquidity_terminates_on_arrival(self):
        """A. Nothing executable at arrival: the order must not survive."""
        harness = build_harness()
        harness.update_market(_away_for_buy())
        latency = _latency(harness)
        plan = execution_plan(
            planned_order(
                time_in_force=TimeInForce.IOC, limit_price=95.0, ttl_ms=60_000
            ),
            created_at=T0,
        )
        await harness.veska.execute(plan, T0)
        order = harness.orders_of(plan.plan_id)[0]

        await harness.veska.poll(T0 + latency)

        assert order.is_terminal, (
            "an IOC that found no executable liquidity on arrival is still "
            f"{order.status.value}; policy.can_rest(IOC) is "
            f"{policy.can_rest(TimeInForce.IOC)}"
        )

    async def test_an_ioc_remainder_terminates_after_a_partial_fill(self):
        """B. Partial immediate fill: the remainder must not stay working."""
        harness = build_harness()
        harness.update_market(
            market_state(
                venue_state(
                    venue=VENUE_A,
                    bids=price_levels((99.0, 10.0)),
                    # Only 0.2 available at a price our limit accepts.
                    asks=price_levels((100.0, 0.2), (200.0, 100.0)),
                ),
            )
        )
        latency = _latency(harness)
        plan = execution_plan(
            planned_order(
                quantity=5.0,
                time_in_force=TimeInForce.IOC,
                limit_price=100.5,
                ttl_ms=60_000,
            ),
            created_at=T0,
        )
        await harness.veska.execute(plan, T0)
        order = harness.orders_of(plan.plan_id)[0]

        await harness.veska.poll(T0 + latency)

        assert order.filled_quantity < order.quantity, (
            "the audit expected a partial fill and got a full one; the "
            "remainder case below is untested"
        )
        assert order.is_terminal, (
            f"an IOC filled {order.filled_quantity} of {order.quantity} is "
            f"still {order.status.value}: its remainder is resting"
        )

    async def test_an_ioc_does_not_fill_from_liquidity_that_arrives_later(self):
        """C. New liquidity later must not reach an expired instruction."""
        harness = build_harness()
        harness.update_market(_away_for_buy())
        latency = _latency(harness)
        plan = execution_plan(
            planned_order(
                time_in_force=TimeInForce.IOC, limit_price=95.0, ttl_ms=60_000
            ),
            created_at=T0,
        )
        await harness.veska.execute(plan, T0)
        order = harness.orders_of(plan.plan_id)[0]

        await harness.veska.poll(T0 + latency)

        # Liquidity appears well after the instruction's one attempt.
        harness.update_market(
            market_state(
                venue_state(
                    venue=VENUE_A,
                    bids=price_levels((94.0, 10.0)),
                    asks=price_levels((94.5, 10.0)),
                    as_of=T0 + latency + 5_000,
                ),
                created_at=T0 + latency + 5_000,
            )
        )
        fills = await harness.veska.poll(T0 + latency + 5_001)

        assert not fills, (
            f"an IOC submitted at {T0} filled from liquidity that appeared "
            f"{5_000}ms after its single execution attempt: {fills}"
        )
        assert order.filled_quantity == 0.0

    async def test_an_ioc_does_not_behave_as_gtc_until_ttl(self):
        """D. The direct statement of the defect."""
        harness = build_harness()
        harness.update_market(_away_for_buy())
        latency = _latency(harness)
        ttl = 60_000
        plan = execution_plan(
            planned_order(
                time_in_force=TimeInForce.IOC, limit_price=95.0, ttl_ms=ttl
            ),
            created_at=T0,
        )
        await harness.veska.execute(plan, T0)
        order = harness.orders_of(plan.plan_id)[0]

        await harness.veska.poll(T0 + latency)
        mid_life = T0 + latency + ttl // 2
        await harness.veska.poll(mid_life)

        assert order.is_terminal, (
            f"an IOC is still {order.status.value} at {mid_life}, "
            f"{mid_life - T0}ms after submission, because the only thing that "
            f"ends it is its {ttl}ms TTL"
        )


class TestFOK:
    """H4 and H31 — fill or kill, against a capability that says False."""

    def test_the_executor_does_not_claim_to_support_fok(self):
        assert PAPER_CAPABILITIES.supports_fok is False
        assert not PAPER_CAPABILITIES.supports_time_in_force(TimeInForce.FOK)

    def test_preflight_flags_an_fok_plan_as_unsupported(self):
        """The check exists; whether anything consults it is the next test."""
        plan = execution_plan(
            planned_order(time_in_force=TimeInForce.FOK), created_at=T0
        )
        result = preflight_plan(plan, capabilities=PAPER_CAPABILITIES)
        assert result.blocked
        assert "UNSUPPORTED_TIME_IN_FORCE" in result.reason_codes

    async def test_an_unsupported_fok_instruction_is_refused_rather_than_worked(
        self,
    ):
        """A capability of False must mean something at submission time.

        Either the plan is rejected, or the order is, or an exception is
        raised. What must not happen is silent acceptance and execution under
        some other instruction's semantics.
        """
        harness = build_harness()
        harness.update_market(_crossed_for_buy())
        plan = execution_plan(
            planned_order(time_in_force=TimeInForce.FOK, limit_price=101.0),
            created_at=T0,
        )
        report = await harness.veska.execute(plan, T0)
        order = harness.orders_of(plan.plan_id)[0]

        refused = (
            order.status is OrderStatus.REJECTED
            or bool(report.notes)
            or not report.orders
        )
        assert refused, (
            "the executor advertises supports_fok=False and accepted an FOK "
            f"order anyway: status {order.status.value}, notes {report.notes}"
        )

    async def test_an_fok_order_never_fills_partially(self):
        """If it is worked at all, the all-or-nothing rule must hold."""
        harness = build_harness()
        harness.update_market(
            market_state(
                venue_state(
                    venue=VENUE_A,
                    bids=price_levels((99.0, 10.0)),
                    asks=price_levels((100.0, 0.2), (200.0, 100.0)),
                ),
            )
        )
        latency = _latency(harness)
        plan = execution_plan(
            planned_order(
                quantity=5.0,
                time_in_force=TimeInForce.FOK,
                limit_price=100.5,
            ),
            created_at=T0,
        )
        await harness.veska.execute(plan, T0)
        order = harness.orders_of(plan.plan_id)[0]

        await harness.veska.poll(T0 + latency)

        assert order.filled_quantity in (0.0, order.quantity), (
            f"an FOK order filled {order.filled_quantity} of "
            f"{order.quantity}; policy.requires_full_fill(FOK) is "
            f"{policy.requires_full_fill(TimeInForce.FOK)}"
        )


class TestPostOnly:
    """H5 — a post-only order must never remove liquidity."""

    async def test_post_only_does_not_take_when_crossed_at_arrival(self):
        """The book is already through our price when the order lands.

        A real venue rejects or reprices such an order precisely so that it
        cannot take. Here it reaches ``fill_passive``, which sees ``crossed``
        and fills at the full maker probability.
        """
        harness = build_harness()
        # Our buy limit is 100.5; the ask is 100.0, so we would cross.
        harness.update_market(
            market_state(
                venue_state(
                    venue=VENUE_A,
                    bids=price_levels((99.5, 10.0)),
                    asks=price_levels((100.0, 10.0)),
                ),
            )
        )
        latency = _latency(harness)
        plan = execution_plan(
            planned_order(
                side=Side.BUY,
                time_in_force=TimeInForce.POST_ONLY,
                limit_price=100.5,
                expected_price=100.5,
            ),
            created_at=T0,
        )
        await harness.veska.execute(plan, T0)
        order = harness.orders_of(plan.plan_id)[0]

        fills = await harness.veska.poll(T0 + latency)

        taking = [f for f in fills if f.client_order_id == order.client_order_id]
        assert not taking, (
            "a POST_ONLY buy at 100.5 arriving into a 100.0 ask took "
            f"liquidity: {[(f.price, f.liquidity.value) for f in taking]}; "
            f"policy.must_not_take(POST_ONLY) is "
            f"{policy.must_not_take(TimeInForce.POST_ONLY)}"
        )

    async def test_post_only_that_becomes_crossed_later_still_does_not_take(self):
        """Passive at arrival, then the market moves through the price."""
        harness = build_harness()
        harness.update_market(_away_for_buy())
        latency = _latency(harness)
        plan = execution_plan(
            planned_order(
                time_in_force=TimeInForce.POST_ONLY,
                limit_price=100.0,
                expected_price=100.0,
                ttl_ms=60_000,
            ),
            created_at=T0,
        )
        await harness.veska.execute(plan, T0)
        order = harness.orders_of(plan.plan_id)[0]
        await harness.veska.poll(T0 + latency)

        # The market comes through our price.
        harness.update_market(
            market_state(
                venue_state(
                    venue=VENUE_A,
                    bids=price_levels((98.0, 10.0)),
                    asks=price_levels((99.0, 10.0)),
                    as_of=T0 + latency + 100,
                ),
                created_at=T0 + latency + 100,
            )
        )
        fills = await harness.veska.poll(T0 + latency + 101)

        crossing = [
            f
            for f in fills
            if f.client_order_id == order.client_order_id and f.price > 99.0
        ]
        assert not crossing, (
            "a POST_ONLY order the market moved through filled at a price "
            f"that removed liquidity: {[(f.price, f.liquidity.value) for f in crossing]}"
        )

    async def test_a_post_only_fill_that_crossed_is_not_labelled_maker(self):
        """The narrower claim: a taking fill must not claim the maker tier.

        Separated from the previous two because it is the part that reaches the
        ledger. ``FillSimulator.fill_passive`` hard-codes ``Liquidity.MAKER``
        and charges the maker fee whatever the book did, so a crossing fill is
        billed at the wrong tier as well as being illegal.
        """
        harness = build_harness()
        harness.update_market(
            market_state(
                venue_state(
                    venue=VENUE_A,
                    bids=price_levels((99.5, 10.0)),
                    asks=price_levels((100.0, 10.0)),
                ),
            )
        )
        latency = _latency(harness)
        plan = execution_plan(
            planned_order(
                time_in_force=TimeInForce.POST_ONLY,
                limit_price=100.5,
                expected_price=100.5,
            ),
            created_at=T0,
        )
        await harness.veska.execute(plan, T0)
        fills = await harness.veska.poll(T0 + latency)

        for fill in fills:
            if fill.liquidity is Liquidity.MAKER:
                assert fill.price <= 99.5 or fill.price >= 100.0, (
                    "unreachable guard, kept so the assertion below reads "
                    "against a real price"
                )
        maker_fills = [f for f in fills if f.liquidity is Liquidity.MAKER]
        assert not maker_fills, (
            "a POST_ONLY order arriving into a crossed book produced "
            f"{len(maker_fills)} MAKER-labelled fill(s) at "
            f"{[f.price for f in maker_fills]}, against an ask of 100.0"
        )


class TestGTCMakerFee:
    """A crossing GTC limit is priced at the maker tier."""

    async def test_a_crossing_gtc_limit_is_not_billed_as_a_maker(self):
        """Not a TIF violation — a fee-tier one, and it reaches the ledger.

        ``policy.expects_maker_fee(GTC)`` is False precisely because a GTC
        order can do either, and the flag has to come from what happened.
        ``fill_passive`` sets it from the instruction.
        """
        harness = build_harness()
        harness.update_market(
            market_state(
                venue_state(
                    venue=VENUE_A,
                    bids=price_levels((99.5, 10.0)),
                    asks=price_levels((100.0, 10.0)),
                ),
            )
        )
        latency = _latency(harness)
        plan = execution_plan(
            planned_order(
                time_in_force=TimeInForce.GTC,
                limit_price=100.5,
                expected_price=100.5,
            ),
            created_at=T0,
        )
        await harness.veska.execute(plan, T0)
        fills = await harness.veska.poll(T0 + latency)

        maker = [f for f in fills if f.liquidity is Liquidity.MAKER]
        assert not maker, (
            "a GTC limit priced through the book was filled and billed as a "
            f"maker: {[(f.price, f.fee) for f in maker]}"
        )


class TestCapabilityClaims:
    """H31 — every claim the executor makes about itself."""

    def test_paper_capabilities_declares_itself_paper(self):
        assert PAPER_CAPABILITIES.is_paper is True

    def test_market_orders_are_not_claimed(self):
        assert PAPER_CAPABILITIES.supports_market is False
        assert not PAPER_CAPABILITIES.supports_order_type(OrderType.MARKET)

    def test_the_router_never_emits_an_unclaimed_instruction(self):
        """Reachability evidence for the FOK and MARKET findings.

        The router emits exactly two shapes — LIMIT/POST_ONLY when patient and
        LIMIT/IOC when crossing — so an unsupported instruction can only reach
        the executor through a hand-built or replayed plan. That bounds the
        severity without excusing the gap.
        """
        import inspect

        from execution.router import VenueRouter

        source = inspect.getsource(VenueRouter)
        assert "TimeInForce.FOK" not in source
        assert "OrderType.MARKET" not in source
        assert source.count("TimeInForce.POST_ONLY") == 1
        assert source.count("TimeInForce.IOC") == 1

    async def test_a_claimed_capability_is_honoured_end_to_end(self):
        """``supports_cancel`` and ``supports_order_lookup``, exercised."""
        harness = build_harness()
        harness.update_market(_away_for_buy())
        assert PAPER_CAPABILITIES.supports_cancel
        assert PAPER_CAPABILITIES.supports_order_lookup

        plan = execution_plan(
            planned_order(time_in_force=TimeInForce.GTC, limit_price=95.0),
            created_at=T0,
        )
        await harness.veska.execute(plan, T0)
        order_id = harness.orders_of(plan.plan_id)[0].client_order_id

        assert harness.executor.get_order(order_id) is not None
        await harness.veska.poll(T0 + _latency(harness))
        await harness.veska.cancel(order_id, T0 + _latency(harness) + 1)
        assert harness.oms.get(order_id).status is OrderStatus.CANCEL_PENDING
