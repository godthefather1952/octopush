"""Phase 5 — H8, H9, H10, H11: the emergency boundary.

Pre-trade prevention and post-fill detection are different jobs. RUNE stops a
trade being authorised; the kill switch is what notices that the platform has
ended up somewhere it should not be — through partial fills, slippage, or the
concurrent commitments audited elsewhere in this suite — and does something
about it.

Four questions:

* **H8** — is there any automatic path that engages ``RISK_LIMIT_BREACH`` when
  a live portfolio exceeds a configured hard limit?
* **H9** — does ``EXCESSIVE_LATENCY`` consume latency, or data age under a
  latency-shaped name?
* **H10** — after ``DISABLE_EXECUTION``, does clearing the switch restore the
  platform's ability to submit?
* **H11** — can an order that was already live when FLATTEN fired re-open
  exposure after the flatten completes?
"""

from __future__ import annotations

import inspect

import pytest

from apps.orchestrator.orchestrator import Orchestrator
from core.bus import InMemoryEventBus
from core.clock import ManualClock
from core.config import Settings, load_settings, simulated_venues
from core.models.ops import KillAction, KillSwitchState
from risk.kill_switch import (
    CONFIRMATIONS,
    TRIGGER_ACTIONS,
    TRIGGERS,
    KillSwitch,
    KillSwitchInputs,
)
from tests.audit.rune_fixtures import portfolio, portfolio_with, position
from tests.conftest import START_MS


def settings(**risk_overrides) -> Settings:
    base = load_settings().model_copy(update={"venues": simulated_venues()})
    if risk_overrides:
        return base.model_copy(
            update={"risk": base.risk.model_copy(update=risk_overrides)}
        )
    return base


def switch(**risk_overrides) -> KillSwitch:
    clock = ManualClock(START_MS)
    return KillSwitch(
        bus=InMemoryEventBus(raise_on_handler_error=True),
        clock=clock,
        settings=settings(**risk_overrides),
    )


def inputs(**overrides) -> KillSwitchInputs:
    defaults = dict(
        portfolio=portfolio(),
        health=None,
        reconciliation_ok=True,
        market_data_ok=True,
        book_corruption=False,
        max_latency_ms=0.0,
        storage_ok=True,
        unexpected_position=False,
        required_components=[],
    )
    defaults.update(overrides)
    return KillSwitchInputs(**defaults)


class TestTriggerInventory:
    """Every action mapping should have a predicate that can reach it."""

    def test_every_evaluated_trigger_has_an_action_mapping(self):
        missing = sorted(set(TRIGGERS) - set(TRIGGER_ACTIONS))
        assert not missing, f"triggers with no action mapping: {missing}"

    def test_every_action_mapping_is_reachable(self):
        """H8. ``TRIGGER_ACTIONS`` names what happens when a trigger fires;
        ``TRIGGERS`` is what actually fires. A mapping with no predicate and no
        caller is a safety response nothing can invoke.

        External validation reported TWO such mappings, and they were
        different findings, so each has its own test below rather than being
        lumped into one list. This test records the inventory.

        ``RISK_LIMIT_BREACH`` left the list in Remediation C, which gave it a
        predicate in :data:`TRIGGERS`. ``AGENT_FAILURE`` left it in E1, which
        removed it — the list is now empty, and MANUAL remains the one
        deliberate operator-only mapping.
        """
        callers = _explicit_engage_calls()
        unreachable = sorted(
            name
            for name in TRIGGER_ACTIONS
            # MANUAL is operator-invoked by design; see the test below.
            if name != "MANUAL" and name not in TRIGGERS and name not in callers
        )
        assert unreachable == [], (
            "every automatic action mapping must have a predicate that can "
            "reach it. RISK_LIMIT_BREACH gained one in Remediation C; "
            "AGENT_FAILURE was removed in E1 rather than given one, because "
            "no distinct safety condition was ever defined for it. Now: "
            f"{unreachable}"
        )

    def test_risk_limit_breach_is_reachable(self):
        """P5-3. The serious one: a hard exposure limit with no detection.

        ``RISK_LIMIT_BREACH`` requests HALT_NEW_TRADES and CANCEL_ALL — a
        response no other trigger delivers for an exposure breach — and nothing
        can invoke it.
        """
        callers = _explicit_engage_calls()
        assert "RISK_LIMIT_BREACH" in TRIGGERS or "RISK_LIMIT_BREACH" in callers, (
            "RISK_LIMIT_BREACH has actions defined but no predicate in TRIGGERS "
            "and no caller. A live portfolio past max_gross_exposure, "
            "max_net_exposure, max_venue_exposure, max_strategy_exposure, "
            "max_position_notional or max_leverage is never detected."
        )

    def test_agent_failure_is_no_longer_a_vestigial_mapping(self):
        """P5-17, resolved by removal rather than by invention.

        ``AGENT_FAILURE`` had actions defined, no predicate and no caller. Its
        action set was ``(HALT_NEW_TRADES,)`` — byte-identical to
        ``SYSTEM_HEALTH_FAILURE``'s, whose predicate already covers a required
        component ceasing to be HEALTHY, and a required agent that answers
        with nothing is separately caught by consensus completeness. So
        nothing was unprotected; the entry named a response another trigger
        already delivered.

        Giving it a predicate would have meant inventing a safety condition
        nobody had defined. It was removed instead. This test now guards
        against its reintroduction without one.
        """
        callers = _explicit_engage_calls()
        assert "AGENT_FAILURE" not in TRIGGER_ACTIONS, (
            "AGENT_FAILURE is back in the action table. A safety mapping must "
            "name a response some predicate or caller can actually invoke; if "
            "it has been reintroduced, it needs a predicate meaning something "
            "SYSTEM_HEALTH_FAILURE does not."
        )
        assert "AGENT_FAILURE" not in TRIGGERS
        assert "AGENT_FAILURE" not in callers

    def test_manual_is_reachable_by_design(self):
        """MANUAL has no predicate on purpose — an operator engages it through
        the API. Pinned so the findings above are scoped to the automatic
        triggers rather than to every predicate-less mapping."""
        assert "MANUAL" in TRIGGER_ACTIONS
        assert "MANUAL" not in TRIGGERS

    def test_the_api_can_engage_any_trigger_name(self):
        """Why 'operator-invoked' is a real category: the endpoint passes an
        arbitrary string through. That makes MANUAL reachable — and would make
        the two above reachable too, but only if an operator already knew to
        type them, which is not detection."""
        from apps.api import app as api

        source = inspect.getsource(api)
        assert "platform.kill_switch.engage(trigger, detail)" in source
        assert 'trigger: str = "MANUAL"' in source

    def test_confirmations_only_delay_measurement_triggers(self):
        """A breached limit is true the moment it is observed; a measurement
        can blip. Recorded so the distinction stays deliberate."""
        assert set(CONFIRMATIONS) == {
            "SYSTEM_HEALTH_FAILURE",
            "EXCESSIVE_LATENCY",
            "MARKET_DATA_OUTAGE",
        }
        for immediate in ("MAX_DRAWDOWN_BREACHED", "MAX_DAILY_LOSS_BREACHED",
                          "BOOK_CORRUPTION", "RECONCILIATION_MISMATCH",
                          "RISK_LIMIT_BREACH"):
            assert CONFIRMATIONS.get(immediate, 1) == 1, (
                f"{immediate} is a breached hard boundary, true the first tick "
                "it is observed; a confirmation delay would leave the platform "
                "trading through it"
            )


def _explicit_engage_calls() -> set[str]:
    """Trigger names passed to ``kill_switch.engage(...)`` anywhere in the app."""
    import apps.orchestrator.orchestrator as orch
    import risk.kill_switch as ks

    names: set[str] = set()
    for module in (orch, ks):
        source = inspect.getsource(module)
        for line in source.splitlines():
            if ".engage(" not in line:
                continue
            fragment = line.split(".engage(", 1)[1]
            for quote in ('"', "'"):
                if fragment.startswith(quote):
                    names.add(fragment[1:].split(quote, 1)[0])
    return names


class TestAutomaticExposureBreachDetection:
    """H8, stated as the invariant rather than as an inventory question.

    Every configured hard exposure limit should have SOME automatic path that
    notices a live breach. Pre-trade gates cannot provide it: they run before a
    trade, and the states this is about arise after one.

    Remediation C supplies that path — ``RISK_LIMIT_BREACH``, evaluated every
    protected tick against filled plus committed exposure.
    """

    #: Limits whose breach is a live-portfolio condition rather than a
    #: pre-trade projection. ``max_unhedged_notional`` is in the list because
    #: it too is a configured hard maximum on a live measurement, and the
    #: production probe breached it at 25,103 against 10,000 while the only
    #: emergency path watching it waited for three times that.
    LIVE_EXPOSURE_LIMITS = [
        "max_gross_exposure",
        "max_net_exposure",
        "max_venue_exposure",
        "max_strategy_exposure",
        "max_position_notional",
        "max_leverage",
        "max_unhedged_notional",
    ]

    def test_drawdown_and_daily_loss_do_have_detection(self):
        """The control: two limits that ARE watched continuously."""
        assert "MAX_DRAWDOWN_BREACHED" in TRIGGERS
        assert "MAX_DAILY_LOSS_BREACHED" in TRIGGERS

    def test_unhedged_exposure_is_watched_at_two_levels(self):
        """Its own limit and the catastrophic multiple, not one or the other.

        ``UNEXPECTED_POSITION`` remains the 3x anomaly trigger — it is not
        weakened or removed — but a breach of ``max_unhedged_notional`` itself
        is now caught by ``RISK_LIMIT_BREACH`` one layer earlier. Between the
        two there used to be a band where the configured hard limit said
        unsafe and the emergency layer said nothing.
        """
        assert "UNEXPECTED_POSITION" in TRIGGERS
        assert "RISK_LIMIT_BREACH" in TRIGGERS
        source = inspect.getsource(Orchestrator._protect)
        assert "unexpected_position=abs(unhedged)" in source
        assert "max_unhedged_notional * 3" in source, "the 3x layer must remain"
        assert "unhedged_notional=unhedged," in source, (
            "the measurement must also reach the own-limit predicate"
        )

    @pytest.mark.parametrize("limit_name", LIVE_EXPOSURE_LIMITS)
    def test_every_live_exposure_limit_has_an_automatic_breach_trigger(self, limit_name):
        watched = _limits_read_by_triggers()
        assert limit_name in watched, (
            f"nothing evaluated every tick reads {limit_name}. A live "
            "portfolio past this limit — through partial fills, slippage or "
            "concurrent authorisation — is never detected, and RISK_LIMIT_BREACH "
            f"is defined but unreachable. Triggers read: {sorted(watched)}"
        )

    async def test_a_portfolio_past_gross_exposure_now_engages_the_switch(self):
        """The invariant this class exists for, run through the real evaluator.

        This assertion used to record the defect — a portfolio at double the
        gross limit fired nothing and trading carried on. It is inverted
        rather than deleted so the same concrete state still proves the
        opposite claim.
        """
        engine = switch()
        limit = engine.settings.risk.max_gross_exposure
        book = portfolio_with(
            position("VENUE_A", quantity=limit / 100.0 * 2),  # double the limit
        )
        assert book.gross_exposure > limit
        fired = await engine.evaluate(inputs(portfolio=book))
        assert "RISK_LIMIT_BREACH" in fired, (
            f"a portfolio at {book.gross_exposure} against a {limit} gross "
            f"limit fired {fired}"
        )
        assert not engine.state.trading_allowed
        assert engine.state.cancel_all_requested
        assert engine.state.flatten_requested


def _limits_read_by_triggers() -> set[str]:
    """Which ``settings.risk`` fields the trigger predicates actually consult.

    Follows one level of delegation into :mod:`risk.kill_switch`'s own
    helpers. ``_risk_limit_breach`` is a thin wrapper over
    ``live_risk_breaches``, which is where the limits are read — a scan of the
    predicate body alone would report detection absent when it is real. The
    resolution is generic (any module-level function the predicate calls),
    not a special case for one name, so a future predicate that delegates
    differently is still seen.
    """
    import risk.kill_switch as ks
    from core.config import RiskLimits

    def sources(predicate) -> list[str]:
        body = inspect.getsource(predicate)
        out = [body]
        for name, value in vars(ks).items():
            if callable(value) and f"{name}(" in body and value is not predicate:
                try:
                    out.append(inspect.getsource(value))
                except (OSError, TypeError):
                    continue
        return out

    watched: set[str] = set()
    for predicate in TRIGGERS.values():
        for source in sources(predicate):
            for field in RiskLimits.model_fields:
                if f"settings.risk.{field}" in source or f"limits.{field}" in source:
                    watched.add(field)
    return watched


class TestLatencyTriggerMeasuresLatency:
    """H9 — ``EXCESSIVE_LATENCY`` versus what the orchestrator feeds it."""

    def test_the_predicate_compares_against_a_data_age_limit(self):
        import risk.kill_switch as ks

        source = inspect.getsource(ks._latency)
        assert "inputs.max_latency_ms > settings.risk.max_data_age_ms * 5" in source

    def test_the_orchestrator_feeds_transport_latency(self):
        """P5-10, inverted. ``EXCESSIVE_LATENCY`` used to be fed
        ``max(s.age_ms)`` — a second, looser staleness check wearing a
        latency-shaped name, with genuine transport latency unmonitored."""
        source = inspect.getsource(Orchestrator._protect)
        assert "float(s.latency_ms or 0.0) for s in market.venues.values()" in source
        assert "max_latency_ms=max_latency" in source
        assert "max_latency_ms=float(max_age)" not in source

    def test_data_age_keeps_its_own_protections(self):
        """§12 — the latency correction must not quietly remove staleness
        cover. ``market_data_ok`` still gates on usable venue data, and RUNE's
        own MARKET_DATA_FRESH gate is untouched."""
        source = inspect.getsource(Orchestrator._protect)
        assert "market_data_ok=bool(tradeable_symbols)" in source
        assert "if s.quality.is_usable" in source

        from risk import limits as gates

        age_gate = inspect.getsource(gates.gate_data_age)
        assert "limits.max_data_age_ms" in age_gate

    def test_venue_state_reports_the_two_as_different_numbers(self):
        """``age_ms`` is ``as_of - last_update_ts``; ``latency_ms`` is the
        smoothed ``received_ts - exchange_ts``. They answer different
        questions and can point in opposite directions."""
        from core.models.market import VenueMarketState
        from tests.conftest import make_book, venue_state_from_book

        # Fresh data, slow transport: received a moment ago, but the exchange
        # observed it long before.
        book = make_book("VENUE_A", "BTC-USD", 100.0, ts=START_MS)
        state = venue_state_from_book(book, as_of=START_MS)
        fresh_but_slow = state.model_copy(update={"latency_ms": 30_000.0})
        assert fresh_but_slow.age_ms == 0
        assert fresh_but_slow.latency_ms == 30_000.0

        # Old data, fast transport.
        stale_but_quick = venue_state_from_book(
            make_book("VENUE_A", "BTC-USD", 100.0, ts=START_MS - 30_000),
            as_of=START_MS,
        ).model_copy(update={"latency_ms": 1.0})
        assert stale_but_quick.age_ms == 30_000
        assert stale_but_quick.latency_ms == 1.0
        assert isinstance(fresh_but_slow, VenueMarketState)

    async def test_high_transport_latency_fires_after_its_confirmations(self):
        """§15. Fresh data, slow transport: the trigger now sees the number
        its name promises and engages once the streak is met."""
        engine = switch()
        threshold = engine.settings.risk.max_data_age_ms * 5
        fired = []
        for _ in range(CONFIRMATIONS["EXCESSIVE_LATENCY"]):
            fired = await engine.evaluate(inputs(max_latency_ms=threshold + 1.0))
        assert "EXCESSIVE_LATENCY" in fired
        assert not engine.state.trading_allowed

    async def test_stale_data_with_quick_transport_does_not_fire_it(self):
        """§15, the other half. High age with low latency is a staleness
        problem, and staleness has its own protections; this trigger must not
        double as a second one under a misleading name."""
        engine = switch()
        for _ in range(CONFIRMATIONS["EXCESSIVE_LATENCY"] + 2):
            fired = await engine.evaluate(inputs(max_latency_ms=1.0))
            assert "EXCESSIVE_LATENCY" not in fired
        assert "EXCESSIVE_LATENCY" not in engine.state.triggered_by

    def test_the_threshold_itself_is_unchanged(self):
        """§14 — P5-10 is an input-semantics fix, not a calibration one."""
        import risk.kill_switch as ks

        source = inspect.getsource(ks._latency)
        assert "inputs.max_latency_ms > settings.risk.max_data_age_ms * 5" in source

    def test_the_input_field_is_named_for_latency(self):
        assert "max_latency_ms" in KillSwitchInputs.__dataclass_fields__


class TestTriggerExceptionsFailClosed:
    """P5-12 — a mandatory safety predicate that cannot be evaluated has not
    been satisfied.

    It used to be logged and skipped, so a crashing predicate silently stopped
    protecting while the platform carried on trading believing it had one more
    safety condition than it did. The same fail-closed rule RUNE applies to an
    UNKNOWN gate now applies here."""

    def test_the_exception_path_engages_rather_than_continuing(self):
        source = inspect.getsource(KillSwitch.evaluate)
        after = source.split("except Exception:")[1]
        assert "self.engage(" in after
        assert '"SYSTEM_HEALTH_FAILURE",' in after
        assert "continue" in after, (
            "the loop still moves on to the remaining predicates; what changed "
            "is that it engages first rather than moving on silently"
        )

    def test_it_reuses_the_existing_health_response(self):
        """§17 — no new exposure trigger is invented for this."""
        source = inspect.getsource(KillSwitch.evaluate)
        assert "SYSTEM_HEALTH_FAILURE" in source.split("except Exception:")[1]
        assert "PREDICATE_FAILURE" not in source

    def test_the_exception_path_does_not_recurse(self):
        """§18 — ``engage`` applies state and actions directly. Neither the
        failing predicate nor ``evaluate`` is called again, so this stays safe
        even when the predicate that raised was SYSTEM_HEALTH_FAILURE."""
        after = inspect.getsource(KillSwitch.evaluate).split("except Exception:")[1]
        assert "self.evaluate(" not in after
        assert "predicate(" not in after

    async def test_a_raising_predicate_halts_trading(self, monkeypatch):
        import risk.kill_switch as ks

        def exploding(_inputs, _settings):
            raise RuntimeError("predicate exploded")

        monkeypatch.setitem(ks.TRIGGERS, "MAX_DRAWDOWN_BREACHED", exploding)
        engine = switch()
        fired = await engine.evaluate(inputs())
        assert "SYSTEM_HEALTH_FAILURE" in fired
        assert "SYSTEM_HEALTH_FAILURE" in engine.state.triggered_by
        assert not engine.state.trading_allowed

    async def test_the_exception_itself_does_not_propagate(self, monkeypatch):
        """§19 — one broken predicate must not take the evaluation loop with
        it; the remaining predicates still run."""
        import risk.kill_switch as ks

        def exploding(_inputs, _settings):
            raise RuntimeError("predicate exploded")

        monkeypatch.setitem(ks.TRIGGERS, "MAX_DRAWDOWN_BREACHED", exploding)
        engine = switch()
        fired = await engine.evaluate(inputs(book_corruption=True))
        assert "BOOK_CORRUPTION" in fired

    async def test_a_failing_health_predicate_still_fails_closed(self, monkeypatch):
        """§18's edge case: the predicate that raised IS the one whose
        trigger the exception path engages."""
        import risk.kill_switch as ks

        def exploding(_inputs, _settings):
            raise RuntimeError("health predicate exploded")

        monkeypatch.setitem(ks.TRIGGERS, "SYSTEM_HEALTH_FAILURE", exploding)
        engine = switch()
        fired = await engine.evaluate(inputs())
        assert "SYSTEM_HEALTH_FAILURE" in fired
        assert not engine.state.trading_allowed


class TestDisableExecutionRecovery:
    """H10 — the lifecycle after ``DISABLE_EXECUTION``."""

    def test_the_orchestrator_latches_the_executor_flag(self):
        source = inspect.getsource(Orchestrator._protect)
        assert "if self.kill_switch.state.execution_disabled:" in source
        assert "self.veska.executor.execution_disabled = True" in source

    def test_exactly_one_component_clears_the_executor_flag(self):
        """P5-5, inverted, and scoped.

        Nothing used to reset ``PaperExecutor.execution_disabled``, so a
        cleared switch left the platform permanently unable to submit. Now the
        ORCHESTRATOR does, and only the orchestrator: it is the component that
        applied the latch, and the kill switch must not hold a reference to
        the executor.
        """
        import apps.orchestrator.orchestrator as orch
        import execution.paper.executor as executor
        import risk.kill_switch as ks

        assert "execution_disabled = False" in inspect.getsource(orch)
        for module in (ks, executor):
            assert "execution_disabled = False" not in inspect.getsource(module), (
                f"{module.__name__} clears the flag; recovery belongs to the "
                "orchestrator, which owns application-side effects"
            )

    def test_the_kill_switch_holds_no_reference_to_the_executor(self):
        """§6 — architectural boundary. Safety state and trigger evaluation on
        one side, application effects on the other.

        Checked as imports and constructor dependencies rather than as any
        mention of the word: the module's prose necessarily explains which
        component owns the latch it cannot reach.
        """
        import risk.kill_switch as ks

        imported = [
            line.strip()
            for line in inspect.getsource(ks).splitlines()
            if line.startswith(("import ", "from "))
        ]
        for line in imported:
            for forbidden in ("execution", "veska", "apps."):
                assert forbidden not in line.lower(), (
                    f"risk.kill_switch imports {line!r}; it must not reach into "
                    "the components that apply its actions"
                )
        assert set(inspect.signature(KillSwitch.__init__).parameters) == {
            "self",
            "bus",
            "clock",
            "settings",
        }
        assert not any(
            hasattr(switch(), attr) for attr in ("executor", "veska", "orchestrator")
        )

    def test_clearing_is_never_automatic(self):
        """§7 — recovery requires an explicit operator request. If the unsafe
        condition still holds, the next protected tick engages again, which is
        correct rather than a failure of the clear."""
        callers = [
            name
            for name in (
                "_protect",
                "_manage",
                "_seek",
                "_measure",
                "_settle",
                "tick",
                "_heartbeat",
            )
            if "clear_kill_switch" in inspect.getsource(getattr(Orchestrator, name))
            or "kill_switch.clear(" in inspect.getsource(getattr(Orchestrator, name))
        ]
        assert callers == [], (
            f"the kill switch is cleared automatically from {callers}; a "
            "condition that stopped trading must be understood before trading "
            "resumes"
        )

    async def test_clearing_the_switch_resets_the_switch_state(self):
        """The premise: the switch itself does recover."""
        engine = switch()
        await engine.engage("RECONCILIATION_MISMATCH", "audit")
        assert engine.state.execution_disabled is True

        await engine.clear("operator investigated")
        assert engine.state.execution_disabled is False
        assert engine.state.trading_allowed

    async def test_a_recovery_path_exists_for_the_executor_latch(self):
        """H10, stated as the invariant.

        The orchestrator latches ``PaperExecutor.execution_disabled = True``.
        Something must be able to set it back, or a manual clear restores the
        kill-switch state while leaving the platform permanently unable to
        submit, with no operator action able to undo it for the life of the
        process (P5-5). ``Orchestrator.clear_kill_switch`` is that path.
        """
        import apps.orchestrator.orchestrator as orch
        import execution.paper.executor as executor
        import risk.kill_switch as ks
        from apps.api import app as api

        modules = {
            "apps.orchestrator.orchestrator": orch,
            "risk.kill_switch": ks,
            "execution.paper.executor": executor,
            "apps.api.app": api,
        }
        clears = sorted(
            name
            for name, module in modules.items()
            if "execution_disabled = False" in inspect.getsource(module)
        )
        assert clears, (
            "nothing re-enables PaperExecutor.execution_disabled. "
            "RECONCILIATION_MISMATCH and UNEXPECTED_POSITION latch it, "
            "KillSwitch.clear() resets only KillSwitchState, and the API "
            "exposes engage but no counterpart. Searched: "
            f"{sorted(modules)}"
        )

    def test_the_executor_refuses_submissions_while_disabled(self):
        """Confirms the flag is load-bearing, so the missing reset matters."""
        import execution.paper.executor as executor

        assert "if self.execution_disabled:" in inspect.getsource(executor)

    def test_which_triggers_reach_this_state(self):
        latching = sorted(
            name
            for name, actions in TRIGGER_ACTIONS.items()
            if KillAction.DISABLE_EXECUTION in actions
        )
        assert latching == ["RECONCILIATION_MISMATCH", "UNEXPECTED_POSITION"]


class TestFlattenAndLiveOrders:
    """H11 — can a previously-live opening order undo a flatten?"""

    #: Every trigger that flattens, derived rather than listed, so a trigger
    #: added later cannot slip past the invariant below.
    FLATTEN_TRIGGERS = sorted(
        name
        for name, actions in TRIGGER_ACTIONS.items()
        if KillAction.FLATTEN in actions
    )

    def test_something_actually_flattens(self):
        """Premise for the parametrisation: the list is not empty."""
        assert self.FLATTEN_TRIGGERS

    @pytest.mark.parametrize("trigger", FLATTEN_TRIGGERS)
    def test_every_flatten_trigger_also_cancels_resting_orders(self, trigger):
        """The P5-6 invariant, over EVERY flatten trigger rather than the two
        that originally failed it."""
        actions = TRIGGER_ACTIONS[trigger]
        assert KillAction.FLATTEN in actions, "premise: this trigger flattens"
        assert KillAction.CANCEL_ALL in actions, (
            f"{trigger} requests FLATTEN without CANCEL_ALL. An opening order "
            "that was already resting when the trigger fired can still fill "
            "after the flatten completes, re-opening the exposure the safety "
            f"action just closed. Actions: {[a.value for a in actions]}"
        )

    @pytest.mark.parametrize("trigger", FLATTEN_TRIGGERS)
    def test_no_flatten_trigger_disables_execution(self, trigger):
        """Closing a position is itself a submission. A trigger that must
        reduce exposure and also refuses to submit cannot carry out its own
        response."""
        actions = TRIGGER_ACTIONS[trigger]
        assert KillAction.DISABLE_EXECUTION not in actions, (
            f"{trigger} flattens but also disables execution, so the exit "
            "orders its own flatten needs can never be submitted"
        )

    def test_flatten_visits_the_state_whose_orders_are_still_live(self):
        """EXECUTING is where entry orders may still be resting or partly
        filled — exactly the record a flatten most needs to reach (P5-6)."""
        source = inspect.getsource(Orchestrator._flatten)
        assert "StrategyState.MONITORING" in source
        assert "StrategyState.EXECUTING" in source, (
            "_flatten skips records in EXECUTING — exactly the state whose "
            "entry orders are still live — so their orders are neither "
            "cancelled by the action set nor unwound by the flatten"
        )

    def test_executing_to_exiting_is_a_legal_transition(self):
        """Flattening an EXECUTING record needs no state-machine change."""
        from core.models.opportunity import STRATEGY_TRANSITIONS, StrategyState

        assert (
            StrategyState.EXITING in STRATEGY_TRANSITIONS[StrategyState.EXECUTING]
        )

    def test_the_exit_is_sized_from_the_position_not_the_authorisation(self):
        """A flattened EXECUTING record may be only partly filled. Sizing its
        exit from the notional RUNE authorised would try to close more than
        exists; the quantity actually held is the only correct amount."""
        source = inspect.getsource(Orchestrator._submit_exit)
        assert "quantity = abs(position.quantity)" in source
        assert "approved_notional" not in source.split("legs.append(")[0]

    def test_cancel_all_is_applied_before_flatten_in_the_tick(self):
        """Ordering matters: cancelling after flattening would leave the same
        window — the flatten closes the position, then a still-resting entry
        order fills and re-opens it."""
        source = inspect.getsource(Orchestrator._protect)
        cancel_at = source.index("if self.kill_switch.state.cancel_all_requested:")
        flatten_at = source.index("if self.kill_switch.state.flatten_requested:")
        assert cancel_at < flatten_at

    async def test_market_data_outage_holds_positions_deliberately(self):
        """§33 — recorded as a design decision, not a defect.

        Losing the feed cancels resting orders and stops new ones, but does not
        flatten: closing a position requires prices, and an outage is exactly
        when there are none.
        """
        actions = TRIGGER_ACTIONS["MARKET_DATA_OUTAGE"]
        assert KillAction.HALT_NEW_TRADES in actions
        assert KillAction.CANCEL_ALL in actions
        assert KillAction.FLATTEN not in actions


class TestKillSwitchStateSemantics:
    async def test_engaging_is_idempotent(self):
        engine = switch()
        first = await engine.engage("BOOK_CORRUPTION")
        second = await engine.engage("BOOK_CORRUPTION")
        assert first.triggered_by == second.triggered_by == ["BOOK_CORRUPTION"]
        assert len(engine.history) == 1

    async def test_trading_is_disallowed_the_moment_anything_engages(self):
        engine = switch()
        assert engine.state.trading_allowed
        await engine.engage("SYSTEM_HEALTH_FAILURE")
        assert not engine.state.trading_allowed

    async def test_clearing_resets_streaks_as_well_as_state(self):
        engine = switch()
        await engine.evaluate(inputs(market_data_ok=False))
        assert engine.streaks.get("MARKET_DATA_OUTAGE") == 1
        await engine.clear()
        assert engine.streaks == {}
        assert engine.state == KillSwitchState()

    async def test_a_condition_that_clears_before_confirmation_resets_its_streak(self):
        engine = switch()
        await engine.evaluate(inputs(market_data_ok=False))
        assert engine.streaks["MARKET_DATA_OUTAGE"] == 1
        await engine.evaluate(inputs(market_data_ok=True))
        assert "MARKET_DATA_OUTAGE" not in engine.streaks

    async def test_a_manual_engage_still_falls_back_to_the_live_clock(self):
        """An operator action happens outside any tick, so the clock remains
        the right default for it (§24)."""
        engine = switch()
        assert "now_ms" in inspect.signature(KillSwitch.engage).parameters
        engine.clock.advance(5_000)
        await engine.engage("BOOK_CORRUPTION")
        assert engine.state.triggered_at == START_MS + 5_000


class TestAutomaticSafetyEventsUseLogicalTime:
    """P5-16 — an automatic engagement belongs to the tick that observed the
    state justifying it.

    The economic actions never depended on which instant was stamped, but the
    recorded causal ordering of a safety event did: a live clock read at
    publish time could place the event after work the tick had already done,
    so a replay could order it differently from the run that produced it.
    """

    def test_the_interface_accepts_an_explicit_instant(self):
        for method in (KillSwitch.engage, KillSwitch.clear, KillSwitch.evaluate):
            assert "now_ms" in inspect.signature(method).parameters, method

    def test_the_clock_is_the_fallback_never_the_primary(self):
        for method in (KillSwitch.engage, KillSwitch.clear, KillSwitch.evaluate):
            source = inspect.getsource(method)
            assert "self.clock.now_ms() if now_ms is None else now_ms" in source

    async def test_an_engagement_is_stamped_at_the_instant_it_was_given(self):
        """§26. The clock is 5,000ms ahead; the decision was made at T."""
        engine = switch()
        engine.clock.advance(5_000)
        await engine.engage("BOOK_CORRUPTION", "audit", now_ms=START_MS)
        assert engine.state.triggered_at == START_MS
        assert engine.history[-1][0] == START_MS

    async def test_the_published_event_carries_the_same_instant(self):
        from core.events import EventType

        seen: list[tuple[int, int]] = []

        async def collect(event):
            seen.append((event.ts_ms, event.payload["created_at"]))

        engine = switch()
        engine.bus.subscribe(
            collect, types=[EventType.KILL_SWITCH_TRIGGERED], name="stamp-observer"
        )
        engine.clock.advance(5_000)
        await engine.engage("BOOK_CORRUPTION", "audit", now_ms=START_MS)
        await engine.bus.drain()
        assert seen == [(START_MS, START_MS)]

    async def test_evaluate_threads_one_instant_into_every_engagement(self):
        """§22 — including the confirmation path."""
        engine = switch()
        engine.clock.advance(5_000)
        fired = await engine.evaluate(inputs(book_corruption=True), now_ms=START_MS)
        assert "BOOK_CORRUPTION" in fired
        assert engine.state.triggered_at == START_MS

    async def test_the_fail_closed_path_uses_it_too(self, monkeypatch):
        """§22 — P5-12's engagement is an automatic one and must be stamped
        like any other."""
        import risk.kill_switch as ks

        def exploding(_inputs, _settings):
            raise RuntimeError("predicate exploded")

        monkeypatch.setitem(ks.TRIGGERS, "MAX_DRAWDOWN_BREACHED", exploding)
        engine = switch()
        engine.clock.advance(5_000)
        fired = await engine.evaluate(inputs(), now_ms=START_MS)
        assert "SYSTEM_HEALTH_FAILURE" in fired
        assert engine.state.triggered_at == START_MS

    async def test_a_clear_can_be_stamped_explicitly(self):
        from core.events import EventType

        seen: list[int] = []

        async def collect(event):
            seen.append(event.ts_ms)

        engine = switch()
        engine.bus.subscribe(
            collect, types=[EventType.KILL_SWITCH_CLEARED], name="clear-observer"
        )
        engine.clock.advance(5_000)
        await engine.clear("audit", now_ms=START_MS)
        await engine.bus.drain()
        assert seen == [START_MS]

    def test_the_orchestrator_passes_its_tick_time(self):
        """§23 — automatic evaluation uses the same canonical instant as the
        market snapshot, portfolio and risk decisions beside it."""
        source = inspect.getsource(Orchestrator._protect)
        assert "now_ms=self.tick_time," in source

    def test_the_operator_clear_reads_the_clock_once(self):
        """§25 — one read at the orchestration boundary, threaded through, so
        the state reset and its event share an instant."""
        source = inspect.getsource(Orchestrator.clear_kill_switch)
        assert source.count("self.clock.now_ms()") == 1
        assert "now_ms=now" in source


class TestOperatorRecoveryRestoresTheExecutor:
    """P5-5, end to end on a real platform.

    The switch latches ``PaperExecutor.execution_disabled`` through
    ``_protect``; clearing the switch's own state left that latch set, so the
    platform reported itself recovered while rejecting every submission for
    the rest of the process. ``Orchestrator.clear_kill_switch`` is the one
    coordinated path that undoes both halves.
    """

    @staticmethod
    def _plan(platform):
        from core.models.common import OrderType, Side, TimeInForce
        from core.models.opportunity import ExecutionPlan, PlannedOrder

        return ExecutionPlan(
            created_at=platform.orchestrator.tick_time,
            intent_id="recovery-probe-intent",
            strategy="cross_venue",
            symbol="BTC-USD",
            orders=[
                PlannedOrder(
                    venue="VENUE_A",
                    symbol="BTC-USD",
                    side=Side.BUY,
                    quantity=0.01,
                    order_type=OrderType.LIMIT,
                    time_in_force=TimeInForce.GTC,
                    limit_price=100.0,
                    expected_price=100.0,
                    expected_fee_bps=1.0,
                    ttl_ms=5_000,
                )
            ],
            deadline_ms=platform.orchestrator.tick_time + 10_000,
            max_slippage_bps=10.0,
            notional=1.0,
        )

    async def test_clearing_restores_the_platforms_ability_to_submit(self, platform):
        await platform.start(record=False, feeds=False)
        await platform.step_market(1)

        # 1-2. Engage a trigger that disables execution, then let the tick
        #      latch it onto the executor exactly as production does.
        await platform.kill_switch.engage("RECONCILIATION_MISMATCH", "audit")
        assert platform.kill_switch.state.execution_disabled is True
        await platform.orchestrator.tick()
        assert platform.veska.executor.execution_disabled is True

        # 3. Ordinary submission is refused while the latch is set.
        before = platform.veska.executor.rejected_submissions
        report = await platform.veska.executor.submit(
            self._plan(platform), platform.orchestrator.tick_time
        )
        assert platform.veska.executor.rejected_submissions == before + 1
        assert any("execution disabled" in note for note in report.notes)

        # 4. The explicit operator recovery.
        state = await platform.orchestrator.clear_kill_switch("operator investigated")

        # 5. Both halves are restored.
        assert not state.engaged
        assert state.execution_disabled is False
        assert state.trading_allowed
        assert platform.veska.executor.execution_disabled is False
        assert platform.state.kill_switch is state

        # 6. And a later submission is no longer refused for that reason.
        rejected = platform.veska.executor.rejected_submissions
        after = await platform.veska.executor.submit(
            self._plan(platform), platform.orchestrator.tick_time
        )
        assert platform.veska.executor.rejected_submissions == rejected
        assert not any("execution disabled" in note for note in after.notes)
        await platform.stop()

    async def test_the_api_clear_goes_through_the_orchestrator(self):
        """§8 — the endpoint must not call ``platform.kill_switch.clear``
        directly, because that restores only half the state."""
        from apps.api import app as api

        source = inspect.getsource(api)
        assert "platform.orchestrator.clear_kill_switch(reason)" in source
        assert "platform.kill_switch.clear(" not in source

    async def test_the_engage_endpoint_is_unchanged(self, platform):
        """§9 — recovery is a separate action; engaging still only stops
        things."""
        from apps.api import app as api

        source = inspect.getsource(api)
        assert "platform.kill_switch.engage(trigger, detail)" in source
        engage_block = source.split('@app.post("/api/kill-switch")')[1].split(
            '@app.post("/api/kill-switch/clear")'
        )[0]
        assert "execution_disabled = False" not in engage_block
        assert "clear" not in engage_block.split("async def engage")[1].split(
            "return"
        )[0]

    async def test_a_still_unsafe_condition_engages_again_on_the_next_tick(
        self, platform
    ):
        """§7 — clearing acknowledges an emergency; it does not resolve one.
        Re-engagement is the correct outcome, not a failed recovery."""
        await platform.start(record=False, feeds=False)
        await platform.step_market(1)
        await platform.kill_switch.engage("RECONCILIATION_MISMATCH", "audit")
        await platform.orchestrator.tick()
        assert platform.veska.executor.execution_disabled is True

        await platform.orchestrator.clear_kill_switch("operator investigated")
        assert platform.state.kill_switch.trading_allowed

        # Inject the same unsafe condition and tick again.
        import risk.kill_switch as ks

        original = ks.TRIGGERS["RECONCILIATION_MISMATCH"]
        ks.TRIGGERS["RECONCILIATION_MISMATCH"] = lambda _inputs, _settings: True
        try:
            await platform.step_market(1)
            await platform.orchestrator.tick()
        finally:
            ks.TRIGGERS["RECONCILIATION_MISMATCH"] = original

        assert "RECONCILIATION_MISMATCH" in platform.kill_switch.state.triggered_by
        assert not platform.state.kill_switch.trading_allowed
        await platform.stop()
