"""Phase 2 finalization: the replay CLI, as a user actually runs it.

Batch 2 closed with an admitted gap -- CLI flags were tested at the parser
level, not end to end -- and this is the final Phase 2 mission, so it closes
here. Every case below runs ``python -m replay`` in a real subprocess against
a real SQLite database, which is the only way to exercise the things that
only exist outside the library: argparse, ``load_settings()`` reading the
environment, resource lifecycle across the whole process, the JSON printed on
stdout, and the exit status.

The recorded session each test replays is produced once per module by running
the platform for real and recording it, so these are genuine recordings
rather than hand-assembled rows.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from apps.orchestrator.wiring import build_platform
from core.bus import InMemoryEventBus
from core.clock import ManualClock
from core.config import load_settings, simulated_venues
from replay.engine import config_digest
from simulation.market import default_market
from storage import SQLiteEventStore
from storage.base import SessionStatus
from tests.conftest import START_MS

REPO_ROOT = Path(__file__).resolve().parents[2]
TICKS = 30


def _env(db_path: Path, **extra: str) -> dict[str, str]:
    """A child process pointed at one throwaway database.

    ``TF_FEED=simulated`` keeps the venue set offline; nothing here may touch
    a network, and a replay never starts feeds anyway.
    """
    env = dict(os.environ)
    env.update(
        {
            "TF_STORAGE_BACKEND": "sqlite",
            "TF_SQLITE_PATH": str(db_path),
            "TF_FEED": "simulated",
            "TF_LOG_LEVEL": "ERROR",
            "PYTHONPATH": str(REPO_ROOT),
        }
    )
    env.update(extra)
    return env


def run_cli(db_path: Path, *args: str, env_extra: dict[str, str] | None = None):
    return subprocess.run(
        [sys.executable, "-m", "replay", *args],
        cwd=REPO_ROOT,
        env=_env(db_path, **(env_extra or {})),
        capture_output=True,
        text=True,
        timeout=300,
    )


def summary_of(result) -> dict:
    """The JSON summary the CLI prints, or a helpful failure.

    Located by its last top-level ``{`` line rather than the first ``{``
    anywhere: log output is JSON too, so a single stray log line would
    otherwise be parsed as the summary.
    """
    assert result.returncode == 0, (
        f"CLI failed ({result.returncode})\nstdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )
    lines = result.stdout.splitlines()
    starts = [i for i, line in enumerate(lines) if line == "{"]
    assert starts, f"no JSON summary in stdout:\n{result.stdout}"
    return json.loads("\n".join(lines[starts[-1] :]))


async def _record(db_path: Path) -> tuple[str, str]:
    """Record one real session into ``db_path``. Returns (id, config digest)."""
    settings = load_settings().model_copy(update={"venues": simulated_venues()})
    store = SQLiteEventStore(str(db_path))
    platform = build_platform(
        settings,
        clock=ManualClock(START_MS),
        bus=InMemoryEventBus(raise_on_handler_error=True),
        store=store,
        market=default_market(start_ms=START_MS),
        raise_on_handler_error=True,
    )
    await platform.start(record=True)
    for _ in range(TICKS):
        platform.clock.advance(100)
        await platform.step_market(1)
        await platform.orchestrator.tick()
    await platform.bus.drain()
    session_id = platform.session_id
    await platform.stop()
    return session_id, config_digest(settings.model_dump())


@pytest.fixture(scope="module")
def recorded(tmp_path_factory):
    """One recorded session, reused read-only by every test below."""
    import asyncio

    db_path = tmp_path_factory.mktemp("replay-cli") / "sessions.db"
    session_id, digest = asyncio.run(_record(db_path))
    return db_path, session_id, digest


async def _session_info(db_path: Path, session_id: str):
    store = SQLiteEventStore(str(db_path))
    await store.open()
    try:
        return await store.session(session_id)
    finally:
        await store.close()


async def _fingerprint(db_path: Path, session_id: str) -> dict:
    """Everything about a session that a replay must not change.

    A count alone is not enough on a durable backend: events could be
    replaced in place and the count would not move.
    """
    store = SQLiteEventStore(str(db_path))
    await store.open()
    try:
        events = [e async for e in store.read(session_id)]
        info = await store.session(session_id)
        return {
            "info": (
                info.session_id,
                info.started_at,
                info.ended_at,
                info.status.value,
                info.event_count,
                info.config_hash,
                info.label,
            ),
            "events": [
                (e.id, e.sequence, e.ts_ms, e.type.value, e.source, e.schema_version)
                for e in events
            ],
        }
    finally:
        await store.close()


class TestListing:
    def test_list_shows_the_session_with_its_status(self, recorded):
        db_path, session_id, _ = recorded
        result = run_cli(db_path, "--list")
        assert result.returncode == 0, result.stderr
        assert session_id in result.stdout
        assert "COMPLETE" in result.stdout

    def test_list_on_an_empty_database(self, tmp_path):
        result = run_cli(tmp_path / "empty.db", "--list")
        assert result.returncode == 0, result.stderr
        assert "no recorded sessions" in result.stdout


class TestNormalReplay:
    def test_a_complete_session_replays_exactly(self, recorded):
        db_path, session_id, digest = recorded
        summary = summary_of(run_cli(db_path, "--session", session_id))
        assert summary["session"] == session_id
        assert summary["recording_status"] == "COMPLETE"
        assert summary["config_hash_recorded"] == digest
        assert summary["mode"] == "FAST"
        assert summary["is_exact"] is True
        assert summary["fidelity_issues"] == []
        assert summary["timeline_fidelity"] is None
        assert summary["ticks"] == TICKS
        assert all(
            verdict == "VERIFIED" for verdict in summary["fidelity"].values()
        ), summary["fidelity"]

    def test_a_missing_session_fails_cleanly(self, recorded):
        db_path, _, _ = recorded
        result = run_cli(db_path, "--session", "no-such-session")
        assert result.returncode != 0
        assert "no such session" in (result.stdout + result.stderr)

    def test_session_is_required(self, recorded):
        db_path, _, _ = recorded
        result = run_cli(db_path)
        assert result.returncode != 0
        assert "--session is required" in result.stderr


class TestRealtimeFlags:
    def test_realtime_reports_its_mode_and_speed(self, recorded):
        db_path, session_id, _ = recorded
        # A large speed keeps the wall-clock cost of a genuinely paced replay
        # negligible while still exercising the REALTIME path end to end.
        summary = summary_of(
            run_cli(
                db_path, "--session", session_id, "--realtime", "--speed", "10000"
            )
        )
        assert summary["mode"] == "REALTIME"
        assert summary["speed"] == 10000.0
        assert summary["is_exact"] is True

    def test_realtime_produces_the_same_economics_as_fast(self, recorded):
        db_path, session_id, _ = recorded
        fast = summary_of(run_cli(db_path, "--session", session_id))
        real = summary_of(
            run_cli(
                db_path, "--session", session_id, "--realtime", "--speed", "10000"
            )
        )
        economic = (
            "ticks",
            "opportunities",
            "orders",
            "fills",
            "net_pnl",
            "fees",
            "drawdown",
            "reconciliation_ok",
            "kill_switch",
            "events_replayed",
            "ticks_replayed",
            "span_ms",
        )
        assert {k: fast[k] for k in economic} == {k: real[k] for k in economic}

    def test_speed_without_realtime_is_refused(self, recorded):
        db_path, session_id, _ = recorded
        result = run_cli(db_path, "--session", session_id, "--speed", "2")
        assert result.returncode != 0
        assert "--speed only applies with --realtime" in result.stderr

    def test_a_non_positive_speed_is_refused(self, recorded):
        db_path, session_id, _ = recorded
        result = run_cli(
            db_path, "--session", session_id, "--realtime", "--speed", "0"
        )
        assert result.returncode != 0
        assert "--speed must be positive" in result.stderr


class TestItemLimit:
    def test_max_items_stops_early_and_says_so(self, recorded):
        db_path, session_id, _ = recorded
        summary = summary_of(
            run_cli(db_path, "--session", session_id, "--max-items", "5")
        )
        assert summary["items_replayed"] == 5
        assert summary["stopped_early"] is True
        assert summary["is_exact"] is False, (
            "a partial run is not a reproduction of the whole session"
        )

    @pytest.mark.parametrize("alias", ["--step", "--max-events"])
    def test_the_deprecated_aliases_mean_the_same_thing(self, recorded, alias):
        db_path, session_id, _ = recorded
        summary = summary_of(run_cli(db_path, "--session", session_id, alias, "5"))
        assert summary["items_replayed"] == 5

    def test_a_limit_beyond_the_session_replays_all_of_it(self, recorded):
        db_path, session_id, _ = recorded
        summary = summary_of(
            run_cli(db_path, "--session", session_id, "--max-items", "100000")
        )
        assert summary["stopped_early"] is False
        assert summary["ticks"] == TICKS


class TestConfigFidelity:
    def test_a_changed_material_setting_is_refused(self, recorded):
        db_path, session_id, _ = recorded
        result = run_cli(
            db_path,
            "--session",
            session_id,
            env_extra={"TF_PAPER_INITIAL_BALANCE": "55000"},
        )
        assert result.returncode != 0
        combined = result.stdout + result.stderr
        assert "configuration digest" in combined
        assert "counterfactual" in combined

    def test_the_counterfactual_override_replays_and_labels_itself(self, recorded):
        db_path, session_id, _ = recorded
        summary = summary_of(
            run_cli(
                db_path,
                "--session",
                session_id,
                "--counterfactual",
                env_extra={"TF_PAPER_INITIAL_BALANCE": "55000"},
            )
        )
        assert summary["is_exact"] is False
        assert summary["fidelity"]["configuration"] == "COUNTERFACTUAL_CONFIGURATION"
        assert summary["ticks"] == TICKS, "...and it really did replay"


class TestRecordOutput:
    async def test_output_is_durable_and_distinct_from_the_source(self, recorded):
        db_path, session_id, digest = recorded
        before = await _fingerprint(db_path, session_id)

        summary = summary_of(
            run_cli(db_path, "--session", session_id, "--record-output")
        )

        output_id = summary["output_session_id"]
        assert output_id != session_id
        assert summary["output_backend"] == "sqlite"
        assert summary["output_event_count"] > 0
        assert summary["output_recording_status"] == SessionStatus.COMPLETE.value

        # Durable: read back from a NEW connection, after the process exited.
        info = await _session_info(db_path, output_id)
        assert info is not None, "the output session outlived the process"
        assert info.status is SessionStatus.COMPLETE
        assert info.event_count == summary["output_event_count"]
        assert info.label == f"replay-of-{session_id}"
        assert info.config_hash == digest

        # ...and the source session is untouched, event for event.
        assert await _fingerprint(db_path, session_id) == before

    async def test_without_the_flag_nothing_durable_is_written(self, recorded):
        db_path, session_id, _ = recorded
        store = SQLiteEventStore(str(db_path))
        await store.open()
        before = {s.session_id for s in await store.sessions()}
        await store.close()

        summary = summary_of(run_cli(db_path, "--session", session_id))
        assert "output_session_id" not in summary

        store = SQLiteEventStore(str(db_path))
        await store.open()
        after = {s.session_id for s in await store.sessions()}
        await store.close()
        assert after == before

    def test_a_memory_backend_refuses_rather_than_pretending(self, recorded):
        """The exact defect: recording into a store that vanishes at exit
        looked like it worked and produced nothing (P2-5)."""
        db_path, session_id, _ = recorded
        result = run_cli(
            db_path,
            "--session",
            session_id,
            "--record-output",
            env_extra={"TF_STORAGE_BACKEND": "memory"},
        )
        assert result.returncode != 0
        combined = result.stdout + result.stderr
        assert "durable storage backend" in combined

    async def test_a_partial_replay_says_so_in_the_output_label(self, recorded):
        """Recording a bounded replay is allowed -- calling it a recording of
        the whole session is not."""
        db_path, session_id, _ = recorded
        summary = summary_of(
            run_cli(
                db_path,
                "--session",
                session_id,
                "--record-output",
                "--max-items",
                "6",
            )
        )
        info = await _session_info(db_path, summary["output_session_id"])
        assert info is not None
        assert "partial" in info.label
        assert session_id in info.label
        assert info.status is SessionStatus.COMPLETE, (
            "complete AS A RECORDING of the partial replay that was requested"
        )
        assert summary["is_exact"] is False, (
            "...while the replay itself is not an exact reproduction"
        )

    async def test_two_recorded_replays_do_not_collide(self, recorded):
        """The output id must come from the live generator, not the
        deterministic one replay installs -- otherwise the second run would
        be refused for reusing a session id."""
        db_path, session_id, _ = recorded
        first = summary_of(
            run_cli(db_path, "--session", session_id, "--record-output")
        )
        second = summary_of(
            run_cli(db_path, "--session", session_id, "--record-output")
        )
        assert first["output_session_id"] != second["output_session_id"]
        for summary in (first, second):
            info = await _session_info(db_path, summary["output_session_id"])
            assert info is not None
            assert info.status is SessionStatus.COMPLETE


class TestRefusals:
    async def test_an_open_session_is_refused_and_the_flag_answers_it(self, tmp_path):
        db_path = tmp_path / "open.db"
        session_id, _ = await _record_unfinalised(db_path)

        refused = run_cli(db_path, "--session", session_id)
        assert refused.returncode != 0
        assert "OPEN" in (refused.stdout + refused.stderr)

        summary = summary_of(
            run_cli(
                db_path,
                "--session",
                session_id,
                "--allow-incomplete-session",
                "--counterfactual",
            )
        )
        assert summary["is_exact"] is False
        assert (
            summary["fidelity"]["recording_integrity"]
            == "UNVERIFIED_RECORDING_INTEGRITY"
        )

    def test_a_legacy_timeline_is_refused(self, tmp_path):
        """--counterfactual waives the (separate, also-unverifiable) config
        dimension so this asserts the TIMELINE refusal rather than whichever
        check happens to run first."""
        db_path = tmp_path / "legacy.db"
        _seed_marker_less(db_path)
        result = run_cli(
            db_path, "--session", "legacy-session", "--counterfactual"
        )
        assert result.returncode != 0
        combined = result.stdout + result.stderr
        assert "ORCHESTRATOR_TICK" in combined
        assert "legacy_timeline" in combined

    def test_a_legacy_timeline_replays_under_both_overrides(self, tmp_path):
        db_path = tmp_path / "legacy.db"
        _seed_marker_less(db_path)
        summary = summary_of(
            run_cli(
                db_path,
                "--session",
                "legacy-session",
                "--counterfactual",
                "--legacy-timeline",
            )
        )
        assert summary["is_exact"] is False
        assert summary["fidelity"]["timeline"] == "LEGACY_REPLAY_UNVERIFIED_TIMELINE"
        assert summary["fidelity"]["configuration"] == "UNVERIFIED_CONFIGURATION"
        assert len(summary["fidelity_issues"]) == 2, (
            "two dimensions are unverified at once, and both are reported -- "
            "the case a single fidelity string could not express"
        )


class TestTheCliClosesTheReplaySessionInProcess:
    """P2-4 at the CLI layer, which a subprocess test cannot reach.

    Every other case here runs the CLI in its own process, and a leaked
    process-global dies with that process -- so a CLI that never called
    ``session.close()`` would pass all of them. (It did, for the whole of
    Batch 2: the code carried an explicit note saying close() was deliberately
    not called.) This one runs ``replay()`` in THIS process and checks the
    globals afterwards, which is the only place the omission is visible.
    """

    async def test_a_successful_replay_restores_the_globals(
        self, recorded, monkeypatch
    ):
        from core.ids import current_generator, set_id_generator
        from core.logging import bind_clock
        from replay.__main__ import build_parser
        from replay.__main__ import replay as run_replay

        db_path, session_id, _ = recorded
        for key, value in _env(db_path).items():
            monkeypatch.setenv(key, value)

        before_ids = current_generator()
        before_clock = bind_clock(None)
        bind_clock(before_clock)
        try:
            args = build_parser().parse_args(["--session", session_id])
            await run_replay(args)

            assert current_generator() is before_ids, (
                "the CLI must put the id generator back -- otherwise the next "
                "thing to mint an id in this process gets replay's "
                "deterministic stream"
            )
            after_clock = bind_clock(None)
            bind_clock(after_clock)
            assert after_clock is before_clock
        finally:
            set_id_generator(before_ids)
            bind_clock(before_clock)

    async def test_a_refused_replay_restores_the_globals(
        self, recorded, monkeypatch
    ):
        from core.ids import current_generator, set_id_generator
        from core.logging import bind_clock
        from replay.__main__ import build_parser
        from replay.__main__ import replay as run_replay

        db_path, session_id, _ = recorded
        for key, value in _env(db_path).items():
            monkeypatch.setenv(key, value)
        monkeypatch.setenv("TF_PAPER_INITIAL_BALANCE", "55000")

        before_ids = current_generator()
        before_clock = bind_clock(None)
        bind_clock(before_clock)
        try:
            args = build_parser().parse_args(["--session", session_id])
            with pytest.raises(SystemExit):
                await run_replay(args)
            assert current_generator() is before_ids
            after_clock = bind_clock(None)
            bind_clock(after_clock)
            assert after_clock is before_clock
        finally:
            set_id_generator(before_ids)
            bind_clock(before_clock)


class TestResourceCleanup:
    async def test_the_database_is_reopenable_after_every_cli_case(self, recorded):
        """Nothing may be left holding the database or half-written."""
        db_path, session_id, _ = recorded
        for args in (
            ("--list",),
            ("--session", session_id),
            ("--session", session_id, "--max-items", "3"),
            ("--session", session_id, "--record-output"),
        ):
            run_cli(db_path, *args)
            store = SQLiteEventStore(str(db_path))
            await store.open()
            assert await store.session(session_id) is not None
            await store.close()

    async def test_a_refused_replay_leaves_no_output_session(self, recorded):
        db_path, session_id, _ = recorded
        store = SQLiteEventStore(str(db_path))
        await store.open()
        before = {s.session_id for s in await store.sessions()}
        await store.close()

        result = run_cli(
            db_path,
            "--session",
            session_id,
            "--record-output",
            env_extra={"TF_PAPER_INITIAL_BALANCE": "55000"},
        )
        assert result.returncode != 0

        store = SQLiteEventStore(str(db_path))
        await store.open()
        after = {s.session_id for s in await store.sessions()}
        sessions = {s.session_id: s for s in await store.sessions()}
        await store.close()

        # The output recorder does not start until the replay has been
        # accepted, so a refusal leaves no output session at all -- and
        # certainly not one finalised COMPLETE for a replay that never ran.
        assert after == before, f"a refused replay created {after - before}"
        assert sessions


async def _record_unfinalised(db_path: Path) -> tuple[str, str]:
    """A session the recorder never closed out -- as if the process died."""
    settings = load_settings().model_copy(update={"venues": simulated_venues()})
    store = SQLiteEventStore(str(db_path))
    platform = build_platform(
        settings,
        clock=ManualClock(START_MS),
        bus=InMemoryEventBus(raise_on_handler_error=True),
        store=store,
        market=default_market(start_ms=START_MS),
        raise_on_handler_error=True,
    )
    await platform.start(record=True)
    for _ in range(10):
        platform.clock.advance(100)
        await platform.step_market(1)
        await platform.orchestrator.tick()
    await platform.bus.drain()
    await platform.recorder.flush()  # durable, but never finalised
    session_id = platform.session_id
    await store.close()
    return session_id, config_digest(settings.model_dump())


def _seed_marker_less(db_path: Path) -> None:
    """A session with market inputs and no tick markers at all."""
    import asyncio

    from core.events import Event, EventType

    async def build() -> None:
        store = SQLiteEventStore(str(db_path))
        await store.open()
        await store.start_session("legacy-session", START_MS, config_hash="")
        await store.append_many(
            "legacy-session",
            [
                Event(
                    type=EventType.BOOK_SNAPSHOT,
                    ts_ms=START_MS + i,
                    sequence=i,
                    source="VENUE_A",
                    schema_name="Test",
                    payload={},
                )
                for i in range(1, 4)
            ],
        )
        await store.finalize_session(
            "legacy-session", START_MS + 10, status=SessionStatus.COMPLETE
        )
        await store.close()

    asyncio.run(build())
