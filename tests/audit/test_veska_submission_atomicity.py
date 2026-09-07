"""Phase 6 — H8, H9: partially-submitted plans, and repeated ones.

Two questions about the boundary between a plan and the orders it becomes.

**H8 — atomicity and hand-off.** ``PaperExecutor.submit`` loops leg by leg,
accepting each into the OMS and publishing as it goes. The orchestrator learns
which orders exist only from the report:

    report = await self.veska.execute(plan, self.tick_time)
    record.order_ids = [order.client_order_id for order in report.orders]

Everything between the first accepted order and that assignment is a window in
which a live order exists that no opportunity record names. The bus publish
inside the loop is the concrete way in: with ``raise_on_handler_error`` set —
which is how the platform is wired in every test and every recording run — a
handler that raises reaches the dispatcher, and a transport failure reaches
``publish`` itself.

"The bus does not normally raise" is not an argument about a safety boundary;
it is a statement about the common case. The question is what the boundary
guarantees when it does.

**H9 — idempotency.** ``OrderManager.create`` ends with
``self.orders[order.client_order_id] = order`` and no existence check. A
``PlannedOrder`` carries its own ``client_order_id``, so a retried, redelivered
or replayed plan presents the same identity twice.
"""

from __future__ import annotations

import inspect

import pytest

from core.events import Event
from core.models.execution import OrderStatus
from execution.oms import OrderManager
from tests.audit.veska_fixtures import (
    VENUE_A,
    VENUE_A_LATENCY_MS,
    VENUE_B,
    audit_settings,
    book,
    deterministic_execution,
    market,
    one_venue_market,
    plan,
    planned,
    rig,
    venue_state,
)
from tests.conftest import START_MS

CERTAIN = audit_settings(**deterministic_execution())
ACK_AT = START_MS + VENUE_A_LATENCY_MS


def two_venue_market(mid: float = 40_000.0):
    return market(
        venue_state(book(VENUE_A, mid=mid)),
        venue_state(book(VENUE_B, mid=mid)),
    )


def two_leg_plan(limit_price: float = 40_100.0):
    return plan(
        planned(
            venue=VENUE_A, client_order_id="ord-leg-a", limit_price=limit_price
        ),
        planned(
            venue=VENUE_B, client_order_id="ord-leg-b", limit_price=limit_price
        ),
    )


class ExplodingBus:
    """A bus whose ``publish`` raises once ``fail_after`` calls have succeeded.

    Duck-typed rather than a real ``EventBus``: the executor calls exactly one
    method on the bus, and a real ``InMemoryEventBus`` would not reproduce the
    condition anyway — it enqueues on ``publish`` and only surfaces a handler
    failure on ``drain``, which happens after ``submit`` has returned. The
    failure this models is the publication itself failing, which is what a
    transport error looks like from inside the loop.
    """

    def __init__(self, fail_after: int = 1) -> None:
        self.fail_after = fail_after
        self.published: list[Event] = []

    async def publish(self, event: Event) -> None:
        if len(self.published) >= self.fail_after:
            raise RuntimeError("bus publication failed mid-plan")
        self.published.append(event)

    @property
    def queue_depth(self) -> int:
        return 0

    async def drain(self) -> None:
        return None


class TestTheHandoffWindow:
    """H8. Structural shape of the window, before it is exercised."""

    def test_the_orchestrator_learns_the_order_ids_only_after_submission(self):
        import apps.orchestrator.orchestrator as orchestrator

        source = inspect.getsource(orchestrator.Orchestrator._decide)
        execute_at = source.index("await self.veska.execute(plan")
        assign_at = source.index("record.order_ids = ")
        assert execute_at < assign_at, (
            "the record's order ids are still assigned after execute() "
            "returns; everything in between is unmanageable"
        )

    def test_submit_accepts_orders_one_at_a_time(self):
        from execution.paper.executor import PaperExecutor

        source = inspect.getsource(PaperExecutor.submit)
        assert "for planned in plan.orders:" in source
        assert "await self._publish_order(" in source, (
            "submit publishes inside the per-leg loop, so a publication "
            "failure lands between two accepted orders"
        )

    def test_submit_has_no_rollback_for_a_partial_plan(self):
        from execution.paper.executor import PaperExecutor

        source = inspect.getsource(PaperExecutor.submit)
        for undo in ("try:", "except", "rollback", "finally"):
            assert undo not in source, (
                f"submit now contains {undo!r}; the atomicity question may "
                "already be answered"
            )


class TestPartialSubmissionLeavesAnUnmanageableOrder:
    """H8. First leg accepted, second leg's publication raises."""

    @staticmethod
    def _rig_that_fails_mid_plan(mid: float = 30_000.0):
        """A rig whose second publication raises, mid two-leg submission."""
        built = rig(settings=CERTAIN, market_state=two_venue_market(mid=mid))
        built.executor.bus = ExplodingBus(fail_after=1)
        return built

    async def test_the_first_leg_survives_the_failure(self):
        """The premise: an order is accepted and working at the venue."""
        built = self._rig_that_fails_mid_plan()
        with pytest.raises(RuntimeError):
            await built.executor.submit(two_leg_plan(), START_MS)

        first = built.oms.get("ord-leg-a")
        assert first is not None
        assert first.status is OrderStatus.SUBMITTING
        assert "ord-leg-a" in built.executor._pending

    async def test_a_partially_submitted_plan_leaves_no_unmanageable_order(self):
        """The safety property.

        After a failed submission, every order the OMS holds live must be one
        the caller could have learned about. ``submit`` raised, so the caller
        received no report and knows none of them.
        """
        built = self._rig_that_fails_mid_plan()
        with pytest.raises(RuntimeError):
            await built.executor.submit(two_leg_plan(), START_MS)

        stranded = [o.client_order_id for o in built.oms.live_orders()]
        assert not stranded, (
            "submit() raised, so its caller has no report and no order ids, "
            f"but {stranded} are live in the OMS: no opportunity record names "
            "them, no exit can be built for them, and _advance_execution will "
            "see an empty order list and close the record"
        )

    async def test_the_stranded_order_can_still_fill(self):
        """Why it matters: it is not inert, it is exposure."""
        built = self._rig_that_fails_mid_plan()
        with pytest.raises(RuntimeError):
            await built.executor.submit(two_leg_plan(), START_MS)

        # The executor keeps working the order it already accepted.
        built.executor.bus = ExplodingBus(fail_after=10_000)
        fills = await built.executor.poll(ACK_AT)

        assert not fills, (
            "an order stranded by a failed submission went on to fill: "
            f"{[(f.client_order_id, f.quantity, f.price) for f in fills]} — "
            "the platform now holds a position it cannot attribute to any "
            "opportunity"
        )

    async def test_the_kill_switch_can_still_reach_a_stranded_order(self):
        """The one mitigation that does exist, recorded so the finding is
        scoped honestly.

        ``cancel_all`` iterates the OMS rather than the opportunity records, so
        an engaged kill switch does reach a stranded order. That bounds the
        blast radius; it does not make the order manageable in ordinary
        operation, and nothing outside the kill switch reaches it at all.
        """
        built = self._rig_that_fails_mid_plan()
        with pytest.raises(RuntimeError):
            await built.executor.submit(two_leg_plan(), START_MS)

        built.executor.bus = ExplodingBus(fail_after=10_000)
        assert await built.executor.cancel_all(START_MS + 1) >= 1


class TestPlanSubmissionIdempotency:
    """H9. One client order id is one order identity."""

    def test_create_overwrites_without_checking(self):
        """The premise, from the source."""
        source = inspect.getsource(OrderManager.create)
        assert "self.orders[order.client_order_id] = order" in source
        assert "in self.orders" not in source, (
            "create now checks for an existing identity; the tests below need "
            "rechecking against whatever it does"
        )

    async def test_resubmitting_the_same_plan_does_not_duplicate_exposure(self):
        """A retried or redelivered plan must not create a second order for
        the same identity, nor silently replace the first."""
        built = rig(settings=CERTAIN, market_state=one_venue_market())
        original = plan(planned(client_order_id="ord-repeat", limit_price=1.0))

        await built.executor.submit(original, START_MS)
        first = built.oms.get("ord-repeat")
        await built.executor.poll(ACK_AT)
        assert first is not None and first.status is OrderStatus.OPEN

        await built.executor.submit(original, START_MS + 100)
        second = built.oms.get("ord-repeat")

        assert second is first, (
            "resubmitting a plan replaced the live order object for "
            "client_order_id 'ord-repeat'; the venue still holds the first one"
        )

    async def test_a_resubmission_does_not_reset_a_partially_filled_order(self):
        """The destructive case: fills already applied are erased."""
        built = rig(
            settings=CERTAIN,
            market_state=one_venue_market(mid=30_000.0, levels=1, size=0.02),
        )
        repeated = plan(
            planned(
                client_order_id="ord-repeat",
                quantity=0.10,
                limit_price=40_100.0,
            )
        )
        await built.executor.submit(repeated, START_MS)
        await built.executor.poll(ACK_AT)
        filled_before = built.oms.get("ord-repeat").filled_quantity
        assert filled_before > 0

        await built.executor.submit(repeated, START_MS + 100)
        filled_after = built.oms.get("ord-repeat").filled_quantity

        assert filled_after == pytest.approx(filled_before), (
            f"a resubmission reset filled_quantity from {filled_before} to "
            f"{filled_after}; the fills were applied to the account and "
            "published, so the order record no longer matches the position"
        )

    async def test_a_resubmission_does_not_erase_order_history(self):
        built = rig(settings=CERTAIN, market_state=one_venue_market())
        repeated = plan(planned(client_order_id="ord-repeat", limit_price=1.0))
        await built.executor.submit(repeated, START_MS)
        await built.executor.poll(ACK_AT)
        history_before = list(built.oms.get("ord-repeat").history)
        assert len(history_before) >= 3

        await built.executor.submit(repeated, START_MS + 100)
        history_after = list(built.oms.get("ord-repeat").history)

        assert history_after[: len(history_before)] == history_before, (
            "a resubmission discarded the order's history: "
            f"{[s.value for _t, s in history_before]} became "
            f"{[s.value for _t, s in history_after]}, so reconciliation has "
            "no evidence the first order ever existed"
        )

    async def test_two_plans_sharing_one_client_order_id_are_refused(self):
        """Distinct plans, one identity. Either the second is rejected or it
        is recognised as the same order; creating a second live order under
        one id is the outcome that cannot be reconciled."""
        built = rig(settings=CERTAIN, market_state=one_venue_market())
        await built.executor.submit(
            plan(planned(client_order_id="ord-shared", quantity=0.10, limit_price=1.0)),
            START_MS,
        )
        await built.executor.poll(ACK_AT)

        report = await built.executor.submit(
            plan(planned(client_order_id="ord-shared", quantity=5.00, limit_price=1.0)),
            START_MS + 100,
        )

        order = built.oms.get("ord-shared")
        rejected = report.orders and report.orders[0].status is OrderStatus.REJECTED
        assert rejected or order.quantity == pytest.approx(0.10), (
            "a second plan reusing an existing client_order_id silently "
            f"replaced the live order: quantity is now {order.quantity}, "
            f"status {order.status.value}, notes {report.notes}"
        )

    async def test_the_orders_created_counter_still_counts_the_duplicate(self):
        """Recorded: the lifetime counter and the resident set disagree."""
        built = rig(settings=CERTAIN, market_state=one_venue_market())
        repeated = plan(planned(client_order_id="ord-repeat", limit_price=1.0))
        await built.executor.submit(repeated, START_MS)
        await built.executor.submit(repeated, START_MS + 100)

        assert built.oms.orders_created == 2
        assert len(built.oms.orders) == 1
