"""Configuration.

Everything tunable lives here and is env-overridable.  Defaults are chosen so
that ``python -m apps.orchestrator`` runs a complete, self-contained paper
session with no external services.
"""

from __future__ import annotations

import json
import os
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from core.models.common import AgentId, TradingMode

ENV_PREFIX = "TF_"

#: Accepted values for ``TF_FEED``. "simulated" is the offline deterministic
#: generator; "live" is the read-only public market-data adapters.
_FEEDS = frozenset({"simulated", "live"})


class ConfigError(ValueError):
    """A configuration value could not be used.

    Always names the variable, the value seen and what was expected — a bare
    ``could not convert string to float`` leaves an operator guessing which of
    sixteen environment variables was wrong.
    """


def _env(name: str, default: Any) -> Any:
    variable = ENV_PREFIX + name
    raw = os.environ.get(variable)
    if raw is None:
        return default
    try:
        if isinstance(default, bool):
            lowered = raw.strip().lower()
            if lowered in {"1", "true", "yes", "on"}:
                return True
            if lowered in {"0", "false", "no", "off"}:
                return False
            raise ValueError("expected a boolean such as true/false")
        if isinstance(default, int) and not isinstance(default, bool):
            return int(raw)
        if isinstance(default, float):
            return float(raw)
        if isinstance(default, (list, dict)):
            return json.loads(raw)
    except (ValueError, json.JSONDecodeError) as exc:
        raise ConfigError(
            f"{variable}={raw!r} is not valid: expected "
            f"{type(default).__name__}. ({exc})"
        ) from exc
    return raw


class FeeSchedule(BaseModel):
    """Venue fee tiers, in basis points of notional."""

    model_config = ConfigDict(extra="forbid")

    maker_bps: float = Field(default=1.0, ge=-10.0, le=1_000.0)
    taker_bps: float = Field(default=5.0, ge=-10.0, le=1_000.0)

    def fee_bps(self, is_maker: bool) -> float:
        return self.maker_bps if is_maker else self.taker_bps


class VenueConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    #: Human label shown on the dashboard.
    display_name: str
    #: Adapter implementation key, resolved by ``venues.registry``.
    adapter: str
    #: Public market-data endpoint. No authenticated or trading endpoint is
    #: configurable anywhere in this codebase.
    ws_url: str | None = None
    rest_url: str | None = None
    fees: FeeSchedule = Field(default_factory=FeeSchedule)
    #: Modelled one-way latency to the venue, used by the paper simulator.
    latency_ms: int = Field(default=40, ge=0, le=60_000)
    #: Modelled cancel round-trip.
    cancel_latency_ms: int = Field(default=60, ge=0, le=60_000)
    #: Book depth to maintain per side, in price levels.
    book_depth_levels: int = Field(default=25, gt=0, le=5_000)
    enabled: bool = True
    symbols: list[str] = Field(default_factory=lambda: ["BTC-USD", "ETH-USD"])


class RiskLimits(BaseModel):
    """RUNE-CORE's deterministic limits. Every one is a hard gate."""

    model_config = ConfigDict(extra="forbid")

    max_position_notional: float = Field(default=50_000.0, gt=0)
    max_gross_exposure: float = Field(default=150_000.0, gt=0)
    max_net_exposure: float = Field(default=25_000.0, gt=0)
    max_leverage: float = Field(default=2.0, gt=0)
    max_daily_loss: float = Field(default=2_500.0, gt=0)
    max_drawdown: float = Field(default=5_000.0, gt=0)
    max_venue_exposure: float = Field(default=75_000.0, gt=0)
    max_strategy_exposure: float = Field(default=100_000.0, gt=0)
    max_order_notional: float = Field(default=25_000.0, gt=0)
    #: Below this, a trade is not worth doing: the headroom left by the other
    #: limits is too small to carry meaningful edge past its costs.
    min_trade_notional: float = Field(default=250.0, gt=0)
    max_unhedged_notional: float = Field(default=10_000.0, gt=0)
    #: Market data older than this cannot support a trade.
    max_data_age_ms: int = Field(default=2_000, gt=0)
    #: Minimum expected net edge, in bps, for a trade to be permitted.
    min_expected_edge_bps: float = Field(default=2.0, ge=0.0)
    #: Rolling API/feed error rate above which trading halts.
    max_error_rate: float = Field(default=0.25, ge=0.0, le=1.0)
    #: Maximum simultaneous open orders.
    max_open_orders: int = Field(default=20, gt=0)

    @model_validator(mode="after")
    def _limits_are_coherent(self) -> RiskLimits:
        if self.min_trade_notional > self.max_order_notional:
            raise ValueError(
                f"min_trade_notional ({self.min_trade_notional}) exceeds "
                f"max_order_notional ({self.max_order_notional}): no order size "
                "would ever be permitted"
            )
        if self.max_order_notional > self.max_position_notional:
            raise ValueError(
                f"max_order_notional ({self.max_order_notional}) exceeds "
                f"max_position_notional ({self.max_position_notional}): a single "
                "permitted order would breach the position limit"
            )
        if self.max_position_notional > self.max_gross_exposure:
            raise ValueError(
                f"max_position_notional ({self.max_position_notional}) exceeds "
                f"max_gross_exposure ({self.max_gross_exposure})"
            )
        return self


class ConsensusConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    weights: dict[AgentId, float] = Field(
        default_factory=lambda: {
            AgentId.TIDAL: 1.4,
            AgentId.NORO: 1.5,
            AgentId.ZEPHR: 1.5,
            AgentId.LUMEN: 0.5,
        }
    )
    #: Agents whose absence suspends the strategy rather than being ignored.
    required_agents: list[AgentId] = Field(
        default_factory=lambda: [AgentId.TIDAL, AgentId.NORO, AgentId.ZEPHR]
    )
    #: Thresholds are calibrated against the *achievable* range, not against
    #: 1.0. Consensus is a weighted mean, so an agent that is structurally
    #: near-neutral (TIDAL's microstructure read usually is) caps the maximum
    #: attainable agreement at roughly the non-neutral share of total weight.
    #: With these weights that ceiling is around 0.70, so 0.60 already demands
    #: that both valuation and executability argue strongly for the trade.
    entry_threshold: float = Field(default=0.60, ge=0.0, le=1.0)
    #: Deliberately below the entry threshold to avoid churning a position out
    #: on the first tick of noise.
    exit_threshold: float = Field(default=0.45, ge=0.0, le=1.0)
    #: Grace period during which an expired opinion counts as DEGRADED (and is
    #: down-weighted) rather than STALE (excluded entirely).
    degraded_grace_ms: int = Field(default=500, ge=0)
    #: How long the orchestrator waits for the required agents to answer an
    #: evaluation request before deciding without them. This is a transport
    #: synchronisation bound, not a strategy threshold: with in-process agents
    #: the responses are already present when the bus drains and this never
    #: applies. It exists so that remote agents get a bounded, explicit wait
    #: instead of the orchestrator inferring completion from the bus.
    agent_response_timeout_ms: int = Field(default=1_000, gt=0)
    #: Multiplier applied to a DEGRADED agent's weight.
    degraded_weight_factor: float = Field(default=0.35, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _exit_below_entry(self) -> ConsensusConfig:
        if self.exit_threshold >= self.entry_threshold:
            raise ValueError(
                f"exit_threshold ({self.exit_threshold}) must be below "
                f"entry_threshold ({self.entry_threshold}); an exit threshold at "
                "or above entry gives no hysteresis and churns positions"
            )
        if not self.weights:
            raise ValueError("consensus weights cannot be empty")
        missing = [a.value for a in self.required_agents if a not in self.weights]
        if missing:
            raise ValueError(
                f"required agents have no consensus weight: {', '.join(missing)}"
            )
        return self


class ExecutionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: Probability that a passive order at the touch is filled per evaluation.
    maker_fill_probability: float = Field(default=0.35, ge=0.0, le=1.0)
    #: Fraction of visible size at a level that is assumed to be ahead of us.
    queue_ahead_fraction: float = Field(default=0.6, ge=0.0, le=1.0)
    #: Simulated fraction of an order that may fill on one pass.
    max_partial_fraction: float = Field(default=1.0, gt=0.0, le=1.0)
    #: Probability a resting level vanishes before we reach it.
    liquidity_vanish_probability: float = Field(default=0.05, ge=0.0, le=1.0)
    #: Adverse price drift applied per 100ms of modelled latency, in bps.
    latency_drift_bps_per_100ms: float = Field(default=0.4, ge=0.0)
    default_order_ttl_ms: int = Field(default=5_000, gt=0)
    #: Deterministic seed for the simulator's randomness.
    seed: int = 20260901


class ZephrConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: Notional ladder used to find the maximum economical size.
    size_ladder: list[float] = Field(
        default_factory=lambda: [1_000, 5_000, 10_000, 25_000, 50_000]
    )
    #: Market-impact coefficient: impact_bps = k * (size / depth) ** exponent.
    impact_coefficient: float = Field(default=12.0, gt=0.0)
    impact_exponent: float = Field(default=1.35, gt=0.0)
    #: Penalty applied for expected latency, in bps.
    latency_penalty_bps: float = Field(default=0.5, ge=0.0)
    #: Cost assumed for the hedge leg, in bps.
    hedge_cost_bps: float = Field(default=1.0, ge=0.0)
    #: Edge below which a size is not considered economical.
    min_net_edge_bps: float = Field(default=1.0, ge=0.0)


class NoroConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: Depth window (bps from mid) used to weight each venue's contribution.
    liquidity_window_bps: float = Field(default=10.0, gt=0.0)
    #: Weight given to the microprice vs the mid when forming a venue price.
    microprice_weight: float = Field(default=0.5, ge=0.0, le=1.0)
    #: Opinion TTL.
    ttl_ms: int = Field(default=2_000, gt=0)
    #: Deviation, in bps, at which the signal saturates to |1|.
    saturation_bps: float = Field(default=15.0, gt=0.0)


class LumenConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: Literal["null", "none", "disabled", "scripted", "claude"] = "null"
    model: str = "claude-opus-5"
    #: The intelligence loop runs far slower than the market loop.
    poll_interval_s: float = Field(default=30.0, gt=0.0)
    ttl_s: int = Field(default=60, gt=0)
    timeout_s: float = Field(default=20.0, gt=0.0)
    max_tokens: int = Field(default=1024, gt=0)
    #: Consecutive failures after which LUMEN reports OFFLINE. It never blocks
    #: the fast loop; the orchestrator simply loses an optional input.
    failure_threshold: int = Field(default=3, gt=0)


class StorageConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: "memory" | "sqlite" | "postgres"
    backend: Literal["memory", "sqlite", "postgres"] = "sqlite"
    sqlite_path: str = "./data/trading_floor.db"
    postgres_dsn: str = "postgresql://trading:trading@localhost:5432/trading_floor"
    #: Persist raw venue payloads alongside normalised events.
    record_raw: bool = True
    # There is deliberately no `checkpoint_every` here. It documented book
    # checkpointing to bound replay reconstruction — a feature that does not
    # exist and that nothing read the setting for. A knob that changes nothing
    # is worse than no knob: an operator tuning it believes they have altered
    # replay behaviour.


class Settings(BaseModel):
    """Root configuration object."""

    model_config = ConfigDict(extra="forbid")

    mode: TradingMode = TradingMode.PAPER
    environment: str = "local"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    log_format: Literal["json", "text"] = "json"

    bus: Literal["memory", "redis"] = "memory"
    redis_url: str = "redis://localhost:6379/0"

    paper_initial_balance: float = Field(default=100_000.0, gt=0)
    #: Symbols the platform trades. Start small; the architecture takes more
    #: without redesign.
    symbols: list[str] = Field(
        default_factory=lambda: ["BTC-USD", "ETH-USD"], min_length=1
    )

    venues: list[VenueConfig] = Field(default_factory=list)
    risk: RiskLimits = Field(default_factory=RiskLimits)
    consensus: ConsensusConfig = Field(default_factory=ConsensusConfig)
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)
    zephr: ZephrConfig = Field(default_factory=ZephrConfig)
    noro: NoroConfig = Field(default_factory=NoroConfig)
    lumen: LumenConfig = Field(default_factory=LumenConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)

    #: How often the orchestrator recomputes state, in seconds.
    tick_interval_s: float = Field(default=0.25, gt=0)
    #: How often health heartbeats are published.
    heartbeat_interval_s: float = Field(default=2.0, gt=0)
    #: Detection threshold: minimum gross cross-venue dislocation, in bps.
    min_dislocation_bps: float = Field(default=4.0, ge=0.0)
    #: Delta tolerance, in quote notional, before OKAPI hedges.
    hedge_tolerance_notional: float = Field(default=500.0, ge=0.0)
    api_host: str = "0.0.0.0"
    api_port: int = Field(default=8080, ge=1, le=65_535)

    @model_validator(mode="after")
    def _settings_are_coherent(self) -> Settings:
        if self.mode is not TradingMode.PAPER:
            raise ValueError(f"unsupported execution mode: {self.mode}")
        if not self.venues:
            raise ValueError("at least one venue must be configured")
        if self.paper_initial_balance < self.risk.min_trade_notional:
            raise ValueError(
                f"paper_initial_balance ({self.paper_initial_balance}) is below "
                f"min_trade_notional ({self.risk.min_trade_notional}): no trade "
                "could ever be funded"
            )
        names = [v.name for v in self.venues]
        duplicates = {n for n in names if names.count(n) > 1}
        if duplicates:
            raise ValueError(f"duplicate venue names: {', '.join(sorted(duplicates))}")
        if self.bus == "redis" and not self.redis_url:
            raise ValueError("bus='redis' requires redis_url")
        if self.storage.backend == "postgres" and not self.storage.postgres_dsn:
            raise ValueError("storage.backend='postgres' requires postgres_dsn")
        return self

    @property
    def enabled_venues(self) -> list[VenueConfig]:
        return [v for v in self.venues if v.enabled]

    def venue(self, name: str) -> VenueConfig:
        for v in self.venues:
            if v.name == name:
                return v
        raise KeyError(f"unknown venue: {name}")


def default_venues() -> list[VenueConfig]:
    """The two venues the initial scope calls for.

    Both adapters are public-market-data only.  ``simulated`` is the offline
    generator used by tests, replay development and the default local run.
    """
    return [
        VenueConfig(
            name="VENUE_A",
            display_name="Venue A (Binance-style)",
            adapter="binance_public",
            ws_url="wss://stream.binance.com:9443/stream",
            rest_url="https://api.binance.com",
            fees=FeeSchedule(maker_bps=1.0, taker_bps=5.0),
            latency_ms=35,
        ),
        VenueConfig(
            name="VENUE_B",
            display_name="Venue B (Coinbase-style)",
            adapter="coinbase_public",
            ws_url="wss://ws-feed.exchange.coinbase.com",
            rest_url="https://api.exchange.coinbase.com",
            fees=FeeSchedule(maker_bps=2.0, taker_bps=6.0),
            latency_ms=55,
        ),
    ]


def simulated_venues() -> list[VenueConfig]:
    """Offline venues backed by the synthetic market generator."""
    return [
        VenueConfig(
            name="VENUE_A",
            display_name="Venue A (simulated)",
            adapter="simulated",
            fees=FeeSchedule(maker_bps=1.0, taker_bps=5.0),
            latency_ms=35,
        ),
        VenueConfig(
            name="VENUE_B",
            display_name="Venue B (simulated)",
            adapter="simulated",
            fees=FeeSchedule(maker_bps=2.0, taker_bps=6.0),
            latency_ms=55,
        ),
    ]


def load_settings(**overrides: Any) -> Settings:
    """Build settings from defaults, then environment, then explicit overrides."""
    # A typo here used to fall through to the live public venue set, silently
    # trading a deterministic offline generator for network-dependent feeds.
    feed = str(_env("FEED", "simulated")).strip().lower()
    if feed not in _FEEDS:
        raise ConfigError(
            f"{ENV_PREFIX}FEED={feed!r} is not a known feed. "
            f"Expected one of {sorted(_FEEDS)}."
        )
    # Read the mode explicitly so an operator asking for anything other than
    # paper is refused loudly. Silently ignoring TF_MODE=live was safe (the
    # boundary is structural) but it discarded operator intent without a word.
    requested_mode = str(_env("MODE", TradingMode.PAPER.value)).strip().lower()
    if requested_mode != TradingMode.PAPER.value.lower():
        raise ConfigError(
            f"{ENV_PREFIX}MODE={requested_mode!r} is not supported. This build is "
            "paper-trading only and contains no exchange order-submission "
            f"implementation; the only accepted value is "
            f"{TradingMode.PAPER.value.lower()!r}."
        )

    base: dict[str, Any] = {
        "environment": _env("ENVIRONMENT", "local"),
        "log_level": _env("LOG_LEVEL", "INFO"),
        "log_format": _env("LOG_FORMAT", "json"),
        "bus": _env("BUS", "memory"),
        "redis_url": _env("REDIS_URL", "redis://localhost:6379/0"),
        "paper_initial_balance": _env("PAPER_INITIAL_BALANCE", 100_000.0),
        "symbols": _env("SYMBOLS", ["BTC-USD", "ETH-USD"]),
        "tick_interval_s": _env("TICK_INTERVAL_S", 0.25),
        "min_dislocation_bps": _env("MIN_DISLOCATION_BPS", 4.0),
        "api_host": _env("API_HOST", "0.0.0.0"),
        "api_port": _env("API_PORT", 8080),
        "venues": [
            v.model_dump()
            for v in (simulated_venues() if feed == "simulated" else default_venues())
        ],
        "storage": {
            "backend": _env("STORAGE_BACKEND", "sqlite"),
            "sqlite_path": _env("SQLITE_PATH", "./data/trading_floor.db"),
            "postgres_dsn": _env(
                "POSTGRES_DSN",
                "postgresql://trading:trading@localhost:5432/trading_floor",
            ),
            "record_raw": _env("RECORD_RAW", True),
        },
        "lumen": {
            "provider": _env("INTELLIGENCE_PROVIDER", "null"),
            "model": _env("CLAUDE_MODEL", "claude-opus-5"),
            "poll_interval_s": _env("LUMEN_POLL_INTERVAL_S", 30.0),
        },
    }
    base.update(overrides)
    return Settings.model_validate(base)
