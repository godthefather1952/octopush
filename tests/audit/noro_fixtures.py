"""Drive the REAL ``Noro`` agent from a list of venue states.

Audit-only. The point is that every number this audit reports comes from
production code paths -- ``Noro.on_market_state`` and ``Noro.evaluate`` -- not
from a reimplementation of the formulas. A reimplementation would agree with
itself and prove nothing.
"""

from __future__ import annotations

from agents.noro.agent import Noro
from core.bus import InMemoryEventBus
from core.clock import ManualClock
from core.config import NoroConfig, Settings, load_settings
from core.health import HealthRegistry
from core.models.agent import AgentOpinion
from core.models.market import VenueMarketState
from core.models.opportunity import Opportunity
from tests.audit.helpers import START_MS, SYMBOL, market, opportunity


def settings_with(config: NoroConfig, *, symbols: list[str] | None = None) -> Settings:
    base = load_settings()
    return base.model_copy(
        update={"noro": config, "symbols": symbols or [SYMBOL]}
    )


def build_noro(
    config: NoroConfig | None = None,
    *,
    symbols: list[str] | None = None,
    clock: ManualClock | None = None,
) -> Noro:
    clock = clock or ManualClock(START_MS)
    return Noro(
        bus=InMemoryEventBus(raise_on_handler_error=True),
        clock=clock,
        settings=settings_with(config or NoroConfig(), symbols=symbols),
        health=HealthRegistry(clock=clock),
    )


def opinion_for(
    states: list[VenueMarketState],
    config: NoroConfig,
    buy_venue: str,
    sell_venue: str,
    *,
    symbol: str = SYMBOL,
    now_ms: int = START_MS,
    opp: Opportunity | None = None,
    symbols: list[str] | None = None,
) -> AgentOpinion | None:
    """The opinion the real agent produces for this market and opportunity."""
    noro = build_noro(config, symbols=symbols or [symbol])
    noro.on_market_state(market(*states))
    return noro.evaluate(
        opp or opportunity(buy_venue, sell_venue, symbol=symbol), now_ms
    )


def signal_for(
    states: list[VenueMarketState],
    config: NoroConfig,
    buy_venue: str,
    sell_venue: str,
    **kwargs,
) -> float | None:
    opinion = opinion_for(states, config, buy_venue, sell_venue, **kwargs)
    return None if opinion is None else opinion.signal
