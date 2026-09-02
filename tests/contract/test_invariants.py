"""System-wide invariants — properties that must hold everywhere, forever.

The audit asked for these as a class: identity, time, configuration, state,
serialisation and the paper boundary. They are written against the whole
model and module tree rather than against named classes, so a type added
later is covered without anyone remembering to add a test for it.
"""

from __future__ import annotations

import inspect
import json
import math
import pkgutil
import re
from pathlib import Path

import pytest
from pydantic import BaseModel

from core.models.common import Base, Envelope

ROOT = Path(__file__).resolve().parents[2]

#: Packages that make up the platform. Tests and tooling are excluded.
PACKAGES = (
    "agents",
    "apps",
    "core",
    "execution",
    "monitoring",
    "replay",
    "risk",
    "simulation",
    "storage",
    "strategies",
    "venues",
)


def all_models() -> list[type[BaseModel]]:
    """Every Pydantic model reachable from the platform packages."""
    import importlib

    found: dict[str, type[BaseModel]] = {}
    for package in PACKAGES:
        module = importlib.import_module(package)
        for info in pkgutil.walk_packages(module.__path__, prefix=f"{package}."):
            try:
                submodule = importlib.import_module(info.name)
            except Exception:  # pragma: no cover - optional deps
                continue
            for _, obj in inspect.getmembers(submodule, inspect.isclass):
                if issubclass(obj, BaseModel) and obj not in (BaseModel, Base, Envelope):
                    found[f"{obj.__module__}.{obj.__qualname__}"] = obj
    return list(found.values())


def source_files() -> list[Path]:
    return [
        path
        for package in PACKAGES
        for path in (ROOT / package).rglob("*.py")
        if "__pycache__" not in path.parts
    ]


@pytest.fixture(scope="module")
def models() -> list[type[BaseModel]]:
    discovered = all_models()
    assert len(discovered) > 30, f"model discovery found only {len(discovered)}"
    return discovered


@pytest.fixture(scope="module")
def sources() -> list[Path]:
    found = source_files()
    assert len(found) > 30, f"source discovery found only {len(found)}"
    return found


class TestIdentity:
    def test_every_envelope_mints_a_unique_id(self, models):
        for model in models:
            if not issubclass(model, Envelope):
                continue
            field = model.model_fields.get("id")
            if field is None or field.default_factory is None:
                continue
            minted = {field.default_factory() for _ in range(200)}
            assert len(minted) == 200, f"{model.__name__} minted a duplicate id"

    def test_an_id_survives_serialisation(self):
        from core.events import Event, EventType

        event = Event(type=EventType.SYSTEM_EVENT, ts_ms=1, source="T", payload={})
        restored = Event.model_validate(json.loads(event.model_dump_json()))
        assert restored.id == event.id

    def test_identifiers_carry_a_namespace_prefix(self):
        """`fill-...` and `ord-...` are greppable in a log; a bare hex is not."""
        from core.ids import new_id

        for namespace in ("fill", "ord", "session", "opp"):
            assert new_id(namespace).startswith(f"{namespace}-")

    def test_event_sort_keys_are_a_total_order(self):
        """Replay ordering depends on no two events comparing equal."""
        from core.events import Event, EventType

        events = [
            Event(type=EventType.SYSTEM_EVENT, ts_ms=5, source="T", payload={}, sequence=1)
            for _ in range(200)
        ]
        keys = [e.sort_key() for e in events]
        assert len(set(keys)) == len(keys), "two events share a sort key"


class TestTime:
    def test_an_envelope_never_expires_before_it_was_created(self, models):
        for model in models:
            fields = model.model_fields
            if "created_at" not in fields or "expires_at" not in fields:
                continue
            instance = _try_build(model)
            if instance is None or getattr(instance, "expires_at", None) is None:
                continue
            assert instance.expires_at >= instance.created_at, (
                f"{model.__name__} expires before it exists"
            )

    def test_no_configured_duration_is_negative(self):
        from core.config import load_settings

        settings = load_settings()
        for name, value in _numeric_leaves(settings):
            if name.endswith(("_ms", "_s")):
                assert value >= 0, f"{name} is a negative duration ({value})"

    def test_a_negative_ttl_is_refused(self):
        from core.models.opportunity import PlannedOrder

        with pytest.raises(ValueError):
            PlannedOrder.model_validate(_planned_order_fields(ttl_ms=-1))

    def test_the_manual_clock_never_goes_backwards(self):
        from core.clock import ManualClock

        clock = ManualClock(start_ms=1_000)
        seen = [clock.now_ms()]
        for _ in range(100):
            clock.advance(1)
            seen.append(clock.now_ms())
        assert seen == sorted(seen)
        assert len(set(seen)) == len(seen)


class TestConfiguration:
    def test_every_numeric_setting_declares_a_constraint(self):
        """An unconstrained number is a value nobody checked.

        Constraints are what turn a nonsensical environment variable into a
        refusal at startup instead of behaviour nobody predicted.
        """
        import core.config.settings as module

        unconstrained = []
        for _, obj in inspect.getmembers(module, inspect.isclass):
            if not (issubclass(obj, BaseModel) and obj.__module__ == module.__name__):
                continue
            for name, field in obj.model_fields.items():
                annotation = str(field.annotation)
                if "int" not in annotation and "float" not in annotation:
                    continue
                if "dict" in annotation or "list" in annotation:
                    continue
                if not field.metadata:
                    unconstrained.append(f"{obj.__name__}.{name}")
        assert not unconstrained, f"numeric settings with no bounds: {unconstrained}"

    def test_every_settings_model_forbids_unknown_keys(self):
        """A typo in a config file must not be silently discarded."""
        import core.config.settings as module

        for _, obj in inspect.getmembers(module, inspect.isclass):
            if issubclass(obj, BaseModel) and obj.__module__ == module.__name__:
                assert obj.model_config.get("extra") == "forbid", (
                    f"{obj.__name__} silently accepts unknown keys"
                )

    def test_no_module_defines_its_own_tolerance_constant(self, sources):
        """Duplicated epsilons drift; MARIN's had already been copied.

        Two constants that happen to be equal today are two constants that
        can stop being equal, and reconciliation comparing on a different
        tolerance from the rest of the system reports arithmetic mismatches
        that are really configuration mismatches.

        This checks *definitions*, not every appearance of a small float.
        Inline guards against division by zero (`max(1e-9, denominator)`) and
        thresholds a single algorithm picked for itself are a different
        concept from a shared comparison tolerance, and folding them in here
        would mean retuning numerical behaviour under the banner of tidying
        constants. Where a literal was exactly a shared tolerance it now
        refers to it by name; where it was not, it was left alone.
        """
        definition = re.compile(r"^\s*[A-Z][A-Z0-9_]*\s*(?::\s*float\s*)?=\s*1e-\d+\s*(?:#.*)?$")
        found = []
        for path in sources:
            for number, line in enumerate(path.read_text().splitlines(), 1):
                if definition.match(line):
                    found.append(f"{path.relative_to(ROOT)}:{number} {line.strip()}")
        offenders = [item for item in found if "core/models/common.py" not in item]
        assert not offenders, (
            "tolerances must be defined once in core/models/common.py and referred "
            "to by name; found extra definitions:\n" + "\n".join(offenders)
        )
        assert len(found) == 3, f"expected the three shared epsilons, found: {found}"

    def test_marins_tolerances_are_the_shared_ones(self):
        """The specific duplication the audit found, pinned."""
        from agents.marin.agent import CASH_TOLERANCE, QTY_TOLERANCE
        from core.models.common import MONEY_EPSILON, QTY_EPSILON

        assert CASH_TOLERANCE is MONEY_EPSILON
        assert QTY_TOLERANCE is QTY_EPSILON


class TestState:
    def test_terminal_order_states_have_no_way_out(self):
        from core.models.execution import ORDER_TRANSITIONS, TERMINAL_STATUSES

        for status in TERMINAL_STATUSES:
            assert ORDER_TRANSITIONS[status] == set(), f"{status} is not really terminal"

    def test_every_order_status_appears_in_the_transition_table(self):
        from core.models.execution import ORDER_TRANSITIONS, OrderStatus

        assert set(ORDER_TRANSITIONS) == set(OrderStatus)

    def test_every_transition_target_is_a_real_status(self):
        from core.models.execution import ORDER_TRANSITIONS, OrderStatus

        for source, targets in ORDER_TRANSITIONS.items():
            for target in targets:
                assert target in OrderStatus, f"{source} -> {target} is not a status"

    def test_live_and_terminal_are_mutually_exclusive(self):
        from core.models.execution import OrderStatus, PaperOrder

        for status in OrderStatus:
            order = PaperOrder.model_validate({**_paper_order_fields(), "status": status})
            assert not (order.is_live and order.is_terminal)

    def test_unknown_is_neither_live_nor_terminal(self):
        """It is a real state: not assumed filled, not assumed failed."""
        from core.models.execution import OrderStatus, PaperOrder

        order = PaperOrder.model_validate(
            {**_paper_order_fields(), "status": OrderStatus.UNKNOWN}
        )
        assert not order.is_live
        assert not order.is_terminal

    def test_every_status_is_reachable_from_created(self):
        """A status nothing can reach is dead code in the state machine."""
        from core.models.execution import ORDER_TRANSITIONS, OrderStatus

        seen = {OrderStatus.CREATED}
        frontier = [OrderStatus.CREATED]
        while frontier:
            for target in ORDER_TRANSITIONS[frontier.pop()]:
                if target not in seen:
                    seen.add(target)
                    frontier.append(target)
        assert seen == set(OrderStatus), f"unreachable: {set(OrderStatus) - seen}"


class TestSerialisation:
    def test_every_envelope_serialises_to_strict_json(self, models):
        def reject(token):  # pragma: no cover - only on a violation
            raise AssertionError(f"non-JSON constant {token}")

        for model in models:
            if not issubclass(model, Envelope):
                continue
            instance = _try_build(model)
            if instance is None:
                continue
            payload = instance.to_json_dict()
            json.loads(json.dumps(payload), parse_constant=reject)

    def test_non_finite_values_are_sanitised_wherever_they_appear(self):
        from core.models.common import sanitize_json

        payload = sanitize_json(
            {
                "a": math.inf,
                "b": [1.0, -math.inf, {"c": math.nan}],
                "d": {"e": {"f": math.inf}},
                "g": "inf",
            }
        )
        assert payload == {
            "a": None,
            "b": [1.0, None, {"c": None}],
            "d": {"e": {"f": None}},
            "g": "inf",
        }

    def test_sanitising_leaves_ordinary_values_alone(self):
        from core.models.common import sanitize_json

        original = {"a": 1, "b": 2.5, "c": "x", "d": None, "e": True, "f": [1, 2]}
        assert sanitize_json(original) == original


class TestPaperBoundary:
    """The boundary is structural. These check the source, not the runtime.

    The existing tests assert that PaperExecutor is the only Executor and
    that no adapter claims order submission. Those are runtime facts about
    the objects that happen to be loaded; these are facts about what is in
    the tree at all.
    """

    def test_no_module_imports_an_exchange_trading_sdk(self, sources):
        forbidden = ("ccxt", "binance.client", "coinbasepro", "krakenex", "ib_insync")
        for path in sources:
            text = path.read_text()
            for name in forbidden:
                assert f"import {name}" not in text, f"{path.relative_to(ROOT)} imports {name}"

    def test_nothing_signs_a_request(self, sources):
        """Authenticated exchange calls need a signature. Nothing here makes one."""
        signing = re.compile(r"\bhmac\b|\bapi_secret\b|\bapiSecret\b|X-MBX-APIKEY|CB-ACCESS-SIGN")
        for path in sources:
            for number, line in enumerate(path.read_text().splitlines(), 1):
                if line.lstrip().startswith("#"):
                    continue
                assert not signing.search(line), (
                    f"{path.relative_to(ROOT)}:{number} looks like request signing: "
                    f"{line.strip()}"
                )

    def test_no_private_key_or_wallet_handling_exists(self, sources):
        forbidden = re.compile(r"\bprivate_key\b|\bmnemonic\b|\bseed_phrase\b|\bkeystore\b")
        for path in sources:
            for number, line in enumerate(path.read_text().splitlines(), 1):
                if line.lstrip().startswith("#"):
                    continue
                assert not forbidden.search(line), (
                    f"{path.relative_to(ROOT)}:{number} handles key material"
                )

    def test_venue_adapters_never_issue_a_writing_http_request(self):
        """Read-only means GET. A POST to a venue is an order or a withdrawal."""
        writing = re.compile(r"\.(post|put|delete|patch)\s*\(", re.IGNORECASE)
        for path in (ROOT / "venues").rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            for number, line in enumerate(path.read_text().splitlines(), 1):
                assert not writing.search(line), (
                    f"{path.relative_to(ROOT)}:{number} writes to a venue: {line.strip()}"
                )

    def test_no_withdrawal_or_transfer_path_exists(self, sources):
        forbidden = re.compile(r"\bdef\s+(withdraw|transfer_funds|send_funds)\b")
        for path in sources:
            assert not forbidden.search(path.read_text()), (
                f"{path.relative_to(ROOT)} defines a funds-movement function"
            )

    def test_paper_mode_is_the_only_mode_that_exists(self):
        from core.models.common import TradingMode

        assert [mode.value for mode in TradingMode] == ["PAPER"]


# -- helpers ---------------------------------------------------------------


def _numeric_leaves(model: BaseModel, prefix: str = ""):
    for name, value in model:
        path = f"{prefix}{name}"
        if isinstance(value, BaseModel):
            yield from _numeric_leaves(value, prefix=f"{path}.")
        elif isinstance(value, bool):
            continue
        elif isinstance(value, (int, float)):
            yield path, value


def _paper_order_fields(**overrides) -> dict:
    fields = {
        "created_at": 1_700_000_000_000,
        "venue": "VENUE_A",
        "symbol": "BTC-USD",
        "side": "BUY",
        "order_type": "LIMIT",
        "time_in_force": "GTC",
        "quantity": 1.0,
        "expected_price": 50_000.0,
    }
    fields.update(overrides)
    return fields


def _planned_order_fields(**overrides) -> dict:
    fields = {
        "venue": "VENUE_A",
        "symbol": "BTC-USD",
        "side": "BUY",
        "order_type": "LIMIT",
        "time_in_force": "GTC",
        "quantity": 1.0,
        "expected_price": 50_000.0,
    }
    fields.update(overrides)
    return fields


def _try_build(model: type[BaseModel]) -> BaseModel | None:
    """Instantiate a model from its defaults, or give up quietly.

    Models with required domain fields are covered by their own tests; this
    sweep exists to catch the ones nobody wrote a test for.
    """
    try:
        return model()
    except Exception:
        return None
