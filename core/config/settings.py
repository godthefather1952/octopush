"""Configuration.

Everything tunable lives here and is env-overridable.  Defaults are chosen so
that ``python -m apps.orchestrator`` runs a complete, self-contained paper
session with no external services.
"""

from __future__ import annotations

import json
import os
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from core.models.common import AgentId, TradingMode

ENV_PREFIX = "TF_"


def _env(name: str, default: Any) -> Any:
    raw = os.environ.get(ENV_PREFIX + name)
    if raw is None:
        return default
    if isinstance(default, bool):
        return raw.strip().lower() in {"1", "true", "yes", "on"}
    if isinstance(default, int) and not isinstance(default, bool):
        return int(raw)
    if isinstance(default, float):
        return float(raw)
    if isinstance(default, (list, dict)):
        return json.loads(raw)
    return raw


class FeeSchedule(BaseModel):
    """Venue fee tiers, in basis points of notional."""

    model_config = ConfigDict(extra="forbid")

    maker_bps: float = 1.0
    taker_bps: float = 5.0

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
    latency_ms: int = 40
    #: Modelled cancel round-trip.
    cancel_latency_ms: int = 60
    #: Book depth to maintain per side.
    book_depth: int = 25
    enabled: bool = True
    symbols: list[str] = Field(default_factory=lambda: ["BTC-USD", "ETH-USD"])


class RiskLimits(BaseModel):
    """RUNE-CORE's deterministic limits. Every one is a hard gate."""

    model_config = ConfigDict(extra="forbid")

    max_position_notional: float = 50_000.0
    max_gross_exposure: float = 150_000.0
    max_net_exposure: float = 25_000.0
    max_leverage: float = 2.0
    max_daily_loss: float = 2_500.0
    max_drawdown: float = 5_000.0
    max_venue_exposure: float = 75_000.0
    max_strategy_exposure: float = 100_000.0
    max_order_notional: float = 25_000.0
    #: Below this, a trade is not worth doing: the headroom left by the other
    #: limits is too small to carry meaningful edge past its costs.
    min_trade_notional: float = 250.0
    max_unhedged_notional: float = 10_000.0
    #: Market data older than this cannot support a trade.
    max_data_age_ms: int = 2_000
    #: Minimum expected net edge, in bps, for a trade to be permitted.
    min_expected_edge_bps: float = 2.0
    #: Rolling API/feed error rate above which trading halts.
    max_error_rate: float = 0.25
    #: Maximum simultaneous open orders.
    max_open_orders: int = 20


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
    entry_threshold: float = 0.60
    #: Deliberately below the entry threshold to avoid churning a position out
    #: on the first tick of noise.
    exit_threshold: float = 0.45
    #: Grace period during which an expired opinion counts as DEGRADED (and is
    #: down-weighted) rather than STALE (excluded entirely).
    degraded_grace_ms: int = 500
    #: Multiplier applied to a DEGRADED agent's weight.
    degraded_weight_factor: float = 0.35


class ExecutionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: Probability that a passive order at the touch is filled per evaluation.
    maker_fill_probability: float = 0.35
    #: Fraction of visible size at a level that is assumed to be ahead of us.
    queue_ahead_fraction: float = 0.6
    #: Simulated fraction of an order that may fill on one pass.
    max_partial_fraction: float = 1.0
    #: Probability a resting level vanishes before we reach it.
    liquidity_vanish_probability: float = 0.05
    #: Adverse price drift applied per 100ms of modelled latency, in bps.
    latency_drift_bps_per_100ms: float = 0.4
    default_order_ttl_ms: int = 5_000
    #: Deterministic seed for the simulator's randomness.
    seed: int = 20260901


class ZephrConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: Notional ladder used to find the maximum economical size.
    size_ladder: list[float] = Field(
        default_factory=lambda: [1_000, 5_000, 10_000, 25_000, 50_000]
    )
    #: Market-impact coefficient: impact_bps = k * (size / depth) ** exponent.
    impact_coefficient: float = 12.0
    impact_exponent: float = 1.35
    #: Penalty applied for expected latency, in bps.
    latency_penalty_bps: float = 0.5
    #: Cost assumed for the hedge leg, in bps.
    hedge_cost_bps: float = 1.0
    #: Edge below which a size is not considered economical.
    min_net_edge_bps: float = 1.0


class NoroConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: Depth window (bps from mid) used to weight each venue's contribution.
    liquidity_window_bps: float = 10.0
    #: Weight given to the microprice vs the mid when forming a venue price.
    microprice_weight: float = 0.5
    #: Opinion TTL.
    ttl_ms: int = 2_000
    #: Deviation, in bps, at which the signal saturates to |1|.
    saturation_bps: float = 15.0


class LumenConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str = "null"
    model: str = "claude-opus-5"
    #: The intelligence loop runs far slower than the market loop.
    poll_interval_s: float = 30.0
    ttl_s: int = 60
    timeout_s: float = 20.0
    max_tokens: int = 1024
    #: Consecutive failures after which LUMEN reports OFFLINE. It never blocks
    #: the fast loop; the orchestrator simply loses an optional input.
    failure_threshold: int = 3


class StorageConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: "memory" | "sqlite" | "postgres"
    backend: str = "sqlite"
    sqlite_path: str = "./data/trading_floor.db"
    postgres_dsn: str = "postgresql://trading:trading@localhost:5432/trading_floor"
    #: Persist raw venue payloads alongside normalised events.
    record_raw: bool = True
    #: Emit a book checkpoint every N updates, to bound replay reconstruction.
    checkpoint_every: int = 200


class Settings(BaseModel):
    """Root configuration object."""

    model_config = ConfigDict(extra="forbid")

    mode: TradingMode = TradingMode.PAPER
    environment: str = "local"
    log_level: str = "INFO"
    log_format: str = "json"

    bus: str = "memory"
    redis_url: str = "redis://localhost:6379/0"

    paper_initial_balance: float = 100_000.0
    #: Symbols the platform trades. Start small; the architecture takes more
    #: without redesign.
    symbols: list[str] = Field(default_factory=lambda: ["BTC-USD", "ETH-USD"])

    venues: list[VenueConfig] = Field(default_factory=list)
    risk: RiskLimits = Field(default_factory=RiskLimits)
    consensus: ConsensusConfig = Field(default_factory=ConsensusConfig)
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)
    zephr: ZephrConfig = Field(default_factory=ZephrConfig)
    noro: NoroConfig = Field(default_factory=NoroConfig)
    lumen: LumenConfig = Field(default_factory=LumenConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)

    #: How often the orchestrator recomputes state, in seconds.
    tick_interval_s: float = 0.25
    #: How often health heartbeats are published.
    heartbeat_interval_s: float = 2.0
    #: Detection threshold: minimum gross cross-venue dislocation, in bps.
    min_dislocation_bps: float = 4.0
    #: Delta tolerance, in quote notional, before OKAPI hedges.
    hedge_tolerance_notional: float = 500.0
    api_host: str = "0.0.0.0"
    api_port: int = 8080

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
    feed = str(_env("FEED", "simulated")).lower()
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
