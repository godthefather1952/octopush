"""H6, H7, H20, H30 — UNKNOWN is outstanding, and nothing may forget it.

THE DISTINCTION PHASE 6 DREW
============================
``PaperOrder`` carries two properties that differ on exactly one status::

    is_live         = not is_terminal and status is not UNKNOWN
    is_outstanding  = not is_terminal

``is_live`` asks *is this order known to be working?* — False for UNKNOWN,
correctly, because nobody knows. ``is_outstanding`` asks *could this still
turn out to have traded?* — True for UNKNOWN, because it could.

The model's own docstring says a caller deciding whether to **wait** wants the
first and a caller deciding whether the platform is **exposed** wants the
second. This module audits every place the second question is asked with the
first predicate.

WHAT THE AUDIT FOUND STATICALLY
===============================
Three orchestrator sites read ``is_live``:

* ``_advance_execution`` — an UNKNOWN entry order is not live, so the method
  falls through to "nothing traded: there is no position to hedge or monitor"
  and closes the opportunity.
* ``_hedge_in_flight`` — an UNKNOWN hedge is not live, so a second hedge for
  the same symbol is submitted.
* ``_advance_exit`` — an UNKNOWN exit order is not live, so the exit is
  retried.

One site does **not**, and deserves saying: RUNE's committed-exposure snapshot
tests terminality rather than liveness, and its docstring explains why at
length. That surface is correct and is asserted below so a regression in it
would be caught.
"""

from __future__ import annotations

import ast
import inspect

from core.models.common import OrderType, Side, TimeInForce
from core.models.execution import TERMINAL_STATUSES, OrderStatus, PaperOrder
from execution.paper.executor import PAPER_CAPABILITIES
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


def _quiet_book(created_at: int = T0):
    return market_state(
        venue_state(
            venue=VENUE_A,
            bids=price_levels((90.0, 10.0)),
            asks=price_levels((110.0, 10.0)),
            as_of=created_at,
        ),
        created_at=created_at,
    )


async def _make_unknown(harness, *, ttl_ms: int = 60_000):
    """Submit one order and drive it to UNKNOWN through the injection seam."""
    plan = execution_plan(
        planned_order(
            time_in_force=TimeInForce.GTC, limit_price=95.0, ttl_ms=ttl_ms
        ),
        created_at=T0,
    )
    await harness.veska.execute(plan, T0)
    order = harness.orders_of(plan.plan_id)[0]
    harness.executor.inject_timeout(order.client_order_id)
    await harness.veska.poll(T0 + _latency(harness))
    return plan, order


class TestTheModelItself:
    """The two properties, and the one status where they differ."""

    def test_unknown_is_not_terminal(self):
        assert OrderStatus.UNKNOWN not in TERMINAL_STATUSES

    def test_unknown_is_not_live_but_is_outstanding(self):
        order = PaperOrder(
            created_at=T0,
            venue=VENUE_A,
            symbol="BTC-USD",
            side=Side.BUY,
            quantity=1.0,
            order_type=OrderType.LIMIT,
            time_in_force=TimeInForce.GTC,
            expected_price=100.0,
            status=OrderStatus.UNKNOWN,
        )
        assert order.is_live is False
        assert order.is_outstanding is True
        assert order.is_terminal is False

    def test_the_two_predicates_differ_only_on_unknown(self):
        """Exhaustive over the status enum, so nothing is taken on trust."""
        differing = set()
        for status in OrderStatus:
            live = status not in TERMINAL_STATUSES and status is not OrderStatus.UNKNOWN
            outstanding = status not in TERMINAL_STATUSES
            if live != outstanding:
                differing.add(status)
        assert differing == {OrderStatus.UNKNOWN}


class TestExecutorQuerySurfaces:
    """H7 — which query an UNKNOWN order appears in."""

    async def test_an_unknown_order_leaves_open_orders(self):
        harness = build_harness()
        harness.update_market(_quiet_book())
        _, order = await _make_unknown(harness)
        assert order.status is OrderStatus.UNKNOWN

        open_ids = {o.client_order_id for o in harness.veska.open_orders()}
        assert order.client_order_id not in open_ids

    async def test_an_unknown_order_stays_in_outstanding_orders(self):
        harness = build_harness()
        harness.update_market(_quiet_book())
        _, order = await _make_unknown(harness)

        outstanding = {o.client_order_id for o in harness.veska.outstanding_orders()}
        assert order.client_order_id in outstanding, (
            "an UNKNOWN order vanished from outstanding_orders, which is the "
            "one surface that is supposed to keep it"
        )

    async def test_an_unknown_order_never_appears_terminal(self):
        harness = build_harness()
        harness.update_market(_quiet_book())
        _, order = await _make_unknown(harness)

        terminal = {o.client_order_id for o in harness.oms.terminal_orders()}
        assert order.client_order_id not in terminal

    async def test_an_unknown_order_appears_in_every_total_surface(self):
        """H29 in miniature: no order may vanish from all query surfaces."""
        harness = build_harness()
        harness.update_market(_quiet_book())
        _, order = await _make_unknown(harness)

        assert order.client_order_id in {
            o.client_order_id for o in harness.executor.all_orders()
        }
        assert order.client_order_id in {
            o.client_order_id for o in harness.veska.unknown_orders()
        }
        assert harness.executor.get_order(order.client_order_id) is not None


class TestOpenOrderCapacity:
    """H7 — the capacity a risk limit counts against."""

    def test_the_orchestrator_feeds_open_orders_into_the_capacity_gate(self):
        """Static evidence for the finding: which predicate reaches RUNE.

        ``gate_open_orders`` projects ``open_orders + incoming_orders`` against
        ``max_open_orders``. If ``open_orders`` excludes UNKNOWN, an order that
        may still be resting at the venue consumes no capacity.
        """
        from apps.orchestrator import orchestrator as orch_module

        source = inspect.getsource(orch_module.Orchestrator)
        assert "open_orders=len(self.veska.open_orders())" in source, (
            "the capacity gate's input has changed; this finding's evidence "
            "needs re-deriving"
        )

    async def test_an_unknown_order_consumes_open_order_capacity(self):
        """The invariant: capacity must reflect what the venue may hold.

        An order whose venue-side truth is unknown might be one of the twenty
        the limit permits. Counting it out means the venue can hold more live
        orders than ``max_open_orders`` allows.
        """
        harness = build_harness()
        harness.update_market(_quiet_book())
        await _make_unknown(harness)

        counted = len(harness.veska.open_orders())
        assert counted == 1, (
            "an UNKNOWN order that may still be resting at the venue counts "
            f"as {counted} against the open-order limit; the venue could hold "
            "max_open_orders + (number of UNKNOWN orders) real orders"
        )


class TestOrchestratorConsumers:
    """H6 — the three sites that read liveness as though it were truth."""

    def _sites_reading_is_live(self) -> set[str]:
        from apps.orchestrator import orchestrator as orch_module

        tree = ast.parse(inspect.getsource(orch_module.Orchestrator))
        sites: set[str] = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            if "is_live" in ast.dump(node):
                sites.add(node.name)
        return sites

    def test_the_sites_reading_is_live_are_the_ones_the_audit_names(self):
        """Pins the evidence, so a new call site is a new finding not a silent one."""
        assert self._sites_reading_is_live() == {
            "_advance_execution",
            "_hedge_in_flight",
            "_advance_exit",
        }

    def test_advance_execution_treats_a_non_live_order_as_settled(self):
        """The entry case: UNKNOWN reads as 'nothing traded, close it'."""
        from apps.orchestrator import orchestrator as orch_module

        source = inspect.getsource(orch_module.Orchestrator._advance_execution)
        assert "is_live" in source
        assert "is_outstanding" not in source, (
            "_advance_execution now consults outstanding-ness; this finding "
            "may be resolved and its evidence needs re-deriving"
        )
        assert "Nothing traded" in source

    def test_hedge_in_flight_treats_a_non_live_hedge_as_finished(self):
        """The hedge case: an UNKNOWN hedge admits a duplicate."""
        from apps.orchestrator import orchestrator as orch_module

        source = inspect.getsource(orch_module.Orchestrator._hedge_in_flight)
        assert "is_live" in source
        assert "is_outstanding" not in source

    def test_advance_exit_treats_a_non_live_exit_as_finished(self):
        """The exit case: an UNKNOWN exit admits a retry."""
        from apps.orchestrator import orchestrator as orch_module

        source = inspect.getsource(orch_module.Orchestrator._advance_exit)
        assert "is_live" in source
        assert "is_outstanding" not in source


class TestRiskReservationIsCorrect:
    """The surface that gets it right, asserted so it stays right.

    RUNE's committed-exposure snapshot tests terminality, not liveness, and
    says why: releasing an UNKNOWN order's reservation "would free budget
    against risk the platform still carries". This is a refutation, recorded
    as one.
    """

    def test_committed_exposure_releases_on_terminality_not_liveness(self):
        from apps.orchestrator import orchestrator as orch_module

        source = inspect.getsource(orch_module.Orchestrator)
        marker = "WHAT COUNTS: EVERYTHING NOT YET TERMINAL"
        assert marker in source, (
            "the committed-exposure snapshot no longer documents terminality "
            "as its test; the refutation recorded for H7 needs re-deriving"
        )


class TestUnknownIsNeverAutoResolved:
    """H20 and H30 — the only way out is an explicit authoritative caller."""

    async def test_polling_does_not_resolve_an_unknown_order(self):
        harness = build_harness()
        harness.update_market(_quiet_book())
        _, order = await _make_unknown(harness)

        for step in range(1, 6):
            harness.update_market(_quiet_book(created_at=T0 + step * 1_000))
            await harness.veska.poll(T0 + step * 1_000)

        assert order.status is OrderStatus.UNKNOWN, (
            f"repeated polling moved an UNKNOWN order to {order.status.value} "
            "without anything authoritative arriving"
        )

    async def test_expiry_does_not_resolve_an_unknown_order(self):
        """A TTL says nothing about what the venue did."""
        harness = build_harness()
        harness.update_market(_quiet_book())
        latency = _latency(harness)
        _, order = await _make_unknown(harness, ttl_ms=latency + 100)
        assert order.expires_at is not None

        await harness.veska.poll(order.expires_at + 10_000)

        assert order.status is OrderStatus.UNKNOWN

    async def test_compaction_never_discards_an_unknown_order(self):
        """H20 — retention must not be a route to forgetting unresolved truth."""
        harness = build_harness()
        harness.update_market(_quiet_book())
        _, order = await _make_unknown(harness)

        released = harness.executor.compact_terminal_state(unsealed_fills=set())

        assert harness.oms.get(order.client_order_id) is not None, (
            f"compaction released {released} order(s) and one of them was "
            "UNKNOWN: unresolved venue truth was deleted"
        )
        assert order.client_order_id in {
            o.client_order_id for o in harness.veska.outstanding_orders()
        }


class TestResolveUnknownContract:
    """H30 — the Phase 6 resolution seam, on its own terms."""

    def test_the_executor_claims_to_support_resolution(self):
        assert PAPER_CAPABILITIES.supports_unknown_resolution is True

    async def test_resolving_an_order_that_is_not_unknown_is_refused(self):
        harness = build_harness()
        harness.update_market(_quiet_book())
        plan = execution_plan(
            planned_order(time_in_force=TimeInForce.GTC, limit_price=95.0),
            created_at=T0,
        )
        await harness.veska.execute(plan, T0)
        order = harness.orders_of(plan.plan_id)[0]
        await harness.veska.poll(T0 + _latency(harness))
        assert order.status is OrderStatus.OPEN

        result = await harness.veska.resolve_unknown(
            order.client_order_id, OrderStatus.FILLED, T0 + 9_999
        )
        assert result.accepted is False
        assert "not UNKNOWN" in result.reason
        assert order.status is OrderStatus.OPEN

    async def test_resolving_an_unknown_order_requires_an_explicit_status(self):
        harness = build_harness()
        harness.update_market(_quiet_book())
        _, order = await _make_unknown(harness)

        result = await harness.veska.resolve_unknown(
            order.client_order_id, OrderStatus.CANCELLED, T0 + 9_999
        )
        assert result.accepted is True
        assert result.status is OrderStatus.CANCELLED
        assert order.status is OrderStatus.CANCELLED

    async def test_resolution_uses_the_supplied_instant(self):
        """The seam takes ``now_ms``; the result must carry it back."""
        harness = build_harness()
        harness.update_market(_quiet_book())
        _, order = await _make_unknown(harness)

        at = T0 + 123_456
        result = await harness.veska.resolve_unknown(
            order.client_order_id, OrderStatus.CANCELLED, at
        )
        assert result.at_ms == at
        terminal_stamps = [
            ts for ts, status in order.history if status is OrderStatus.CANCELLED
        ]
        assert terminal_stamps == [at], (
            "the resolution was recorded at "
            f"{terminal_stamps} rather than at the supplied instant {at}"
        )

    async def test_resolving_an_unknown_order_updates_every_query_surface(self):
        harness = build_harness()
        harness.update_market(_quiet_book())
        _, order = await _make_unknown(harness)
        oid = order.client_order_id

        await harness.veska.resolve_unknown(oid, OrderStatus.CANCELLED, T0 + 9_999)

        assert oid not in {o.client_order_id for o in harness.veska.unknown_orders()}
        assert oid not in {
            o.client_order_id for o in harness.veska.outstanding_orders()
        }
        assert oid in {o.client_order_id for o in harness.oms.terminal_orders()}
        snapshot = harness.veska.execution_snapshot(T0 + 9_999)
        assert oid not in snapshot.unknown_order_ids
        assert oid not in snapshot.outstanding_order_ids

    def test_only_marins_explicit_bridge_reaches_the_seam(self):
        """"No automatic call" is the whole safety property, so pin who can call.

        Exactly one production caller exists — ``Marin.apply_order_resolution``,
        which Phase 7 built as the reconciliation side of the same door and
        which its own docstring says nothing invokes. Any other caller is a new
        route out of UNKNOWN and a finding in its own right.
        """
        from pathlib import Path

        owns_the_seam = {
            "execution/paper/executor.py",
            "execution/veska/engine.py",
            "execution/veska/executor.py",
            "execution/oms/__init__.py",
            "agents/marin/agent.py",
        }
        callers: list[str] = []
        for root in ("apps", "agents", "execution", "risk", "strategies"):
            for path in Path(root).rglob("*.py"):
                if path.as_posix() in owns_the_seam:
                    continue
                if "resolve_unknown(" in path.read_text():
                    callers.append(path.as_posix())
        assert callers == [], (
            f"resolve_unknown is reachable from {callers}; the only "
            "production caller should be Marin.apply_order_resolution"
        )

    def test_marins_bridge_is_not_invoked_by_its_own_loop(self):
        """MARIN's reconcile and run paths must not open the door themselves."""
        from agents.marin.agent import Marin

        bridge = inspect.getsource(Marin.apply_order_resolution)
        assert "veska.resolve_unknown(" in bridge, (
            "the bridge no longer reaches the seam; this evidence is stale"
        )

        whole = inspect.getsource(Marin)
        # The only mention outside the bridge's own body must be prose.
        body_stripped = whole.replace(bridge, "")
        assert "await veska.resolve_unknown(" not in body_stripped
        assert "self.apply_order_resolution(" not in body_stripped, (
            "MARIN invokes its own resolution bridge; UNKNOWN would then be "
            "resolved without a caller arriving with evidence"
        )
