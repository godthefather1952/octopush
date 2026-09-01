"""Read-only HTTP API over the running platform.

Every endpoint is a projection of state the platform already holds; nothing
here can place, cancel or size a trade.  The one mutating endpoint is the
manual kill switch, which can only ever make the system *safer*.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, PlainTextResponse

from apps.orchestrator.wiring import Platform
from core.models.common import AgentId
from core.models.ops import HealthStatus

DASHBOARD = Path(__file__).resolve().parents[1] / "dashboard" / "index.html"


def _venue_rows(platform: Platform) -> list[dict[str, Any]]:
    market = platform.state.market
    if market is None:
        return []
    rows = []
    for key, state in sorted(market.venues.items()):
        rows.append(
            {
                "key": key,
                "venue": state.venue,
                "symbol": state.symbol,
                "bid": state.metrics.best_bid,
                "ask": state.metrics.best_ask,
                "mid": state.metrics.mid,
                "microprice": state.metrics.microprice,
                "spread_bps": state.metrics.spread_bps,
                "imbalance": state.metrics.imbalance,
                "bid_depth": state.metrics.bid_depth_notional,
                "ask_depth": state.metrics.ask_depth_notional,
                "vol_bps": state.metrics.short_vol_bps,
                "quality": state.quality.value,
                "age_ms": state.age_ms,
                "latency_ms": state.latency_ms,
                "connected": state.connected,
                "reconnects": state.reconnects,
                "sequence_gaps": state.sequence_gaps,
            }
        )
    return rows


def _consolidated_rows(platform: Platform) -> list[dict[str, Any]]:
    market = platform.state.market
    if market is None:
        return []
    return [
        {
            "symbol": view.symbol,
            "reference_price": view.reference_price,
            "best_bid": view.best_bid,
            "best_bid_venue": view.best_bid_venue,
            "best_ask": view.best_ask,
            "best_ask_venue": view.best_ask_venue,
            "cross_venue_spread_bps": view.cross_venue_spread_bps,
            "max_deviation_bps": view.max_deviation_bps,
            "max_deviation_venue": view.max_deviation_venue,
            "quality": view.quality.value,
            "fair_value": (
                platform.noro.fair_value(view.symbol).fair_value
                if platform.noro.fair_value(view.symbol)
                else None
            ),
        }
        for view in sorted(market.consolidated.values(), key=lambda v: v.symbol)
    ]


def _opportunity_rows(platform: Platform, limit: int = 25) -> list[dict[str, Any]]:
    records = sorted(
        platform.state.opportunities.values(), key=lambda r: r.updated_at, reverse=True
    )[:limit]
    return [
        {
            "opportunity_id": r.opportunity.opportunity_id,
            "symbol": r.opportunity.symbol,
            "kind": r.opportunity.kind.value,
            "state": r.state.value,
            "gross_edge_bps": r.opportunity.gross_edge_bps,
            "expected_net_edge_bps": (
                r.intent.expected_net_edge_bps if r.intent else None
            ),
            "notional": r.intent.notional if r.intent else None,
            "consensus": r.last_agreement,
            "entry_consensus": r.entry_agreement,
            "rejected_reason": r.rejected_reason,
            "realized_pnl": r.realized_pnl,
            "fees": r.fees,
            "updated_at": r.updated_at,
        }
        for r in records
    ]


def create_app(platform: Platform) -> FastAPI:
    app = FastAPI(
        title="Multi-Agent Trading Floor",
        description="Paper-trading control surface. No live execution exists in this build.",
        version="0.1.0",
    )

    @app.get("/", response_class=HTMLResponse)
    async def dashboard() -> str:
        if not DASHBOARD.exists():  # pragma: no cover - packaging guard
            raise HTTPException(status_code=404, detail="dashboard not installed")
        return DASHBOARD.read_text()

    @app.get("/health")
    async def health() -> dict[str, Any]:
        snapshot = platform.health.snapshot()
        return {
            "status": snapshot.status.value,
            "mode": platform.settings.mode.value,
            "warmed_up": platform.orchestrator.warmed_up,
            "components": {
                name: {
                    "status": component.status.value,
                    "version": component.version,
                    "queue_depth": component.queue_depth,
                    "last_heartbeat_ms": component.last_heartbeat_ms,
                    "errors": component.error_count,
                    "detail": component.detail,
                }
                for name, component in sorted(snapshot.components.items())
            },
        }

    @app.get("/api/state")
    async def state() -> dict[str, Any]:
        portfolio = platform.state.portfolio
        kill = platform.state.kill_switch
        util = platform.state.risk_utilization
        return {
            "mode": platform.settings.mode.value,
            "session_id": platform.session_id,
            "ticks": platform.orchestrator.ticks,
            "warmed_up": platform.orchestrator.warmed_up,
            "now_ms": platform.clock.now_ms(),
            "venues": _venue_rows(platform),
            "consolidated": _consolidated_rows(platform),
            "opportunities": _opportunity_rows(platform),
            "portfolio": {
                "initial_balance": portfolio.initial_balance if portfolio else None,
                "cash": portfolio.cash if portfolio else None,
                "equity": portfolio.equity if portfolio else None,
                "gross_pnl": portfolio.gross_pnl if portfolio else None,
                "net_pnl": portfolio.net_pnl if portfolio else None,
                "realized_pnl": portfolio.realized_pnl if portfolio else None,
                "unrealized_pnl": portfolio.unrealized_pnl if portfolio else None,
                "fees_paid": portfolio.fees_paid if portfolio else None,
                "drawdown": portfolio.drawdown if portfolio else None,
                "gross_exposure": portfolio.gross_exposure if portfolio else None,
                "net_exposure": portfolio.net_exposure if portfolio else None,
                "positions": [
                    {
                        "key": key,
                        "venue": position.venue,
                        "symbol": position.symbol,
                        "quantity": position.quantity,
                        "average_entry_price": position.average_entry_price,
                        "mark_price": position.mark_price,
                        "unrealized_pnl": position.unrealized_pnl,
                        "realized_pnl": position.realized_pnl,
                    }
                    for key, position in sorted((portfolio.positions if portfolio else {}).items())
                ],
            },
            "risk": {
                "gross_exposure": util.gross_exposure,
                "max_gross_exposure": util.max_gross_exposure,
                "net_exposure": util.net_exposure,
                "max_net_exposure": util.max_net_exposure,
                "day_loss": util.day_loss,
                "max_day_loss": util.max_day_loss,
                "drawdown": util.drawdown,
                "max_drawdown": util.max_drawdown,
                "unhedged_notional": util.unhedged_notional,
                "max_unhedged_notional": util.max_unhedged_notional,
                "worst_utilization": util.worst_utilization(),
            },
            "kill_switch": {
                "engaged": kill.engaged,
                "trading_allowed": kill.trading_allowed,
                "halt_new_trades": kill.halt_new_trades,
                "execution_disabled": kill.execution_disabled,
                "triggered_by": kill.triggered_by,
                "triggered_at": kill.triggered_at,
            },
            "open_orders": [
                {
                    "client_order_id": order.client_order_id,
                    "venue": order.venue,
                    "symbol": order.symbol,
                    "side": order.side.value,
                    "quantity": order.quantity,
                    "filled": order.filled_quantity,
                    "status": order.status.value,
                    "limit_price": order.limit_price,
                }
                for order in platform.veska.open_orders()
            ],
            "rejections": [
                {
                    "symbol": decision.symbol,
                    "verdict": decision.verdict.value,
                    "reasons": decision.reason_codes,
                    "gates": [g.name for g in decision.failed_gates],
                    "created_at": decision.created_at,
                }
                for decision in platform.state.rejections[-15:][::-1]
            ],
            "errors": platform.state.errors[-15:][::-1],
        }

    @app.get("/api/agents")
    async def agents() -> dict[str, Any]:
        snapshot = platform.health.snapshot()
        scores = platform.orchestrator.scorecard.scores()
        weights = platform.settings.consensus.weights
        return {
            "agents": [
                {
                    "agent": agent.value,
                    "weight": weights.get(agent),
                    "required": agent in platform.settings.consensus.required_agents,
                    "status": (
                        snapshot.components[agent.value].status.value
                        if agent.value in snapshot.components
                        else HealthStatus.OFFLINE.value
                    ),
                    "observations": scores[agent].observations if agent in scores else 0,
                    "predictive_contribution": (
                        scores[agent].predictive_contribution if agent in scores else 0.0
                    ),
                    "hit_rate": scores[agent].hit_rate if agent in scores else 0.0,
                }
                for agent in AgentId
                if agent in weights
            ],
            "sufficient_evidence": platform.orchestrator.scorecard.sufficient_evidence,
            "intelligence": {
                "provider": platform.lumen.provider.name,
                "calls": platform.lumen.calls,
                "failures": platform.lumen.failures,
                "mean_latency_ms": platform.lumen.mean_latency_ms,
            },
        }

    @app.get("/api/attributions")
    async def attributions(limit: int = 25) -> dict[str, Any]:
        trades = platform.orchestrator.scorecard.trades[-limit:][::-1]
        return {
            "trades": [
                {
                    "trade_ref": t.trade_ref,
                    "symbol": t.symbol,
                    "strategy": t.strategy,
                    "consensus": t.consensus_agreement,
                    "expected_net_edge_bps": t.expected_net_edge_bps,
                    "expected_costs_bps": t.expected_costs_bps,
                    "realized_pnl": t.realized_pnl,
                    "fees": t.fees,
                    "slippage_bps": t.slippage_bps,
                    "filled_notional": t.filled_notional,
                    "contributions": {k.value: v for k, v in t.contributions.items()},
                    "signals": {k.value: v for k, v in t.signals.items()},
                }
                for t in trades
            ]
        }

    @app.get("/api/metrics")
    async def api_metrics() -> dict[str, float]:
        return platform.metrics.snapshot()

    @app.get("/metrics", response_class=PlainTextResponse)
    async def prometheus() -> str:
        return platform.metrics.render()

    @app.post("/api/kill-switch")
    async def engage(trigger: str = "MANUAL", detail: str = "engaged via API") -> dict[str, Any]:
        """Manual kill switch. The only mutating endpoint, and it only stops things."""
        state = await platform.kill_switch.engage(trigger, detail)
        platform.state.kill_switch = state
        return {"engaged": state.engaged, "triggered_by": state.triggered_by}

    return app
