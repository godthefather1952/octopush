"""Phase 5 Remediation B — the committed-exposure derivation itself.

``Orchestrator._current_committed_exposure`` is the one place that answers
"what has this platform committed to that has not filled yet?". Everything
downstream — five gates, five headroom candidates and the published
utilization snapshot — is only as sound as that answer, so it is tested here
directly rather than only through its effects.

The orchestrator is not constructed. The method reads exactly two attributes,
``state`` and ``working_hedges``, so it is called against a stub carrying those
— which exercises the real production function while keeping each scenario to
the orders it is actually about. A scenario built by driving the simulator
would couple the answer to whatever the market happened to do, which is the
same reason the rest of this audit builds its inputs explicitly.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from apps.orchestrator.orchestrator import Orchestrator
from core.clock import ManualClock
from core.models.common import OrderType, Side, TimeInForce
from core.models.execution import OrderStatus, PaperOrder
from core.models.opportunity import Opportunity, OpportunityKind
from core.state import OpportunityRecord, SystemState
from tests.audit.rune_fixtures import SYMBOL, VENUE_A, VENUE_B, intent
from tests.conftest import START_MS

#: Every terminal status, restated so the audit does not import the production
#: constant it is checking against.
TERMINAL = [
    OrderStatus.FILLED,
    OrderStatus.CANCELLED,
    OrderStatus.REJECTED,
    OrderStatus.EXPIRED,
]

#: Everything else. UNKNOWN is in here deliberately: it means "the venue-side
#: truth is not known", not "the order is gone".
UNRESOLVED = [
    OrderStatus.CREATED,
    OrderStatus.SUBMITTING,
    OrderStatus.ACKNOWLEDGED,
    OrderStatus.OPEN,
    OrderStatus.PARTIALLY_FILLED,
    OrderStatus.CANCEL_PENDING,
    OrderStatus.UNKNOWN,
]


def order(
    client_order_id: str,
    *,
    intent_id: str | None = None,
    venue: str = VENUE_A,
    symbol: str = SYMBOL,
    side: Side = Side.BUY,
    quantity: float = 100.0,
    expected_price: float = 100.0,
    filled_quantity: float = 0.0,
    status: OrderStatus = OrderStatus.OPEN,
) -> PaperOrder:
    """A paper order in whatever state the scenario needs.

    ``status`` is assigned rather than transitioned into: this audit is about
    what a given state reserves, not about which transitions are legal, and
    the transition table is already the subject of its own suite.
    """
    built = PaperOrder(
        created_at=START_MS,
        client_order_id=client_order_id,
        intent_id=intent_id,
        venue=venue,
        symbol=symbol,
        side=side,
        order_type=OrderType.LIMIT,
        time_in_force=TimeInForce.GTC,
        quantity=quantity,
        expected_price=expected_price,
        filled_quantity=filled_quantity,
    )
    built.status = status
    return built


def platform(*orders: PaperOrder, records=(), hedges=None):
    """The two attributes the derivation reads, and nothing else."""
    state = SystemState(clock=ManualClock(START_MS))
    for one in orders:
        state.orders[one.client_order_id] = one
    for record in records:
        state.opportunities[record.opportunity.opportunity_id] = record
    return SimpleNamespace(state=state, working_hedges=dict(hedges or {}))


def entry_record(opportunity_id: str, proposed, order_ids: list[str]):
    """An opportunity record whose ``intent`` is the entry intent.

    ``Orchestrator._decide`` is the only place that sets ``record.intent``, so
    this is exactly the shape the classifier keys off.
    """
    opportunity = Opportunity(
        created_at=START_MS,
        opportunity_id=opportunity_id,
        kind=OpportunityKind.CROSS_VENUE_DISLOCATION,
        strategy=proposed.strategy,
        symbol=proposed.symbol,
        legs=list(proposed.legs),
        gross_edge_bps=proposed.gross_edge_bps,
        expires_at=START_MS + 10_000,
    )
    record = OpportunityRecord(opportunity=opportunity)
    record.intent = proposed
    record.order_ids = list(order_ids)
    return record


def committed(stub):
    return Orchestrator._current_committed_exposure(stub)


class TestWhatReleasesAndWhatDoesNot:
    @pytest.mark.parametrize("status", TERMINAL)
    def test_a_terminal_order_reserves_nothing(self, status):
        proposed = intent()
        one = order("ord-1", intent_id=proposed.intent_id, status=status)
        snapshot = committed(
            platform(one, records=[entry_record("opp-1", proposed, ["ord-1"])])
        )
        assert snapshot.is_zero, f"{status} is terminal and must release"

    @pytest.mark.parametrize("status", UNRESOLVED)
    def test_an_unresolved_order_stays_reserved(self, status):
        proposed = intent()
        one = order("ord-1", intent_id=proposed.intent_id, status=status)
        snapshot = committed(
            platform(one, records=[entry_record("opp-1", proposed, ["ord-1"])])
        )
        assert snapshot.gross_exposure == pytest.approx(10_000.0), (
            f"{status} has not resolved; the exposure is still committed"
        )

    def test_unknown_is_reserved_even_though_it_is_not_live(self):
        """The distinction the reservation turns on.

        ``is_live`` excludes UNKNOWN, so using it would release the
        reservation of an order that may be resting on the venue and may fill
        at any moment — freeing budget against risk the platform still carries.
        """
        proposed = intent()
        one = order("ord-1", intent_id=proposed.intent_id, status=OrderStatus.UNKNOWN)
        assert not one.is_live
        assert not one.is_terminal
        snapshot = committed(
            platform(one, records=[entry_record("opp-1", proposed, ["ord-1"])])
        )
        assert snapshot.gross_exposure == pytest.approx(10_000.0)


class TestHowMuchIsReserved:
    def test_a_fresh_order_reserves_the_notional_rune_authorised(self):
        """``build_plan`` sizes an entry leg as ``approved_notional /
        expected_price``, so ``remaining_quantity * expected_price`` puts the
        authorised per-leg notional back exactly."""
        proposed = intent()
        approved_notional = 12_500.0
        expected_price = 250.0
        one = order(
            "ord-1",
            intent_id=proposed.intent_id,
            quantity=approved_notional / expected_price,
            expected_price=expected_price,
        )
        snapshot = committed(
            platform(one, records=[entry_record("opp-1", proposed, ["ord-1"])])
        )
        assert snapshot.gross_exposure == pytest.approx(approved_notional)

    @pytest.mark.parametrize(
        ("filled", "expected"),
        [(0.0, 10_000.0), (25.0, 7_500.0), (50.0, 5_000.0), (99.0, 100.0)],
    )
    def test_a_partial_fill_shrinks_the_reservation(self, filled, expected):
        """The filled part has become a position the portfolio already counts;
        only the remainder is still committed. Reserving the full original
        quantity would double-count the trade against every exposure limit."""
        proposed = intent()
        one = order(
            "ord-1",
            intent_id=proposed.intent_id,
            filled_quantity=filled,
            status=OrderStatus.PARTIALLY_FILLED,
        )
        snapshot = committed(
            platform(one, records=[entry_record("opp-1", proposed, ["ord-1"])])
        )
        assert snapshot.gross_exposure == pytest.approx(expected)

    def test_an_over_filled_order_never_reserves_a_negative_amount(self):
        """``remaining_quantity`` floors at zero, so a reservation can only
        shrink to nothing — it can never become a credit against the limits."""
        proposed = intent()
        one = order("ord-1", intent_id=proposed.intent_id, filled_quantity=150.0)
        snapshot = committed(
            platform(one, records=[entry_record("opp-1", proposed, ["ord-1"])])
        )
        assert snapshot.gross_exposure == pytest.approx(0.0)


class TestDirectionAndKeys:
    def test_net_exposure_is_signed_by_side(self):
        proposed = intent()
        record = entry_record("opp-1", proposed, ["buy", "sell"])
        snapshot = committed(
            platform(
                order("buy", intent_id=proposed.intent_id, side=Side.BUY),
                order(
                    "sell",
                    intent_id=proposed.intent_id,
                    side=Side.SELL,
                    venue=VENUE_B,
                ),
                records=[record],
            )
        )
        assert snapshot.gross_exposure == pytest.approx(20_000.0)
        assert snapshot.net_exposure == pytest.approx(0.0), (
            "a balanced pair in flight adds no net delta, exactly as two "
            "filled positions would not"
        )

    def test_two_one_sided_orders_stack(self):
        proposed = intent()
        record = entry_record("opp-1", proposed, ["a", "b"])
        snapshot = committed(
            platform(
                order("a", intent_id=proposed.intent_id, side=Side.BUY),
                order("b", intent_id=proposed.intent_id, side=Side.BUY),
                records=[record],
            )
        )
        assert snapshot.net_exposure == pytest.approx(20_000.0)

    def test_venue_and_position_are_keyed_as_the_portfolio_keys_them(self):
        """``position_exposure`` must use ``venue:symbol``, because that is
        how ``PortfolioState.positions`` and ``legs_per_position`` key, and a
        reservation filed under a different key adds to nothing."""
        proposed = intent()
        record = entry_record("opp-1", proposed, ["a", "b", "c"])
        snapshot = committed(
            platform(
                order("a", intent_id=proposed.intent_id),
                order("b", intent_id=proposed.intent_id),
                order("c", intent_id=proposed.intent_id, venue=VENUE_B),
                records=[record],
            )
        )
        assert snapshot.venue_exposure == pytest.approx(
            {VENUE_A: 20_000.0, VENUE_B: 10_000.0}
        )
        assert snapshot.position_exposure == pytest.approx(
            {
                f"{VENUE_A}:{SYMBOL}": 20_000.0,
                f"{VENUE_B}:{SYMBOL}": 10_000.0,
            }
        )


class TestOnlyEntriesAreCommitments:
    def test_an_exit_order_is_not_a_new_commitment(self):
        """``_submit_exit`` replaces ``record.order_ids`` with the exit's
        orders and never stores its intent on the record, so an exit order is
        tracked but is not the record's entry intent. Counting it would make
        the platform's own risk reduction look like risk taking."""
        proposed = intent()
        record = entry_record("opp-1", proposed, ["exit-1"])
        snapshot = committed(
            platform(order("exit-1", intent_id="int-exit", side=Side.SELL), records=[record])
        )
        assert snapshot.is_zero

    def test_a_hedge_order_is_not_a_new_commitment(self):
        snapshot = committed(
            platform(
                order("hedge-1", intent_id="int-hedge", side=Side.SELL),
                hedges={SYMBOL: ["hedge-1"]},
            )
        )
        assert snapshot.is_zero

    def test_an_entry_and_an_exit_side_by_side(self):
        """Only the entry reserves; both are unresolved."""
        entry = intent(opportunity_id="opp-1")
        other = intent(opportunity_id="opp-2")
        snapshot = committed(
            platform(
                order("entry-1", intent_id=entry.intent_id),
                order("exit-1", intent_id="int-exit", side=Side.SELL),
                records=[
                    entry_record("opp-1", entry, ["entry-1"]),
                    entry_record("opp-2", other, ["exit-1"]),
                ],
            )
        )
        assert snapshot.gross_exposure == pytest.approx(10_000.0)
        assert snapshot.net_exposure == pytest.approx(10_000.0)


class TestTheFallbackIsFailClosed:
    def test_an_untracked_order_is_reserved_rather_than_ignored(self):
        """An order nothing still references — its opportunity has aged out of
        the bounded history, say — cannot be shown to be exposure-reducing.
        Reserving it over-reserves and blocks; skipping it under-reserves and
        authorises. Only one of those is a safe default for a hard limit."""
        snapshot = committed(platform(order("orphan", intent_id="int-gone")))
        assert snapshot.gross_exposure == pytest.approx(10_000.0)

    def test_an_order_with_no_intent_id_is_reserved(self):
        snapshot = committed(platform(order("no-intent")))
        assert snapshot.gross_exposure == pytest.approx(10_000.0)

    def test_the_fallback_still_releases_on_terminality(self):
        """Fail-closed is not fail-forever: an unclassifiable order that has
        reached a terminal state has nothing left to reserve."""
        snapshot = committed(
            platform(order("orphan", intent_id="int-gone", status=OrderStatus.CANCELLED))
        )
        assert snapshot.is_zero


class TestTheSnapshotIsDerivedNotStored:
    def test_the_same_orders_always_produce_the_same_snapshot(self):
        """No accumulation, no clock, no order age: calling twice in a row
        cannot drift, which is the property a mutable ledger cannot offer."""
        proposed = intent()
        stub = platform(
            order("a", intent_id=proposed.intent_id),
            order("b", intent_id=proposed.intent_id, side=Side.SELL, venue=VENUE_B),
            records=[entry_record("opp-1", proposed, ["a", "b"])],
        )
        first = committed(stub)
        second = committed(stub)
        assert first == second

    def test_removing_the_order_removes_the_reservation(self):
        """Release is a consequence of the order state, not of a call the
        platform has to remember to make."""
        proposed = intent()
        stub = platform(
            order("a", intent_id=proposed.intent_id),
            records=[entry_record("opp-1", proposed, ["a"])],
        )
        assert committed(stub).gross_exposure == pytest.approx(10_000.0)
        stub.state.orders["a"].status = OrderStatus.FILLED
        assert committed(stub).is_zero

    def test_an_empty_platform_reserves_nothing(self):
        assert committed(platform()).is_zero


# ======================================================================
# P5-18 — worst-case unhedged leg risk, derived from the same order walk
# ======================================================================


class TestPendingUnhedgedFillRisk:
    """``unhedged_fill_risk`` is a bound on a residual that does not exist yet.

    Not actual unhedged exposure — that is OKAPI's measurement of the FILLED
    book — but the largest residual the orders currently working could produce
    if their legs land in the most adverse order. Accumulated per symbol by
    side, on the same walk over the same unresolved entry orders, so it cannot
    describe a different set of orders from the rest of the snapshot.
    """

    def test_a_balanced_pair_still_bounds_one_whole_leg(self):
        """The heart of P5-18. Net exposure nets to zero; the fill-sequence
        bound does not, because between the first fill and the second the book
        is one-sided by a full leg."""
        proposed = intent()
        snapshot = committed(
            platform(
                order("buy", intent_id=proposed.intent_id, side=Side.BUY),
                order(
                    "sell",
                    intent_id=proposed.intent_id,
                    side=Side.SELL,
                    venue=VENUE_B,
                ),
                records=[entry_record("opp-1", proposed, ["buy", "sell"])],
            )
        )
        assert snapshot.net_exposure == pytest.approx(0.0)
        assert snapshot.gross_exposure == pytest.approx(20_000.0)
        assert snapshot.unhedged_fill_risk == pytest.approx(10_000.0), (
            "one whole leg, not the net (zero) and not the gross (20,000)"
        )

    def test_the_worse_side_wins_on_one_symbol(self):
        """§21. 8,000 of BUY and 5,000 of SELL still working on one symbol
        bounds at 8,000 — not 3,000 net, and not 13,000 gross. It is a
        fill-SEQUENCE bound, which is neither of those things."""
        proposed = intent()
        record = entry_record("opp-1", proposed, ["b1", "b2", "s1"])
        snapshot = committed(
            platform(
                order("b1", intent_id=proposed.intent_id, quantity=50.0),
                order("b2", intent_id=proposed.intent_id, quantity=30.0),
                order(
                    "s1",
                    intent_id=proposed.intent_id,
                    side=Side.SELL,
                    quantity=50.0,
                    venue=VENUE_B,
                ),
                records=[record],
            )
        )
        assert snapshot.unhedged_fill_risk == pytest.approx(8_000.0)

    def test_symbols_are_summed_because_each_can_go_one_sided(self):
        """§22. BTC balanced at 5k a side and ETH balanced at 4k a side bounds
        at 9k: both symbols can be transiently one-sided at once, and
        ``Okapi.total_unhedged`` sums absolute residuals per symbol."""
        proposed = intent()
        ids = ["btc-b", "btc-s", "eth-b", "eth-s"]
        snapshot = committed(
            platform(
                order("btc-b", intent_id=proposed.intent_id, quantity=50.0),
                order(
                    "btc-s",
                    intent_id=proposed.intent_id,
                    side=Side.SELL,
                    quantity=50.0,
                    venue=VENUE_B,
                ),
                order(
                    "eth-b",
                    intent_id=proposed.intent_id,
                    symbol="ETH-USD",
                    quantity=40.0,
                ),
                order(
                    "eth-s",
                    intent_id=proposed.intent_id,
                    symbol="ETH-USD",
                    side=Side.SELL,
                    quantity=40.0,
                    venue=VENUE_B,
                ),
                records=[entry_record("opp-1", proposed, ids)],
            )
        )
        assert snapshot.unhedged_fill_risk == pytest.approx(9_000.0)

    def test_the_same_symbol_across_venues_still_offsets(self):
        """Unhedged residual is a delta measured across venues, so a BUY on one
        venue and a SELL on another are two positions but one symbol — they
        cancel for this bound even though they never cancel for the position
        limit."""
        proposed = intent()
        snapshot = committed(
            platform(
                order("a", intent_id=proposed.intent_id, venue=VENUE_A),
                order(
                    "b", intent_id=proposed.intent_id, venue=VENUE_B, side=Side.SELL
                ),
                records=[entry_record("opp-1", proposed, ["a", "b"])],
            )
        )
        assert len(snapshot.position_exposure) == 2
        assert snapshot.unhedged_fill_risk == pytest.approx(10_000.0)

    @pytest.mark.parametrize(
        ("filled", "expected"),
        [(0.0, 10_000.0), (25.0, 7_500.0), (50.0, 5_000.0), (100.0, 0.0)],
    )
    def test_a_partial_fill_shrinks_the_pending_bound(self, filled, expected):
        """§I. The filled part is no longer pending — it is an actual residual
        OKAPI now measures. The bound shrinks by exactly what the measurement
        gains, which is the same derived-state handoff P5-1 relies on."""
        proposed = intent()
        one = order(
            "ord-1",
            intent_id=proposed.intent_id,
            filled_quantity=filled,
            status=OrderStatus.PARTIALLY_FILLED,
        )
        snapshot = committed(
            platform(one, records=[entry_record("opp-1", proposed, ["ord-1"])])
        )
        assert snapshot.unhedged_fill_risk == pytest.approx(expected)

    @pytest.mark.parametrize("status", TERMINAL)
    def test_a_terminal_entry_order_releases_the_bound(self, status):
        """§J. Nothing left that can fill, so nothing left that can go
        one-sided."""
        proposed = intent()
        one = order("ord-1", intent_id=proposed.intent_id, status=status)
        snapshot = committed(
            platform(one, records=[entry_record("opp-1", proposed, ["ord-1"])])
        )
        assert snapshot.unhedged_fill_risk == pytest.approx(0.0)

    def test_an_unknown_entry_order_keeps_its_bound(self):
        """§K. UNKNOWN means the venue-side truth is not known, not that the
        order is gone — it may still fill and go one-sided."""
        proposed = intent()
        one = order("ord-1", intent_id=proposed.intent_id, status=OrderStatus.UNKNOWN)
        snapshot = committed(
            platform(one, records=[entry_record("opp-1", proposed, ["ord-1"])])
        )
        assert snapshot.unhedged_fill_risk == pytest.approx(10_000.0)

    def test_exits_and_hedges_consume_no_pending_budget(self):
        """§L. They reduce exposure. Charging them against the entry fill-risk
        budget would make the platform's own risk reduction look like risk
        taking — and could stop it hedging its way out of a breach."""
        proposed = intent()
        snapshot = committed(
            platform(
                order("exit-1", intent_id="int-exit", side=Side.SELL),
                order("hedge-1", intent_id="int-hedge", side=Side.SELL),
                records=[entry_record("opp-1", proposed, ["exit-1"])],
                hedges={SYMBOL: ["hedge-1"]},
            )
        )
        assert snapshot.unhedged_fill_risk == pytest.approx(0.0)
        assert snapshot.is_zero

    def test_an_unclassifiable_order_still_contributes(self):
        """The fail-closed fallback covers this dimension too. An order
        conservatively counted as an entry commitment must not vanish from the
        fill-risk bound — that would reserve gross while ignoring the residual
        the same order could create."""
        snapshot = committed(platform(order("orphan", intent_id="int-gone")))
        assert snapshot.gross_exposure == pytest.approx(10_000.0)
        assert snapshot.unhedged_fill_risk == pytest.approx(10_000.0)

    def test_an_empty_platform_has_no_pending_fill_risk(self):
        assert committed(platform()).unhedged_fill_risk == pytest.approx(0.0)
