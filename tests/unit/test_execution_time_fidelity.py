"""Phase 2 Batch 1.3: execution decides on the logical time it is GIVEN.

P2-14 was an original-vs-replay economic divergence with no market-data and
no randomness component at all: the executor read the clock itself.

Under the async harness a live feed advances the clock *while* an
orchestrator tick is still running, so ``PaperExecutor.submit()``'s own clock
read landed up to 300ms after the tick's snapshot. That read fixed
``submitted_at``, hence ``ack_at``. Replay re-executes a tick at the one
timestamp the recording preserves for it (the ``ORCHESTRATOR_TICK`` marker),
so it reconstructed the same submission 300ms earlier -- and the very next
tick then fell on the far side of the acknowledgement deadline. Replay
acknowledged and filled an order the original had left un-acknowledged:
different fills, prices, positions, P&L and kill-switch state, from a
byte-identical recorded market.

The fix is that no execution-time decision is taken from a clock read: the
orchestrator captures one canonical ``tick_time`` at the market-snapshot
boundary (the value the marker records) and passes it in. These tests pin
both halves of that: the structural invariant (the executor never reads a
clock) and the behavioural boundaries where a wrong time changes the answer
(``ack_at``, expiry, the cancel race), across one venue and two.
"""

from __future__ import annotations

import inspect

import pytest

from core.events import EventType
from core.models.common import Side
from core.models.execution import OrderStatus
from core.models.opportunity import ExecutionPlan, PlannedOrder
from execution.oms import OrderManager
from execution.paper.account import PaperAccount
from execution.paper.executor import PaperExecutor
from execution.paper.simulator import FillSimulator
from tests.conftest import make_book, venue_state_from_book

START_MS = 1_788_000_000_000
VENUE = "VENUE_A"
OTHER_VENUE = "VENUE_B"
SYMBOL = "BTC-USD"


@pytest.fixture
def oms() -> OrderManager:
    from core.clock import ManualClock

    return OrderManager(clock=ManualClock(START_MS))


@pytest.fixture
def account(clock) -> PaperAccount:
    return PaperAccount(clock=clock, initial_balance=1_000_000.0)


@pytest.fixture
def executor(bus, clock, settings, oms, account) -> PaperExecutor:
    from core.config import simulated_venues

    tuned = settings.model_copy(update={"venues": simulated_venues()})
    ex = PaperExecutor(
        bus=bus,
        clock=clock,
        settings=tuned,
        oms=oms,
        account=account,
        simulator=FillSimulator(tuned.execution),
    )
    ex.update_market(_market(mid=50_000.0))
    return ex


def _market(*, mid: float, ts: int = START_MS):
    from core.models.market import MarketState

    venues = {}
    for venue in (VENUE, OTHER_VENUE):
        book = make_book(venue, SYMBOL, mid, ts=ts)
        venues[f"{venue}:{SYMBOL}"] = venue_state_from_book(book, as_of=ts)
    return MarketState(created_at=ts, source_data_timestamp=ts, venues=venues, consolidated={})


def _plan(
    *,
    now: int,
    venues=(VENUE,),
    side: Side = Side.BUY,
    limit: float | None = None,
    ttl_ms: int = 5_000,
) -> ExecutionPlan:
    orders = [
        PlannedOrder(
            venue=venue,
            symbol=SYMBOL,
            side=side,
            quantity=0.01,
            order_type="LIMIT",
            time_in_force="GTC",
            limit_price=limit if limit is not None else 60_000.0,
            expected_price=50_000.0,
            expected_fee_bps=5.0,
            ttl_ms=ttl_ms,
        )
        for venue in venues
    ]
    return ExecutionPlan(
        created_at=now,
        source_data_timestamp=now,
        correlation_id="corr-1",
        intent_id="intent-1",
        strategy="TEST",
        symbol=SYMBOL,
        orders=orders,
        deadline_ms=now + 60_000,
        max_slippage_bps=50.0,
        notional=500.0,
    )


def _latency(executor: PaperExecutor, venue: str = VENUE) -> int:
    return executor.settings.venue(venue).latency_ms


class TestExecutorNeverReadsAClock:
    """The structural invariant behind the fix.

    Stated as a test rather than only a comment because a single reintroduced
    ``self.clock.now_ms()`` anywhere in this class silently restores P2-14 --
    the behavioural tests below would only catch it if the reintroduced read
    happened to land on a boundary.
    """

    def test_paper_executor_source_contains_no_clock_reads(self):
        source = inspect.getsource(PaperExecutor)
        assert "self.clock" not in source, (
            "PaperExecutor must take every time-dependent decision from the "
            "logical time its caller passes in; a clock read here is a time "
            "replay cannot reconstruct (see P2-14)"
        )

    def test_execution_entry_points_all_take_an_explicit_time(self):
        for name in ("submit", "cancel", "cancel_all", "poll"):
            params = inspect.signature(getattr(PaperExecutor, name)).parameters
            assert "now_ms" in params, f"{name}() must take an explicit now_ms"


class TestAcknowledgementBoundary:
    """E/F/G: the exact boundary P2-14 straddled."""

    async def test_e_poll_one_ms_before_ack_at_does_not_acknowledge(self, executor):
        report = await executor.submit(_plan(now=START_MS), START_MS)
        order = report.orders[0]
        ack_at = START_MS + _latency(executor)

        assert await executor.poll(ack_at - 1) == []
        assert order.status is OrderStatus.SUBMITTING

    async def test_f_poll_exactly_at_ack_at_acknowledges(self, executor):
        report = await executor.submit(_plan(now=START_MS), START_MS)
        order = report.orders[0]
        ack_at = START_MS + _latency(executor)

        await executor.poll(ack_at)
        assert order.status is not OrderStatus.SUBMITTING
        assert order.acknowledged_at == ack_at

    async def test_g_poll_after_ack_at_acknowledges_at_the_polled_time(self, executor):
        report = await executor.submit(_plan(now=START_MS), START_MS)
        order = report.orders[0]
        ack_at = START_MS + _latency(executor)

        await executor.poll(ack_at + 250)
        assert order.status is not OrderStatus.SUBMITTING
        # The acknowledgement is stamped with the logical time it was polled
        # at -- not a clock read, which is what made this reconstructible.
        assert order.acknowledged_at == ack_at + 250

    async def test_submission_time_comes_from_the_caller_not_the_clock(
        self, executor, clock
    ):
        """The precise P2-14 mechanism, in isolation.

        The clock is moved far away from the logical time passed in; the
        order's timing must follow the argument, not the clock.
        """
        clock.advance(300)
        report = await executor.submit(_plan(now=START_MS), START_MS)
        order = report.orders[0]

        assert order.submitted_at == START_MS
        # ...and therefore the acknowledgement deadline is reconstructible.
        assert await executor.poll(START_MS + _latency(executor) - 1) == []
        assert order.status is OrderStatus.SUBMITTING
        await executor.poll(START_MS + _latency(executor))
        assert order.status is not OrderStatus.SUBMITTING


class TestExpiryBoundary:
    """H: expiry is decided on the polled logical time."""

    async def test_h_order_expires_exactly_at_its_expiry_not_before(self, executor):
        ttl = 1_000
        report = await executor.submit(
            _plan(now=START_MS, ttl_ms=ttl, limit=1.0), START_MS
        )
        order = report.orders[0]
        assert order.expires_at == START_MS + ttl

        # Acknowledged, unfillable (limit far away), and not yet expired.
        await executor.poll(order.expires_at - 1)
        assert order.status is not OrderStatus.EXPIRED

        await executor.poll(order.expires_at)
        assert order.status is OrderStatus.EXPIRED


class TestCancelRaceBoundary:
    """I: a cancel's arrival time comes from the caller too."""

    async def test_i_cancel_arrival_is_measured_from_the_supplied_time(
        self, executor, clock
    ):
        report = await executor.submit(
            _plan(now=START_MS, limit=1.0), START_MS
        )
        order = report.orders[0]
        ack_at = START_MS + _latency(executor)
        await executor.poll(ack_at)
        assert order.status is OrderStatus.OPEN

        cancel_latency = executor.settings.venue(VENUE).cancel_latency_ms
        # The clock is deliberately elsewhere; the cancel must land relative
        # to the logical time supplied, not to the clock.
        clock.advance(10_000)
        await executor.cancel(order.client_order_id, ack_at)
        assert order.status is OrderStatus.CANCEL_PENDING

        await executor.poll(ack_at + cancel_latency - 1)
        assert order.status is OrderStatus.CANCEL_PENDING

        await executor.poll(ack_at + cancel_latency)
        assert order.status is OrderStatus.CANCELLED


class TestMultipleOrdersAcrossVenues:
    """J: two venues with different latencies, one polled logical time."""

    async def test_j_each_venue_acknowledges_on_its_own_latency(self, executor):
        report = await executor.submit(
            _plan(now=START_MS, venues=(VENUE, OTHER_VENUE)), START_MS
        )
        by_venue = {o.venue: o for o in report.orders}
        assert set(by_venue) == {VENUE, OTHER_VENUE}

        fast, slow = sorted(
            (VENUE, OTHER_VENUE), key=lambda v: executor.settings.venue(v).latency_ms
        )
        fast_latency = executor.settings.venue(fast).latency_ms
        slow_latency = executor.settings.venue(slow).latency_ms
        assert fast_latency < slow_latency, "the venues must differ for this to prove anything"

        # A single polled logical time between the two deadlines resolves the
        # faster venue and not the slower one.
        await executor.poll(START_MS + slow_latency - 1)
        assert by_venue[fast].status is not OrderStatus.SUBMITTING
        assert by_venue[slow].status is OrderStatus.SUBMITTING

        await executor.poll(START_MS + slow_latency)
        assert by_venue[slow].status is not OrderStatus.SUBMITTING


class TestOrchestratorTickTime:
    """The orchestrator's side of the contract."""

    async def test_tick_time_is_the_marker_timestamp(self, platform):
        """Every ORCHESTRATOR_TICK marker records the tick time the tick's
        decisions actually ran at -- that identity is what lets replay
        restore it.
        """
        seen: list[int] = []
        platform.bus.subscribe(
            lambda e: seen.append(e.ts_ms) or _noop(),
            types=[EventType.ORCHESTRATOR_TICK],
            name="marker-probe",
        )
        await platform.start(record=False, feeds=False)

        captured: list[int] = []
        original_settle = platform.orchestrator._settle

        async def spy(now):
            captured.append(now)
            return await original_settle(now)

        platform.orchestrator._settle = spy

        for _ in range(3):
            platform.clock.advance(100)
            await platform.step_market(1)
            await platform.orchestrator.tick()
        # The final tick's marker may still be queued: a tick only drains on
        # its own account when settlement produced fills.
        await platform.bus.drain()

        assert len(seen) == 3
        assert captured == seen, (
            "settlement must run at exactly the logical time the marker "
            "recorded, or replay cannot reconstruct it"
        )

    async def test_tick_time_falls_back_to_the_clock_outside_a_tick(self, platform):
        await platform.start(record=False, feeds=False)
        assert platform.orchestrator._tick_time is None
        assert platform.orchestrator.tick_time == platform.clock.now_ms()
        platform.clock.advance(500)
        assert platform.orchestrator.tick_time == platform.clock.now_ms()

    async def test_tick_time_is_cleared_even_if_the_tick_raises(self, platform):
        await platform.start(record=False, feeds=False)

        async def boom(_market):
            raise RuntimeError("tick exploded")

        platform.orchestrator._tick_body = boom
        with pytest.raises(RuntimeError):
            await platform.orchestrator.tick()
        assert platform.orchestrator._tick_time is None


async def _noop() -> None:
    return None
