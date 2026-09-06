"""Configuration must reject dangerous values loudly — regression for P0-H7.

The audit found `load_settings()` accepting a negative starting balance, TCP
port 99999, an empty symbol universe, negative intervals, unknown backends and
an invalid log level. Where it did fail, the message was a bare
`could not convert string to float` that never named the variable.

It also found `TF_MODE=live` silently ignored: safe, because the paper
boundary is structural, but operator intent discarded without a word.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from core.config import Settings, load_settings
from core.config.settings import ConsensusConfig, RiskLimits


def load_with(**env: str):
    """Load settings in a subprocess with the given environment."""
    environment = {**os.environ, **env}
    return subprocess.run(
        [sys.executable, "-c", "from core.config import load_settings; load_settings()"],
        capture_output=True,
        text=True,
        env=environment,
        cwd=os.getcwd(),
    )


class TestExecutionModeIsEnforced:
    @pytest.mark.parametrize(
        "mode", ["live", "production", "real", "testnet-live", "LIVE", "prod"]
    )
    def test_unsupported_modes_abort_startup(self, mode):
        result = load_with(TF_MODE=mode)
        assert result.returncode != 0, f"TF_MODE={mode} was accepted"
        assert "not supported" in result.stderr
        assert "paper" in result.stderr.lower()

    def test_paper_is_accepted(self):
        assert load_with(TF_MODE="paper").returncode == 0

    def test_unset_mode_defaults_to_paper(self):
        environment = {k: v for k, v in os.environ.items() if k != "TF_MODE"}
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "from core.config import load_settings;"
                "print(load_settings().mode.value)",
            ],
            capture_output=True,
            text=True,
            env=environment,
            cwd=os.getcwd(),
        )
        assert result.returncode == 0
        assert result.stdout.strip() == "PAPER"

    def test_the_model_itself_refuses_a_non_paper_mode(self):
        with pytest.raises(ValueError):
            load_settings(mode="live")


class TestInvalidValuesAreRejected:
    @pytest.mark.parametrize(
        "variable,value,why",
        [
            ("TF_PAPER_INITIAL_BALANCE", "-50000", "negative starting balance"),
            ("TF_PAPER_INITIAL_BALANCE", "0", "zero starting balance"),
            ("TF_PAPER_INITIAL_BALANCE", "not-a-number", "unparseable float"),
            ("TF_API_PORT", "99999", "port above 65535"),
            ("TF_API_PORT", "0", "port zero"),
            ("TF_API_PORT", "abc", "unparseable int"),
            ("TF_SYMBOLS", "[]", "empty symbol universe"),
            ("TF_SYMBOLS", "BTC-USD", "not JSON"),
            ("TF_TICK_INTERVAL_S", "-1", "negative tick interval"),
            ("TF_TICK_INTERVAL_S", "0", "zero tick interval"),
            ("TF_MIN_DISLOCATION_BPS", "-999", "negative threshold"),
            ("TF_STORAGE_BACKEND", "cassandra", "unknown storage backend"),
            ("TF_BUS", "kafka", "unknown bus kind"),
            ("TF_INTELLIGENCE_PROVIDER", "skynet", "unknown provider"),
            ("TF_LOG_LEVEL", "VERBOSE", "invalid log level"),
            ("TF_LOG_FORMAT", "yaml", "invalid log format"),
        ],
    )
    def test_dangerous_value_aborts_startup(self, variable, value, why):
        result = load_with(**{variable: value})
        assert result.returncode != 0, f"{variable}={value} accepted ({why})"

    @pytest.mark.parametrize(
        "variable,value",
        [
            ("TF_PAPER_INITIAL_BALANCE", "not-a-number"),
            ("TF_API_PORT", "abc"),
            ("TF_SYMBOLS", "BTC-USD"),
            ("TF_TICK_INTERVAL_S", "wat"),
        ],
    )
    def test_the_error_names_the_variable_and_the_value(self, variable, value):
        """An operator must not have to guess which of sixteen variables broke."""
        result = load_with(**{variable: value})
        assert result.returncode != 0
        assert variable in result.stderr, f"error did not name {variable}"
        assert value in result.stderr, "error did not quote the offending value"

    def test_a_misspelled_feed_does_not_silently_go_to_the_network(self):
        """`TF_FEED` was compared against "simulated" with no else-branch.

        Every other value — including a typo — fell through to the live public
        venue set. The data is read-only, so this was never a trading-safety
        breach, but it silently swaps a deterministic offline generator for
        network-dependent feeds, which is the difference between a reproducible
        run and an unreproducible one.
        """
        result = load_with(TF_FEED="simluated")
        assert result.returncode != 0, "a misspelled TF_FEED was accepted"
        assert "TF_FEED" in result.stderr
        assert "simluated" in result.stderr

    @pytest.mark.parametrize("feed", ["simulated", "live"])
    def test_both_real_feeds_are_accepted(self, feed):
        assert load_with(TF_FEED=feed).returncode == 0

    def test_the_simulated_feed_never_carries_a_network_url(self):
        settings = load_settings(**{})
        adapters = {v.adapter for v in settings.venues}
        assert adapters == {"simulated"}, "default feed reached for network adapters"
        assert all(not v.ws_url for v in settings.venues)

    def test_valid_values_are_still_accepted(self):
        result = load_with(
            TF_PAPER_INITIAL_BALANCE="250000",
            TF_API_PORT="9090",
            TF_SYMBOLS='["BTC-USD"]',
            TF_TICK_INTERVAL_S="0.5",
            TF_LOG_LEVEL="DEBUG",
            TF_STORAGE_BACKEND="memory",
        )
        assert result.returncode == 0, result.stderr


class TestFieldConstraints:
    def test_negative_risk_limits_are_refused(self):
        for field in (
            "max_position_notional",
            "max_gross_exposure",
            "max_daily_loss",
            "max_drawdown",
            "max_order_notional",
        ):
            with pytest.raises(ValueError):
                RiskLimits(**{field: -1.0})

    def test_max_data_age_must_be_positive(self):
        with pytest.raises(ValueError):
            RiskLimits(max_data_age_ms=0)

    def test_error_rate_is_a_fraction(self):
        with pytest.raises(ValueError):
            RiskLimits(max_error_rate=1.5)

    def test_thresholds_are_bounded_to_zero_one(self):
        with pytest.raises(ValueError):
            ConsensusConfig(entry_threshold=1.4)
        with pytest.raises(ValueError):
            ConsensusConfig(exit_threshold=-0.1)


class TestCrossFieldRelationships:
    def test_exit_threshold_must_sit_below_entry(self):
        with pytest.raises(ValueError, match="below"):
            ConsensusConfig(entry_threshold=0.5, exit_threshold=0.6)

    def test_equal_thresholds_are_refused(self):
        """Equal thresholds give no hysteresis, which churns positions."""
        with pytest.raises(ValueError):
            ConsensusConfig(entry_threshold=0.5, exit_threshold=0.5)

    def test_required_agents_must_carry_a_weight(self):
        from core.models.common import AgentId

        with pytest.raises(ValueError, match="no consensus weight"):
            ConsensusConfig(
                weights={AgentId.NORO: 1.0},
                required_agents=[AgentId.NORO, AgentId.ZEPHR],
            )

    def test_min_trade_cannot_exceed_max_order(self):
        with pytest.raises(ValueError, match="min_trade_notional"):
            RiskLimits(min_trade_notional=50_000.0, max_order_notional=1_000.0)

    def test_a_single_order_cannot_breach_the_position_limit(self):
        with pytest.raises(ValueError, match="max_order_notional"):
            RiskLimits(max_order_notional=100_000.0, max_position_notional=10_000.0)

    def test_position_limit_cannot_exceed_gross_exposure(self):
        with pytest.raises(ValueError, match="max_position_notional"):
            RiskLimits(
                max_order_notional=1_000.0,
                max_position_notional=100_000.0,
                max_gross_exposure=10_000.0,
            )

    def test_balance_must_fund_at_least_one_trade(self):
        with pytest.raises(ValueError, match="min_trade_notional"):
            load_settings(paper_initial_balance=10.0)

    def test_duplicate_venue_names_are_refused(self):
        from core.config import simulated_venues

        venues = simulated_venues()
        venues[1] = venues[1].model_copy(update={"name": venues[0].name})
        with pytest.raises(ValueError, match="duplicate venue"):
            load_settings(venues=[v.model_dump() for v in venues])

    def test_an_empty_venue_list_is_refused(self):
        with pytest.raises(ValueError, match="at least one venue"):
            load_settings(venues=[])

    def test_the_default_configuration_is_coherent(self):
        """The shipped defaults must satisfy every relationship."""
        settings = load_settings()
        assert isinstance(settings, Settings)
        assert settings.consensus.exit_threshold < settings.consensus.entry_threshold
        assert settings.risk.min_trade_notional <= settings.risk.max_order_notional
        assert settings.paper_initial_balance > 0


#: Name tokens that mean the field denotes a quantity whose unit an operator
#: cannot guess.  Matched as whole underscore-separated tokens: substring
#: matching would read "age" out of "max_leverage".
DIMENSIONED_TOKENS = frozenset(
    {
        "time",
        "timeout",
        "age",
        "latency",
        "interval",
        "ttl",
        "grace",
        "window",
        "delay",
        "deadline",
        "period",
        "duration",
        "every",
    }
)

#: Units the codebase actually uses.  A field carrying a dimensioned token
#: must end in one of these.
#: "deliveries" joined the set with ``error_rate_window_deliveries`` (P5-8):
#: that window is counted in bus delivery attempts, not milliseconds, and the
#: suffix is exactly what stops a reader assuming a duration.
UNIT_TOKENS = frozenset(
    {"ms", "s", "seconds", "bps", "levels", "notional", "updates", "events", "deliveries"}
)


def config_models():
    """Every Pydantic model declared in the settings module."""
    import inspect

    from pydantic import BaseModel

    import core.config.settings as module

    return [
        obj
        for obj in vars(module).values()
        if inspect.isclass(obj)
        and issubclass(obj, BaseModel)
        and obj.__module__ == module.__name__
    ]


def unit_suffix(name: str) -> str:
    """The trailing unit token, with any leading magnitude stripped.

    ``latency_drift_bps_per_100ms`` carries its unit as ``100ms``; the digits
    are a magnitude, not part of the unit.
    """
    return name.rsplit("_", 1)[-1].lstrip("0123456789")


class TestUnitClarity:
    def test_every_dimensioned_field_names_its_unit(self):
        """A bare `timeout` or `max_age` is ambiguous; the suffix carries it.

        Sweeps every settings model rather than a hand-picked list, so a
        config field added later cannot quietly reintroduce the ambiguity.
        """
        offenders = []
        for cls in config_models():
            for name, info in cls.model_fields.items():
                annotation = str(info.annotation)
                if "int" not in annotation and "float" not in annotation:
                    continue
                if not (set(name.split("_")) & DIMENSIONED_TOKENS):
                    continue
                if unit_suffix(name) not in UNIT_TOKENS:
                    offenders.append(f"{cls.__name__}.{name}")
        assert not offenders, f"dimensioned config fields without a unit suffix: {offenders}"

    def test_the_matcher_reads_whole_tokens_not_substrings(self):
        """Guards the test above: `max_leverage` is not an age."""
        assert not (set(["max", "leverage"]) & DIMENSIONED_TOKENS)
        assert set(["max", "data", "age", "ms"]) & DIMENSIONED_TOKENS

    def test_a_compound_unit_still_counts_as_a_unit(self):
        assert unit_suffix("latency_drift_bps_per_100ms") == "ms"

    def test_book_depth_names_its_unit(self):
        """`book_depth` could be levels or notional; `book_depth_levels` cannot."""
        from core.config.settings import VenueConfig

        assert "book_depth_levels" in VenueConfig.model_fields
        assert "book_depth" not in VenueConfig.model_fields

