"""Metrics registry with Prometheus text exposition.

Deliberately dependency-free: counters, gauges and histograms in a dict, and a
renderer that emits the Prometheus text format.  Swapping in the official
client later is a change to this file only.
"""

from __future__ import annotations

import math
import threading
from dataclasses import dataclass, field

Labels = tuple[tuple[str, str], ...]

DEFAULT_BUCKETS = (1, 5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10_000)


def _labels(labels: dict[str, str] | None) -> Labels:
    return tuple(sorted((labels or {}).items()))


def _render_labels(labels: Labels) -> str:
    if not labels:
        return ""
    inner = ",".join(f'{k}="{_escape(v)}"' for k, v in labels)
    return "{" + inner + "}"


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


@dataclass
class Histogram:
    buckets: tuple[float, ...] = DEFAULT_BUCKETS
    counts: list[int] = field(default_factory=list)
    total: float = 0.0
    count: int = 0

    def __post_init__(self) -> None:
        if not self.counts:
            self.counts = [0] * (len(self.buckets) + 1)

    def observe(self, value: float) -> None:
        if not math.isfinite(value):
            return
        self.total += value
        self.count += 1
        for i, bound in enumerate(self.buckets):
            if value <= bound:
                self.counts[i] += 1
                return
        self.counts[-1] += 1

    @property
    def mean(self) -> float:
        return self.total / self.count if self.count else 0.0

    def cumulative(self) -> list[tuple[float, int]]:
        out: list[tuple[float, int]] = []
        running = 0
        for bound, count in zip(self.buckets, self.counts, strict=False):
            running += count
            out.append((bound, running))
        out.append((float("inf"), running + self.counts[-1]))
        return out


class MetricsRegistry:
    """Thread-safe registry of the platform's metrics."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[tuple[str, Labels], float] = {}
        self._gauges: dict[tuple[str, Labels], float] = {}
        self._histograms: dict[tuple[str, Labels], Histogram] = {}
        self._help: dict[str, str] = {}

    def describe(self, name: str, help_text: str) -> None:
        self._help[name] = help_text

    def inc(self, name: str, value: float = 1.0, **labels: str) -> None:
        key = (name, _labels(labels))
        with self._lock:
            self._counters[key] = self._counters.get(key, 0.0) + value

    def set(self, name: str, value: float, **labels: str) -> None:
        with self._lock:
            self._gauges[(name, _labels(labels))] = value

    def observe(self, name: str, value: float, **labels: str) -> None:
        key = (name, _labels(labels))
        with self._lock:
            if key not in self._histograms:
                self._histograms[key] = Histogram()
            self._histograms[key].observe(value)

    def counter(self, name: str, **labels: str) -> float:
        return self._counters.get((name, _labels(labels)), 0.0)

    def gauge(self, name: str, **labels: str) -> float:
        return self._gauges.get((name, _labels(labels)), 0.0)

    def histogram(self, name: str, **labels: str) -> Histogram | None:
        return self._histograms.get((name, _labels(labels)))

    def snapshot(self) -> dict[str, float]:
        """Flat view, used by the dashboard and the API."""
        out: dict[str, float] = {}
        with self._lock:
            for (name, labels), value in self._counters.items():
                out[name + _render_labels(labels)] = value
            for (name, labels), value in self._gauges.items():
                out[name + _render_labels(labels)] = value
            for (name, labels), hist in self._histograms.items():
                out[f"{name}_mean" + _render_labels(labels)] = hist.mean
                out[f"{name}_count" + _render_labels(labels)] = hist.count
        return out

    def render(self) -> str:
        """Prometheus text exposition format."""
        lines: list[str] = []
        with self._lock:
            emitted: set[str] = set()

            def header(name: str, kind: str) -> None:
                if name in emitted:
                    return
                emitted.add(name)
                if name in self._help:
                    lines.append(f"# HELP {name} {self._help[name]}")
                lines.append(f"# TYPE {name} {kind}")

            for (name, labels), value in sorted(self._counters.items()):
                header(name, "counter")
                lines.append(f"{name}{_render_labels(labels)} {value}")
            for (name, labels), value in sorted(self._gauges.items()):
                header(name, "gauge")
                lines.append(f"{name}{_render_labels(labels)} {value}")
            for (name, labels), hist in sorted(self._histograms.items()):
                header(name, "histogram")
                for bound, count in hist.cumulative():
                    label = "+Inf" if math.isinf(bound) else f"{bound:g}"
                    bucket_labels = (*labels, ("le", label))
                    lines.append(f"{name}_bucket{_render_labels(bucket_labels)} {count}")
                lines.append(f"{name}_sum{_render_labels(labels)} {hist.total}")
                lines.append(f"{name}_count{_render_labels(labels)} {hist.count}")
        return "\n".join(lines) + "\n"


#: Metric names, in one place so the dashboard and the code cannot drift apart.
EVENTS_PROCESSED = "tf_events_processed_total"
MARKET_DATA_LATENCY = "tf_market_data_latency_ms"
WS_RECONNECTS = "tf_venue_reconnects_total"
STALE_FEEDS = "tf_stale_feeds"
SEQUENCE_GAPS = "tf_sequence_gaps_total"
OPPORTUNITIES_DETECTED = "tf_opportunities_detected_total"
OPPORTUNITIES_REJECTED = "tf_opportunities_rejected_total"
PAPER_ORDERS = "tf_paper_orders_total"
PARTIAL_FILLS = "tf_partial_fills_total"
FILL_RATIO = "tf_fill_ratio"
SLIPPAGE_BPS = "tf_slippage_bps"
FEES_PAID = "tf_fees_paid"
GROSS_PNL = "tf_gross_pnl"
NET_PNL = "tf_net_pnl"
DRAWDOWN = "tf_drawdown"
CONSENSUS = "tf_consensus_agreement"
AGENT_UP = "tf_agent_up"
CLAUDE_REQUESTS = "tf_intelligence_requests_total"
CLAUDE_LATENCY = "tf_intelligence_latency_ms"
CLAUDE_FAILURES = "tf_intelligence_failures_total"
RISK_REJECTIONS = "tf_risk_rejections_total"
RECONCILIATION_MISMATCHES = "tf_reconciliation_mismatches_total"
KILL_SWITCH_ENGAGED = "tf_kill_switch_engaged"


def build_registry() -> MetricsRegistry:
    registry = MetricsRegistry()
    registry.describe(EVENTS_PROCESSED, "Events published on the internal bus")
    registry.describe(MARKET_DATA_LATENCY, "Venue timestamp to local receipt, in ms")
    registry.describe(WS_RECONNECTS, "WebSocket reconnections per venue")
    registry.describe(STALE_FEEDS, "Venue/symbol pairs currently not usable")
    registry.describe(SEQUENCE_GAPS, "Order book sequence gaps detected")
    registry.describe(OPPORTUNITIES_DETECTED, "Candidate opportunities detected")
    registry.describe(OPPORTUNITIES_REJECTED, "Opportunities rejected, by stage")
    registry.describe(PAPER_ORDERS, "Paper orders created")
    registry.describe(PARTIAL_FILLS, "Fills that did not complete their order")
    registry.describe(FILL_RATIO, "Filled quantity over submitted quantity")
    registry.describe(SLIPPAGE_BPS, "Realised slippage against the expected price")
    registry.describe(FEES_PAID, "Cumulative simulated fees")
    registry.describe(GROSS_PNL, "P&L before fees")
    registry.describe(NET_PNL, "P&L after fees")
    registry.describe(DRAWDOWN, "Equity drawdown from peak")
    registry.describe(CONSENSUS, "Consensus agreement distribution")
    registry.describe(AGENT_UP, "1 when an agent is HEALTHY, else 0")
    registry.describe(CLAUDE_REQUESTS, "Intelligence-provider requests")
    registry.describe(CLAUDE_LATENCY, "Intelligence-provider latency, in ms")
    registry.describe(CLAUDE_FAILURES, "Intelligence-provider failures")
    registry.describe(RISK_REJECTIONS, "Trade intents rejected by RUNE, by gate")
    registry.describe(RECONCILIATION_MISMATCHES, "Reconciliation mismatches, by kind")
    registry.describe(KILL_SWITCH_ENGAGED, "1 when the kill switch is engaged")
    return registry
