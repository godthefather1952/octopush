"""Phase 5 — H3 and H4: what does an authorised-but-unfilled trade reserve?

RUNE reads ``PortfolioState``, and a portfolio only moves when a fill lands.
Between authorisation and fill a trade is real risk the platform has already
committed to — its orders are live and can execute at any moment.

The orchestrator authorises opportunities one after another inside a single
``_seek`` pass, with only a ``bus.drain()`` between them — no settlement. So
the question is not hypothetical: two opportunities on two symbols can both be
evaluated against the same empty portfolio in one tick.

These tests state the invariant a hard limit is supposed to provide: **the sum
of what is already held and what has been authorised must not exceed the
limit.** A limit that only holds until the second concurrent trade is not a
hard limit.

STATUS AFTER PHASE 5 REMEDIATION B
==================================
``Orchestrator.working_notional`` used to be the platform's only reservation
and it fed exactly one gate, MAX_STRATEGY_EXPOSURE; MAX_GROSS_EXPOSURE,
MAX_POSITION_NOTIONAL, MAX_VENUE_EXPOSURE, MAX_NET_EXPOSURE and MAX_LEVERAGE
all read the filled portfolio alone, which is finding P5-1.

They no longer do. ``Orchestrator._current_committed_exposure`` derives a
:class:`~core.models.risk.CommittedExposure` snapshot from live order state
each time it is asked, and ``RiskContext.committed_exposure`` carries it into
every one of those gates and into the matching headroom candidate. The
invariant assertions below are unchanged — they are the safety property, not a
record of the defect — and the harness now carries the committed snapshot
forward between authorisations exactly as production does between the
authorisations inside one ``_seek`` pass.
"""

from __future__ import annotations

import inspect

import pytest

from apps.orchestrator.orchestrator import Orchestrator
from core.config import RiskLimits
from core.models.common import Side
from core.models.risk import CommittedExposure, RiskVerdict
from tests.audit.rune_fixtures import (
    SYMBOL,
    VENUE_A,
    VENUE_B,
    blocking_names,
    context,
    core,
    gate_named,
    has_gate,
    intent,
    leg,
    portfolio,
    portfolio_with,
    position,
)
from tests.conftest import START_MS


def commit(reserved: CommittedExposure, proposed, approved_notional: float):
    """The committed snapshot after ``proposed`` is authorised and not filled.

    Mirrors ``Orchestrator._current_committed_exposure`` for the case where
    every planned order is fresh and untouched: VESKA sizes an entry leg as
    ``approved_notional / expected_price``, so each leg's
    ``remaining_quantity * expected_price`` is exactly ``approved_notional``.
    Written from that definition rather than by calling production, so the two
    can be compared instead of production being compared with itself.
    """
    gross = reserved.gross_exposure
    net = reserved.net_exposure
    by_venue = dict(reserved.venue_exposure)
    by_position = dict(reserved.position_exposure)
    for one in proposed.legs:
        gross += approved_notional
        net += approved_notional * one.side.sign
        by_venue[one.venue] = by_venue.get(one.venue, 0.0) + approved_notional
        key = f"{one.venue}:{one.symbol}"
        by_position[key] = by_position.get(key, 0.0) + approved_notional
    return CommittedExposure(
        gross_exposure=gross,
        net_exposure=net,
        venue_exposure=by_venue,
        position_exposure=by_position,
    )


def sequential_authorisations(
    limits: RiskLimits,
    proposals: list,
    *,
    book=None,
    reserve_strategy: bool = True,
    reserve_committed: bool = True,
):
    """Authorise ``proposals`` back to back against an unchanging portfolio.

    Models the orchestrator exactly: the portfolio does not move (nothing has
    filled), and two things are carried forward — the strategy reservation,
    stored gross across the opportunity's legs as ``Orchestrator._decide`` does
    since the P5-2 fix, and the committed-exposure snapshot every exposure gate
    now reads since the P5-1 fix.

    ``reserve_committed=False`` reproduces the pre-remediation platform, where
    nothing but the strategy budget survived between two authorisations in one
    tick. It exists so a regression to that behaviour shows up as a failing
    comparison rather than as a quietly larger position.
    """
    engine = core(limits)
    working: dict[str, float] = {}
    reserved = CommittedExposure()
    decisions = []
    for proposed in proposals:
        decision = engine.evaluate(
            proposed,
            context(
                portfolio=book or portfolio(),
                strategy_exposure=sum(working.values()) if reserve_strategy else 0.0,
                committed_exposure=reserved if reserve_committed else CommittedExposure(),
                max_economical_notional=1_000_000.0,
            ),
            START_MS,
        )
        decisions.append(decision)
        if decision.approved:
            working[proposed.opportunity_id] = decision.approved_notional * len(
                proposed.legs
            )
            reserved = commit(reserved, proposed, decision.approved_notional)
    return decisions


def authorised_gross(decisions) -> float:
    """Gross exposure every approved decision commits the platform to."""
    return sum(d.approved_notional * 2 for d in decisions if d.approved)


class TestTheOrchestratorAuthorisesWithoutSettlingFirst:
    """The premise, read off production statically."""

    def test_seek_drives_each_opportunity_to_a_decision_in_one_pass(self):
        source = inspect.getsource(Orchestrator._seek)
        assert "for opportunity in self.detector.detect(" in source
        assert "await self._evaluate_or_defer(" in source
        assert "await self._settle(" not in source, (
            "if _seek settled between opportunities this audit's premise would "
            "not hold"
        )

    def test_the_risk_context_reads_the_tick_portfolio(self):
        source = inspect.getsource(Orchestrator._risk_check)
        assert "portfolio=portfolio," in source

    def test_the_strategy_reservation_still_comes_from_one_helper(self):
        """The P5-2 wiring is undisturbed: ``working_notional`` is read exactly
        once, through ``_current_strategy_exposure()``, and never inline."""
        source = inspect.getsource(Orchestrator._risk_check)
        reserved = [
            line.strip()
            for line in source.splitlines()
            if "_current_strategy_exposure" in line or "working_notional" in line
        ]
        assert len(reserved) == 1, f"reservation wiring changed: {reserved}"
        assert reserved[0] == "strategy_exposure=self._current_strategy_exposure(),"

    def test_the_exposure_gates_now_receive_a_pending_reservation(self):
        """Stated as the shape of RiskContext itself.

        This is the assertion the P5-1 remediation inverts. It used to record
        that the only exposure input was ``portfolio``, which moves only on a
        fill; the context now also carries the committed snapshot the gross,
        venue, position, net and leverage gates read.
        """
        source = inspect.getsource(Orchestrator._risk_check)
        context_block = source.split("ctx = RiskContext(")[1].split(")\n")[0]
        assert "portfolio=portfolio," in context_block
        assert context_block.count("_current_strategy_exposure") == 1
        assert "working_notional" not in context_block
        assert (
            "committed_exposure=self._current_committed_exposure()," in context_block
        )

    def test_the_committed_snapshot_is_derived_from_order_state(self):
        """Not a second ledger: the snapshot is computed from
        ``self.state.orders`` on demand, and releases on terminality rather
        than on any bookkeeping call the platform has to remember to make."""
        source = inspect.getsource(Orchestrator._current_committed_exposure)
        assert "for order in self.state.orders.values():" in source
        assert "if order.is_terminal:" in source
        assert "order.remaining_quantity * order.expected_price" in source
        assert "order.is_live" not in source, (
            "is_live excludes UNKNOWN, whose venue-side truth is not known; "
            "an UNKNOWN order must stay reserved"
        )
        assert "fill.price" not in source and "fill.quantity" not in source


class TestGrossExposureUnderConcurrentAuthorisation:
    """H3 — MAX_GROSS_EXPOSURE."""

    LIMITS = RiskLimits(
        max_gross_exposure=100_000.0,
        max_position_notional=50_000.0,
        max_order_notional=25_000.0,
        max_venue_exposure=1_000_000.0,
        max_net_exposure=1_000_000.0,
        max_strategy_exposure=1_000_000.0,
        max_leverage=100.0,
    )

    @staticmethod
    def _proposals(count: int):
        """Distinct symbols, so nothing but the shared limits couples them."""
        return [
            intent(
                opportunity_id=f"opp-{i}",
                correlation_id=f"opp-{i}",
                symbol=f"SYM{i}-USD",
                legs=[
                    leg(VENUE_A, Side.BUY, symbol=f"SYM{i}-USD"),
                    leg(VENUE_B, Side.SELL, symbol=f"SYM{i}-USD"),
                ],
                notional=25_000.0,
            )
            for i in range(count)
        ]

    def test_one_trade_alone_fits(self):
        decisions = sequential_authorisations(self.LIMITS, self._proposals(1))
        assert decisions[0].verdict is RiskVerdict.APPROVED
        assert authorised_gross(decisions) == pytest.approx(50_000.0)

    def test_two_trades_exactly_fill_the_gross_budget(self):
        decisions = sequential_authorisations(self.LIMITS, self._proposals(2))
        assert all(d.approved for d in decisions)
        assert authorised_gross(decisions) == pytest.approx(100_000.0)

    @pytest.mark.parametrize("count", [1, 2, 3, 4])
    def test_authorised_gross_never_exceeds_the_gross_limit(self, count):
        decisions = sequential_authorisations(self.LIMITS, self._proposals(count))
        committed = authorised_gross(decisions)
        approved = sum(1 for d in decisions if d.approved)
        assert committed <= self.LIMITS.max_gross_exposure + 1e-6, (
            f"{approved} of {count} trades authorised without settling, "
            f"committing {committed} of gross exposure against a "
            f"{self.LIMITS.max_gross_exposure} limit. "
            "The portfolio each decision read was empty, because no fill had "
            "landed yet."
        )

    def test_the_third_trade_finds_the_budget_already_committed(self):
        """Diagnostic: shows what the third decision actually looked at.

        Two trades commit the whole 100,000 budget, so the third has no
        headroom at all and is refused before the gate list runs — the
        MIN_TRADE_NOTIONAL short-circuit, which is why there is no
        MAX_GROSS_EXPOSURE gate to inspect on this decision. Before the P5-1
        fix the same call showed ``observed == 50,000``: the third decision
        projected only its own two legs, because the 100,000 already
        authorised was in no portfolio it could read.
        """
        decisions = sequential_authorisations(self.LIMITS, self._proposals(3))
        assert decisions[2].verdict is RiskVerdict.REJECTED
        assert decisions[2].reason_codes == ["MIN_TRADE_NOTIONAL"]
        assert not has_gate(decisions[2], "MAX_GROSS_EXPOSURE")

    def test_the_second_trade_sees_the_first_in_its_projection(self):
        """The gate the second decision runs is measured against filled +
        committed, and says so in its detail."""
        decisions = sequential_authorisations(self.LIMITS, self._proposals(2))
        check = gate_named(decisions[1], "MAX_GROSS_EXPOSURE")
        assert check.observed == pytest.approx(100_000.0)
        assert "50,000.00 committed" in check.detail

    def test_without_the_committed_snapshot_the_limit_is_breached(self):
        """The finding itself, kept executable.

        Same three proposals, with the committed snapshot suppressed: this is
        the platform as it behaved before the remediation, and it authorises
        150,000 against a 100,000 limit. If this ever stops breaching, the
        harness has stopped modelling the defect and the passing tests above
        prove less than they claim.
        """
        decisions = sequential_authorisations(
            self.LIMITS, self._proposals(3), reserve_committed=False
        )
        assert authorised_gross(decisions) > self.LIMITS.max_gross_exposure


class TestVenueExposureUnderConcurrentAuthorisation:
    """H3 — MAX_VENUE_EXPOSURE. Every leg of every cross-venue trade lands on
    one of two venues, so venue concentration is where concurrency bites
    hardest."""

    LIMITS = RiskLimits(
        max_venue_exposure=40_000.0,
        max_gross_exposure=1_000_000.0,
        max_position_notional=50_000.0,
        max_order_notional=25_000.0,
        max_net_exposure=1_000_000.0,
        max_strategy_exposure=1_000_000.0,
        max_leverage=100.0,
    )

    @staticmethod
    def _proposals(count: int):
        return [
            intent(
                opportunity_id=f"opp-{i}",
                correlation_id=f"opp-{i}",
                symbol=f"SYM{i}-USD",
                legs=[
                    leg(VENUE_A, Side.BUY, symbol=f"SYM{i}-USD"),
                    leg(VENUE_B, Side.SELL, symbol=f"SYM{i}-USD"),
                ],
                notional=25_000.0,
            )
            for i in range(count)
        ]

    @pytest.mark.parametrize("count", [1, 2, 3])
    def test_authorised_venue_exposure_never_exceeds_the_venue_limit(self, count):
        decisions = sequential_authorisations(self.LIMITS, self._proposals(count))
        # Every trade puts its full per-leg notional on VENUE_A.
        on_venue_a = sum(d.approved_notional for d in decisions if d.approved)
        assert on_venue_a <= self.LIMITS.max_venue_exposure + 1e-6, (
            f"{on_venue_a} authorised onto VENUE_A against a "
            f"{self.LIMITS.max_venue_exposure} venue limit across {count} "
            "concurrent authorisations"
        )

    def test_the_second_trade_is_reduced_to_the_venues_remaining_room(self):
        """Reduced, not refused. The whole point of sizing before gating is
        that a trade which no longer fits whole is cut to what does — 25,000
        is committed on VENUE_A, so 15,000 of its 40,000 remains."""
        decisions = sequential_authorisations(self.LIMITS, self._proposals(2))
        assert decisions[0].verdict is RiskVerdict.APPROVED
        assert decisions[0].approved_notional == pytest.approx(25_000.0)
        assert decisions[1].verdict is RiskVerdict.APPROVED_REDUCED
        assert decisions[1].approved_notional == pytest.approx(15_000.0)

    def test_the_third_trade_finds_the_venue_full(self):
        decisions = sequential_authorisations(self.LIMITS, self._proposals(3))
        assert decisions[2].verdict is RiskVerdict.REJECTED
        assert decisions[2].reason_codes == ["MIN_TRADE_NOTIONAL"]

    def test_two_legs_on_one_venue_consume_that_venue_twice(self):
        """Grouping (P5-11) and committed exposure (P5-1) have to compose: a
        second trade routing BOTH legs to VENUE_A must see the first trade's
        commitment there and still count its own two legs against it."""
        both_on_a = [
            intent(
                opportunity_id=f"opp-{i}",
                correlation_id=f"opp-{i}",
                symbol=f"SYM{i}-USD",
                legs=[
                    leg(VENUE_A, Side.BUY, symbol=f"SYM{i}-USD"),
                    leg(VENUE_A, Side.SELL, symbol=f"SYM{i}-USD"),
                ],
                notional=25_000.0,
            )
            for i in range(2)
        ]
        decisions = sequential_authorisations(self.LIMITS, both_on_a)
        on_venue_a = sum(d.approved_notional * 2 for d in decisions if d.approved)
        assert on_venue_a <= self.LIMITS.max_venue_exposure + 1e-6, (
            f"{on_venue_a} authorised onto VENUE_A against a "
            f"{self.LIMITS.max_venue_exposure} venue limit"
        )


class TestPositionExposureUnderConcurrentAuthorisation:
    """H3 — MAX_POSITION_NOTIONAL, with two opportunities on the SAME
    venue/symbol. The detector holds one opportunity per symbol in flight, so
    this is the generic-hard-risk-layer case rather than today's strategy."""

    LIMITS = RiskLimits(
        max_position_notional=30_000.0,
        max_order_notional=25_000.0,
        max_gross_exposure=1_000_000.0,
        max_venue_exposure=1_000_000.0,
        max_net_exposure=1_000_000.0,
        max_strategy_exposure=1_000_000.0,
        max_leverage=100.0,
    )

    def test_two_authorisations_on_one_position_stay_within_its_limit(self):
        proposals = [
            intent(opportunity_id="opp-0", correlation_id="opp-0", notional=25_000.0),
            intent(opportunity_id="opp-1", correlation_id="opp-1", notional=25_000.0),
        ]
        decisions = sequential_authorisations(self.LIMITS, proposals)
        committed = sum(d.approved_notional for d in decisions if d.approved)
        assert committed <= self.LIMITS.max_position_notional + 1e-6, (
            f"{committed} authorised onto one venue/symbol against a "
            f"{self.LIMITS.max_position_notional} position limit"
        )

    def test_the_second_is_reduced_to_what_the_position_still_allows(self):
        """25,000 of the 30,000 position budget is already working, so the
        second authorisation is cut to the 5,000 that remains rather than
        being allowed a second full 25,000 onto the same position."""
        proposals = [
            intent(opportunity_id="opp-0", correlation_id="opp-0", notional=25_000.0),
            intent(opportunity_id="opp-1", correlation_id="opp-1", notional=25_000.0),
        ]
        decisions = sequential_authorisations(self.LIMITS, proposals)
        assert decisions[0].approved_notional == pytest.approx(25_000.0)
        assert decisions[1].verdict is RiskVerdict.APPROVED_REDUCED
        assert decisions[1].approved_notional == pytest.approx(5_000.0)
        check = gate_named(decisions[1], "MAX_POSITION_NOTIONAL")
        assert check.observed == pytest.approx(30_000.0)
        assert "25,000.00 committed" in check.detail

    def test_a_held_position_and_a_committed_one_add_up(self):
        """Filled and committed exposure on the SAME position compose. A
        10,000 position already held plus 20,000 working leaves nothing of a
        30,000 limit, and the next trade must find that out."""
        book = portfolio_with(position(VENUE_A, quantity=100.0))
        assert book.positions[f"{VENUE_A}:{SYMBOL}"].notional == pytest.approx(10_000.0)
        proposals = [
            intent(opportunity_id="opp-0", correlation_id="opp-0", notional=25_000.0),
            intent(opportunity_id="opp-1", correlation_id="opp-1", notional=25_000.0),
        ]
        decisions = sequential_authorisations(self.LIMITS, proposals, book=book)
        assert decisions[0].approved_notional == pytest.approx(20_000.0)
        assert decisions[1].verdict is RiskVerdict.REJECTED
        assert decisions[1].reason_codes == ["MIN_TRADE_NOTIONAL"]

    def test_without_the_committed_snapshot_the_position_limit_is_breached(self):
        """The finding, kept executable: suppressing the snapshot authorises
        50,000 onto a position limited to 30,000."""
        proposals = [
            intent(opportunity_id="opp-0", correlation_id="opp-0", notional=25_000.0),
            intent(opportunity_id="opp-1", correlation_id="opp-1", notional=25_000.0),
        ]
        decisions = sequential_authorisations(
            self.LIMITS, proposals, reserve_committed=False
        )
        committed = sum(d.approved_notional for d in decisions if d.approved)
        assert committed > self.LIMITS.max_position_notional


class TestNetExposureUnderConcurrentAuthorisation:
    """H3 — MAX_NET_EXPOSURE, the dimension where the sign matters.

    Gross, venue and position exposure are unsigned, so a reservation there can
    only ever be additive. Net exposure is signed, and a committed reservation
    has to carry its direction: two working one-sided BUYs stack, while a
    working BUY and a working SELL of the same size cancel exactly as two
    filled positions would.
    """

    LIMITS = RiskLimits(
        max_net_exposure=30_000.0,
        max_order_notional=25_000.0,
        max_position_notional=1_000_000.0,
        max_gross_exposure=1_000_000.0,
        max_venue_exposure=1_000_000.0,
        max_strategy_exposure=1_000_000.0,
        max_leverage=100.0,
    )

    @staticmethod
    def _one_sided(index: int, side: Side = Side.BUY):
        """A single-leg intent: nothing offsets it, so it is all net delta."""
        return intent(
            opportunity_id=f"opp-{index}",
            correlation_id=f"opp-{index}",
            symbol=f"SYM{index}-USD",
            legs=[leg(VENUE_A, side, symbol=f"SYM{index}-USD")],
            notional=25_000.0,
        )

    def test_two_one_sided_buys_cannot_exceed_the_net_limit(self):
        """The invariant. 25,000 is approved and unfilled; the second BUY may
        add at most the 5,000 that keeps net inside 30,000."""
        decisions = sequential_authorisations(
            self.LIMITS, [self._one_sided(0), self._one_sided(1)]
        )
        net = sum(d.approved_notional for d in decisions if d.approved)
        assert net <= self.LIMITS.max_net_exposure + 1e-6, (
            f"{net} of one-sided net delta authorised against a "
            f"{self.LIMITS.max_net_exposure} net limit while nothing had filled"
        )
        assert decisions[1].verdict in (
            RiskVerdict.APPROVED_REDUCED,
            RiskVerdict.REJECTED,
        )
        if decisions[1].approved:
            assert decisions[1].approved_notional == pytest.approx(5_000.0)

    def test_a_third_one_sided_buy_is_refused_outright(self):
        decisions = sequential_authorisations(
            self.LIMITS, [self._one_sided(i) for i in range(3)]
        )
        assert decisions[2].verdict is RiskVerdict.REJECTED
        assert decisions[2].reason_codes == ["MIN_TRADE_NOTIONAL"]

    def test_an_opposing_commitment_offsets_rather_than_stacks(self):
        """The reservation is signed, not a magnitude. A working SELL after a
        working BUY brings committed net back to zero, so the trade after them
        has its full room — over-reserving here would block a genuinely
        risk-neutral book."""
        decisions = sequential_authorisations(
            self.LIMITS,
            [
                self._one_sided(0, Side.BUY),
                self._one_sided(1, Side.SELL),
                self._one_sided(2, Side.BUY),
            ],
        )
        assert all(d.approved for d in decisions)
        assert decisions[2].approved_notional == pytest.approx(25_000.0)

    def test_a_balanced_two_leg_commitment_adds_no_net_delta(self):
        """The shape the platform actually trades: a cross-venue BUY/SELL pair
        nets to zero, so committing one must not consume net headroom."""
        balanced = intent(
            opportunity_id="opp-0", correlation_id="opp-0", notional=25_000.0
        )
        decisions = sequential_authorisations(
            self.LIMITS, [balanced, self._one_sided(1)]
        )
        assert decisions[0].approved_notional == pytest.approx(25_000.0)
        assert decisions[1].verdict is RiskVerdict.APPROVED
        assert decisions[1].approved_notional == pytest.approx(25_000.0)

    def test_without_the_committed_snapshot_the_net_limit_is_breached(self):
        """The finding, kept executable: two one-sided BUYs each read a net
        exposure of zero and together commit 50,000 against a 30,000 limit."""
        decisions = sequential_authorisations(
            self.LIMITS,
            [self._one_sided(0), self._one_sided(1)],
            reserve_committed=False,
        )
        net = sum(d.approved_notional for d in decisions if d.approved)
        assert net > self.LIMITS.max_net_exposure


class TestLeverageUnderConcurrentAuthorisation:
    """H3 — MAX_LEVERAGE. Equity does not move on authorisation either, so the
    denominator is as static as the numerator."""

    LIMITS = RiskLimits(
        max_leverage=1.0,
        max_gross_exposure=1_000_000.0,
        max_position_notional=100_000.0,
        max_order_notional=25_000.0,
        max_venue_exposure=1_000_000.0,
        max_net_exposure=1_000_000.0,
        max_strategy_exposure=1_000_000.0,
    )

    @pytest.mark.parametrize("count", [1, 2, 3])
    def test_authorised_leverage_never_exceeds_the_limit(self, count):
        proposals = [
            intent(
                opportunity_id=f"opp-{i}",
                correlation_id=f"opp-{i}",
                symbol=f"SYM{i}-USD",
                legs=[
                    leg(VENUE_A, Side.BUY, symbol=f"SYM{i}-USD"),
                    leg(VENUE_B, Side.SELL, symbol=f"SYM{i}-USD"),
                ],
                notional=25_000.0,
            )
            for i in range(count)
        ]
        decisions = sequential_authorisations(self.LIMITS, proposals)
        committed = authorised_gross(decisions)
        equity = portfolio().equity
        assert committed / equity <= self.LIMITS.max_leverage + 1e-9, (
            f"{committed} of gross exposure authorised against {equity} of "
            f"equity — {committed / equity:.2f}x against a "
            f"{self.LIMITS.max_leverage}x limit"
        )


class TestOpenOrderCapacity:
    """H4 — does the open-order gate account for the orders this intent creates?

    ``build_plan`` emits one order per leg, so the question a capacity limit
    asks is ``current + incoming <= max_open_orders``, not ``current <
    max_open_orders``. The old form let 19 live orders admit a two-leg trade
    and reach 21 (P5-4).
    """

    LIMITS = RiskLimits(max_open_orders=20)

    def test_veska_creates_one_order_per_leg(self):
        """The premise, read off production."""
        from execution.veska.engine import Veska

        source = inspect.getsource(Veska.build_plan)
        assert "for leg in intent.legs:" in source
        assert "orders.append(" in source

    def test_the_gate_reports_the_projected_count(self):
        """§21 — a rejection must be readable. ``observed`` is what the
        platform would hold after planning, so "21 against 20" explains itself
        where "19 against 20" did not."""
        decision = core(self.LIMITS).evaluate(
            intent(), context(open_orders=19), START_MS
        )
        check = gate_named(decision, "MAX_OPEN_ORDERS")
        assert check.observed == pytest.approx(21.0)
        assert check.limit == pytest.approx(20.0)
        assert "19 live + 2 incoming = 21" in check.detail

    @pytest.mark.parametrize(
        ("current", "expected_pass"),
        [(0, True), (17, True), (18, True), (19, False), (20, False)],
    )
    def test_the_exact_capacity_boundary(self, current, expected_pass):
        """18 + 2 == 20 passes; 19 + 2 == 21 fails."""
        decision = core(self.LIMITS).evaluate(
            intent(), context(open_orders=current), START_MS
        )
        blocked = "MAX_OPEN_ORDERS" in blocking_names(decision)
        assert blocked is not expected_pass, (
            f"{current} live + 2 incoming = {current + 2} against a "
            f"{self.LIMITS.max_open_orders} limit"
        )

    def test_a_one_leg_intent_may_use_the_last_slot(self):
        """The gate must not become blanket-conservative: at 19 live orders a
        single-leg intent reaches exactly the limit and is allowed."""
        proposed = intent(legs=[leg(VENUE_A, Side.BUY)])
        decision = core(self.LIMITS).evaluate(
            proposed, context(open_orders=19), START_MS
        )
        check = gate_named(decision, "MAX_OPEN_ORDERS")
        assert check.observed == pytest.approx(20.0)
        assert not check.blocking

    def test_at_the_limit_no_further_trade_is_authorised(self):
        decision = core(self.LIMITS).evaluate(
            intent(), context(open_orders=20), START_MS
        )
        assert decision.verdict is RiskVerdict.REJECTED
        assert "MAX_OPEN_ORDERS" in blocking_names(decision)

    @pytest.mark.parametrize("current", [17, 18, 19, 20])
    def test_authorisation_cannot_push_live_orders_past_the_limit(self, current):
        """The invariant the limit exists to provide: after this trade is
        planned, the platform must not hold more than ``max_open_orders``."""
        proposed = intent()
        legs = len(proposed.legs)
        decision = core(self.LIMITS).evaluate(
            proposed, context(open_orders=current), START_MS
        )
        resulting = current + (legs if decision.approved else 0)
        assert resulting <= self.LIMITS.max_open_orders, (
            f"{current} orders already live, a {legs}-leg intent "
            f"{'authorised' if decision.approved else 'rejected'}, leaving "
            f"{resulting} against a {self.LIMITS.max_open_orders} limit"
        )

    @pytest.mark.parametrize("current", [16, 17, 18, 19])
    def test_the_limit_holds_for_a_larger_leg_count(self, current):
        """A generic hard-risk layer must hold for any leg count, not only the
        two the shipped strategy uses. A three-leg intent consumes three
        slots, so 17 live orders is already too many."""
        from tests.audit.rune_fixtures import VENUE_C

        proposed = intent(
            legs=[
                leg(VENUE_A, Side.BUY),
                leg(VENUE_B, Side.SELL),
                leg(VENUE_C, Side.BUY),
            ],
        )
        decision = core(self.LIMITS).evaluate(
            proposed, context(open_orders=current), START_MS
        )
        resulting = current + (3 if decision.approved else 0)
        assert resulting <= self.LIMITS.max_open_orders, (
            f"a 3-leg intent authorised at {current} live orders leaves "
            f"{resulting} against a {self.LIMITS.max_open_orders} limit"
        )


class TestSettledExposureIsCountedCorrectly:
    """A control: once a trade has actually filled, the gates do see it.

    This is what makes the concurrency finding specific — the projection logic
    is sound, it is the *timing* of when exposure becomes visible that leaves
    the window."""

    LIMITS = RiskLimits(
        max_gross_exposure=100_000.0,
        max_position_notional=50_000.0,
        max_order_notional=25_000.0,
        max_net_exposure=1_000_000.0,
        max_venue_exposure=1_000_000.0,
        max_leverage=100.0,
    )

    def test_a_filled_first_trade_blocks_the_third(self):
        # Two trades' worth of fills: 100,000 of gross, the whole budget.
        book = portfolio_with(
            position(VENUE_A, quantity=500.0),
            position(VENUE_B, quantity=-500.0),
        )
        assert book.gross_exposure == pytest.approx(100_000.0)
        decision = core(self.LIMITS).evaluate(
            intent(notional=25_000.0),
            context(portfolio=book, max_economical_notional=1_000_000.0),
            START_MS,
        )
        assert decision.verdict is RiskVerdict.REJECTED
        assert "MIN_TRADE_NOTIONAL" in decision.reason_codes or (
            "MAX_GROSS_EXPOSURE" in blocking_names(decision)
        )
