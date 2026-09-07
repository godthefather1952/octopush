"""Phase 6 — H17, H21: what the event stream promises about the state.

**H17 — atomicity of state and event.** ``PaperExecutor._record_fill`` applies
the fill to the OMS and to the account, and *then* publishes ``PAPER_FILL``:

    if not self.oms.apply_fill(fill): return
    self.account.apply_fill(fill)
    await self.bus.publish(...)

The economic state has already moved when the publication happens. If it fails,
the platform holds a position and a cash balance that no event in the durable
record explains. The same shape appears in ``submit`` (order created, then
published) and in ``poll`` (order transitioned, then published).

The replayable record and the economic state diverging is not a cosmetic
problem: MARIN reconciles against events, the recorder persists them, and
replay reconstructs the platform from them.

**H21 — what an ``ExecutionReport`` reports.** Its docstring says "What VESKA
reports back after working a plan". It is emitted once, immediately after
``submit``, with ``complete=False`` and an empty ``fills`` list. Nothing ever
emits a second one. If no report ever describes the outcome, the model is a
submission acknowledgement wearing a completion report's name.
"""

from __future__ import annotations

import inspect

import pytest

from core.events import Event, EventType
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


class FailAfter:
    """A bus whose ``publish`` starts failing after N successful calls."""

    def __init__(self, fail_after: int) -> None:
        self.fail_after = fail_after
        self.published: list[Event] = []

    async def publish(self, event: Event) -> None:
        if len(self.published) >= self.fail_after:
            raise RuntimeError("publication failed")
        self.published.append(event)

    @property
    def queue_depth(self) -> int:
        return 0

    async def drain(self) -> None:
        return None


class TestTheOrderingOfStateAndEvent:
    """H17. Structural: state first, publication second, everywhere."""

    def test_a_fill_is_applied_before_it_is_published(self):
        source = inspect.getsource(PaperExecutor._record_fill)
        apply_at = source.index("self.oms.apply_fill(fill)")
        account_at = source.index("self.account.apply_fill(fill)")
        publish_at = source.index("await self.bus.publish(")
        assert apply_at < account_at < publish_at

    def test_an_order_is_created_before_it_is_published(self):
        source = inspect.getsource(PaperExecutor.submit)
        create_at = source.index("self.oms.from_plan(")
        publish_at = source.index("await self._publish_order(order, EventType.PAPER_ORDER_CREATED")
        assert create_at < publish_at

    def test_a_transition_is_applied_before_it_is_published(self):
        source = inspect.getsource(PaperExecutor.poll)
        transition_at = source.index(
            "self.oms.transition(order.client_order_id, OrderStatus.ACKNOWLEDGED)"
        )
        publish_at = source.index("await self._publish_order(", transition_at)
        assert transition_at < publish_at

    def test_no_publication_failure_is_caught_anywhere_in_the_executor(self):
        """The executor has exactly one ``except``, and it is the venue-latency
        lookup — nothing wraps a state mutation or a publication."""
        source = inspect.getsource(PaperExecutor)
        handlers = source.count("except")
        assert handlers == 1, (
            f"the executor now has {handlers} exception handlers; H17's "
            "premise is that a publication failure is caught nowhere"
        )
        assert "except KeyError" in inspect.getsource(PaperExecutor._latency)
        for method in (
            PaperExecutor.submit,
            PaperExecutor.poll,
            PaperExecutor._record_fill,
            PaperExecutor._publish_order,
        ):
            assert "except" not in inspect.getsource(method)


class TestAFailedPublicationLeavesStateAhead:
    """H17. The measurement, on the fill path."""

    async def test_a_fill_is_applied_even_when_its_event_is_lost(self):
        """The premise: the position moves, the record does not."""
        built = rig(settings=CERTAIN, market_state=one_venue_market(mid=30_000.0))
        # Let submission publish; fail on the first fill-time publication.
        built.executor.bus = FailAfter(fail_after=2)
        await built.executor.submit(
            plan(planned(limit_price=40_100.0)), START_MS
        )

        with pytest.raises(RuntimeError):
            await built.executor.poll(ACK_AT)

        order = built.only_order()
        position = built.account.snapshot().positions
        assert order.filled_quantity > 0
        assert any(not p.is_flat for p in position.values())

    async def test_the_durable_record_still_explains_the_position(self):
        """The safety property.

        Every fill applied to the account must be recoverable from the events
        the platform published. A position with no ``PAPER_FILL`` behind it is
        invisible to the recorder, to replay and to reconciliation.
        """
        built = rig(settings=CERTAIN, market_state=one_venue_market(mid=30_000.0))
        failing = FailAfter(fail_after=2)
        built.executor.bus = failing
        await built.executor.submit(
            plan(planned(limit_price=40_100.0)), START_MS
        )
        with pytest.raises(RuntimeError):
            await built.executor.poll(ACK_AT)

        order = built.only_order()
        recorded = [e for e in failing.published if e.type is EventType.PAPER_FILL]

        assert len(recorded) == len(order.fills), (
            f"the order holds {len(order.fills)} applied fill(s) worth "
            f"{order.filled_quantity} but the event stream carries "
            f"{len(recorded)}; the account moved without a record that any "
            "downstream consumer can see"
        )

    async def test_a_terminal_transition_survives_a_lost_update_event(self):
        """The same shape on the order-state path."""
        built = rig(settings=CERTAIN, market_state=one_venue_market())
        failing = FailAfter(fail_after=1)
        built.executor.bus = failing
        await built.executor.submit(plan(planned(limit_price=1.0)), START_MS)

        with pytest.raises(RuntimeError):
            await built.executor.poll(ACK_AT)

        order = built.only_order()
        updates = [
            e for e in failing.published if e.type is EventType.PAPER_ORDER_UPDATED
        ]
        assert order.status.value == "SUBMITTING" or updates, (
            f"the order advanced to {order.status.value} with no "
            "PAPER_ORDER_UPDATED published"
        )

    def test_the_recorder_sits_inside_publish_not_after_it(self):
        """Which decides whether H17 is observability or durability.

        The recorder attaches as bus *middleware*, so it runs during
        ``publish``. That is the right place — but it also means a publication
        that fails is a publication the recorder never saw. There is no
        after-the-fact path by which a lost ``PAPER_FILL`` reaches the durable
        record, so the state that moved before it has nothing behind it.
        """
        from storage.recorder import Recorder

        source = inspect.getsource(Recorder)
        assert "bus.add_middleware(self.record)" in source
        assert "middleware" in source.lower()

    def test_nothing_replays_a_publication_that_failed(self):
        """No retry, no outbox, no dead-letter path in the executor."""
        source = inspect.getsource(PaperExecutor)
        for recovery in ("retry", "outbox", "dead_letter", "republish"):
            assert recovery not in source.lower(), (
                f"the executor now has a {recovery!r} path; the durability "
                "question may already be answered"
            )


class TestExecutionReportSemantics:
    """H21. Is it a completion report or a submission acknowledgement?"""

    def test_the_model_describes_a_completed_plan(self):
        from core.models.execution import ExecutionReport

        doc = inspect.getdoc(ExecutionReport) or ""
        assert "after working a plan" in doc
        assert "complete" in ExecutionReport.model_fields
        assert "fills" in ExecutionReport.model_fields

    async def test_the_only_report_is_emitted_before_anything_happens(self):
        built = rig(settings=CERTAIN, market_state=one_venue_market(mid=30_000.0))
        report = await built.executor.submit(
            plan(planned(limit_price=40_100.0)), START_MS
        )
        assert report.complete is False
        assert report.fills == []
        assert all(o.filled_quantity == 0.0 for o in report.orders)

    async def test_a_report_eventually_describes_the_outcome(self):
        """The property the model's own docstring promises."""
        built = rig(settings=CERTAIN, market_state=one_venue_market(mid=30_000.0))
        await built.executor.submit(
            plan(planned(limit_price=40_100.0)), START_MS
        )
        await built.executor.poll(ACK_AT)
        await built.bus.drain()

        reports = built.events(EventType.EXECUTION_REPORT)
        completed = [r for r in reports if r.payload.get("complete") is True]
        assert completed, (
            f"{len(reports)} EXECUTION_REPORT event(s) were published and none "
            "reports complete=True; nothing in the stream ever states the "
            "plan's outcome, so the report is a submission acknowledgement"
        )

    def test_no_code_path_sets_complete_to_true(self):
        """Structural confirmation, so the finding is about the whole system
        rather than about one executor."""
        import execution.veska.engine as engine

        for module in (PaperExecutor, engine.Veska):
            source = inspect.getsource(module)
            assert "complete=True" not in source


class TestPublishedPayloadsAreFaithful:
    """Controls: what is published matches what happened."""

    async def test_every_order_event_carries_the_orders_own_state(self):
        built = rig(settings=CERTAIN, market_state=one_venue_market(mid=30_000.0))
        await built.executor.submit(
            plan(planned(limit_price=40_100.0)), START_MS
        )
        await built.executor.poll(ACK_AT)
        await built.bus.drain()

        order = built.only_order()
        updates = built.events(EventType.PAPER_ORDER_UPDATED)
        assert updates
        assert updates[-1].payload["status"] == order.status.value
        assert updates[-1].payload["filled_quantity"] == pytest.approx(
            order.filled_quantity
        )

    async def test_every_fill_event_carries_its_own_price_and_fee(self):
        built = rig(settings=CERTAIN, market_state=one_venue_market(mid=30_000.0))
        await built.executor.submit(
            plan(planned(limit_price=40_100.0)), START_MS
        )
        fills = await built.executor.poll(ACK_AT)
        await built.bus.drain()

        published = built.events(EventType.PAPER_FILL)
        assert len(published) == len(fills)
        for event, fill in zip(published, fills, strict=True):
            assert event.payload["price"] == pytest.approx(fill.price)
            assert event.payload["fee"] == pytest.approx(fill.fee)
            assert event.ts_ms == fill.created_at

    async def test_correlation_survives_from_plan_to_fill(self):
        built = rig(settings=CERTAIN, market_state=one_venue_market(mid=30_000.0))
        await built.executor.submit(
            plan(planned(limit_price=40_100.0), correlation_id="opp-trace"),
            START_MS,
        )
        fills = await built.executor.poll(ACK_AT)
        await built.bus.drain()

        assert fills and fills[0].correlation_id == "opp-trace"
        assert all(
            e.correlation_id == "opp-trace"
            for e in built.events(
                EventType.PAPER_ORDER_CREATED,
                EventType.PAPER_ORDER_UPDATED,
                EventType.PAPER_FILL,
            )
        )
