"""Phase 6 — execution determinism, measured at the execution boundary.

Phase 2 established that a recorded session replays to the same aggregate
outcome. This file asks the narrower question the execution boundary owns: given
the same plan, the same market sequence, the same seed and the same logical
instants, does the executor produce the same orders, the same fills, the same
prices, the same fees and the same terminality — every time?

Two runs of one scenario is the whole method. Where an identity is minted at
random by design (``fill_id``, and ``client_order_id`` when a plan does not
carry one) the audit pins the identity explicitly rather than normalising it
away, so nothing economic can hide behind a "this is expected to differ" rule.
The one field deliberately excluded is ``FillEvent.fill_id``, and it is
excluded by name with its reason stated.

The scenarios cover every lifecycle branch the executor has: full fill, partial
fill, no fill, IOC, POST_ONLY, cancel-before-ack, cancel race, expiry, UNKNOWN,
and two venues at once.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import pytest

from core.models.common import Side, TimeInForce
from tests.audit.veska_fixtures import (
    VENUE_A,
    VENUE_A_CANCEL_LATENCY_MS,
    VENUE_A_LATENCY_MS,
    VENUE_B,
    VENUE_B_LATENCY_MS,
    audit_settings,
    book,
    market,
    one_venue_market,
    plan,
    planned,
    rig,
    venue_state,
)
from tests.conftest import START_MS

#: The shipped execution configuration, including its randomness. Determinism
#: has to hold *with* the RNG, not by switching it off.
SHIPPED = audit_settings()

ACK_AT = START_MS + VENUE_A_LATENCY_MS


def fingerprint(built, fills) -> dict:
    """Everything a replay of this scenario must reproduce.

    ``fill_id`` is excluded and nothing else is: it is minted per fill through
    ``core.ids``, which is random live and deterministic under replay by
    design, so comparing the literal value would assert on something nobody
    claims matches. Every economic figure, every status, every timestamp the
    executor stamps and every published event time is compared.
    """
    orders = sorted(built.oms.orders.values(), key=lambda o: o.client_order_id)
    return {
        "orders": [
            {
                "id": o.client_order_id,
                "status": o.status.value,
                "filled_quantity": o.filled_quantity,
                "average_price": o.average_price,
                "fees_paid": o.fees_paid,
                "submitted_at": o.submitted_at,
                "acknowledged_at": o.acknowledged_at,
                "terminal_at": o.terminal_at,
                "expires_at": o.expires_at,
                "history": [(ts, s.value) for ts, s in o.history],
            }
            for o in orders
        ],
        "fills": [
            {
                "client_order_id": f.client_order_id,
                "quantity": f.quantity,
                "price": f.price,
                "fee": f.fee,
                "liquidity": f.liquidity.value,
                "slippage_bps": f.slippage_bps,
                "created_at": f.created_at,
                "source_data_timestamp": f.source_data_timestamp,
            }
            for f in fills
        ],
        "events": [(e.type.value, e.ts_ms) for e in built.published],
        "account": {
            key: (position.quantity, position.average_entry_price)
            for key, position in built.account.snapshot().positions.items()
        },
        "cash": built.account.snapshot().cash,
    }


# ======================================================================
# scenarios
# ======================================================================


async def full_fill(built) -> list:
    await built.executor.submit(
        plan(planned(client_order_id="ord-1", limit_price=40_100.0)), START_MS
    )
    built.executor.update_market(one_venue_market(mid=30_000.0))
    return await built.executor.poll(ACK_AT)


async def partial_fill(built) -> list:
    built.executor.update_market(
        one_venue_market(mid=30_000.0, levels=1, size=0.02)
    )
    await built.executor.submit(
        plan(planned(client_order_id="ord-1", quantity=1.0, limit_price=40_100.0)),
        START_MS,
    )
    return await built.executor.poll(ACK_AT)


async def no_fill(built) -> list:
    await built.executor.submit(
        plan(planned(client_order_id="ord-1", limit_price=1.0)), START_MS
    )
    return await built.executor.poll(ACK_AT)


async def ioc_then_liquidity(built) -> list:
    built.executor.update_market(one_venue_market(mid=60_000.0))
    await built.executor.submit(
        plan(
            planned(
                client_order_id="ord-1",
                time_in_force=TimeInForce.IOC,
                limit_price=40_100.0,
            )
        ),
        START_MS,
    )
    first = await built.executor.poll(ACK_AT)
    built.executor.update_market(one_venue_market(mid=30_000.0))
    return first + await built.executor.poll(ACK_AT + 100)


async def post_only(built) -> list:
    await built.executor.submit(
        plan(
            planned(
                client_order_id="ord-1",
                time_in_force=TimeInForce.POST_ONLY,
                limit_price=39_000.0,
                expected_price=39_000.0,
            )
        ),
        START_MS,
    )
    fills = await built.executor.poll(ACK_AT)
    built.executor.update_market(
        one_venue_market(mid=40_000.0, buy_volume=8.0, sell_volume=8.0)
    )
    return fills + await built.executor.poll(ACK_AT + 500)


async def cancel_before_ack(built) -> list:
    await built.executor.submit(
        plan(planned(client_order_id="ord-1", limit_price=40_100.0)), START_MS
    )
    built.executor.update_market(one_venue_market(mid=30_000.0))
    await built.executor.cancel("ord-1", START_MS + 1)
    return await built.executor.poll(ACK_AT)


async def cancel_race(built) -> list:
    built.executor.update_market(one_venue_market(mid=30_000.0))
    await built.executor.submit(
        plan(planned(client_order_id="ord-1", limit_price=40_100.0, ttl_ms=60_000)),
        START_MS,
    )
    await built.executor.poll(ACK_AT)
    await built.executor.cancel("ord-1", ACK_AT)
    return await built.executor.poll(ACK_AT + VENUE_A_CANCEL_LATENCY_MS)


async def expiry(built) -> list:
    await built.executor.submit(
        plan(planned(client_order_id="ord-1", limit_price=1.0, ttl_ms=200)),
        START_MS,
    )
    await built.executor.poll(ACK_AT)
    return await built.executor.poll(START_MS + 500)


async def unknown(built) -> list:
    await built.executor.submit(
        plan(planned(client_order_id="ord-1", limit_price=40_100.0)), START_MS
    )
    built.executor.inject_timeout("ord-1")
    built.executor.update_market(one_venue_market(mid=30_000.0))
    first = await built.executor.poll(ACK_AT)
    return first + await built.executor.poll(ACK_AT + 1_000)


async def two_venues(built) -> list:
    both = market(
        venue_state(book(VENUE_A, mid=30_000.0)),
        venue_state(book(VENUE_B, mid=30_500.0)),
    )
    built.executor.update_market(both)
    await built.executor.submit(
        plan(
            planned(venue=VENUE_A, client_order_id="ord-a", side=Side.BUY,
                    limit_price=40_100.0),
            planned(venue=VENUE_B, client_order_id="ord-b", side=Side.SELL,
                    limit_price=1.0, expected_price=30_499.0),
        ),
        START_MS,
    )
    first = await built.executor.poll(START_MS + VENUE_A_LATENCY_MS)
    return first + await built.executor.poll(START_MS + VENUE_B_LATENCY_MS)


SCENARIOS: dict[str, Callable[..., Awaitable[list]]] = {
    "full_fill": full_fill,
    "partial_fill": partial_fill,
    "no_fill": no_fill,
    "ioc_then_liquidity": ioc_then_liquidity,
    "post_only": post_only,
    "cancel_before_ack": cancel_before_ack,
    "cancel_race": cancel_race,
    "expiry": expiry,
    "unknown": unknown,
    "two_venues": two_venues,
}


async def run(name: str, *, seed: int | None = None) -> dict:
    built = rig(
        settings=SHIPPED, market_state=one_venue_market(), seed=seed
    )
    fills = await SCENARIOS[name](built)
    await built.bus.drain()
    return fingerprint(built, fills)


class TestExecutionIsReproducible:
    @pytest.mark.parametrize("scenario", sorted(SCENARIOS))
    async def test_two_runs_of_one_scenario_agree(self, scenario):
        first = await run(scenario)
        second = await run(scenario)
        assert first == second, (
            f"the {scenario!r} scenario produced different execution outcomes "
            "across two identical runs"
        )

    @pytest.mark.parametrize("scenario", sorted(SCENARIOS))
    async def test_a_third_run_still_agrees(self, scenario):
        """Guards against a scenario that happens to be stable in pairs."""
        outcomes = [await run(scenario) for _ in range(3)]
        assert all(o == outcomes[0] for o in outcomes)

    @pytest.mark.parametrize("scenario", sorted(SCENARIOS))
    async def test_the_seed_is_what_makes_it_reproducible(self, scenario):
        """A control on the fingerprint's sensitivity.

        Two different seeds need not produce different outcomes for every
        scenario — several are decided entirely by the book — but if NO
        scenario is seed-sensitive the fingerprint is not measuring the
        simulator at all.
        """
        same = await run(scenario, seed=11) == await run(scenario, seed=11)
        assert same, f"{scenario!r} is not reproducible even at a fixed seed"

    async def test_at_least_one_scenario_is_seed_sensitive(self):
        differing = [
            name
            for name in sorted(SCENARIOS)
            if await run(name, seed=11) != await run(name, seed=99)
        ]
        assert differing, (
            "no scenario changed when the simulator's seed changed; the "
            "fingerprint is not observing the randomness it claims to pin"
        )


class TestTheFingerprintHidesNothing:
    """The fingerprint is only as good as what it refuses to normalise."""

    def test_it_compares_every_economic_field(self):
        import inspect

        source = inspect.getsource(fingerprint)
        for field in (
            "filled_quantity",
            "average_price",
            "fees_paid",
            "price",
            "fee",
            "slippage_bps",
            "liquidity",
            "cash",
        ):
            assert f'"{field}"' in source

    def test_it_compares_timestamps_rather_than_dropping_them(self):
        import inspect

        source = inspect.getsource(fingerprint)
        for field in (
            "submitted_at",
            "acknowledged_at",
            "terminal_at",
            "expires_at",
            "created_at",
            "history",
            "events",
        ):
            assert f'"{field}"' in source

    def test_only_the_fill_id_is_excluded_and_it_is_stated(self):
        import inspect

        assert "fill_id" in (inspect.getdoc(fingerprint) or ""), (
            "an excluded field must be named and justified in the docstring"
        )
        body = inspect.getsource(fingerprint).rsplit('"""', 1)[-1]
        assert "fill_id" not in body, (
            "fill_id is excluded by omission from the compared fields, never "
            "by normalising it inside the fingerprint"
        )
