"""One conformance suite every Clock implementation must satisfy.

Determinism in this codebase rests on a single rule: domain code never calls
``time.time()``, it asks a Clock. That rule is only worth anything if both
implementations agree on what a Clock does — a ManualClock that behaved
differently from a SystemClock would mean the tests exercise a system that
does not exist.

The properties below are therefore written once and run against both.
Anything specific to one implementation (stepping, waiter inspection) is in
its own class and says so.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from core.clock import Clock, ManualClock, SystemClock

START_MS = 1_700_000_000_000


@pytest.fixture(params=["manual", "system"])
def clock(request) -> Clock:
    return ManualClock(start_ms=START_MS) if request.param == "manual" else SystemClock()


async def advance(clock: Clock, ms: int) -> None:
    """Move a clock forward, whichever kind it is."""
    if isinstance(clock, ManualClock):
        clock.advance(ms)
    else:
        await asyncio.sleep(ms / 1000.0)


class TestTheClockContract:
    def test_time_is_epoch_milliseconds(self, clock):
        """Not seconds, not nanoseconds, not a monotonic counter from zero."""
        now = clock.now_ms()
        assert isinstance(now, int)
        assert now > 1_600_000_000_000, f"{now} is not a millisecond epoch"
        assert now < 4_102_444_800_000, f"{now} is implausibly far in the future"

    def test_now_s_agrees_with_now_ms(self, clock):
        assert clock.now_s() == pytest.approx(clock.now_ms() / 1000.0, abs=0.5)

    def test_time_never_goes_backwards(self, clock):
        readings = [clock.now_ms() for _ in range(1_000)]
        assert readings == sorted(readings)

    async def test_time_moves_forward_when_advanced(self, clock):
        before = clock.now_ms()
        await advance(clock, 50)
        assert clock.now_ms() >= before + 40

    async def test_sleeping_zero_does_not_hang(self, clock):
        await asyncio.wait_for(clock.sleep(0), timeout=1.0)

    async def test_sleeping_a_negative_duration_does_not_hang(self, clock):
        """A deadline already past must return, not wait forever."""
        await asyncio.wait_for(clock.sleep(-1.0), timeout=1.0)

    async def test_a_sleep_yields_to_other_tasks(self, clock):
        """Cooperative multitasking must keep working under either clock."""
        order = []

        async def sleeper():
            order.append("before")
            await clock.sleep(0)
            order.append("after")

        async def other():
            order.append("other")

        task = asyncio.ensure_future(sleeper())
        await asyncio.sleep(0)
        await other()
        await asyncio.wait_for(task, timeout=1.0)
        assert order[0] == "before"
        assert "other" in order


class TestManualClockSpecifics:
    """Determinism guarantees only the manual clock can offer."""

    def test_it_refuses_to_move_backwards(self):
        clock = ManualClock(start_ms=START_MS)
        with pytest.raises(ValueError, match="backwards"):
            clock.set(START_MS - 1)

    def test_advancing_by_zero_is_allowed_and_changes_nothing(self):
        clock = ManualClock(start_ms=START_MS)
        clock.advance(0)
        assert clock.now_ms() == START_MS

    def test_two_clocks_with_the_same_seed_read_identically(self):
        a, b = ManualClock(start_ms=START_MS), ManualClock(start_ms=START_MS)
        for step in (1, 7, 250, 1_000):
            a.advance(step)
            b.advance(step)
            assert a.now_ms() == b.now_ms()

    def test_it_does_not_consume_wall_time(self):
        """A thousand simulated seconds must not take a thousand real ones."""
        clock = ManualClock(start_ms=START_MS)
        started = time.perf_counter()
        clock.advance(1_000_000)
        assert time.perf_counter() - started < 0.1
        assert clock.now_ms() == START_MS + 1_000_000

    async def test_a_sleeper_wakes_exactly_when_time_reaches_its_deadline(self):
        clock = ManualClock(start_ms=START_MS)
        woken = []

        async def sleeper():
            await clock.sleep(1.0)
            woken.append(clock.now_ms())

        task = asyncio.ensure_future(sleeper())
        await asyncio.sleep(0)
        assert clock.pending_sleepers == 1

        clock.advance(999)
        await asyncio.sleep(0)
        assert woken == [], "woke before its deadline"

        clock.advance(1)
        await asyncio.wait_for(task, timeout=1.0)
        assert woken == [START_MS + 1_000]

    async def test_sleepers_wake_in_deadline_order(self):
        """Out-of-order wakeups would make a replay non-deterministic."""
        clock = ManualClock(start_ms=START_MS)
        woken = []

        async def sleeper(name: str, seconds: float):
            await clock.sleep(seconds)
            woken.append(name)

        tasks = [
            asyncio.ensure_future(sleeper("third", 3.0)),
            asyncio.ensure_future(sleeper("first", 1.0)),
            asyncio.ensure_future(sleeper("second", 2.0)),
        ]
        await asyncio.sleep(0)
        clock.advance(3_000)
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=1.0)
        assert woken == ["first", "second", "third"]

    async def test_ties_are_broken_by_insertion_order(self):
        """Equal deadlines must still resolve to one fixed order."""
        clock = ManualClock(start_ms=START_MS)
        woken = []

        async def sleeper(name: str):
            await clock.sleep(1.0)
            woken.append(name)

        tasks = [asyncio.ensure_future(sleeper(f"s{i}")) for i in range(20)]
        await asyncio.sleep(0)
        clock.advance(1_000)
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=1.0)
        assert woken == [f"s{i}" for i in range(20)]

    def test_next_deadline_reports_the_earliest(self):
        clock = ManualClock(start_ms=START_MS)
        assert clock.next_deadline() is None


class TestSystemClockSpecifics:
    def test_it_tracks_wall_time(self):
        clock = SystemClock()
        assert abs(clock.now_ms() - int(time.time() * 1000)) < 1_000

    async def test_a_sleep_actually_takes_time(self):
        clock = SystemClock()
        started = time.perf_counter()
        await clock.sleep(0.05)
        assert time.perf_counter() - started >= 0.04


class TestNoDomainCodeReadsTheWallClockDirectly:
    """The rule the whole abstraction exists to enforce.

    A single wall-clock *timestamp* in domain code is enough to make a replay
    irreproducible, and it does not show up as a test failure — it shows up
    as two runs of the same recording disagreeing.

    The rule is about reading the current time, not about measuring elapsed
    duration. ``perf_counter`` returns a monotonic interval, never becomes a
    timestamp in recorded data, and is the only way to measure how long a
    real network call actually took — a logical clock cannot answer that,
    because under a logical clock no time passed. Those uses are allowed but
    confined, by name, below.
    """

    #: Where measuring real elapsed time is legitimate, and why.
    DURATION_ALLOWLIST = {
        # Reports how long the Anthropic API actually took, for observability.
        # A logical clock would report zero.
        "agents/lumen/provider.py",
    }

    @staticmethod
    def _scan(root, packages, pattern):
        import re

        expression = re.compile(pattern)
        hits = []
        for package in packages:
            for path in (root / package).rglob("*.py"):
                if "__pycache__" in path.parts:
                    continue
                for number, line in enumerate(path.read_text().splitlines(), 1):
                    if line.lstrip().startswith("#"):
                        continue
                    if expression.search(line):
                        hits.append((str(path.relative_to(root)), number, line.strip()))
        return hits

    DOMAIN_PACKAGES = (
        "agents",
        "apps",
        "execution",
        "replay",
        "risk",
        "simulation",
        "storage",
        "strategies",
        "venues",
    )

    def test_no_domain_module_reads_a_wall_clock_timestamp(self):
        """`time.time()` and `datetime.now()` are what break replay."""
        from pathlib import Path

        root = Path(__file__).resolve().parents[2]
        hits = self._scan(
            root, self.DOMAIN_PACKAGES, r"\btime\.time\s*\(|\bdatetime\.now\s*\("
        )
        assert not hits, (
            "domain code must take timestamps from a Clock:\n"
            + "\n".join(f"{p}:{n} {line}" for p, n, line in hits)
        )

    def test_elapsed_time_measurement_stays_where_it_is_declared(self):
        """Allowed, but it must not spread quietly."""
        from pathlib import Path

        root = Path(__file__).resolve().parents[2]
        hits = self._scan(
            root, self.DOMAIN_PACKAGES, r"\btime\.(perf_counter|monotonic)\s*\("
        )
        unexpected = [h for h in hits if h[0] not in self.DURATION_ALLOWLIST]
        assert not unexpected, (
            "measuring elapsed real time outside the declared allowlist:\n"
            + "\n".join(f"{p}:{n} {line}" for p, n, line in unexpected)
        )

    def test_only_the_system_clock_reads_wall_time_in_core(self):
        from pathlib import Path

        root = Path(__file__).resolve().parents[2]
        hits = self._scan(root, ("core",), r"\btime\.time\s*\(")
        files = {path for path, _, _ in hits}
        assert files == {"core/clock/__init__.py"}, files
