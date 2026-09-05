"""Credentials must not be recoverable from a dump or a log — P0-L2.

The audit found `postgres_dsn` held as a plain `str`, appearing in full in
`Settings.model_dump()`. Nothing leaked it: the startup banner logs a curated
field list and the value only otherwise reaches `config_digest()`, where it
is hashed. But nothing *prevented* a leak either, and `log.info(settings)` is
one line away in any future phase.

The DSN is the only credential this build has. There are no exchange keys —
the paper boundary is structural — so this is the whole of the surface, which
is why closing it is cheap.
"""

from __future__ import annotations

import json
import logging

import pytest
from pydantic import SecretStr

from core.config import Settings, load_settings
from core.logging import JsonFormatter

PASSWORD = "hunter2-do-not-leak"
DSN = f"postgresql://trading:{PASSWORD}@db.internal:5432/trading_floor"


@pytest.fixture
def settings() -> Settings:
    return load_settings(storage={"backend": "postgres", "postgres_dsn": DSN})


class TestTheDsnIsNotPlainText:
    def test_it_is_held_as_a_secret(self, settings):
        assert isinstance(settings.storage.postgres_dsn, SecretStr)

    def test_the_password_is_not_in_repr(self, settings):
        assert PASSWORD not in repr(settings)
        assert PASSWORD not in repr(settings.storage)
        assert PASSWORD not in str(settings.storage.postgres_dsn)

    def test_the_password_is_not_in_a_model_dump(self, settings):
        assert PASSWORD not in json.dumps(settings.model_dump(mode="json"))

    def test_the_password_is_not_in_the_json_serialisation(self, settings):
        assert PASSWORD not in settings.model_dump_json()

    def test_a_log_line_carrying_the_settings_does_not_leak_it(self, settings):
        """The one-line mistake this exists to make harmless."""
        record = logging.LogRecord(
            name="t", level=logging.INFO, pathname="p", lineno=1,
            msg="starting with %s", args=(settings,), exc_info=None,
        )
        assert PASSWORD not in JsonFormatter().format(record)

    def test_a_log_extra_carrying_the_settings_does_not_leak_it(self, settings):
        record = logging.LogRecord(
            name="t", level=logging.INFO, pathname="p", lineno=1,
            msg="starting", args=(), exc_info=None,
        )
        record.settings = settings
        record.storage = settings.storage
        assert PASSWORD not in JsonFormatter().format(record)


class TestItIsStillUsable:
    """A secret nothing can read is not a fix, it is a break."""

    def test_the_real_value_is_available_to_the_code_that_connects(self, settings):
        assert settings.storage.postgres_dsn.get_secret_value() == DSN

    def test_the_store_is_built_with_the_real_dsn(self, settings):
        from storage import build_store

        store = build_store(
            settings.storage.backend,
            sqlite_path=settings.storage.sqlite_path,
            postgres_dsn=settings.storage.postgres_dsn.get_secret_value(),
        )
        assert store.dsn == DSN

    def test_an_empty_dsn_is_still_refused_for_the_postgres_backend(self):
        with pytest.raises(ValueError, match="postgres_dsn"):
            load_settings(storage={"backend": "postgres", "postgres_dsn": ""})

    def test_it_can_be_set_from_the_environment(self):
        import os
        import subprocess
        import sys

        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "from core.config import load_settings;"
                "s = load_settings();"
                "print(s.storage.postgres_dsn.get_secret_value())",
            ],
            capture_output=True,
            text=True,
            env={**os.environ, "TF_POSTGRES_DSN": DSN},
            cwd=os.getcwd(),
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == DSN


class TestTheConfigDigestStillWorks:
    """What the digest promises, after Phase 2 narrowed it to MATERIAL settings.

    Phase 0 digested the whole settings object, so this suite asserted that a
    changed DSN changed the digest. Phase 2 gave the digest a job -- it gates
    exact replay (P2-9), and a mismatch refuses the run -- and that job made
    the breadth actively harmful: pointing at a different database, or moving
    a SQLite file, would have counted as "the configuration materially
    changed", so every replay of a session recorded elsewhere would demand the
    override, and a routinely-overridden gate protects nothing.

    ``storage.sqlite_path`` and ``storage.postgres_dsn`` are therefore
    excluded: they say WHERE events live and how to reach it, and neither can
    change what a replay computes from them (proved end to end in
    ``tests/contract/test_replay_config_reproducibility.py``, which replays
    one recording under changed infrastructure and compares every economic
    output). ``storage.backend`` stays material.

    The security property is unchanged and pinned below: a secret is hashed
    rather than masked, so wherever a SecretStr IS material the digest can
    still tell two credentials apart -- and it never carries either in the
    clear.
    """

    def test_the_dsn_is_excluded_from_the_replay_digest(self, settings):
        from replay.engine import config_digest

        other = load_settings(
            storage={"backend": "postgres", "postgres_dsn": DSN.replace("db.internal", "db2")}
        )
        same_backend = load_settings(storage={"backend": "postgres", "postgres_dsn": DSN})
        assert config_digest(same_backend.model_dump()) == config_digest(
            other.model_dump()
        ), "which server holds the events cannot change what they replay to"
        assert PASSWORD not in config_digest(settings.model_dump())

    def test_the_backend_choice_is_still_material(self):
        """Narrowed, not abandoned: the store TYPE remains part of the digest."""
        from replay.engine import config_digest

        sqlite = load_settings(storage={"backend": "sqlite"})
        postgres = load_settings(storage={"backend": "postgres", "postgres_dsn": DSN})
        assert config_digest(sqlite.model_dump()) != config_digest(
            postgres.model_dump()
        )

    def test_a_secret_is_hashed_rather_than_masked(self):
        """Why the digest takes the model dump, not the json dump.

        model_dump(mode="json") renders every SecretStr as the same asterisks,
        so two different credentials digest identically and a changed setting
        goes unnoticed. ``_unmask`` hashes the real value instead. Asserted on
        the hashing helper directly, since the only SecretStr in Settings
        today happens to sit in an excluded position -- the guarantee has to
        hold for the next one, wherever it lands.
        """
        from pydantic import SecretStr

        from replay.engine import _unmask, config_digest

        first = _unmask({"credential": SecretStr(PASSWORD)})
        second = _unmask({"credential": SecretStr("something-else")})
        assert first != second, "two credentials must not digest identically"
        assert PASSWORD not in str(first), "...and neither may appear in the clear"
        assert PASSWORD not in config_digest({"credential": SecretStr(PASSWORD)})

    def test_a_masked_dump_really_does_lose_the_difference(self):
        """The behaviour the hashing exists to defeat, pinned so a change to
        pydantic's masking cannot quietly invalidate the argument above."""
        a = load_settings(storage={"backend": "postgres", "postgres_dsn": DSN})
        b = load_settings(
            storage={"backend": "postgres", "postgres_dsn": DSN.replace("db.internal", "db2")}
        )
        assert (
            a.model_dump(mode="json")["storage"]["postgres_dsn"]
            == b.model_dump(mode="json")["storage"]["postgres_dsn"]
        ), "if this ever differs, the masking behaviour changed"
