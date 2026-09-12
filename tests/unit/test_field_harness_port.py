"""The field harness reaches the port the platform actually listens on: P12-T1.

THE DEFECT
==========
The first Phase 12 SHADOW field rehearsal ended with

    ERROR: Trading Floor API never became reachable

and that was wrong. The API had started and had logged ``http://0.0.0.0:8080/``.
The field script was polling ``http://127.0.0.1:8000`` — a default it had
invented for itself, which had never matched this repository.

This is a harness defect, not a production defect, and the fix is not to write
``8080`` into a second place. It is for the field tooling to derive the port
from ``scripts/lib/common.sh``, the one file that already defines it, so the
two cannot drift apart again.

These tests pin that: the shared definition is what it claims to be, the field
script consumes it rather than redefining it, and nothing in the repository's
tooling still carries the wrong port.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
COMMON_SH = REPO / "scripts" / "lib" / "common.sh"
FIELD_SH = REPO / "scripts" / "field-check-shadow.sh"
FIELD_PY = REPO / "scripts" / "lib" / "field_shadow.py"


@pytest.fixture(scope="module")
def common_text() -> str:
    return COMMON_SH.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def field_sh_text() -> str:
    return FIELD_SH.read_text(encoding="utf-8")


class TestTheSharedDefinitionIsTheOneSourceOfTruth:
    def test_common_sh_defines_the_api_port(self, common_text):
        assert re.search(r'TF_API_PORT="\$\{TF_API_PORT:-8080\}"', common_text), (
            "scripts/lib/common.sh must remain the single place the API port "
            "is defined"
        )

    def test_common_sh_builds_the_url_from_that_port(self, common_text):
        assert 'TF_API_URL="http://localhost:${TF_API_PORT}"' in common_text

    def test_the_repository_default_is_8080_not_8000(self, common_text):
        match = re.search(r"TF_API_PORT:-(\d+)", common_text)
        assert match is not None
        assert match.group(1) == "8080"


class TestTheFieldHarnessReusesItRatherThanRedefiningIt:
    def test_the_field_script_exists(self):
        assert FIELD_SH.exists(), "Phase 12 field tooling must live in the repository"
        assert FIELD_PY.exists()

    def test_it_sources_common_sh(self, field_sh_text):
        assert "lib/common.sh" in field_sh_text, (
            "the field script must source the shared helpers rather than "
            "carrying its own connection settings"
        )

    def test_it_does_not_define_its_own_port_default(self, field_sh_text):
        """The literal P12-T1 defect, in the form it must never take again."""
        assert "TF_API_PORT:-" not in field_sh_text, (
            "a second port default is how the harness and the platform drifted "
            "apart in the first place"
        )
        assert "TF_API_URL=" not in field_sh_text.replace(
            "export TF_API_URL", ""
        ), "the field script may export TF_API_URL but must not assign a value to it"

    def test_the_python_helper_refuses_to_guess_a_url(self):
        """No default, so a mis-sourced harness fails loudly instead of
        polling a port nothing is listening on and blaming the platform."""
        text = FIELD_PY.read_text(encoding="utf-8")
        assert 'os.environ.get("TF_API_URL", "")' in text
        assert "8000" not in text
        assert "localhost:8080" not in text, (
            "the helper must take the URL it is given, not hard-code one"
        )


class TestNoRepositoryToolingStillCarriesTheWrongPort:
    def test_no_shell_script_references_port_8000(self):
        offenders = [
            path.relative_to(REPO)
            for path in (REPO / "scripts").rglob("*.sh")
            if ":8000" in path.read_text(encoding="utf-8")
        ]
        assert offenders == [], f"stale API port in field tooling: {offenders}"

    def test_no_tooling_helper_references_port_8000(self):
        offenders = [
            path.relative_to(REPO)
            for path in (REPO / "scripts").rglob("*.py")
            if ":8000" in path.read_text(encoding="utf-8")
        ]
        assert offenders == []
