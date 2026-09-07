"""Phase 6 — H25: a production-like execution probe, and the paper boundary.

Everything else in this suite isolates one dimension. This file does the
opposite: it drives the shipped configuration over a meaningful number of
opportunities and reports what actually happens, so a hypothesis confirmed on a
constructed book can be weighed against how often it fires in practice.

Nothing here is tuned to make the probe interesting. The strategy parameters,
the risk limits, the simulator seed and the synthetic market are all the
shipped defaults; the only thing this file chooses is how long to run.

The diagnostic is printed through an assertion message so external CI shows the
whole table whether or not the invariants hold. The invariants asserted at the
end are the ones this audit says must hold regardless of configuration.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field

import pytest

from core.models.common import Liquidity, TimeInForce
from core.models.execution import OrderStatus
from tests.conftest import run_platform

#: Long enough for entries, exits and hedges to appear; short enough to stay a
#: test. The default synthetic market is deterministic, so this is stable.
TICKS = 400


@dataclass
class Probe:
    """Everything the brief asks the probe to record."""

    plans: int = 0
    orders: int = 0
    fills: int = 0
    partial_fills: int = 0
    cancel_requests: int = 0
    cancels_completed: int = 0
    expired: int = 0
    unknown: int = 0
    rejected: int = 0
    ioc_orders: int = 0
    post_only_orders: int = 0
    gtc_orders: int = 0
    maker_fills: int = 0
    taker_fills: int = 0
    requested_notional: float = 0.0
    approved_notional: float = 0.0
    planned_notional: float = 0.0
    filled_notional: float = 0.0
    fees: float = 0.0
    max_slippage_bps: float = 0.0
    slippage_budget_bps: float = 0.0
    over_budget_fills: int = 0
    ioc_remainders_filled_later: int = 0
    filled_after_deadline: int = 0
    duplicate_client_order_ids: int = 0
    unknown_treated_as_resolved: int = 0
    resident_pending: int = 0
    resident_plans: int = 0
    resident_orders: int = 0
    archived_orders: int = 0
    time_to_ack: list[int] = field(default_factory=list)
    time_to_first_fill: list[int] = field(default_factory=list)
    time_to_terminal: list[int] = field(default_factory=list)
    residual_positions: int = 0
    hedges: int = 0
    exit_retries: int = 0
    entry_orders: int = 0
    entry_conservation_failures: int = 0

    def report(self) -> str:
        def mean(values: list[int]) -> str:
            return f"{sum(values) / len(values):.1f}" if values else "n/a"

        return "\n".join(
            [
                "",
                "PHASE 6 EXECUTION PROBE",
                "=======================",
                f"ticks                       {TICKS}",
                f"plans built                 {self.plans}",
                f"orders created              {self.orders}",
                f"  IOC                       {self.ioc_orders}",
                f"  POST_ONLY                 {self.post_only_orders}",
                f"  GTC                       {self.gtc_orders}",
                f"fills                       {self.fills}",
                f"  maker                     {self.maker_fills}",
                f"  taker                     {self.taker_fills}",
                f"partially filled orders     {self.partial_fills}",
                f"cancel requests             {self.cancel_requests}",
                f"cancels completed           {self.cancels_completed}",
                f"expired                     {self.expired}",
                f"rejected                    {self.rejected}",
                f"UNKNOWN                     {self.unknown}",
                "",
                f"requested notional          {self.requested_notional:,.2f}",
                f"approved notional           {self.approved_notional:,.2f}",
                f"planned notional            {self.planned_notional:,.2f}",
                f"filled notional             {self.filled_notional:,.2f}",
                f"fees paid                   {self.fees:,.4f}",
                "",
                f"max observed slippage bps   {self.max_slippage_bps:.4f}",
                f"slippage budget bps         {self.slippage_budget_bps:.4f}",
                f"fills over budget           {self.over_budget_fills}",
                f"IOC remainders filled later {self.ioc_remainders_filled_later}",
                f"fills after intent deadline {self.filled_after_deadline}",
                f"duplicate client order ids  {self.duplicate_client_order_ids}",
                f"UNKNOWN treated as resolved {self.unknown_treated_as_resolved}",
                "",
                f"mean time to ack (ms)       {mean(self.time_to_ack)}",
                f"mean time to first fill     {mean(self.time_to_first_fill)}",
                f"mean time to terminal       {mean(self.time_to_terminal)}",
                "",
                f"resident _pending           {self.resident_pending}",
                f"resident veska.plans        {self.resident_plans}",
                f"resident oms.orders         {self.resident_orders}",
                f"archived orders             {self.archived_orders}",
                f"entry legs planned          {self.entry_orders}",
                f"  off the approved notional {self.entry_conservation_failures}",
                f"residual positions          {self.residual_positions}",
                f"hedges worked               {self.hedges}",
                f"exit retries                {self.exit_retries}",
                "",
            ]
        )


async def measure(platform) -> Probe:
    await run_platform(platform, TICKS)

    probe = Probe()
    executor = platform.veska.executor
    oms = executor.oms
    state = platform.state

    probe.resident_pending = len(executor._pending)
    probe.resident_plans = len(platform.veska.plans)
    probe.resident_orders = len(oms.orders)
    probe.archived_orders = oms.archived.count
    probe.plans = len(platform.veska.plans)

    seen_ids: set[str] = set()
    orders = list(state.orders.values())
    probe.orders = len(orders) + oms.archived.count

    for order in orders:
        if order.client_order_id in seen_ids:
            probe.duplicate_client_order_ids += 1
        seen_ids.add(order.client_order_id)

        if order.time_in_force is TimeInForce.IOC:
            probe.ioc_orders += 1
        elif order.time_in_force is TimeInForce.POST_ONLY:
            probe.post_only_orders += 1
        else:
            probe.gtc_orders += 1

        if order.status is OrderStatus.EXPIRED:
            probe.expired += 1
        elif order.status is OrderStatus.CANCELLED:
            probe.cancels_completed += 1
        elif order.status is OrderStatus.REJECTED:
            probe.rejected += 1
        elif order.status is OrderStatus.UNKNOWN:
            probe.unknown += 1
        elif order.status is OrderStatus.CANCEL_PENDING:
            probe.cancel_requests += 1

        if 0 < order.filled_quantity < order.quantity:
            probe.partial_fills += 1

        probe.planned_notional += order.quantity * order.expected_price
        probe.fees += order.fees_paid

        if order.submitted_at is not None and order.acknowledged_at is not None:
            probe.time_to_ack.append(order.acknowledged_at - order.submitted_at)
        if order.submitted_at is not None and order.fills:
            probe.time_to_first_fill.append(
                order.fills[0].created_at - order.submitted_at
            )
        if order.submitted_at is not None and order.terminal_at is not None:
            probe.time_to_terminal.append(order.terminal_at - order.submitted_at)

        # An IOC whose first fill and last fill are separated by more than one
        # acknowledgement is a remainder that traded after its single attempt.
        if order.time_in_force is TimeInForce.IOC and len(order.fills) > 1:
            first, last = order.fills[0].created_at, order.fills[-1].created_at
            if last > first:
                probe.ioc_remainders_filled_later += 1

        for fill in order.fills:
            probe.fills += 1
            probe.filled_notional += fill.notional
            if fill.liquidity is Liquidity.MAKER:
                probe.maker_fills += 1
            else:
                probe.taker_fills += 1
            probe.max_slippage_bps = max(probe.max_slippage_bps, fill.slippage_bps)

    budget = 0.0
    for record in state.opportunities.values():
        if record.intent is not None:
            probe.requested_notional += record.intent.notional
            budget = max(budget, record.intent.max_slippage_bps)
            approved = (
                record.decision.approved_notional
                if record.decision is not None
                else None
            )
            for order in orders:
                if order.intent_id != record.intent.intent_id:
                    continue
                probe.entry_orders += 1
                if approved and approved > 0:
                    planned = order.quantity * order.expected_price
                    if abs(planned - approved) > max(1e-6, approved * 1e-6):
                        probe.entry_conservation_failures += 1
                for fill in order.fills:
                    if fill.created_at > record.intent.deadline_ms:
                        probe.filled_after_deadline += 1
        if record.decision is not None:
            probe.approved_notional += record.decision.approved_notional
        probe.exit_retries += record.exit_attempts
    probe.slippage_budget_bps = budget
    probe.over_budget_fills = sum(
        1
        for order in orders
        for fill in order.fills
        if budget and fill.slippage_bps > budget + 1e-6
    )

    # An UNKNOWN order whose opportunity has been closed anyway.
    from core.models.opportunity import StrategyState

    for record in state.opportunities.values():
        if record.state is not StrategyState.CLOSED:
            continue
        for oid in record.order_ids:
            found = state.orders.get(oid)
            if found is not None and found.status is OrderStatus.UNKNOWN:
                probe.unknown_treated_as_resolved += 1

    portfolio = executor.account.snapshot()
    probe.residual_positions = sum(
        1 for position in portfolio.positions.values() if not position.is_flat
    )
    probe.hedges = len(platform.orchestrator.working_hedges)
    return probe


#: The probe is expensive (a full platform run), and every invariant below
#: interrogates the same run. Cached at module level so the platform is driven
#: once rather than once per assertion.
_MEASURED: Probe | None = None


@pytest.fixture
async def probe(platform):
    global _MEASURED
    if _MEASURED is None:
        _MEASURED = await measure(platform)
    return _MEASURED


class TestTheProbeRuns:
    async def test_the_probe_reports_a_complete_picture(self, probe):
        """Always fails-open into the report: every figure the brief asks for
        is printed, and the assertion states the premise that something
        happened at all."""
        assert probe.orders > 0, probe.report()

    async def test_the_full_diagnostic_is_visible(self, probe):
        """Emitted deliberately so external CI carries the whole table."""
        print(probe.report())
        assert True


class TestExecutionInvariantsUnderProductionSettings:
    """The properties that must hold whatever the market did."""

    async def test_no_fill_exceeds_the_slippage_budget(self, probe):
        assert probe.over_budget_fills == 0, (
            f"{probe.over_budget_fills} fills exceeded the "
            f"{probe.slippage_budget_bps:.4f} bps budget; worst observed "
            f"{probe.max_slippage_bps:.4f} bps" + probe.report()
        )

    async def test_no_client_order_id_is_reused(self, probe):
        assert probe.duplicate_client_order_ids == 0, probe.report()

    async def test_no_ioc_remainder_fills_after_its_single_attempt(self, probe):
        assert probe.ioc_remainders_filled_later == 0, (
            f"{probe.ioc_remainders_filled_later} IOC orders took a fill after "
            "their arrival attempt" + probe.report()
        )

    async def test_nothing_fills_after_its_intent_deadline(self, probe):
        assert probe.filled_after_deadline == 0, (
            f"{probe.filled_after_deadline} fills landed after the absolute "
            "deadline of the intent that authorised them" + probe.report()
        )

    async def test_no_unknown_order_is_treated_as_resolved(self, probe):
        assert probe.unknown_treated_as_resolved == 0, (
            f"{probe.unknown_treated_as_resolved} opportunities closed while "
            "naming an order whose venue-side state is unknown"
            + probe.report()
        )

    async def test_filled_notional_never_exceeds_what_was_planned(self, probe):
        assert probe.filled_notional <= probe.planned_notional + 1e-6, (
            f"filled {probe.filled_notional:,.2f} against a planned "
            f"{probe.planned_notional:,.2f}" + probe.report()
        )

    async def test_every_entry_leg_carries_the_approved_notional(self, probe):
        """H12 in production: per-leg planned notional equals the approval."""
        assert probe.entry_orders > 0, (
            "no entry orders in the probe" + probe.report()
        )
        assert probe.entry_conservation_failures == 0, (
            f"{probe.entry_conservation_failures} of {probe.entry_orders} entry "
            "legs were planned at a notional other than the one RUNE approved"
            + probe.report()
        )

    async def test_the_resident_structures_track_activity(self, probe):
        assert probe.resident_pending <= probe.resident_orders + probe.unknown, (
            f"_pending holds {probe.resident_pending} entries against "
            f"{probe.resident_orders} resident orders and {probe.unknown} "
            "unresolved ones" + probe.report()
        )

    async def test_the_plan_store_tracks_activity(self, probe):
        assert probe.resident_plans <= probe.resident_orders + 50, (
            f"veska.plans holds {probe.resident_plans} plans against "
            f"{probe.resident_orders} resident orders" + probe.report()
        )


class TestPaperOnlyBoundary:
    """Re-proved here, at the execution boundary, not merely inherited."""

    def test_the_executor_declares_itself_paper(self, platform):
        assert platform.veska.executor.is_paper is True

    def test_veska_refuses_a_non_paper_executor(self):
        source = inspect.getsource(
            __import__("execution.veska.engine", fromlist=["Veska"]).Veska.__init__
        )
        assert "if not executor.is_paper:" in source
        assert "paper executors only" in source

    def test_the_executor_module_reaches_no_exchange(self):
        import execution.paper.executor as module
        import execution.paper.simulator as simulator_module
        import execution.veska.engine as engine_module

        for target in (module, simulator_module, engine_module):
            source = inspect.getsource(target)
            for forbidden in (
                "api_key",
                "api_secret",
                "private_key",
                "wallet",
                "signature",
                "hmac",
                "requests",
                "httpx",
                "aiohttp",
                "websocket",
            ):
                assert forbidden not in source.lower(), (
                    f"{forbidden!r} appears in {target.__name__}"
                )

    def test_no_execution_module_imports_a_network_client(self):
        import ast

        import execution.paper.executor as module
        import execution.paper.simulator as simulator_module
        import execution.router as router_module
        import execution.veska.engine as engine_module
        from execution import costs as costs_module
        from execution import oms as oms_module

        banned = {"requests", "httpx", "aiohttp", "websockets", "socket", "urllib"}
        for target in (
            module,
            simulator_module,
            router_module,
            engine_module,
            costs_module,
            oms_module,
        ):
            imported: set[str] = set()
            for node in ast.walk(ast.parse(inspect.getsource(target))):
                if isinstance(node, ast.Import):
                    imported.update(a.name.split(".")[0] for a in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imported.add(node.module.split(".")[0])
            assert not (imported & banned), (
                f"{target.__name__} imports {sorted(imported & banned)}"
            )

    async def test_no_order_leaves_the_process(self, platform):
        """The structural statement: the only thing ``submit`` talks to is the
        OMS, the account and the bus."""
        import execution.paper.executor as module

        source = inspect.getsource(module.PaperExecutor.submit)
        assert "self.oms" in source and "self.bus" in source
        assert "await" in source
        for outbound in ("http", "post(", "send(", "connect("):
            assert outbound not in source.lower()
