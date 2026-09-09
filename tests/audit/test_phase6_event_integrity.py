"""Event publication — what execution tells the rest of the platform, and when.

WHY THIS IS ITS OWN MODULE
==========================
Every downstream consumer of execution truth learns about it through events:
the orchestrator's ``_on_order`` and ``_on_fill`` handlers, the recorder as bus
middleware, MARIN's reconciliation inputs, Phase 12's shadow observer. A fill
the platform applied but never published is invisible to every one of them, and
a published event whose payload disagrees with the order it describes is worse
than no event at all.

WHAT IS ASSERTED
================
* Which event types execution emits, and which it does not.
* That every order state change reaches the bus.
* That the timestamps on events are the caller's logical instants, never a
  clock read (the same P2-14 property H1 tests from the order's side).
* That correlation ids survive the whole path, so a consumer can tie a fill
  back to the opportunity that caused it.
* That a payload round-trips into the model it claims to be.

WHAT IS NOT ASSERTED
====================
Delivery. The bus's ordering, capacity and dispatch semantics were validated in
Phase 1 and are not re-litigated here; this module stops at the publish call.
"""

from __future__ import annotations

from core.events import EventType
from core.models.common import Side, TimeInForce
from core.models.execution import FillEvent, OrderStatus, PaperOrder
from core.models.opportunity import ExecutionPlan
from tests.audit.veska_fixtures import (
    T0,
    VENUE_A,
    VENUE_B,
    build_harness,
    execution_plan,
    market_state,
    planned_order,
    price_levels,
    two_venue_market,
    venue_state,
)

CORRELATION = "opp-event-audit"


def _latency(harness, venue: str = VENUE_A) -> int:
    return harness.settings.venue(venue).latency_ms


def _quiet(created_at: int = T0):
    return market_state(
        venue_state(
            venue=VENUE_A,
            bids=price_levels((90.0, 10.0)),
            asks=price_levels((110.0, 10.0)),
            as_of=created_at,
        ),
        created_at=created_at,
    )


class TestWhichEventsExecutionEmits:
    """The vocabulary, pinned."""

    async def test_a_submission_publishes_a_plan_a_creation_and_a_report(self):
        harness = build_harness()
        harness.update_market(two_venue_market())
        plan = execution_plan(
            planned_order(client_order_id="o-1"),
            created_at=T0,
            correlation_id=CORRELATION,
        )
        await harness.veska.execute(plan, T0)
        await harness.drain_events()

        types = [e.type for e in harness.published]
        assert types == [
            EventType.EXECUTION_PLAN,
            EventType.PAPER_ORDER_CREATED,
            EventType.EXECUTION_REPORT,
        ], f"submission published {types}"

    async def test_execution_never_emits_a_market_or_risk_event(self):
        """Execution reports what it did; it does not speak for other layers."""
        harness = build_harness()
        harness.update_market(two_venue_market())
        plan = execution_plan(
            planned_order(quantity=0.5, limit_price=101.0), created_at=T0
        )
        await harness.veska.execute(plan, T0)
        await harness.drain_events()
        await harness.veska.poll(T0 + _latency(harness))
        await harness.drain_events()

        emitted = {e.type for e in harness.published}
        forbidden = {
            EventType.RISK_PASS,
            EventType.RISK_FAIL,
            EventType.TRADE_INTENT,
            EventType.CONSENSUS_UPDATED,
            EventType.MARKET_STATE,
            EventType.KILL_SWITCH_TRIGGERED,
        }
        assert emitted & forbidden == set(), (
            f"execution emitted {emitted & forbidden}"
        )

    async def test_every_execution_event_names_its_source(self):
        harness = build_harness()
        harness.update_market(two_venue_market())
        plan = execution_plan(
            planned_order(quantity=0.5, limit_price=101.0), created_at=T0
        )
        await harness.veska.execute(plan, T0)
        await harness.drain_events()
        await harness.veska.poll(T0 + _latency(harness))
        await harness.drain_events()

        for event in harness.published:
            assert event.source in {"VESKA", "PAPER_EXECUTOR"}, (
                f"{event.type.value} was published by {event.source!r}"
            )


class TestEveryStateChangeIsPublished:
    """A transition nobody hears about is a transition replay cannot rebuild."""

    async def test_acknowledgement_is_published(self):
        harness = build_harness()
        harness.update_market(_quiet())
        plan = execution_plan(
            planned_order(
                time_in_force=TimeInForce.GTC, limit_price=95.0, ttl_ms=600_000
            ),
            created_at=T0,
        )
        await harness.veska.execute(plan, T0)
        await harness.drain_events()
        before = len(harness.events_of(EventType.PAPER_ORDER_UPDATED))

        await harness.veska.poll(T0 + _latency(harness))
        await harness.drain_events()

        after = len(harness.events_of(EventType.PAPER_ORDER_UPDATED))
        assert after > before, "the OPEN transition was not published"

    async def test_a_fill_publishes_both_the_fill_and_the_order_update(self):
        harness = build_harness()
        harness.update_market(two_venue_market())
        plan = execution_plan(
            planned_order(
                quantity=0.5, time_in_force=TimeInForce.IOC, limit_price=101.0
            ),
            created_at=T0,
        )
        await harness.veska.execute(plan, T0)
        await harness.drain_events()
        fills = await harness.veska.poll(T0 + _latency(harness))
        await harness.drain_events()

        assert fills, "no fill; the publication check below is untested"
        assert len(harness.events_of(EventType.PAPER_FILL)) == len(fills)
        assert harness.events_of(EventType.PAPER_ORDER_UPDATED)

    async def test_a_cancellation_is_published(self):
        harness = build_harness()
        harness.update_market(_quiet())
        latency = _latency(harness)
        cancel_latency = harness.settings.venue(VENUE_A).cancel_latency_ms
        plan = execution_plan(
            planned_order(
                time_in_force=TimeInForce.GTC, limit_price=95.0, ttl_ms=600_000
            ),
            created_at=T0,
        )
        await harness.veska.execute(plan, T0)
        await harness.drain_events()
        oid = harness.orders_of(plan.plan_id)[0].client_order_id
        await harness.veska.poll(T0 + latency)
        await harness.drain_events()

        before = len(harness.events_of(EventType.PAPER_ORDER_UPDATED))
        await harness.veska.cancel(oid, T0 + latency + 1)
        await harness.drain_events()
        await harness.veska.poll(T0 + latency + 1 + cancel_latency)
        await harness.drain_events()

        after = len(harness.events_of(EventType.PAPER_ORDER_UPDATED))
        assert after >= before + 2, (
            "the cancel request and its resolution should each have been "
            f"published; updates went from {before} to {after}"
        )

    async def test_an_expiry_is_published(self):
        harness = build_harness()
        harness.update_market(_quiet())
        latency = _latency(harness)
        plan = execution_plan(
            planned_order(
                time_in_force=TimeInForce.GTC, limit_price=95.0, ttl_ms=latency + 1
            ),
            created_at=T0,
        )
        await harness.veska.execute(plan, T0)
        await harness.drain_events()
        order = harness.orders_of(plan.plan_id)[0]

        before = len(harness.events_of(EventType.PAPER_ORDER_UPDATED))
        await harness.veska.poll(order.expires_at)
        await harness.drain_events()

        assert order.status is OrderStatus.EXPIRED
        assert len(harness.events_of(EventType.PAPER_ORDER_UPDATED)) > before

    async def test_an_unknown_transition_is_published(self):
        harness = build_harness()
        harness.update_market(_quiet())
        plan = execution_plan(
            planned_order(
                time_in_force=TimeInForce.GTC, limit_price=95.0, ttl_ms=600_000
            ),
            created_at=T0,
        )
        await harness.veska.execute(plan, T0)
        await harness.drain_events()
        oid = harness.orders_of(plan.plan_id)[0].client_order_id
        harness.executor.inject_timeout(oid)

        before = len(harness.events_of(EventType.PAPER_ORDER_UPDATED))
        await harness.veska.poll(T0 + _latency(harness))
        await harness.drain_events()

        assert harness.oms.get(oid).status is OrderStatus.UNKNOWN
        assert len(harness.events_of(EventType.PAPER_ORDER_UPDATED)) > before, (
            "an order became UNKNOWN and nothing was published, so no "
            "consumer learns that its venue truth was lost"
        )

    async def test_a_rejected_submission_is_published(self):
        harness = build_harness()
        harness.update_market(_quiet())
        harness.executor.execution_disabled = True
        plan = execution_plan(planned_order(), created_at=T0)

        await harness.veska.execute(plan, T0)
        await harness.drain_events()

        updates = harness.events_of(EventType.PAPER_ORDER_UPDATED)
        assert updates, "a rejection was not published"
        assert updates[0].payload["status"] == OrderStatus.REJECTED.value


class TestEventTimestamps:
    """Logical instants, never clock reads."""

    async def test_order_events_carry_the_supplied_instant(self):
        harness = build_harness()
        harness.update_market(_quiet())
        harness.set_clock(T0 + 90_000)
        plan = execution_plan(planned_order(), created_at=T0)

        await harness.veska.execute(plan, T0)
        await harness.drain_events()

        created = harness.events_of(EventType.PAPER_ORDER_CREATED)
        assert created and created[0].ts_ms == T0, (
            f"a submission at {T0} published its creation event at "
            f"{created[0].ts_ms} with the clock at {T0 + 90_000}"
        )

    async def test_a_fill_event_carries_the_fills_own_instant(self):
        harness = build_harness()
        harness.update_market(two_venue_market())
        harness.set_clock(T0 + 90_000)
        plan = execution_plan(
            planned_order(
                quantity=0.5, time_in_force=TimeInForce.IOC, limit_price=101.0
            ),
            created_at=T0,
        )
        await harness.veska.execute(plan, T0)
        await harness.drain_events()
        at = T0 + _latency(harness)
        fills = await harness.veska.poll(at)
        await harness.drain_events()

        assert fills
        published = harness.events_of(EventType.PAPER_FILL)
        assert published
        assert published[0].ts_ms == fills[0].created_at == at

    async def test_the_plan_event_carries_the_plans_creation_instant(self):
        """Not the submission instant — the plan's own, so replay can key on it."""
        harness = build_harness()
        harness.update_market(_quiet())
        plan = execution_plan(planned_order(), created_at=T0)

        await harness.veska.execute(plan, T0 + 5_000)
        await harness.drain_events()

        published = harness.events_of(EventType.EXECUTION_PLAN)
        assert published and published[0].ts_ms == plan.created_at == T0


class TestCorrelationSurvivesTheWholePath:
    """A consumer must be able to tie a fill back to its opportunity."""

    async def test_every_execution_event_carries_the_plans_correlation_id(self):
        harness = build_harness()
        harness.update_market(two_venue_market())
        plan = execution_plan(
            planned_order(
                quantity=0.5, time_in_force=TimeInForce.IOC, limit_price=101.0
            ),
            created_at=T0,
            correlation_id=CORRELATION,
        )
        await harness.veska.execute(plan, T0)
        await harness.drain_events()
        await harness.veska.poll(T0 + _latency(harness))
        await harness.drain_events()

        for event in harness.published:
            assert event.correlation_id == CORRELATION, (
                f"{event.type.value} carried correlation "
                f"{event.correlation_id!r}"
            )

    async def test_the_order_itself_carries_the_correlation_id(self):
        harness = build_harness()
        harness.update_market(_quiet())
        plan = execution_plan(
            planned_order(), created_at=T0, correlation_id=CORRELATION
        )
        await harness.veska.execute(plan, T0)
        await harness.drain_events()
        assert harness.orders_of(plan.plan_id)[0].correlation_id == CORRELATION

    async def test_a_fill_carries_the_correlation_id_of_its_order(self):
        harness = build_harness()
        harness.update_market(two_venue_market())
        plan = execution_plan(
            planned_order(
                quantity=0.5, time_in_force=TimeInForce.IOC, limit_price=101.0
            ),
            created_at=T0,
            correlation_id=CORRELATION,
        )
        await harness.veska.execute(plan, T0)
        await harness.drain_events()
        fills = await harness.veska.poll(T0 + _latency(harness))
        assert fills
        assert all(f.correlation_id == CORRELATION for f in fills)

    async def test_a_multi_venue_plan_shares_one_correlation_id(self):
        harness = build_harness()
        harness.update_market(two_venue_market())
        plan = execution_plan(
            planned_order(venue=VENUE_A, side=Side.BUY, limit_price=101.0),
            planned_order(venue=VENUE_B, side=Side.SELL, limit_price=99.0),
            created_at=T0,
            correlation_id=CORRELATION,
        )
        await harness.veska.execute(plan, T0)
        await harness.drain_events()
        assert {o.correlation_id for o in harness.orders_of(plan.plan_id)} == {
            CORRELATION
        }


class TestPayloadIntegrity:
    """A payload must round-trip into the model it says it is."""

    async def test_an_order_payload_reconstructs_the_order(self):
        harness = build_harness()
        harness.update_market(_quiet())
        plan = execution_plan(planned_order(), created_at=T0)
        await harness.veska.execute(plan, T0)
        await harness.drain_events()

        event = harness.events_of(EventType.PAPER_ORDER_CREATED)[0]
        assert event.schema_name == "PaperOrder"
        rebuilt = PaperOrder.model_validate(event.payload)
        original = harness.orders_of(plan.plan_id)[0]
        assert rebuilt.client_order_id == original.client_order_id
        assert rebuilt.status is original.status
        assert rebuilt.quantity == original.quantity
        assert rebuilt.venue == original.venue

    async def test_a_fill_payload_reconstructs_the_fill(self):
        harness = build_harness()
        harness.update_market(two_venue_market())
        plan = execution_plan(
            planned_order(
                quantity=0.5, time_in_force=TimeInForce.IOC, limit_price=101.0
            ),
            created_at=T0,
        )
        await harness.veska.execute(plan, T0)
        await harness.drain_events()
        fills = await harness.veska.poll(T0 + _latency(harness))
        await harness.drain_events()
        assert fills

        event = harness.events_of(EventType.PAPER_FILL)[0]
        assert event.schema_name == "FillEvent"
        rebuilt = FillEvent.model_validate(event.payload)
        assert rebuilt.fill_id == fills[0].fill_id
        assert rebuilt.quantity == fills[0].quantity
        assert rebuilt.price == fills[0].price
        assert rebuilt.fee == fills[0].fee
        assert rebuilt.liquidity is fills[0].liquidity

    async def test_a_plan_payload_reconstructs_the_plan(self):
        harness = build_harness()
        harness.update_market(_quiet())
        plan = execution_plan(planned_order(), created_at=T0)
        await harness.veska.execute(plan, T0)
        await harness.drain_events()

        event = harness.events_of(EventType.EXECUTION_PLAN)[0]
        assert event.schema_name == "ExecutionPlan"
        rebuilt = ExecutionPlan.model_validate(event.payload)
        assert rebuilt.plan_id == plan.plan_id
        assert rebuilt.notional == plan.notional
        assert rebuilt.approved_notional == plan.approved_notional
        assert rebuilt.deadline_ms == plan.deadline_ms
        assert len(rebuilt.orders) == len(plan.orders)

    async def test_the_published_order_matches_the_order_at_publication_time(
        self,
    ):
        """The payload is a snapshot, not a live reference.

        ``to_json_dict`` is called inside ``_publish_order``, so a later
        mutation must not retroactively change what was published.
        """
        harness = build_harness()
        harness.update_market(_quiet())
        plan = execution_plan(planned_order(), created_at=T0)
        await harness.veska.execute(plan, T0)
        await harness.drain_events()

        created = harness.events_of(EventType.PAPER_ORDER_CREATED)[0]
        assert created.payload["status"] == OrderStatus.SUBMITTING.value

        await harness.veska.poll(T0 + _latency(harness))
        await harness.drain_events()

        assert created.payload["status"] == OrderStatus.SUBMITTING.value, (
            "the creation event's payload changed after the order moved on"
        )
