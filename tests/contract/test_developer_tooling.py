"""The convenience tooling must not be a route around the paper boundary.

These scripts exist to make the platform easy to start. That makes them the
one place where a shortcut would be most tempting and least visible, so the
same boundary the application enforces is asserted here too — against the
scripts as they actually run, not against a reading of them.

The first test guards a defect these tests found: `common.sh` exported
``TF_MODE=paper`` before checking the caller's value, so ``TF_MODE=live
./start-paper.sh`` was silently rewritten to paper and proceeded. Safe, but
it discarded operator intent without a word — which is the behaviour P0-M1
was raised about.
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

#: Entrypoints a developer is told to run.
ENTRYPOINTS = (
    "start-paper.sh",
    "stop-paper.sh",
    "status.sh",
    "logs.sh",
    "test.sh",
    "verify-phase0.sh",
    "trading-floor",
)


def run(args, env=None, timeout=90):
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        cwd=ROOT,
        timeout=timeout,
        env={**os.environ, "NO_COLOR": "1", **(env or {})},
    )


class TestPaperModeIsEnforcedByTheTooling:
    @pytest.mark.parametrize(
        "mode", ["live", "production", "real", "testnet-live", "LIVE", "PROD"]
    )
    def test_start_refuses_a_non_paper_mode_from_the_environment(self, mode):
        """Regression: the requested mode must be read before it is overwritten."""
        result = run(["bash", "scripts/start-paper.sh"], env={"TF_MODE": mode})
        assert result.returncode != 0, f"TF_MODE={mode} was accepted"
        combined = result.stdout + result.stderr
        assert "TF_MODE is set to" in combined
        assert "Starting infrastructure" not in combined, (
            "it began starting services before refusing"
        )

    def test_start_refuses_a_non_paper_mode_from_dotenv(self, tmp_path):
        """A .env asking for live mode is refused before anything starts."""
        env_file = ROOT / ".env"
        backup = env_file.read_text() if env_file.exists() else None
        try:
            env_file.write_text((backup or "") + "\nTF_MODE=live\n")
            result = run(["bash", "scripts/start-paper.sh"])
            assert result.returncode != 0
            assert ".env sets TF_MODE=live" in result.stdout + result.stderr
        finally:
            if backup is None:
                env_file.unlink(missing_ok=True)
            else:
                env_file.write_text(backup)

    def test_no_script_offers_a_live_flag(self):
        """There must be no flag or alias a developer could pass to get one."""
        for path in sorted((ROOT / "scripts").rglob("*.sh")):
            text = path.read_text()
            for token in ("--live", "--production", "--real"):
                assert token not in text, f"{path.name} accepts {token}"

    def test_no_script_exports_or_ships_a_non_paper_mode(self):
        """A non-paper mode may only appear as a probe that expects rejection.

        `setup-codespace.sh` and `verify-phase0.sh` both run `TF_MODE=live` on
        purpose, to confirm the boundary refuses it — that is the opposite of
        offering live mode. What must never appear is a line that *exports* a
        non-paper mode or hands one to the stack, so that is what is checked
        rather than the bare string.
        """
        offenders = []
        for path in sorted((ROOT / "scripts").rglob("*.sh")):
            for number, line in enumerate(path.read_text().splitlines(), 1):
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                # Quoted literals are prose — the diagnostics quite reasonably
                # mention TF_MODE=live when explaining that it was refused.
                # Only an assignment in command position counts.
                code = re.sub(r'"[^"]*"', "", stripped)
                if not re.search(r"TF_MODE=(?!paper\b)\S+", code):
                    continue
                # Allowed shape: a one-shot probe whose failure is the point.
                probe = re.search(r"\bTF_MODE=(live|production|real)\b", code) and (
                    "docker run" in code or code.startswith("if (")
                )
                if not probe:
                    offenders.append(f"{path.name}:{number} {stripped}")
        assert not offenders, "scripts set a non-paper mode:\n" + "\n".join(offenders)

    def test_the_offender_check_would_catch_a_real_export(self, tmp_path):
        """Guards the test above: prose is excluded, an assignment is not."""
        prose = 'fail_with "TF_MODE=live was rejected"'
        real = "export TF_MODE=live"
        assert not re.search(r"TF_MODE=(?!paper\b)\S+", re.sub(r'"[^"]*"', "", prose))
        assert re.search(r"TF_MODE=(?!paper\b)\S+", re.sub(r'"[^"]*"', "", real))

    def test_the_boundary_probes_assert_rejection(self):
        """The probes must check for failure, not merely run."""
        setup = (ROOT / "scripts" / "setup-codespace.sh").read_text()
        assert "The paper-mode boundary is not being enforced" in setup, (
            "setup runs TF_MODE=live but does not fail when it is accepted"
        )
        verify = (ROOT / "scripts" / "verify-phase0.sh").read_text()
        assert "TF_MODE=live was ACCEPTED" in verify

    def test_every_script_pins_paper_mode(self):
        """The shared library sets it; nothing may quietly opt out."""
        common = (ROOT / "scripts" / "lib" / "common.sh").read_text()
        assert "export TF_MODE=paper" in common
        assert "TF_REQUESTED_MODE" in common, (
            "the caller's mode must be captured before it is overwritten"
        )

    def test_compose_pins_paper_mode(self):
        compose = (ROOT / "docker-compose.yml").read_text()
        assert "TF_MODE: paper" in compose


class TestTheEntrypointsAreUsable:
    @pytest.mark.parametrize("name", ENTRYPOINTS)
    def test_the_entrypoint_exists_and_is_executable(self, name):
        path = ROOT / name
        assert path.is_file(), f"{name} is missing"
        assert path.stat().st_mode & stat.S_IXUSR, f"{name} is not executable"

    @pytest.mark.parametrize("name", ENTRYPOINTS)
    def test_the_entrypoint_is_syntactically_valid(self, name):
        result = run(["bash", "-n", str(ROOT / name)])
        assert result.returncode == 0, result.stderr

    def test_every_script_in_scripts_is_syntactically_valid(self):
        for path in sorted((ROOT / "scripts").rglob("*.sh")):
            result = run(["bash", "-n", str(path)])
            assert result.returncode == 0, f"{path.name}: {result.stderr}"

    def test_the_dispatcher_lists_its_commands(self):
        result = run(["./trading-floor", "--help"])
        assert result.returncode == 0
        for command in ("setup", "start", "stop", "status", "logs", "test", "verify"):
            assert command in result.stdout

    def test_an_unknown_command_is_rejected_with_help(self):
        result = run(["./trading-floor", "definitely-not-a-command"])
        assert result.returncode == 2
        assert "Unknown command" in result.stdout

    def test_top_level_wrappers_delegate_rather_than_duplicate(self):
        """One implementation, three ways to reach it."""
        for name in ENTRYPOINTS:
            if name == "trading-floor":
                continue
            text = (ROOT / name).read_text()
            assert f"scripts/{name}" in text, f"{name} does not delegate"
            assert len(text.splitlines()) < 8, f"{name} has grown its own logic"


class TestTheDevContainer:
    @pytest.fixture(scope="class")
    @classmethod
    def devcontainer(cls) -> dict:
        import json

        raw = (ROOT / ".devcontainer" / "devcontainer.json").read_text()
        # devcontainer.json permits // comments; strip them before parsing.
        stripped = re.sub(r"^\s*//.*$", "", raw, flags=re.MULTILINE)
        return json.loads(stripped)

    def test_it_is_valid_json_once_comments_are_stripped(self, devcontainer):
        assert devcontainer["name"]

    def test_it_pins_a_python_the_project_supports(self, devcontainer):
        """pyproject requires >= 3.11; the image must not be older."""
        image = devcontainer["image"]
        assert "python" in image
        version = re.search(r"3\.(\d+)", image)
        assert version is not None, image
        assert int(version.group(1)) >= 11, f"{image} is older than the project's floor"

    def test_docker_is_provided_by_a_feature_not_a_shell_script(self, devcontainer):
        """The stack runs containers, so the Codespace needs its own daemon."""
        features = devcontainer.get("features", {})
        assert any("docker-in-docker" in key for key in features), features

    def test_setup_runs_after_create(self, devcontainer):
        assert "setup-codespace.sh" in devcontainer["postCreateCommand"]

    def test_the_api_port_is_forwarded_and_labelled(self, devcontainer):
        assert 8080 in devcontainer["forwardPorts"]
        attributes = devcontainer["portsAttributes"]["8080"]
        assert attributes["label"]

    def test_the_datastores_are_not_auto_forwarded(self, devcontainer):
        """No browser-facing reason to expose them, and they carry credentials."""
        for port in ("5432", "6379"):
            assert devcontainer["portsAttributes"][port]["onAutoForward"] == "ignore"

    def test_the_container_declares_paper_mode(self, devcontainer):
        assert devcontainer["containerEnv"]["TF_MODE"] == "paper"


class TestSetupIsIdempotent:
    def test_it_never_overwrites_an_existing_dotenv(self):
        """Running setup twice must not touch a file the developer edited."""
        env_file = ROOT / ".env"
        backup = env_file.read_text() if env_file.exists() else None
        marker = "# a developer's own edit\n"
        try:
            env_file.write_text((backup or "") + marker)
            before = env_file.read_text()
            result = run(["bash", "scripts/setup-codespace.sh"], timeout=600)
            assert result.returncode == 0, result.stdout[-2000:]
            assert env_file.read_text() == before, ".env was modified"
            assert "left untouched" in result.stdout
        finally:
            if backup is None:
                env_file.unlink(missing_ok=True)
            else:
                env_file.write_text(backup)

    def test_it_never_writes_a_generated_secret(self):
        """Setup copies the committed example; it does not invent credentials."""
        setup = (ROOT / "scripts" / "setup-codespace.sh").read_text()
        for token in ("openssl rand", "uuidgen", "secrets.token", "$RANDOM"):
            assert token not in setup, f"setup generates a secret with {token}"

    def test_it_does_not_install_system_packages(self):
        """Docker comes from the dev container feature, not from apt."""
        setup = (ROOT / "scripts" / "setup-codespace.sh").read_text()
        for token in ("apt-get install", "apt install", "yum install", "brew install"):
            assert token not in setup, f"setup runs {token}"


class TestMakefileAndDispatcherAgree:
    def test_the_makefile_exposes_the_documented_targets(self):
        makefile = (ROOT / "Makefile").read_text()
        for target in ("setup", "paper", "stop", "status", "logs", "test", "verify"):
            assert f"\n{target}:" in makefile, f"make {target} is missing"

    def test_makefile_targets_delegate_to_the_same_scripts(self):
        makefile = (ROOT / "Makefile").read_text()
        for script in ("setup-codespace.sh", "start-paper.sh", "stop-paper.sh",
                       "status.sh", "logs.sh", "test.sh", "verify-phase0.sh"):
            assert script in makefile, f"the Makefile does not use {script}"
