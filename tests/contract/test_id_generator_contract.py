"""One conformance suite every IdGenerator must satisfy — P0-C2.

The audit found identifiers minted with ``uuid4()`` even under replay, so no
replay could ever reproduce a run's identities: the economics matched and the
names did not, which makes a recorded session impossible to diff against its
own replay.

Two implementations now exist and they must agree on everything except the
one property that separates them — whether a fresh generator repeats itself.
That difference is the whole point, so it is asserted explicitly rather than
left implicit.
"""

from __future__ import annotations

import pytest

from core.ids import (
    DeterministicIdGenerator,
    IdGenerator,
    RandomIdGenerator,
    deterministic_ids,
    new_id,
    set_id_generator,
    using_id_generator,
)

NAMESPACES = ("fill", "ord", "opp", "intent", "session", "plan")


@pytest.fixture(params=["random", "deterministic"])
def generator(request) -> IdGenerator:
    if request.param == "random":
        return RandomIdGenerator()
    return DeterministicIdGenerator(seed="contract-seed")


class TestTheIdGeneratorContract:
    def test_ids_carry_their_namespace(self, generator):
        """`fill-...` is greppable in a log and self-describing in a payload."""
        for namespace in NAMESPACES:
            assert generator.new_id(namespace).startswith(f"{namespace}-")

    def test_ids_are_unique_within_a_generator(self, generator):
        minted = [generator.new_id("fill") for _ in range(10_000)]
        assert len(set(minted)) == 10_000

    def test_namespaces_do_not_collide(self, generator):
        """Two namespaces must not produce the same string."""
        minted = [generator.new_id(ns) for ns in NAMESPACES for _ in range(500)]
        assert len(set(minted)) == len(minted)

    def test_ids_are_opaque_and_url_safe(self, generator):
        """They end up in URLs, log lines and JSON keys."""
        import re

        for namespace in NAMESPACES:
            value = generator.new_id(namespace)
            assert re.fullmatch(r"[A-Za-z0-9_-]+", value), value

    def test_ids_are_a_sensible_length(self, generator):
        """Long enough not to collide, short enough to read in a log."""
        value = generator.new_id("fill")
        assert 12 <= len(value) <= 64, value

    def test_an_empty_namespace_is_still_usable(self, generator):
        assert generator.new_id("")


class TestDeterministicGeneration:
    """The property replay depends on."""

    def test_the_same_seed_produces_the_same_sequence(self):
        a = DeterministicIdGenerator(seed="run-1")
        b = DeterministicIdGenerator(seed="run-1")
        assert [a.new_id("fill") for _ in range(500)] == [
            b.new_id("fill") for _ in range(500)
        ]

    def test_a_different_seed_produces_a_different_sequence(self):
        a = DeterministicIdGenerator(seed="run-1")
        b = DeterministicIdGenerator(seed="run-2")
        assert a.new_id("fill") != b.new_id("fill")

    def test_namespaces_are_counted_independently(self):
        """Interleaving must not shift another namespace's sequence.

        Otherwise a replay that produced the same fills but a different
        number of, say, opportunities would rename every fill after the
        divergence, turning one difference into hundreds.
        """
        straight = DeterministicIdGenerator(seed="s")
        fills_alone = [straight.new_id("fill") for _ in range(20)]

        interleaved = DeterministicIdGenerator(seed="s")
        mixed = []
        for _ in range(20):
            interleaved.new_id("opp")
            interleaved.new_id("ord")
            mixed.append(interleaved.new_id("fill"))

        assert mixed == fills_alone

    def test_it_reports_how_many_it_has_issued(self):
        generator = DeterministicIdGenerator(seed="s")
        for _ in range(5):
            generator.new_id("fill")
        for _ in range(3):
            generator.new_id("ord")
        assert generator.issued == 8

    def test_reset_returns_it_to_the_start(self):
        generator = DeterministicIdGenerator(seed="s")
        first = [generator.new_id("fill") for _ in range(10)]
        generator.reset()
        assert [generator.new_id("fill") for _ in range(10)] == first
        assert generator.issued == 10


class TestRandomGeneration:
    """The converse property: live runs must not collide."""

    def test_two_fresh_generators_do_not_agree(self):
        a, b = RandomIdGenerator(), RandomIdGenerator()
        assert a.new_id("fill") != b.new_id("fill")

    def test_ids_do_not_collide_across_many_generators(self):
        minted = {RandomIdGenerator().new_id("fill") for _ in range(5_000)}
        assert len(minted) == 5_000

    def test_ids_carry_enough_entropy(self):
        """128 bits, so a long session cannot birthday-collide."""
        value = RandomIdGenerator().new_id("fill")
        assert len(value.split("-", 1)[1]) >= 32


class TestInstallation:
    """The module-level generator the whole platform mints through."""

    def test_the_default_generator_is_random(self):
        """A live session must never repeat a previous session's ids."""
        assert new_id("fill") != new_id("fill")

    def test_installing_a_generator_changes_what_new_id_returns(self):
        original = new_id("fill")
        generator = DeterministicIdGenerator(seed="installed")
        previous = set_id_generator(generator)
        try:
            assert new_id("fill") == DeterministicIdGenerator(seed="installed").new_id("fill")
        finally:
            set_id_generator(previous)
        assert new_id("fill") != original  # random again, not the deterministic one

    def test_the_context_manager_restores_the_previous_generator(self):
        before = new_id("fill")
        with using_id_generator(DeterministicIdGenerator(seed="scoped")):
            scoped = new_id("fill")
        after = new_id("fill")

        assert scoped != before
        assert scoped != after
        assert after != before, "the random generator was not restored"

    def test_the_context_manager_restores_after_an_exception(self):
        """A failing replay must not leave the process minting fixed ids."""
        with (
            pytest.raises(RuntimeError),
            using_id_generator(DeterministicIdGenerator(seed="scoped")),
        ):
            raise RuntimeError("replay blew up")
        assert new_id("fill") != new_id("fill"), "left installed after an error"

    def test_deterministic_ids_is_reproducible_and_scoped(self):
        def run() -> list[str]:
            with deterministic_ids("seed-42"):
                return [new_id("fill") for _ in range(50)]

        assert run() == run()
        assert new_id("fill") != new_id("fill")

    def test_nesting_restores_the_outer_generator(self):
        with using_id_generator(DeterministicIdGenerator(seed="outer")):
            outer_first = new_id("fill")
            with using_id_generator(DeterministicIdGenerator(seed="inner")):
                inner = new_id("fill")
            outer_second = new_id("fill")

        assert inner != outer_first
        # The outer generator resumed its own sequence rather than restarting.
        assert outer_second != outer_first
        expected = DeterministicIdGenerator(seed="outer")
        expected.new_id("fill")
        assert outer_second == expected.new_id("fill")


class TestModelsMintThroughTheInstalledGenerator:
    """Installing a generator is worthless if models bypass it."""

    def test_event_ids_follow_the_installed_generator(self):
        from core.events import Event, EventType

        def make() -> str:
            return Event(
                type=EventType.SYSTEM_EVENT, ts_ms=1, source="T", payload={}
            ).id

        with deterministic_ids("models"):
            first = make()
        with deterministic_ids("models"):
            second = make()
        assert first == second

    def test_fill_and_order_ids_follow_the_installed_generator(self):
        from core.models.common import OrderType, Side, TimeInForce
        from core.models.execution import FillEvent, PaperOrder

        def make() -> tuple[str, str]:
            order = PaperOrder(
                created_at=1,
                venue="VENUE_A",
                symbol="BTC-USD",
                side=Side.BUY,
                order_type=OrderType.LIMIT,
                time_in_force=TimeInForce.GTC,
                quantity=1.0,
                expected_price=50_000.0,
            )
            fill = FillEvent(
                created_at=1,
                client_order_id=order.client_order_id,
                venue="VENUE_A",
                symbol="BTC-USD",
                side=Side.BUY,
                quantity=1.0,
                price=50_000.0,
            )
            return order.client_order_id, fill.fill_id

        with deterministic_ids("models"):
            first = make()
        with deterministic_ids("models"):
            second = make()
        assert first == second

    def test_nothing_mints_an_identifier_with_uuid_directly(self):
        """The defect P0-C2 was: uuid4 called past the abstraction."""
        import re
        from pathlib import Path

        root = Path(__file__).resolve().parents[2]
        packages = (
            "agents",
            "apps",
            "core",
            "execution",
            "replay",
            "risk",
            "simulation",
            "storage",
            "strategies",
            "venues",
        )
        offenders = []
        for package in packages:
            for path in (root / package).rglob("*.py"):
                if "__pycache__" in path.parts or path == root / "core" / "ids.py":
                    continue
                for number, line in enumerate(path.read_text().splitlines(), 1):
                    if line.lstrip().startswith("#"):
                        continue
                    if re.search(r"\buuid4?\s*\(|\buuid\.uuid", line):
                        offenders.append(f"{path.relative_to(root)}:{number} {line.strip()}")
        assert not offenders, (
            "identifiers must be minted through core.ids so replay can fix them:\n"
            + "\n".join(offenders)
        )
