"""Reconciliation and ledger memory must not grow with session length — P0-H4.

The audit measured, on this machine, a paper account and OMS that retained
every order and every fill for the life of the process, and a MARIN that
rescanned all of it on every run:

======  ==============  =========  ==========
fills   reconcile       vs 10k     resident
======  ==============  =========  ==========
10_000     65.0 ms       x 1.00      35.1 MB
50_000    349.0 ms       x 5.37     175.4 MB
100_000   729.1 ms       x11.21     350.8 MB
======  ==============  =========  ==========

Both curves are linear in *lifetime* history, so the work a session does
reconciling is quadratic in the fills it produces. A long-running paper
session degrades until reconciliation dominates the tick.

After the fix, measured the same way, a single reconcile and the resident
footprint are both flat:

======  ==============  =========  ==========
fills   reconcile       vs 10k     resident
======  ==============  =========  ==========
10_000     56.5 ms       x 1.00      35.1 MB
50_000     65.5 ms       x 1.16      36.4 MB
100_000    56.4 ms       x 1.00      36.4 MB
======  ==============  =========  ==========

And cumulatively over a session, reconciling every 250 fills — bounded
retention against unbounded retention on identical code paths, so the
retention policy is the only variable:

========  ===========  =========  =========
fills     unbounded    bounded    speedup
========  ===========  =========  =========
20_000       4.89 s     4.21 s      x1.16
40_000      22.39 s    10.31 s      x2.17
80_000      89.12 s    23.59 s      x3.78
========  ===========  =========  =========

Per doubling of N the unbounded total grows x4.58 then x3.98 (quadratic);
the bounded total grows x2.45 then x2.29 (linear). The gap therefore widens
without limit, which is why this is a defect rather than a slow constant.

These tests assert the shape of the curve, not a wall-clock budget: absolute
timings are meaningless across machines, but the difference between "flat"
and "linear in lifetime history" survives any hardware.
"""

from __future__ import annotations

import time

import pytest

from agents.marin.agent import Marin
from core.clock import ManualClock
from core.health import HealthRegistry
from core.models.common import Liquidity, OrderType, Side, TimeInForce
from core.models.execution import FillEvent, OrderStatus
from execution.oms import OrderManager
from execution.paper.account import PaperAccount

#: How often the platform reconciles, in fills. MARIN runs every few ticks and
#: a tick produces single-digit fills, so a few hundred fills between runs is
#: the realistic cadence.
RECONCILE_EVERY = 250

#: Retention used by these tests. The shipped default is 10_000; a smaller
#: window exercises the same compaction path at every scale under test,
#: rather than only once a run happens to exceed the production figure. The
#: mechanism is what is under test, not the constant.
RETAINED = 500


class NullBus:
    queue_depth = 0

    async def publish(self, event):  # pragma: no cover - not exercised
        return None


class Ledger:
    """A paper account, an OMS and MARIN, driven like the real platform."""

    def __init__(self, retained: int = RETAINED) -> None:
        self.clock = ManualClock(start_ms=1_700_000_000_000)
        self.oms = OrderManager(clock=self.clock)
        self.account = PaperAccount(
            clock=self.clock, initial_balance=10_000_000.0, retained_fills=retained
        )
        self.marin = Marin(
            bus=NullBus(),
            clock=self.clock,
            health=HealthRegistry(clock=self.clock),
            oms=self.oms,
            account=self.account,
        )

    def trade(self, count: int, *, reconcile_every: int = RECONCILE_EVERY) -> None:
        for i in range(count):
            self._round_trip(i)
            self.clock.advance(1)
            if reconcile_every and (i + 1) % reconcile_every == 0:
                result = self.marin.reconcile()
                assert result.ok, [m.detail for m in result.critical]
                self.marin.compact()

    def _round_trip(self, i: int) -> None:
        side = Side.BUY if i % 2 == 0 else Side.SELL
        order = self.oms.create(
            venue="VENUE_A" if i % 2 == 0 else "VENUE_B",
            symbol="BTC-USD",
            side=side,
            quantity=1.0,
            order_type=OrderType.LIMIT,
            time_in_force=TimeInForce.GTC,
            expected_price=50_000.0,
        )
        self.oms.transition(order.client_order_id, OrderStatus.SUBMITTING)
        self.oms.transition(order.client_order_id, OrderStatus.ACKNOWLEDGED)
        fill = FillEvent(
            created_at=self.clock.now_ms(),
            client_order_id=order.client_order_id,
            venue=order.venue,
            symbol=order.symbol,
            side=side,
            quantity=1.0,
            price=50_000.0 + (i % 7),
            fee=1.0,
            liquidity=Liquidity.TAKER,
        )
        self.oms.apply_fill(fill)
        self.account.apply_fill(fill)

    def time_reconcile(self, repeats: int = 5) -> float:
        """Median reconcile time in seconds — median so one GC pause cannot skew it."""
        samples = []
        for _ in range(repeats):
            start = time.perf_counter()
            self.marin.reconcile()
            samples.append(time.perf_counter() - start)
        samples.sort()
        return samples[len(samples) // 2]


@pytest.fixture(scope="module")
def ledgers():
    """One ledger per scale. Module-scoped: building 100k fills is not cheap."""
    built = {}
    for size in (10_000, 50_000, 100_000):
        ledger = Ledger()
        ledger.trade(size)
        built[size] = ledger
    return built


class TestResidentStateIsBounded:
    """Memory must be bounded by the retention window, not by session length."""

    #: The window plus one reconciliation interval of fills that have arrived
    #: since the last seal. Nothing else may be resident.
    BOUND = RETAINED + RECONCILE_EVERY

    def test_orders_do_not_accumulate(self, ledgers):
        for size, ledger in ledgers.items():
            assert ledger.oms.orders_created == size
            assert len(ledger.oms.orders) <= self.BOUND, (
                f"{size} fills left {len(ledger.oms.orders)} orders resident"
            )

    def test_the_fill_log_does_not_accumulate(self, ledgers):
        for size, ledger in ledgers.items():
            assert ledger.account.fills_applied == size
            assert len(ledger.account.fill_log) <= self.BOUND, (
                f"{size} fills left a log of {len(ledger.account.fill_log)}"
            )

    def test_the_dedupe_window_does_not_accumulate(self, ledgers):
        """The idempotency sets were unbounded too — two ids per fill."""
        for ledger in ledgers.values():
            assert len(ledger.oms._applied_fills) <= ledger.oms.dedupe_fills
            assert len(ledger.account._applied) <= ledger.account.retained_fills

    def test_a_short_session_is_never_compacted(self):
        """Below the window, history stays fully resident and fully replayed.

        Compaction is a backstop. A session that never fills the window keeps
        end-to-end verification from its first fill, so the coverage traded
        away is only ever coverage of history already checked many times.
        """
        ledger = Ledger(retained=RETAINED)
        ledger.trade(RETAINED - RECONCILE_EVERY)
        assert ledger.account.checkpoint.fills_sealed == 0
        assert ledger.oms.archived.count == 0
        assert len(ledger.account.fill_log) == ledger.account.fills_applied

    def test_resident_state_is_flat_across_an_order_of_magnitude(self, ledgers):
        """10x the history must not mean 10x the residency."""
        small = ledgers[10_000]
        large = ledgers[100_000]
        assert len(large.oms.orders) <= len(small.oms.orders) * 2
        assert len(large.account.fill_log) <= len(small.account.fill_log) * 2

    def test_compaction_actually_ran(self, ledgers):
        """Guards every other assertion here: a no-op would pass them all."""
        for size, ledger in ledgers.items():
            assert ledger.account.checkpoint.fills_sealed > 0, f"{size}: nothing sealed"
            assert ledger.oms.archived.count > 0, f"{size}: nothing archived"

    def test_nothing_is_lost_when_state_is_compacted(self, ledgers):
        """Archived orders are accounted for, not forgotten."""
        for size, ledger in ledgers.items():
            total = ledger.oms.archived.count + len(ledger.oms.orders)
            assert total == size
            assert ledger.oms.archived.fills + ledger.oms.resident_fills == size
            assert (
                ledger.account.checkpoint.fills_sealed + len(ledger.account.fill_log) == size
            )


class TestReconciliationCostIsBounded:
    def test_reconcile_time_does_not_track_history(self, ledgers):
        """A 10x longer session must not mean a 10x slower reconciliation.

        The threshold is 3.0, chosen to separate three distinct outcomes
        rather than to encode a performance budget:

        * a bounded implementation gives ~1.0 (it does the same work);
        * the measured pre-fix implementation gave 11.21;
        * timing noise on a shared, virtualised runner is comfortably within
          2x for a millisecond-scale measurement.

        3.0 sits above the noise floor and far below the linear result, so
        the test fails if and only if reconciliation starts tracking lifetime
        history again.
        """
        small = ledgers[10_000].time_reconcile()
        large = ledgers[100_000].time_reconcile()
        ratio = large / small
        assert ratio < 3.0, (
            f"reconcile scaled x{ratio:.2f} for 10x the history "
            f"({small * 1000:.2f}ms -> {large * 1000:.2f}ms)"
        )


class TestCompactionPreservesCorrectness:
    """Bounded state is worthless if it reconciles by forgetting."""

    def test_a_compacted_session_still_reconciles_clean(self, ledgers):
        for ledger in ledgers.values():
            assert ledger.marin.reconcile().ok

    def test_cash_survives_sealing(self, ledgers):
        """A compacted session must agree with one that kept everything.

        Compared at 10k rather than at every scale: an uncompacted control is
        exactly the linear-memory case this change exists to remove, so
        building three of them would reintroduce the cost under test. The
        arithmetic being checked does not vary with N.
        """
        size = 10_000
        compacted = ledgers[size]
        assert compacted.account.checkpoint.fills_sealed > 0

        control = Ledger(retained=size * 2)  # never reaches the window
        control.trade(size)
        assert control.account.checkpoint.fills_sealed == 0

        assert compacted.account.cash == pytest.approx(control.account.cash, abs=1e-6)
        assert compacted.account.realized_pnl == pytest.approx(
            control.account.realized_pnl, abs=1e-6
        )
        assert compacted.account.fees_paid == pytest.approx(control.account.fees_paid, abs=1e-6)
        assert compacted.account.positions.keys() == control.account.positions.keys()
        for key, position in control.account.positions.items():
            assert compacted.account.positions[key].quantity == pytest.approx(
                position.quantity, abs=1e-9
            )

    def test_a_corrupted_tail_is_still_caught_after_compaction(self, ledgers):
        """Sealing must not blind reconciliation to a new discrepancy."""
        ledger = Ledger()
        ledger.trade(1_000)
        assert ledger.marin.reconcile().ok
        ledger.account.cash += 5.0
        result = ledger.marin.reconcile()
        assert not result.ok
        assert any(m.key == "cash" for m in result.critical)

    def test_a_dropped_fill_is_caught_even_though_history_was_sealed(self):
        """The lifetime counters are what make this detectable post-seal."""
        ledger = Ledger()
        ledger.trade(1_000)
        assert ledger.marin.reconcile().ok
        # The OMS sees a fill the account never applies.
        order = ledger.oms.create(
            venue="VENUE_A",
            symbol="BTC-USD",
            side=Side.BUY,
            quantity=1.0,
            order_type=OrderType.LIMIT,
            time_in_force=TimeInForce.GTC,
            expected_price=50_000.0,
        )
        ledger.oms.transition(order.client_order_id, OrderStatus.SUBMITTING)
        ledger.oms.transition(order.client_order_id, OrderStatus.ACKNOWLEDGED)
        ledger.oms.apply_fill(
            FillEvent(
                created_at=ledger.clock.now_ms(),
                client_order_id=order.client_order_id,
                venue="VENUE_A",
                symbol="BTC-USD",
                side=Side.BUY,
                quantity=1.0,
                price=50_000.0,
                fee=1.0,
            )
        )
        result = ledger.marin.reconcile()
        assert not result.ok
        assert any(m.key == "fills_applied" for m in result.critical)

    def test_compaction_never_runs_on_a_failed_reconciliation(self):
        """Evidence must survive a mismatch."""
        import asyncio

        ledger = Ledger()
        # Well past the retention window, so a clean run *would* compact.
        ledger.trade(RETAINED * 3, reconcile_every=0)
        assert len(ledger.account.fill_log) > ledger.account.retained_fills

        ledger.account.cash += 1.0
        before_orders = len(ledger.oms.orders)
        before_log = len(ledger.account.fill_log)
        result = asyncio.run(ledger.marin.run())
        assert not result.ok
        assert len(ledger.oms.orders) == before_orders
        assert len(ledger.account.fill_log) == before_log
        assert ledger.account.checkpoint.fills_sealed == 0


class TestSealBoundary:
    def test_a_live_order_holds_the_boundary(self):
        """Nothing after an unfinished order may be sealed."""
        # A tiny window so compaction is forced to want everything.
        ledger = Ledger(retained=2)
        ledger.trade(10, reconcile_every=0)
        live = ledger.oms.create(
            venue="VENUE_A",
            symbol="BTC-USD",
            side=Side.BUY,
            quantity=10.0,
            order_type=OrderType.LIMIT,
            time_in_force=TimeInForce.GTC,
            expected_price=50_000.0,
        )
        ledger.oms.transition(live.client_order_id, OrderStatus.SUBMITTING)
        ledger.oms.transition(live.client_order_id, OrderStatus.ACKNOWLEDGED)
        ledger.oms.apply_fill(
            partial := FillEvent(
                created_at=ledger.clock.now_ms(),
                client_order_id=live.client_order_id,
                venue="VENUE_A",
                symbol="BTC-USD",
                side=Side.BUY,
                quantity=1.0,
                price=50_000.0,
                fee=1.0,
            )
        )
        ledger.account.apply_fill(partial)
        ledger.trade(10, reconcile_every=0)  # more fills behind the live one

        assert ledger.marin.reconcile().ok
        sealed, _ = ledger.marin.compact()

        # Everything before the live order can go; nothing from it onwards.
        assert sealed > 0, "compaction did not run, so the boundary is untested"
        assert live.client_order_id in ledger.oms.orders
        retained = {f.fill_id for f in ledger.account.fill_log}
        assert partial.fill_id in retained
        assert len(ledger.account.fill_log) >= 11, (
            "the live order's fill and the 10 behind it must all stay resident"
        )
        assert ledger.marin.reconcile().ok

    def test_no_resident_order_holds_a_sealed_fill(self, ledgers):
        """The invariant that stops compaction inventing missing fills."""
        for ledger in ledgers.values():
            unsealed = {f.fill_id for f in ledger.account.fill_log}
            for order in ledger.oms.orders.values():
                for fill in order.fills:
                    assert fill.fill_id in unsealed, (
                        f"order {order.client_order_id} is resident but holds "
                        f"sealed fill {fill.fill_id}"
                    )
