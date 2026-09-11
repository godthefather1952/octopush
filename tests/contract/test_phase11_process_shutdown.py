"""Phase 11 process-level shutdown contract.

This covers the field failure a direct ``await platform.stop()`` test cannot:
Docker sends SIGTERM to the Python process while the embedded Uvicorn server,
orchestrator and LUMEN are all running. A graceful process exit must still
reach Recorder.stop() and durably finalise the session.
"""

from __future__ import annotations

import os
import signal
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.name != "posix", reason="requires POSIX SIGTERM semantics"
)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_health(
    proc: subprocess.Popen[str], port: int, timeout: float = 15.0
) -> None:
    deadline = time.monotonic() + timeout
    url = f"http://127.0.0.1:{port}/health"
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            output, _ = proc.communicate()
            pytest.fail(
                f"trading-floor exited before health became ready "
                f"(returncode={proc.returncode})\n{output}"
            )
        try:
            with urllib.request.urlopen(url, timeout=0.5) as response:
                if response.status == 200:
                    return
        except (OSError, urllib.error.URLError):
            pass
        time.sleep(0.05)

    proc.kill()
    output, _ = proc.communicate()
    pytest.fail(f"trading-floor health endpoint did not become ready\n{output}")


def _wait_for_recorded_event(
    proc: subprocess.Popen[str], db_path: Path, label: str, timeout: float = 10.0
) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            output, _ = proc.communicate()
            pytest.fail(
                f"trading-floor exited before recording an event "
                f"(returncode={proc.returncode})\n{output}"
            )
        if db_path.exists():
            try:
                with sqlite3.connect(db_path) as conn:
                    row = conn.execute(
                        "SELECT session_id FROM sessions WHERE label = ?", (label,)
                    ).fetchone()
                    if row is not None:
                        session_id = str(row[0])
                        count = conn.execute(
                            "SELECT COUNT(*) FROM events WHERE session_id = ?",
                            (session_id,),
                        ).fetchone()[0]
                        if count > 0:
                            return session_id
            except sqlite3.OperationalError:
                # Startup may be between schema creation and its first commit.
                pass
        time.sleep(0.05)

    proc.kill()
    output, _ = proc.communicate()
    pytest.fail(f"trading-floor did not durably record an event\n{output}")


def test_sigterm_finalizes_durable_session_with_embedded_api(tmp_path: Path) -> None:
    """A real SIGTERM must converge on the canonical Phase 11 stop path."""

    db_path = tmp_path / "phase11-sigterm.db"
    label = "phase11-sigterm-contract"
    port = _free_port()
    env = os.environ.copy()
    env.update(
        {
            "TF_MODE": "paper",
            "TF_PROFILE": "paper",
            "TF_FEED": "simulated",
            "TF_BUS": "memory",
            "TF_STORAGE_BACKEND": "sqlite",
            "TF_SQLITE_PATH": str(db_path),
            "TF_INTELLIGENCE_PROVIDER": "null",
            "TF_API_HOST": "127.0.0.1",
            "TF_API_PORT": str(port),
            "TF_TICK_INTERVAL_S": "0.02",
            "TF_LOG_LEVEL": "WARNING",
            "TF_LOG_FORMAT": "text",
        }
    )

    proc = subprocess.Popen(
        [sys.executable, "-m", "apps.orchestrator", "--label", label],
        cwd=Path(__file__).resolve().parents[2],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    try:
        _wait_for_health(proc, port)
        session_id = _wait_for_recorded_event(proc, db_path, label)

        proc.send_signal(signal.SIGTERM)
        try:
            output, _ = proc.communicate(timeout=10.0)
        except subprocess.TimeoutExpired:
            proc.kill()
            output, _ = proc.communicate()
            pytest.fail(
                "SIGTERM did not stop the trading-floor process within 10s; "
                "the application shutdown path was not reached\n" + output
            )

        assert proc.returncode == 0, output

        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT status, ended_at, events_lost FROM sessions "
                "WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            event_count = conn.execute(
                "SELECT COUNT(*) FROM events WHERE session_id = ?", (session_id,)
            ).fetchone()[0]

        assert row is not None
        status, ended_at, events_lost = row
        assert status == "COMPLETE"
        assert ended_at is not None
        assert events_lost == 0
        assert event_count > 0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate()
