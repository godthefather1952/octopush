"""Serialisation must produce strict RFC 8259 JSON — the regression for P0-C3.

The audit found `ser_json_inf_nan="constants"` emitting bare `Infinity` into
event payloads, reachable whenever a book side is exhausted. SQLite stored it
as text and round-tripped it; PostgreSQL JSONB rejects it. The platform worked
on the default backend and would fail on the production one.

Every test here drives the *production* serialisation path — `to_json_dict()`
then the store — not a hand-rolled dump.
"""

from __future__ import annotations

import json
import math

import pytest

from agents.zephr.liquidity import build_sizing_curve, quote_leg
from core.config import FeeSchedule, ZephrConfig
from core.events import Event, EventType
from core.models.agent import AgentOpinion
from core.models.common import AgentId, DataQuality, Side, sanitize_json
from core.models.market import OrderBookSnapshot, PriceLevel
from core.models.ops import SystemEvent
from execution.costs import market_impact_bps, walk_book
from storage.sqlite_store import SQLiteEventStore
from tests.conftest import START_MS, make_book, venue_state_from_book


def strict_loads(blob: str):
    """Parse rejecting the non-standard constants PostgreSQL also rejects."""

    def reject(constant: str):
        raise ValueError(f"non-standard JSON constant: {constant}")

    return json.loads(blob, parse_constant=reject)


def assert_strict_json(payload: dict) -> None:
    blob = json.dumps(payload, allow_nan=False)
    strict_loads(blob)


class TestSanitizer:
    @pytest.mark.parametrize(
        "value", [float("inf"), float("-inf"), float("nan")]
    )
    def test_non_finite_scalars_become_null(self, value):
        assert sanitize_json(value) is None

    def test_finite_values_are_untouched(self):
        assert sanitize_json(1.5) == 1.5
        assert sanitize_json(0.0) == 0.0
        assert sanitize_json(-2) == -2

    def test_nested_structures_are_sanitised(self):
        raw = {
            "a": float("inf"),
            "b": [1.0, float("nan"), {"c": float("-inf")}],
            "d": {"e": {"f": float("inf")}},
        }
        clean = sanitize_json(raw)
        assert clean == {"a": None, "b": [1.0, None, {"c": None}], "d": {"e": {"f": None}}}
        assert_strict_json(clean)

    def test_non_float_types_survive(self):
        raw = {"s": "text", "i": 7, "b": True, "n": None}
        assert sanitize_json(raw) == raw


class TestModelSerialisation:
    @pytest.mark.parametrize(
        "value,label",
        [
            (float("inf"), "positive infinity"),
            (float("-inf"), "negative infinity"),
            (float("nan"), "NaN"),
        ],
    )
    def test_agent_opinion_detail_never_emits_non_finite(self, value, label):
        opinion = AgentOpinion(
            agent_id=AgentId.ZEPHR,
            symbol="BTC-USD",
            created_at=START_MS,
            signal=0.0,
            confidence=0.5,
            expires_at=START_MS + 1000,
            model_version="t",
            detail={"impact_bps": value},
        )
        payload = opinion.to_json_dict()
        assert payload["detail"]["impact_bps"] is None, label
        assert_strict_json(payload)

    def test_system_event_detail_is_sanitised(self):
        event = SystemEvent(
            created_at=START_MS,
            kind="X",
            component="TEST",
            message="m",
            detail={"ratio": float("inf")},
        )
        assert_strict_json(event.to_json_dict())

    def test_bus_event_payload_is_strict_json(self):
        opinion = AgentOpinion(
            agent_id=AgentId.ZEPHR,
            symbol="BTC-USD",
            created_at=START_MS,
            signal=0.0,
            confidence=0.5,
            expires_at=START_MS + 1,
            model_version="t",
            detail={"x": float("nan")},
        )
        event = Event(
            type=EventType.AGENT_OPINION,
            ts_ms=START_MS,
            source="ZEPHR",
            payload=opinion.to_json_dict(),
        )
        assert_strict_json(event.model_dump(mode="json"))


class TestReachableNonFinitePaths:
    """The calculations that actually produce non-finite values."""

    def test_empty_book_produces_infinite_impact(self):
        """Internally infinite is correct — it must simply not be serialised."""
        assert market_impact_bps(1_000, 0.0, ZephrConfig()) == float("inf")

    def test_empty_ask_book_walk_is_exhausted_not_nan(self):
        result = walk_book([], 1_000.0, Side.BUY)
        assert result.exhausted
        assert math.isfinite(result.average_price)
        assert math.isfinite(result.slippage_bps)

    def test_empty_bid_book_walk_is_exhausted_not_nan(self):
        result = walk_book([], 1_000.0, Side.SELL)
        assert result.exhausted
        assert math.isfinite(result.slippage_bps)

    def test_zero_usable_liquidity_leg_serialises_safely(self):
        state = venue_state_from_book(
            make_book("V", "BTC-USD", 100.0, levels=4, tick=0.01),
            quality=DataQuality.FRESH,
        )
        leg = quote_leg(state, Side.BUY, 1_000.0, [], FeeSchedule(), ZephrConfig())
        assert not math.isfinite(leg.impact_bps)
        opinion = AgentOpinion(
            agent_id=AgentId.ZEPHR,
            symbol="BTC-USD",
            created_at=START_MS,
            signal=-1.0,
            confidence=0.9,
            expires_at=START_MS + 1,
            model_version="t",
            detail={"impact_bps": leg.impact_bps, "cost_bps": leg.total_cost_bps},
        )
        payload = opinion.to_json_dict()
        assert payload["detail"]["impact_bps"] is None
        assert_strict_json(payload)

    def test_sizing_curve_with_an_exhausted_book_is_serialisable(self):
        state = venue_state_from_book(make_book("V", "BTC-USD", 100.0, levels=3, tick=0.01))
        curve = build_sizing_curve(
            "BTC-USD",
            40.0,
            [(state, Side.BUY, [], FeeSchedule())],
            ZephrConfig(),
        )
        detail = {
            f"net_edge_bps@{p.notional:.0f}": p.net_edge_bps for p in curve.points
        }
        opinion = AgentOpinion(
            agent_id=AgentId.ZEPHR,
            symbol="BTC-USD",
            created_at=START_MS,
            signal=-1.0,
            confidence=0.9,
            expires_at=START_MS + 1,
            model_version="t",
            detail=detail,
        )
        assert_strict_json(opinion.to_json_dict())

    def test_a_book_with_a_single_zero_size_level_does_not_produce_nan(self):
        book = OrderBookSnapshot(
            venue="V",
            symbol="BTC-USD",
            exchange_ts=START_MS,
            received_ts=START_MS,
            bids=[PriceLevel(price=100.0, size=0.0)],
            asks=[PriceLevel(price=101.0, size=0.0)],
        )
        result = walk_book(book.asks, 500.0, Side.BUY)
        assert math.isfinite(result.slippage_bps)


class TestStoreRejectsNonStandardJson:
    """The store is the last line of defence, and it must fail loudly."""

    async def test_sanitised_payloads_persist_and_round_trip(self, tmp_path):
        store = SQLiteEventStore(str(tmp_path / "safe.db"))
        await store.open()
        await store.start_session("s", START_MS)
        opinion = AgentOpinion(
            agent_id=AgentId.ZEPHR,
            symbol="BTC-USD",
            created_at=START_MS,
            signal=0.0,
            confidence=0.5,
            expires_at=START_MS + 1,
            model_version="t",
            detail={"impact_bps": float("inf")},
        )
        await store.append(
            "s",
            Event(
                type=EventType.AGENT_OPINION,
                ts_ms=START_MS,
                source="ZEPHR",
                sequence=1,
                payload=opinion.to_json_dict(),
            ),
        )
        events = [e async for e in store.read("s")]
        assert events[0].payload["detail"]["impact_bps"] is None
        await store.close()

    async def test_an_unsanitised_payload_is_refused_not_silently_written(self, tmp_path):
        """If anything ever bypasses to_json_dict(), the write must fail here
        rather than diverging between SQLite and PostgreSQL."""
        store = SQLiteEventStore(str(tmp_path / "strict.db"))
        await store.open()
        await store.start_session("s", START_MS)
        rogue = Event(
            type=EventType.SYSTEM_EVENT,
            ts_ms=START_MS,
            source="X",
            sequence=1,
            payload={"raw": float("inf")},  # deliberately bypasses sanitisation
        )
        with pytest.raises(ValueError):
            await store.append("s", rogue)
        await store.close()

    async def test_every_recorded_event_in_a_session_is_strict_json(
        self, platform
    ):
        """End-to-end: drive the platform and check the whole recorded stream."""
        from tests.conftest import run_platform

        await run_platform(platform, 300)
        await platform.recorder.flush()
        events = [e async for e in platform.store.read(platform.session_id)]
        assert events
        for event in events:
            assert_strict_json(event.payload)
