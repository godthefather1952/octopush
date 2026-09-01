"""Microstructure metrics derived from a local book and recent trade flow."""

from __future__ import annotations

import itertools
import math
from collections import deque
from dataclasses import dataclass, field

from agents.tidal.book import LocalOrderBook
from core.models.common import Millis, Side
from core.models.market import BookMetrics, safe_bps

#: Distances from mid, in bps, at which resting depth is measured.
DEPTH_BUCKETS_BPS = (1.0, 5.0, 10.0, 25.0)


@dataclass
class TradeFlowWindow:
    """Rolling window of trade prints, used for flow imbalance."""

    window_ms: int = 10_000
    _events: deque[tuple[Millis, Side, float]] = field(default_factory=deque)

    def add(self, ts: Millis, aggressor: Side, notional: float) -> None:
        self._events.append((ts, aggressor, notional))

    def _evict(self, now_ms: Millis) -> None:
        cutoff = now_ms - self.window_ms
        while self._events and self._events[0][0] < cutoff:
            self._events.popleft()

    def volumes(self, now_ms: Millis) -> tuple[float, float]:
        self._evict(now_ms)
        buy = sum(n for _, side, n in self._events if side is Side.BUY)
        sell = sum(n for _, side, n in self._events if side is Side.SELL)
        return buy, sell


@dataclass
class MidWindow:
    """Rolling mid history for short-horizon realised volatility."""

    window_ms: int = 30_000
    _points: deque[tuple[Millis, float]] = field(default_factory=deque)

    def add(self, ts: Millis, mid: float) -> None:
        if self._points and self._points[-1][0] == ts:
            self._points[-1] = (ts, mid)
            return
        self._points.append((ts, mid))

    def volatility_bps(self, now_ms: Millis) -> float:
        """Standard deviation of log returns over the window, in bps."""
        cutoff = now_ms - self.window_ms
        while self._points and self._points[0][0] < cutoff:
            self._points.popleft()
        if len(self._points) < 3:
            return 0.0
        returns = [
            math.log(b / a)
            for (_, a), (_, b) in itertools.pairwise(self._points)
            if a > 0 and b > 0
        ]
        if len(returns) < 2:
            return 0.0
        mean = sum(returns) / len(returns)
        variance = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
        return math.sqrt(variance) * 10_000


def microprice(book: LocalOrderBook) -> float | None:
    """Size-weighted price at the touch.

    ``(bid*ask_size + ask*bid_size) / (bid_size + ask_size)`` — leans towards
    the side with less size, which is the side more likely to be consumed.
    """
    bid, ask = book.best_bid, book.best_ask
    if bid is None or ask is None:
        return None
    bid_size = book.bids[bid]
    ask_size = book.asks[ask]
    total = bid_size + ask_size
    if total <= 0:
        return None
    return (bid * ask_size + ask * bid_size) / total


def depth_within_bps(book: LocalOrderBook, side: Side, reference: float, bps: float) -> float:
    """Resting notional within ``bps`` of ``reference`` on one side."""
    if reference <= 0:
        return 0.0
    limit = (
        reference * (1 - bps / 10_000) if side is Side.BUY else reference * (1 + bps / 10_000)
    )
    total = 0.0
    for level in book.levels(side):
        if side is Side.BUY and level.price < limit:
            break
        if side is Side.SELL and level.price > limit:
            break
        total += level.price * level.size
    return total


def compute_metrics(
    book: LocalOrderBook,
    now_ms: Millis,
    flow: TradeFlowWindow | None = None,
    mids: MidWindow | None = None,
) -> BookMetrics:
    """Full metric set for one venue/symbol."""
    bid, ask = book.best_bid, book.best_ask
    metrics = BookMetrics(best_bid=bid, best_ask=ask)
    if bid is None or ask is None or book.crossed:
        if flow is not None:
            metrics.buy_volume, metrics.sell_volume = flow.volumes(now_ms)
        return metrics

    mid = (bid + ask) / 2
    metrics.mid = mid
    metrics.microprice = microprice(book)
    metrics.spread = ask - bid
    metrics.spread_bps = safe_bps(ask - bid, mid)
    metrics.bid_depth_notional = sum(level.price * level.size for level in book.levels(Side.BUY))
    metrics.ask_depth_notional = sum(level.price * level.size for level in book.levels(Side.SELL))
    for bucket in DEPTH_BUCKETS_BPS:
        key = f"{bucket:g}"
        metrics.bid_depth_by_bps[key] = depth_within_bps(book, Side.BUY, mid, bucket)
        metrics.ask_depth_by_bps[key] = depth_within_bps(book, Side.SELL, mid, bucket)

    total_depth = metrics.bid_depth_notional + metrics.ask_depth_notional
    if total_depth > 0:
        metrics.imbalance = (
            metrics.bid_depth_notional - metrics.ask_depth_notional
        ) / total_depth

    if flow is not None:
        metrics.buy_volume, metrics.sell_volume = flow.volumes(now_ms)
    if mids is not None:
        mids.add(now_ms, mid)
        metrics.short_vol_bps = mids.volatility_bps(now_ms)
    return metrics
