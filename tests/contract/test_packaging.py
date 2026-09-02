"""Packaging must not drift from the code — P0-M (Docker), P0-H7.

The audit found a Dockerfile that installed a hand-copied dependency list
instead of the project's own metadata. The list had already drifted:
`anthropic` was missing, so a container started with
`TF_INTELLIGENCE_PROVIDER=claude` — a value `docker-compose.yml` exposes —
came up healthy and only failed on LUMEN's first poll, because the SDK is
imported lazily inside `_get_client()`. The image also never installed the
project, so the console scripts declared in `pyproject.toml` did not exist.

These checks are static on purpose. Building the image needs a Docker daemon
and a reachable registry; drift between two files in this repository does
not, and is the part that actually rots.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _statements(sql: str) -> list[str]:
    """SQL with comment lines stripped, split on statement boundaries."""
    lines = [line for line in sql.splitlines() if not line.lstrip().startswith("--")]
    return "\n".join(lines).split(";")


def _tables(sql: str) -> set[str]:
    import re

    return set(
        re.findall(
            r"create\s+table\s+(?:if\s+not\s+exists\s+)?(\w+)",
            "\n".join(_statements(sql)),
            re.IGNORECASE,
        )
    )


def _column_declaration(sql: str, column: str) -> str | None:
    """The declaration line for ``column`` inside a CREATE TABLE body."""
    import re

    for statement in _statements(sql):
        if not re.search(r"create\s+table", statement, re.IGNORECASE):
            continue
        body = statement[statement.index("(") + 1 :] if "(" in statement else ""
        for line in body.splitlines():
            stripped = line.strip().rstrip(",")
            if stripped.lower().startswith(f"{column} "):
                return stripped
    return None


@pytest.fixture(scope="module")
def pyproject() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text())


@pytest.fixture(scope="module")
def dockerfile() -> str:
    return (ROOT / "Dockerfile").read_text()


@pytest.fixture(scope="module")
def compose() -> str:
    return (ROOT / "docker-compose.yml").read_text()


class TestTheImageInstallsTheProject:
    def test_dependencies_come_from_pyproject_not_a_copied_list(self, dockerfile):
        """A literal list is the thing that drifted; forbid it outright."""
        assert 'pip install ".[' in dockerfile or "pip install '.[" in dockerfile, (
            "the Dockerfile must install the project from pyproject.toml"
        )

    def test_no_dependency_is_pinned_by_hand_in_the_dockerfile(self, pyproject, dockerfile):
        names = [
            dep.split(">")[0].split("=")[0].split("[")[0].strip()
            for dep in pyproject["project"]["dependencies"]
        ]
        for name in names:
            assert f'"{name}>' not in dockerfile, (
                f"{name} is pinned in the Dockerfile as well as pyproject.toml; "
                "the two will drift"
            )

    def test_every_optional_extra_the_image_can_reach_is_installed(self, pyproject, dockerfile):
        """Reachable from configuration means it must be present.

        `intelligence` is reachable via TF_INTELLIGENCE_PROVIDER and
        `postgres` via TF_STORAGE_BACKEND, both of which docker-compose.yml
        sets. An extra that configuration can select but the image does not
        carry is a runtime crash waiting for the first request.
        """
        for extra in ("postgres", "intelligence"):
            assert extra in pyproject["project"]["optional-dependencies"]
            assert extra in dockerfile, f"the image cannot serve the {extra} configuration"

    def test_the_console_scripts_are_installable(self, pyproject):
        scripts = pyproject["project"]["scripts"]
        assert "trading-floor" in scripts
        assert "trading-floor-replay" in scripts

    def test_the_image_runs_as_a_non_root_user(self, dockerfile):
        assert "USER trading" in dockerfile
        assert dockerfile.index("USER trading") < dockerfile.index("CMD ")


class TestComposeMatchesTheConfiguration:
    def test_every_environment_variable_compose_sets_is_one_we_read(self, compose):
        """A TF_ variable compose sets that nothing reads is a silent no-op."""
        import re

        from core.config import settings as settings_module

        source = Path(settings_module.__file__).read_text()
        declared = set(re.findall(r'_env\(\s*"([A-Z0-9_]+)"', source))
        declared.add("MODE")  # read directly, not through _env

        used = set(re.findall(r"^\s+TF_([A-Z0-9_]+):", compose, re.MULTILINE))
        unknown = used - declared
        assert not unknown, f"docker-compose.yml sets variables nothing reads: {sorted(unknown)}"

    def test_compose_never_asks_for_a_non_paper_mode(self, compose):
        """Startup would abort, but the intent should not be expressible here."""
        assert "TF_MODE" not in compose or "TF_MODE: paper" in compose

    def test_the_feed_compose_selects_is_a_real_feed(self, compose):
        import re

        from core.config.settings import _FEEDS

        for value in re.findall(r"^\s+TF_FEED:\s*(\S+)", compose, re.MULTILINE):
            assert value.strip("\"'") in _FEEDS, f"compose sets an unknown TF_FEED: {value}"

    def test_no_exchange_credentials_are_mounted(self, compose):
        """The paper boundary, checked where deployment config could break it."""
        forbidden = ("API_SECRET", "EXCHANGE_KEY", "PRIVATE_KEY", "BINANCE_API", "COINBASE_API")
        for token in forbidden:
            assert token not in compose, f"docker-compose.yml references {token}"

    def test_the_postgres_service_loads_the_migrations(self, compose):
        assert "storage/migrations" in compose
        assert (ROOT / "storage" / "migrations").is_dir()


class TestTheMigrationsMatchTheStore:
    def test_the_checked_in_migration_declares_the_same_tables(self):
        """Three sources describe this schema; they must agree.

        `storage/migrations/001_initial.sql` seeds a fresh PostgreSQL
        container, while `postgres_store.MIGRATION_SQL` runs on open(). If
        they disagree, a container built from the migration and a store that
        applied its own DDL differ, and only one of them was ever tested.
        """
        from storage.postgres_store import MIGRATION_SQL

        checked_in = (ROOT / "storage" / "migrations" / "001_initial.sql").read_text().lower()
        for table in ("sessions", "events"):
            assert f"create table if not exists {table}" in checked_in
            assert f"create table if not exists {table}" in MIGRATION_SQL.lower()

    def test_the_event_columns_agree_between_the_two_sources(self):
        from storage.postgres_store import MIGRATION_SQL

        checked_in = (ROOT / "storage" / "migrations" / "001_initial.sql").read_text().lower()
        for column in (
            "session_id",
            "event_id",
            "seq",
            "ts_ms",
            "type",
            "source",
            "schema_name",
            "correlation_id",
            "payload",
        ):
            assert column in checked_in, f"the migration omits events.{column}"
            assert column in MIGRATION_SQL.lower()

    def test_timestamps_are_bigint_in_both_sources(self):
        """An INTEGER column silently wraps a millisecond epoch."""
        from storage.postgres_store import MIGRATION_SQL

        checked_in = (ROOT / "storage" / "migrations" / "001_initial.sql").read_text()
        for name, sql in (("migration", checked_in), ("MIGRATION_SQL", MIGRATION_SQL)):
            for column in ("ts_ms", "seq", "started_at"):
                declaration = _column_declaration(sql, column)
                assert declaration is not None, f"{name} does not declare {column}"
                assert "bigint" in declaration.lower(), (
                    f"{name} declares {column} as '{declaration}', which will wrap"
                )

    def test_neither_source_declares_a_table_the_other_does_not(self):
        """Divergence in either direction, not just omission in one.

        The migration used to create a `raw_messages` table that no code ever
        read or wrote and that `MIGRATION_SQL` never created — so a database
        seeded from the migration and one created by the store were different
        shapes, and only the second was ever exercised.
        """
        from storage.postgres_store import MIGRATION_SQL

        checked_in = (ROOT / "storage" / "migrations" / "001_initial.sql").read_text()
        assert _tables(checked_in) == _tables(MIGRATION_SQL)

    def test_no_table_is_declared_that_no_code_touches(self):
        """A table only the schema knows about invites code written against
        storage nobody maintains."""
        from storage.postgres_store import MIGRATION_SQL

        sources = "\n".join(
            path.read_text()
            for path in (ROOT / "storage").rglob("*.py")
            if path.name != "__pycache__"
        )
        for table in _tables(MIGRATION_SQL):
            assert table in sources, f"nothing in storage/ references the {table} table"
