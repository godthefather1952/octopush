"""Phase 5 — H3 and H4: what does an authorised-but-unfilled trade reserve?

RUNE reads ``PortfolioState``, and a portfolio only moves when a fill lands.
Between authorisation and fill a trade is real risk the platform has already
committed to — its orders are live and can execute at any moment — but it is
invisible to every gate that reads positions.

``Orchestrator.working_notional`` is the only reservation the platform keeps,
and it feeds exactly one gate: MAX_STRATEGY_EXPOSURE. MAX_GROSS_EXPOSURE,
MAX_POSITION_NOTIONAL, MAX_VENUE_EXPOSURE, MAX_NET_EXPOSURE and MAX_LEVERAGE
all read the filled portfolio alone.

The orchestrator authorises opportunities one after another inside a single
``_seek`` pass, with only a ``bus.drain()`` between them — no settlement. So
the question is not hypothetical: two opportunities on two symbols can both be
authorised against the same empty portfolio in one tick.

These tests state the invariant a hard limit is supposed to provide: **the sum
of what is already held and what has been authorised must not exceed the
limit.** A limit that only holds until the second concurrent trade is not a
hard limit.
"""

from __future__ import annotations

import inspect

import pytest

from apps.orchestrator.orchestrator import Orchestrator
from core.config import RiskLimits
from core.models.common import Side
from core.models.risk import RiskVerdict
from tests.audit.rune_fixtures import (
    VENUE_A,
    VENUE_B,
    blocking_names,
    context,
    core,
    gate_named,
    intent,
    leg,
    portfolio,
    portfolio_with,
    position,
)
from tests.conftest import START_MS


def sequential_authorisations(
    limits: RiskLimits,
    proposals: list,
    *,
    book=None,
    reserve_strategy: bool = True,
):
    """Authorise ``proposals`` back to back against an unchanging portfolio.

    Models the orchestrator exactly: the portfolio does not move (nothing has
    filled), and the only carried-forward state is ``working_notional``.
    """
    engine = core(limits)
    working: dict[str, float] = {}
    decisions = []
    for proposed in proposals:
        decision = engine.evaluate(
            proposed,
            context(
                portfolio=book or portfolio(),
                strategy_exposure=sum(working.values()) if reserve_strategy else 0.0,
                max_economical_notional=1_000_000.0,
            ),
            START_MS,
        )
        decisions.append(decision)
        if decision.approved:
            working[proposed.opportunity_id] = decision.approved_notional
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

    def test_only_strategy_exposure_carries_a_reservation(self):
        """Nothing else in RiskContext is fed from working_notional.

        The second reference is the risk-utilization snapshot, which reports
        the same sum to the dashboard rather than feeding another gate.
        """
        source = inspect.getsource(Orchestrator._risk_check)
        reserved = [
            line.strip() for line in source.splitlines() if "working_notional" in line
        ]
        assert len(reserved) == 2, f"reservation wiring changed: {reserved}"
        assert reserved[0] == "strategy_exposure=sum(self.working_notional.values()),"
        assert reserved[1].startswith("portfolio, ctx.unhedged_notional,")

    def test_no_exposure_gate_receives_a_pending_reservation(self):
        """Stated as the shape of RiskContext itself: the fields the gross,
        venue, position, net and leverage gates read all come from
        ``portfolio``, which only moves on a fill."""
        source = inspect.getsource(Orchestrator._risk_check)
        context_block = source.split("ctx = RiskContext(")[1].split(")\n")[0]
        assert "portfolio=portfolio," in context_block
        assert context_block.count("working_notional") == 1


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

    def test_the_gate_reports_a_projection_that_ignores_the_earlier_trade(self):
        """Diagnostic: shows what the third decision actually looked at."""
        decisions = sequential_authorisations(self.LIMITS, self._proposals(3))
        third = gate_named(decisions[2], "MAX_GROSS_EXPOSURE")
        assert third.observed == pytest.approx(50_000.0), (
            "the third decision projects only its own two legs; the 100,000 "
            "already authorised is not in the portfolio it read"
        )


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

    ``gate_open_orders`` compares ``open_orders < max_open_orders``. VESKA's
    ``build_plan`` emits one order per leg, so a two-leg intent authorised at
    19 open orders produces 21.
    """

    LIMITS = RiskLimits(max_open_orders=20)

    def test_veska_creates_one_order_per_leg(self):
        """The premise, read off production."""
        from execution.veska.engine import Veska

        source = inspect.getsource(Veska.build_plan)
        assert "for leg in intent.legs:" in source
        assert "orders.append(" in source

    def test_the_gate_reads_only_the_current_count(self):
        decision = core(self.LIMITS).evaluate(
            intent(), context(open_orders=19), START_MS
        )
        check = gate_named(decision, "MAX_OPEN_ORDERS")
        assert check.observed == pytest.approx(19.0)
        assert check.limit == pytest.approx(20.0)

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

    def test_a_larger_leg_count_widens_the_overshoot(self):
        """A generic hard-risk layer must hold for any leg count."""
        from tests.audit.rune_fixtures import VENUE_C

        proposed = intent(
            legs=[
                leg(VENUE_A, Side.BUY),
                leg(VENUE_B, Side.SELL),
                leg(VENUE_C, Side.BUY),
            ],
        )
        decision = core(self.LIMITS).evaluate(
            proposed, context(open_orders=19), START_MS
        )
        resulting = 19 + (3 if decision.approved else 0)
        assert resulting <= self.LIMITS.max_open_orders, (
            f"a 3-leg intent authorised at 19 live orders leaves {resulting} "
            f"against a {self.LIMITS.max_open_orders} limit"
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
