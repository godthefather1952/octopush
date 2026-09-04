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
        """pyproject requires >= 3.11; the shipped image must not be older.

        The devcontainer builds from the repo's own ``Dockerfile`` (the
        Codespaces Docker-in-Docker fix, e17fedb) rather than declaring a
        bare ``image`` field — so the Python version it actually ships is
        whatever that Dockerfile's ``FROM`` line pins, and there is no
        separate ``image`` value left to check or drift from it.
        """
        build = devcontainer.get("build")
        assert build is not None, (
            "expected a build-based devcontainer (build.dockerfile), not a bare "
            "image field — devcontainer.json's shape changed with the "
            "Docker-in-Docker fix and this test must check the real thing"
        )
        assert build.get("context") == "..", "the build context must reach the repo root"
        dockerfile_path = (ROOT / build["dockerfile"]).resolve()
        assert dockerfile_path == (ROOT / "Dockerfile").resolve()
        dockerfile = dockerfile_path.read_text()
        version = re.search(r"FROM python:(3\.\d+)", dockerfile)
        assert version is not None, "Dockerfile has no FROM python:X.Y line"
        assert int(version.group(1).split(".")[1]) >= 11, (
            f"{version.group(1)} is older than the project's floor"
        )

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


class TestStopPreservesData:
    """Normal shutdown must never cost a testing session.

    The event store holds recorded sessions, event history, paper orders and
    fills, and the events replay reads back. Stopping is a routine thing to
    do many times a day; erasing is not. Putting both behind one command
    means a mistyped flag destroys the history — so they are separate
    commands, and stop has no destructive mode at all.
    """

    def test_stop_never_removes_volumes(self):
        """It must not *hand* --volumes to compose.

        The word still appears in stop-paper.sh, in the branch that refuses
        the old spelling by name — so the check is on what is executed, not
        on whether the string occurs.
        """
        stop = (ROOT / "scripts" / "stop-paper.sh").read_text()
        for number, line in enumerate(stop.splitlines(), 1):
            code = line.strip()
            if code.startswith("#"):
                continue
            if re.match(r"^(compose|docker)\b", code):
                assert "--volumes" not in code, f"stop-paper.sh:{number} {code}"
                assert "-v " not in code, f"stop-paper.sh:{number} {code}"
            assert "volume rm" not in re.sub(r"'[^']*'", "", code), f"line {number}"
        assert "compose down --remove-orphans" in stop, "stop should bring the stack down"

    def test_stop_takes_no_destructive_flag(self):
        """The old --volumes spelling is refused by name, not silently."""
        result = run(["bash", "scripts/stop-paper.sh", "--volumes"])
        assert result.returncode != 0
        combined = result.stdout + result.stderr
        assert "no longer deletes data" in combined
        assert "reset-paper.sh" in combined, "it should point at the right command"

    @pytest.mark.parametrize("flag", ["--wipe", "--clean", "-v"])
    def test_other_destructive_spellings_are_refused(self, flag):
        result = run(["bash", "scripts/stop-paper.sh", flag])
        assert result.returncode != 0
        assert "no longer deletes data" in result.stdout + result.stderr

    def test_stop_says_the_data_is_kept(self):
        """A developer should not have to infer it.

        Accepts either wording: with a stack running it reports the data
        preserved, and with nothing running it says the data is untouched.
        """
        result = run(["bash", "scripts/stop-paper.sh"])
        assert result.returncode == 0
        assert re.search(r"preserved|untouched|Nothing to stop", result.stdout), result.stdout

    def test_stop_is_safe_to_repeat(self):
        for _ in range(2):
            assert run(["bash", "scripts/stop-paper.sh"]).returncode == 0

    def test_no_routine_command_removes_volumes(self):
        """Only reset may. Checked across every script, not just stop."""
        offenders = []
        for path in sorted((ROOT / "scripts").rglob("*.sh")):
            if path.name == "reset-paper.sh":
                continue
            verification = path.name == "verify-phase0.sh"
            for number, line in enumerate(path.read_text().splitlines(), 1):
                if line.strip().startswith("#"):
                    continue
                code = re.sub(r'"[^"]*"', "", line).strip()
                if not re.match(r"^(compose|docker)\b", code):
                    continue
                if "--volumes" in code or "volume rm" in code:
                    # verify-phase0.sh runs an isolated compose project, so the
                    # volumes it removes are its own, never the developer's.
                    if verification:
                        continue
                    offenders.append(f"{path.name}:{number} {line.strip()}")
        assert not offenders, "non-reset scripts delete volumes:\n" + "\n".join(offenders)

    def test_verification_uses_an_isolated_compose_project(self):
        """Otherwise it would erase recorded sessions as a side effect.

        verify-phase0.sh needs an empty database to check the migration schema
        and event persistence. Compose scopes volumes to the project, so it
        gets its own and cannot reach the development stack's.
        """
        verify = (ROOT / "scripts" / "verify-phase0.sh").read_text()
        assert 'TF_COMPOSE_PROJECT="trading-floor-verify"' in verify
        common = (ROOT / "scripts" / "lib" / "common.sh").read_text()
        assert "TF_COMPOSE_PROJECT" in common, "the wrapper must honour the project"


class TestResetIsExplicitlyDestructive:
    def test_it_exists_and_is_reachable_three_ways(self):
        assert (ROOT / "reset-paper.sh").is_file()
        assert (ROOT / "scripts" / "reset-paper.sh").is_file()
        assert "reset)" in (ROOT / "trading-floor").read_text()
        assert "\nreset:" in (ROOT / "Makefile").read_text()

    def test_it_names_what_it_will_destroy(self):
        """A warning that does not say what is lost is not a warning."""
        result = run(["bash", "scripts/reset-paper.sh", "--help"])
        assert result.returncode == 0
        text = result.stdout.lower()
        for thing in ("event store", "orders and fills", "replay", "redis"):
            assert thing in text, f"the warning does not mention {thing}"

    @pytest.fixture
    def data_volumes(self):
        """Real Docker volumes named exactly as the stack names them.

        Asserting on a message proves only that a message was printed. These
        tests exist to prove the *data survives*, so the volumes have to
        actually exist for the destructive path to be reached at all.
        """
        import subprocess as sp

        if sp.run(["docker", "info"], capture_output=True).returncode != 0:
            pytest.skip("no Docker daemon")

        project = ROOT.name
        names = [f"{project}_postgres-data", f"{project}_redis-data"]
        created = []
        for name in names:
            exists = sp.run(["docker", "volume", "inspect", name], capture_output=True)
            if exists.returncode != 0:
                sp.run(["docker", "volume", "create", name], capture_output=True, check=True)
                created.append(name)
        try:
            yield names
        finally:
            for name in created:
                sp.run(["docker", "volume", "rm", "-f", name], capture_output=True)

    @staticmethod
    def _volume_exists(name: str) -> bool:
        import subprocess as sp

        return sp.run(["docker", "volume", "inspect", name], capture_output=True).returncode == 0

    def test_it_refuses_without_confirmation_when_not_interactive(self, data_volumes):
        """stdin is not a terminal here, so the question cannot be asked.

        Refusing is the only safe reading: an unanswerable question must not
        be treated as answered yes. And the volumes must still be there
        afterwards — that is the property, not the wording.
        """
        result = run(["bash", "scripts/reset-paper.sh"])
        assert result.returncode != 0
        combined = result.stdout + result.stderr
        assert "Refusing to erase data without confirmation" in combined
        assert "--yes" in combined, "it should say how to proceed deliberately"

        for name in data_volumes:
            assert self._volume_exists(name), f"{name} was deleted without confirmation"

    def test_a_declined_confirmation_leaves_every_volume_intact(self, data_volumes):
        """The core guarantee, checked against Docker rather than output."""
        result = subprocess.run(
            ["bash", "scripts/reset-paper.sh"],
            input="no\n",
            capture_output=True,
            text=True,
            cwd=ROOT,
            timeout=90,
            env={**os.environ, "NO_COLOR": "1"},
        )
        assert "Reset complete" not in result.stdout
        for name in data_volumes:
            assert self._volume_exists(name), f"{name} was deleted after declining"

    def test_stop_leaves_every_volume_intact(self, data_volumes):
        """The whole point of separating the two commands.

        This confirms a real invocation leaves real volumes alone. It is the
        secondary guard, not the primary one: with no stack running, stop
        returns early and never reaches its compose call, so this would pass
        even against a destructive stop. `test_stop_never_removes_volumes`
        is what catches that — it was checked by making stop destructive
        again and watching it fail.
        """
        assert run(["bash", "scripts/stop-paper.sh"]).returncode == 0
        for name in data_volumes:
            assert self._volume_exists(name), f"./stop-paper.sh deleted {name}"

    def test_yes_really_does_delete_them(self, data_volumes):
        """A safety guarantee is only meaningful if reset actually works."""
        result = run(["bash", "scripts/reset-paper.sh", "--yes"], timeout=180)
        assert result.returncode == 0, result.stdout + result.stderr
        for name in data_volumes:
            assert not self._volume_exists(name), f"{name} survived an explicit reset"

    def test_a_wrong_answer_cancels_and_deletes_nothing(self):
        """Anything but the exact word cancels.

        This talks to the real Docker daemon (the script's own first check),
        so it must tell "the script is wrong" apart from "Docker is not
        available in this environment" — a missing daemon is an environment
        limitation, not evidence about the script's confirmation logic, and
        must not be conflated with either passing or failing that logic.
        """
        if subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
            pytest.skip("no Docker daemon available in this environment")

        result = subprocess.run(
            ["bash", "scripts/reset-paper.sh"],
            input="yes\n",
            capture_output=True,
            text=True,
            cwd=ROOT,
            timeout=90,
            env={**os.environ, "NO_COLOR": "1"},
        )
        # Either it cancelled, or there were no volumes to erase in the first
        # place — both mean nothing was destroyed on a non-matching answer.
        combined = result.stdout + result.stderr
        assert "Docker daemon is not running" not in combined, (
            "Docker was available a moment ago; a daemon failure here is a real "
            "environment problem, not this test's concern to swallow"
        )
        assert "Cancelled" in combined or "nothing to erase" in combined
        assert "Reset complete" not in combined

    def test_the_confirmation_word_is_not_a_bare_yes(self):
        """`y` is muscle memory; `reset` has to be meant."""
        reset = (ROOT / "scripts" / "reset-paper.sh").read_text()
        assert '"$REPLY" != "reset"' in reset

    def test_it_still_asserts_paper_mode(self):
        """A destructive command is the last place to skip the boundary."""
        reset = (ROOT / "scripts" / "reset-paper.sh").read_text()
        assert "assert_paper_mode" in reset

        result = run(["bash", "scripts/reset-paper.sh", "--yes"], env={"TF_MODE": "live"})
        assert result.returncode != 0
        assert "TF_MODE is set to" in result.stdout + result.stderr

    def test_an_unknown_option_is_refused(self):
        result = run(["bash", "scripts/reset-paper.sh", "--force"])
        assert result.returncode != 0
        assert "Unknown option" in result.stdout + result.stderr

    def test_help_points_back_at_the_safe_command(self):
        result = run(["bash", "scripts/reset-paper.sh", "--help"])
        assert "./stop-paper.sh" in result.stdout


class TestThePythonVersionIsConsistent:
    """The Dockerfile is what ships, so it defines the canonical version."""

    @staticmethod
    def _python_version(text: str, pattern: str) -> str:
        found = re.search(pattern, text)
        assert found is not None, f"no version matched {pattern}"
        return found.group(1)

    def test_the_dockerfile_declares_the_canonical_version(self):
        dockerfile = (ROOT / "Dockerfile").read_text()
        assert self._python_version(dockerfile, r"FROM python:(3\.\d+)") == "3.12"

    def test_the_devcontainer_matches_the_dockerfile(self):
        """A Codespace should be the version the project runs on.

        The devcontainer no longer declares its own image or version string
        to compare against the Dockerfile — it *builds from* the Dockerfile
        (``build.dockerfile``), so the real relationship to test is that it
        points at the same file this class's other test already asserts pins
        3.12, not a second, independent version string that could drift.
        """
        import json

        raw = (ROOT / ".devcontainer" / "devcontainer.json").read_text()
        stripped = re.sub(r"^\s*//.*$", "", raw, flags=re.MULTILINE)
        devcontainer = json.loads(stripped)
        build = devcontainer.get("build")
        assert build is not None, "devcontainer must build from the repo's Dockerfile"
        dockerfile_path = (ROOT / ".devcontainer" / build["context"] / build["dockerfile"]).resolve()
        assert dockerfile_path == (ROOT / "Dockerfile").resolve(), (
            "devcontainer.json must build from the same Dockerfile that pins the "
            "canonical Python version, not a different or duplicated one"
        )

    def test_ci_runs_the_canonical_version(self):
        workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
        versions = set(re.findall(r'python-version: "(3\.\d+)"', workflow))
        assert "3.12" in versions, "CI does not run the shipped version"

    def test_the_declared_floor_is_actually_tested(self):
        """`requires-python` must be a checked claim, not a number in a file."""
        workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
        pyproject = (ROOT / "pyproject.toml").read_text()
        floor = self._python_version(pyproject, r'requires-python = ">=(3\.\d+)"')
        assert f'python-version: "{floor}"' in workflow, (
            f"requires-python declares {floor} but no CI job runs it"
        )

    def test_ruff_targets_the_floor_not_the_shipped_version(self):
        """Targeting 3.12 would let ruff propose syntax that breaks 3.11."""
        pyproject = (ROOT / "pyproject.toml").read_text()
        floor = self._python_version(pyproject, r'requires-python = ">=(3\.\d+)"')
        target = self._python_version(pyproject, r'target-version = "py(3\d+)"')
        assert target == floor.replace(".", ""), (
            f"ruff targets py{target} but the supported floor is {floor}"
        )

    def test_the_code_uses_no_syntax_the_floor_cannot_parse(self):
        """The floor is only real if the source actually compiles under it."""
        import subprocess as sp

        floor_python = "python3.11"
        if sp.run(["which", floor_python], capture_output=True).returncode != 0:
            pytest.skip(f"{floor_python} is not installed here")

        packages = ["agents", "apps", "core", "execution", "monitoring",
                    "replay", "risk", "simulation", "storage", "strategies", "venues"]
        result = sp.run(
            [floor_python, "-m", "compileall", "-q", "-x", r"__pycache__", *packages],
            capture_output=True, text=True, cwd=ROOT,
        )
        assert result.returncode == 0, result.stdout + result.stderr
