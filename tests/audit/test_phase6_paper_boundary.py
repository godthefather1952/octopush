"""H33, H34, H35 — the paper boundary, and that later phases did not move it.

H33: THE GATEWAY SEAM IS EMPTY
==============================
Phase 6 added ``ExecutionVenueGateway`` as the shape a future authenticated
adapter would take. The safety property is that it is *only* a shape: no
implementation, no construction site, no credential, no HTTP, no signing.

H34: LATER FRAMEWORKS MUST NOT SUBSTITUTE THE EXECUTOR
======================================================
The baseline this audit runs against contains Phases 7 through 12 — including
the Phase 11 ``OperationalProfile`` and the Phase 12 SHADOW profile. A shadow
session is explicitly *not* a trading mode: it runs the same ``PaperExecutor``
against the same ``PaperAccount``. These tests construct each profile and feed
combination and assert exactly that.

**No network is contacted.** The live-feed cases construct settings and inspect
wiring statically; nothing is started.

H35: SHADOW OBSERVES, IT DOES NOT EXECUTE
=========================================
``ShadowObserver`` is given a bus and a registry. It must hold no executor
reference, call nothing on VESKA, and publish nothing — a second execution path
through the observer would mean a shadow session was rehearsing a different
platform from the one it is supposed to be watching.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from core.models.common import TradingMode
from core.models.runtime import FeedKind, OperationalProfile
from execution.gateway import ExecutionVenueGateway
from execution.paper.executor import PAPER_CAPABILITIES, PaperExecutor
from execution.veska import Veska
from tests.audit.veska_fixtures import audit_settings, build_harness

#: Every package the audit scans for a live-execution surface.
PRODUCTION_ROOTS = ("apps", "agents", "core", "execution", "risk", "strategies")


class TestTheGatewaySeamIsEmpty:
    """H33 — an interface, and nothing behind it."""

    def test_the_gateway_is_abstract(self):
        assert inspect.isabstract(ExecutionVenueGateway)
        with pytest.raises(TypeError):
            ExecutionVenueGateway()  # type: ignore[abstract]

    def test_nothing_implements_the_gateway(self):
        assert ExecutionVenueGateway.__subclasses__() == [], (
            "a concrete venue gateway now exists: "
            f"{ExecutionVenueGateway.__subclasses__()}"
        )

    def test_a_gateway_declares_itself_non_paper(self):
        """The one field the composition root's guard reads."""
        assert ExecutionVenueGateway.is_paper is False

    def test_nothing_constructs_a_gateway(self):
        callers: list[str] = []
        for root in PRODUCTION_ROOTS:
            for path in Path(root).rglob("*.py"):
                if path.as_posix() == "execution/gateway.py":
                    continue
                text = path.read_text()
                if "ExecutionVenueGateway(" in text:
                    callers.append(path.as_posix())
        assert callers == []

    def test_the_gateway_module_contains_no_transport(self):
        text = Path("execution/gateway.py").read_text()
        tree = ast.parse(text)
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        forbidden = {
            "httpx",
            "requests",
            "aiohttp",
            "websockets",
            "websocket",
            "urllib",
            "http",
            "socket",
            "ssl",
            "hmac",
            "hashlib",
        }
        assert imported & forbidden == set(), (
            f"the gateway module imports {imported & forbidden}"
        )


class TestNoLiveExecutionSurfaceExists:
    """The structural claim, checked over the whole production tree."""

    def test_there_is_no_live_executor(self):
        offenders: list[str] = []
        for root in PRODUCTION_ROOTS:
            for path in Path(root).rglob("*.py"):
                text = path.read_text()
                if "class LiveExecutor" in text:
                    offenders.append(path.as_posix())
        assert offenders == []

    def test_the_executor_interface_has_exactly_one_implementation(self):
        from execution.veska.executor import Executor

        concrete = [
            klass
            for klass in Executor.__subclasses__()
            if not inspect.isabstract(klass)
        ]
        assert concrete == [PaperExecutor], (
            f"the Executor interface has implementations {concrete}"
        )

    def test_trading_mode_has_only_paper(self):
        assert [member.value for member in TradingMode] == ["PAPER"]

    def test_no_execution_module_carries_a_credential(self):
        offenders: list[tuple[str, str]] = []
        needles = (
            "api_key",
            "api_secret",
            "secret_key",
            "passphrase",
            "X-MBX-APIKEY",
            "CB-ACCESS",
        )
        for path in Path("execution").rglob("*.py"):
            text = path.read_text()
            for line in text.splitlines():
                stripped = line.strip()
                if stripped.startswith("#") or stripped.startswith("*"):
                    continue
                for needle in needles:
                    if needle in stripped:
                        offenders.append((path.as_posix(), stripped))
        assert offenders == [], f"credential-shaped code in execution: {offenders}"

    def test_the_paper_executor_declares_itself_paper(self):
        assert PAPER_CAPABILITIES.is_paper is True
        assert PaperExecutor.is_paper is True


class TestVeskaRefusesANonPaperExecutor:
    """The composition-root guard, exercised rather than read."""

    def test_construction_refuses_an_executor_that_is_not_paper(self):
        harness = build_harness()

        class NotPaper(PaperExecutor):
            is_paper = False

        impostor = NotPaper(
            bus=harness.bus,
            clock=harness.clock,
            settings=harness.settings,
            oms=harness.oms,
            account=harness.account,
            simulator=harness.simulator,
            is_paper=False,
        )
        with pytest.raises(RuntimeError, match="paper executors only"):
            Veska(
                harness.bus,
                harness.clock,
                harness.settings,
                harness.health,
                impostor,
            )

    def test_the_guard_is_the_first_thing_the_constructor_does(self):
        source = inspect.getsource(Veska.__init__)
        guard_at = source.index("if not executor.is_paper:")
        assign_at = source.index("self.executor = executor")
        assert guard_at < assign_at


class TestProfilesDoNotSubstituteTheExecutor:
    """H34 — every profile and feed combination the build permits."""

    @pytest.mark.parametrize(
        "profile", [OperationalProfile.PAPER, OperationalProfile.SHADOW]
    )
    @pytest.mark.parametrize("feed", ["simulated", "live"])
    def test_the_trading_mode_is_paper_in_every_combination(
        self, profile: OperationalProfile, feed: str, monkeypatch
    ):
        """Settings are constructed, never started. No network is touched."""
        monkeypatch.setenv("TF_PROFILE", profile.value)
        monkeypatch.setenv("TF_FEED", feed)
        settings = audit_settings()

        assert settings.mode is TradingMode.PAPER
        assert settings.operational_profile is profile
        assert settings.feed is (
            FeedKind.LIVE if feed == "live" else FeedKind.SIMULATED
        )

    def test_no_profile_value_maps_to_a_trading_mode(self):
        """A profile must not be able to become a mode by any route."""
        for profile in OperationalProfile:
            assert profile.value not in {m.value for m in TradingMode} or (
                profile.value == "PAPER"
            )
        assert "SHADOW" not in {m.value for m in TradingMode}

    def test_the_composition_root_constructs_paper_execution_unconditionally(
        self,
    ):
        """Static: no branch on profile or feed reaches executor construction."""
        from apps.orchestrator import wiring

        source = inspect.getsource(wiring.build_platform)
        assert "executor = PaperExecutor(" in source

        tree = ast.parse(source)
        construction_line = next(
            node.lineno
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "PaperExecutor"
        )
        guarding: list[str] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.If):
                continue
            end = max(
                getattr(child, "lineno", node.lineno) for child in ast.walk(node)
            )
            if node.lineno <= construction_line <= end:
                guarding.append(ast.unparse(node.test))
        assert guarding == [], (
            f"PaperExecutor construction is guarded by {guarding}: a profile "
            "or feed can now decide which executor the platform gets"
        )

    def test_veska_is_constructed_with_that_same_executor(self):
        from apps.orchestrator import wiring

        source = inspect.getsource(wiring.build_platform)
        assert "veska = Veska(bus, clock, settings, health, executor)" in source

    def test_only_one_executor_is_constructed_anywhere_in_wiring(self):
        from apps.orchestrator import wiring

        source = inspect.getsource(wiring)
        assert source.count("PaperExecutor(") == 1
        assert "Executor(" not in source.replace("PaperExecutor(", "")


class TestShadowDoesNotExecute:
    """H35 — the observer watches the same lifecycle; it does not drive one."""

    def test_the_observer_takes_only_a_bus_and_a_registry(self):
        from apps.shadow.observer import ShadowObserver

        parameters = set(inspect.signature(ShadowObserver.__init__).parameters)
        assert parameters == {"self", "bus", "registry", "enabled"}

    def test_the_observer_holds_no_execution_collaborator(self):
        from apps.shadow.observer import ShadowObserver

        source = inspect.getsource(ShadowObserver)
        tree = ast.parse(source)
        attributes: set[str] = set()
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == "self"
            ):
                attributes.add(node.attr)
        forbidden = {"veska", "executor", "account", "oms", "orchestrator", "okapi"}
        assert attributes & forbidden == set(), (
            f"the shadow observer holds {attributes & forbidden}"
        )

    def test_the_observer_never_submits_cancels_or_resolves(self):
        from apps.shadow.observer import ShadowObserver

        source = inspect.getsource(ShadowObserver)
        for forbidden in (
            ".submit(",
            ".cancel(",
            ".cancel_all(",
            ".cancel_plan(",
            ".resolve_unknown(",
            ".execute(",
            ".build_plan(",
            ".apply_fill(",
        ):
            assert forbidden not in source, (
                f"the shadow observer calls {forbidden}"
            )

    def test_the_observer_never_publishes(self):
        from apps.shadow.observer import ShadowObserver

        source = inspect.getsource(ShadowObserver)
        assert ".publish(" not in source

    def test_the_shadow_registry_holds_no_executor(self):
        from apps.shadow.registry import ShadowRegistry

        source = inspect.getsource(ShadowRegistry)
        for forbidden in (".submit(", ".cancel(", ".resolve_unknown(", ".publish("):
            assert forbidden not in source

    def test_the_shadow_models_label_execution_as_simulated(self):
        """A paper fill must never be representable as a venue fill."""
        from core.models.shadow import (
            ExecutionProvenance,
            ShadowExecutionRecord,
        )

        record = ShadowExecutionRecord(
            decision_id="d", created_at=0, updated_at=0
        )
        assert record.execution_provenance is ExecutionProvenance.PAPER_SIMULATOR

    def test_nothing_produces_a_venue_reported_provenance(self):
        from core.models.shadow import ExecutionProvenance

        producers: list[str] = []
        for root in PRODUCTION_ROOTS:
            for path in Path(root).rglob("*.py"):
                if path.as_posix() == "core/models/shadow.py":
                    continue
                if "ExecutionProvenance.VENUE_REPORTED" in path.read_text():
                    producers.append(path.as_posix())
        assert producers == [], (
            f"{producers} can label execution as venue-reported, and no "
            "authenticated venue exists to report it"
        )
        assert ExecutionProvenance.VENUE_REPORTED.value == "VENUE_REPORTED"


class TestBaselineProtection:
    """The audit itself must contain no production change.

    These checks inspect Python syntax rather than raw source substrings, so
    prose describing a forbidden construct does not trip the protection.
    """

    @staticmethod
    def _trees():
        for path in Path("tests/audit").rglob("*.py"):
            yield path, ast.parse(path.read_text())

    def test_the_audit_package_contains_no_production_module(self):
        forbidden_classes = {"PaperExecutor", "Veska"}
        for path, tree in self._trees():
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    assert node.name != "build_platform", (
                        f"{path} defines production construction surface build_platform"
                    )
                elif isinstance(node, ast.ClassDef):
                    assert node.name not in forbidden_classes, (
                        f"{path} defines production class {node.name}"
                    )

    def test_the_audit_never_monkeypatches_execution_internals(self):
        """Fixtures may construct; they may not rewrite what they measure."""
        forbidden_roots = ("execution", "PaperExecutor", "FillSimulator")
        for path in Path("tests/audit").rglob("test_phase6_*.py"):
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                is_monkeypatch_setattr = (
                    isinstance(func, ast.Attribute)
                    and func.attr == "setattr"
                    and isinstance(func.value, ast.Name)
                    and func.value.id == "monkeypatch"
                )
                if not is_monkeypatch_setattr or not node.args:
                    continue
                target = ast.unparse(node.args[0])
                assert not target.startswith(forbidden_roots), (
                    f"{path} monkeypatches execution internals via {target}"
                )

    def test_the_audit_declares_no_skips_or_expected_failures(self):
        forbidden_marks = {"pytest.mark.skip", "pytest.mark.xfail"}
        for path in Path("tests/audit").rglob("test_phase6_*.py"):
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    call_name = ast.unparse(node.func)
                    assert call_name != "pytest.skip", (
                        f"{path} contains an actual pytest.skip() call"
                    )
                if isinstance(
                    node,
                    (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef),
                ):
                    for decorator in node.decorator_list:
                        target = decorator.func if isinstance(decorator, ast.Call) else decorator
                        decorator_name = ast.unparse(target)
                        assert decorator_name not in forbidden_marks, (
                            f"{path} contains forbidden decorator {decorator_name}"
                        )
