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
    def test_the_digest_covers_the_dsn_without_exposing_it(self, settings):
        """Replay refuses to compare sessions from different configs.

        That check has to notice a changed DSN, so the digest must still
        depend on it — just not by carrying it in the clear.
        """
        from replay.engine import config_digest

        other = load_settings(
            storage={"backend": "postgres", "postgres_dsn": DSN.replace("db.internal", "db2")}
        )
        first = config_digest(settings.model_dump())
        second = config_digest(other.model_dump())
        assert first != second
        assert PASSWORD not in first

    def test_a_masked_dump_would_have_hidden_the_change(self):
        """Why the digest takes the model dump, not the json dump.

        model_dump(mode="json") renders every SecretStr as the same asterisks,
        so two different credentials digest identically and a changed store
        goes unnoticed. This pins the reason rather than leaving the argument
        order looking arbitrary.
        """
        from replay.engine import config_digest

        a = load_settings(storage={"backend": "postgres", "postgres_dsn": DSN})
        b = load_settings(
            storage={"backend": "postgres", "postgres_dsn": DSN.replace("db.internal", "db2")}
        )
        assert config_digest(a.model_dump(mode="json")) == config_digest(
            b.model_dump(mode="json")
        ), "if this ever differs, the masking behaviour changed"
        assert config_digest(a.model_dump()) != config_digest(b.model_dump())
