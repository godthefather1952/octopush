"""Phase 2 finalization, P2-9: the config digest has to mean something.

The Recorder has always stored ``config_digest(settings.model_dump())``
alongside each session, and the CLI has always PRINTED it. Nothing ever
compared it. So an exact replay could run under different risk limits, fee
schedules, latency parameters, consensus weights, execution seeds, venue
symbols, paper balance or strategy thresholds -- every one of which changes
what the platform does with identical market data -- and still present itself
as a faithful reproduction.

Now:

    recorded == current              -> exact
    recorded != current              -> refused, unless declared counterfactual
    recorded missing, current given   -> refused, unless declared counterfactual
    no current supplied               -> replays, but never claims exactness

The digest proves equality; it cannot reconstruct settings. That boundary is
deliberate and documented (see ``docs/phase2-validation.md``): exact replay
requires the caller to supply the same material configuration, and this check
verifies that they did rather than rebuilding it for them.
"""

from __future__ import annotations

import pytest

from core.bus import InMemoryEventBus
from core.clock import ManualClock
from core.config import load_settings
from core.events import Event, EventType
from replay.engine import (
    COUNTERFACTUAL_CONFIGURATION,
    UNVERIFIED_CONFIGURATION,
    FidelityDimension,
    ReplayConfigMismatch,
    ReplayConfigVerificationRequired,
    ReplaySession,
    config_digest,
)
from storage import InMemoryEventStore
from storage.base import SessionStatus

START_MS = 1_788_000_000_000
SESSION_ID = "s1"
RECORDED_HASH = "cfg-recorded"


def _input(seq: int, ts_ms: int) -> Event:
    return Event(
        type=EventType.BOOK_SNAPSHOT,
        ts_ms=ts_ms,
        sequence=seq,
        source="VENUE_A",
        schema_name="Test",
        payload={},
    )


def _marker(seq: int, ts_ms: int, tick: int, watermark: int) -> Event:
    return Event(
        type=EventType.ORCHESTRATOR_TICK,
        ts_ms=ts_ms,
        sequence=seq,
        source="ORCHESTRATOR",
        schema_name="OrchestratorTick",
        payload={
            "tick": tick,
            "warmed_up": True,
            "processed_input_sequence": watermark,
        },
    )


EVENTS = [
    _input(1, START_MS + 100),
    _marker(2, START_MS + 100, 1, 1),
]


async def _store(config_hash: str = RECORDED_HASH) -> InMemoryEventStore:
    store = InMemoryEventStore()
    await store.open()
    await store.start_session(SESSION_ID, START_MS, config_hash=config_hash)
    await store.append_many(SESSION_ID, EVENTS)
    await store.finalize_session(
        SESSION_ID, START_MS + 999, status=SessionStatus.COMPLETE
    )
    return store


def _session(store, **kwargs) -> ReplaySession:
    return ReplaySession(
        store=store,
        bus=InMemoryEventBus(raise_on_handler_error=True),
        clock=ManualClock(START_MS),
        session_id=SESSION_ID,
        **kwargs,
    )


class TestTheFourCases:
    async def test_a_matching_digest_is_verified(self):
        store = await _store()
        async with _session(store, current_config_hash=RECORDED_HASH) as session:
            await session.run()
            assert session.stats.fidelity.verified(FidelityDimension.CONFIGURATION)
            assert session.stats.is_exact

    async def test_a_differing_digest_is_refused(self):
        store = await _store()
        with pytest.raises(ReplayConfigMismatch) as excinfo:
            await _session(store, current_config_hash="cfg-other").open()
        message = str(excinfo.value)
        assert RECORDED_HASH in message, "the error names the recorded digest"
        assert "cfg-other" in message, "and the current one"

    async def test_a_session_with_no_recorded_digest_is_refused(self):
        """Nothing to compare against is not evidence of a match."""
        store = await _store(config_hash="")
        with pytest.raises(ReplayConfigVerificationRequired):
            await _session(store, current_config_hash="cfg-other").open()

    async def test_supplying_no_current_digest_replays_but_never_claims_exact(self):
        """The caller never asked to verify, so nothing is refused -- and
        nothing is claimed either."""
        store = await _store()
        async with _session(store) as session:
            await session.run()
        fidelity = session.stats.fidelity
        assert fidelity.issues[FidelityDimension.CONFIGURATION] == (
            UNVERIFIED_CONFIGURATION
        )
        assert not session.stats.is_exact


class TestTheCounterfactualOverride:
    async def test_a_mismatch_replays_as_a_declared_counterfactual(self):
        store = await _store()
        async with _session(
            store, current_config_hash="cfg-other", allow_config_mismatch=True
        ) as session:
            stats = await session.run()
        assert stats.fidelity.issues[FidelityDimension.CONFIGURATION] == (
            COUNTERFACTUAL_CONFIGURATION
        )
        assert not stats.is_exact, (
            "a counterfactual answers a different question and must never "
            "report itself as a reproduction"
        )
        assert stats.ticks_read == 1, "...but it does actually replay"

    async def test_a_missing_recorded_digest_is_unverified_not_counterfactual(self):
        """The two are different: one is 'settings changed', the other is
        'nobody can say whether they changed'."""
        store = await _store(config_hash="")
        async with _session(
            store, current_config_hash="cfg-other", allow_config_mismatch=True
        ) as session:
            await session.run()
        assert session.stats.fidelity.issues[FidelityDimension.CONFIGURATION] == (
            UNVERIFIED_CONFIGURATION
        )


class TestMaterialSettingsChangeTheDigest:
    """Every one of these changes what the platform DOES with identical
    market data, so every one of them must be visible to the check."""

    @pytest.mark.parametrize(
        ("path", "value"),
        [
            (("risk", "max_daily_loss"), 1_234.0),
            (("risk", "max_data_age_ms"), 999),
            (("consensus", "entry_threshold"), 0.75),
            (("consensus", "exit_threshold"), 0.10),
            (("execution", "seed"), 12345),
            (("execution", "maker_fill_probability"), 0.9),
            (("execution", "latency_drift_bps_per_100ms"), 3.0),
            (("lumen", "provider"), "scripted"),
            (("paper_initial_balance",), 55_000.0),
            (("min_dislocation_bps",), 12.5),
        ],
    )
    def test_changing_it_changes_the_digest(self, path, value):
        settings = load_settings()
        baseline = config_digest(settings.model_dump())

        if len(path) == 1:
            changed = settings.model_copy(update={path[0]: value})
        else:
            section, field = path
            sub = getattr(settings, section).model_copy(update={field: value})
            changed = settings.model_copy(update={section: sub})

        assert config_digest(changed.model_dump()) != baseline, (
            f"{'.'.join(path)} changes replayed economics but not the digest, "
            "so a replay under a different value would look exact"
        )

    def test_changing_a_venue_fee_changes_the_digest(self):
        settings = load_settings()
        baseline = config_digest(settings.model_dump())
        venues = list(settings.venues)
        assert venues, "the default settings define venues to change"
        fees = venues[0].fees.model_copy(update={"taker_bps": 42.0})
        venues[0] = venues[0].model_copy(update={"fees": fees})
        changed = settings.model_copy(update={"venues": venues})
        assert config_digest(changed.model_dump()) != baseline, (
            "a venue fee is paid on every fill, so a replay under a different "
            "one produces different P&L from identical market data"
        )

    def test_changing_a_venue_latency_changes_the_digest(self):
        settings = load_settings()
        baseline = config_digest(settings.model_dump())
        venues = list(settings.venues)
        venues[0] = venues[0].model_copy(update={"latency_ms": 500})
        changed = settings.model_copy(update={"venues": venues})
        assert config_digest(changed.model_dump()) != baseline

    def test_identical_settings_produce_an_identical_digest(self):
        assert config_digest(load_settings().model_dump()) == config_digest(
            load_settings().model_dump()
        )


class TestInfrastructureIsExcludedAndProvenHarmless:
    """The exclusions are only defensible if they are actually inert.

    A digest over the WHOLE settings object makes "I lowered the log level"
    or "the database is at a different path" count as a material
    configuration change. The gate would then fire on every replay of a
    session recorded on another machine, the override would become habitual,
    and a check that is always overridden protects nothing.

    So each excluded path is proved inert, not merely asserted to be.
    """

    @pytest.mark.parametrize(
        ("path", "value"),
        [
            (("environment",), "somewhere-else"),
            (("log_level",), "ERROR"),
            (("log_format",), "text"),
            (("api_host",), "127.0.0.1"),
            (("api_port",), 9999),
            (("redis_url",), "redis://elsewhere:6380/3"),
        ],
    )
    def test_an_excluded_setting_does_not_change_the_digest(self, path, value):
        settings = load_settings()
        baseline = config_digest(settings.model_dump())
        changed = settings.model_copy(update={path[0]: value})
        assert config_digest(changed.model_dump()) == baseline

    def test_the_storage_location_is_excluded(self):
        """Where events are stored cannot change what they contain."""
        settings = load_settings()
        baseline = config_digest(settings.model_dump())
        storage = settings.storage.model_copy(
            update={"sqlite_path": "/somewhere/else.db"}
        )
        changed = settings.model_copy(update={"storage": storage})
        assert config_digest(changed.model_dump()) == baseline

    def test_the_storage_backend_choice_is_still_material(self):
        """The exclusion is narrow on purpose: only the location and the
        credential, never the choice of store itself."""
        settings = load_settings()
        baseline = config_digest(settings.model_dump())
        storage = settings.storage.model_copy(update={"backend": "postgres"})
        changed = settings.model_copy(update={"storage": storage})
        assert config_digest(changed.model_dump()) != baseline

    async def test_replaying_under_changed_infrastructure_is_economically_identical(
        self,
    ):
        """The proof behind the exclusions, run rather than argued.

        The same recording is replayed twice -- once under the settings it was
        recorded with, once with every excluded path changed -- and every
        economic output is compared.
        """
        from apps.orchestrator.wiring import build_platform
        from core.config import simulated_venues
        from simulation.market import default_market
        from tests.conftest import START_MS as PLATFORM_START
        from tests.replay.test_replay import replay_equivalence_summary

        base = load_settings().model_copy(update={"venues": simulated_venues()})
        recorded_store = InMemoryEventStore()
        original = build_platform(
            base,
            clock=ManualClock(PLATFORM_START),
            bus=InMemoryEventBus(raise_on_handler_error=True),
            store=recorded_store,
            market=default_market(start_ms=PLATFORM_START),
            raise_on_handler_error=True,
        )
        await original.start(record=True)
        for _ in range(25):
            original.clock.advance(100)
            await original.step_market(1)
            await original.orchestrator.tick()
        await original.bus.drain()
        recorded_id = original.session_id
        await original.stop()

        changed_storage = base.storage.model_copy(
            update={"sqlite_path": "/somewhere/else.db"}
        )
        variants = {
            "unchanged": base,
            "infrastructure changed": base.model_copy(
                update={
                    "environment": "somewhere-else",
                    "log_level": "ERROR",
                    "log_format": "text",
                    "api_host": "127.0.0.1",
                    "api_port": 9999,
                    "redis_url": "redis://elsewhere:6380/3",
                    "storage": changed_storage,
                }
            ),
        }

        results = {}
        for name, variant in variants.items():
            clock = ManualClock(PLATFORM_START)
            bus = InMemoryEventBus(raise_on_handler_error=True)
            replayed = build_platform(
                variant,
                clock=clock,
                bus=bus,
                store=InMemoryEventStore(),
                market=default_market(start_ms=PLATFORM_START),
                raise_on_handler_error=True,
            )
            await replayed.start(record=False, feeds=False)
            session = ReplaySession(
                store=recorded_store,
                bus=bus,
                clock=clock,
                session_id=recorded_id,
                current_config_hash=config_digest(variant.model_dump()),
            )
            async with session:
                while (event := await session.step()) is not None:
                    if event.type is EventType.ORCHESTRATOR_TICK:
                        await replayed.orchestrator.tick()
                assert session.stats.is_exact, (
                    f"the {name} variant must still be an EXACT replay -- "
                    "that is what excluding these paths asserts"
                )
            results[name] = replay_equivalence_summary(replayed)
            await replayed.stop()

        assert results["unchanged"] == results["infrastructure changed"]


class TestSecretSafety:
    """The digest must never reveal a credential, and must still be able to
    tell two different ones apart wherever a secret is material."""

    def test_a_secret_is_hashed_rather_than_masked(self):
        """``model_dump`` renders every SecretStr as identical asterisks, so a
        digest taken from it could not distinguish two credentials at all
        (P0-L2). ``_unmask`` hashes the real value instead."""
        from pydantic import SecretStr

        from replay.engine import _unmask

        one = _unmask({"credential": SecretStr("first")})
        two = _unmask({"credential": SecretStr("second")})
        assert one != two
        assert one["credential"].startswith("secret:")
        assert "first" not in str(one)

    def test_the_digest_of_a_material_secret_never_contains_it(self):
        from pydantic import SecretStr

        secret = "hunter2-not-in-any-output"
        digest = config_digest({"some_material_credential": SecretStr(secret)})
        assert secret not in digest

    def test_the_postgres_dsn_is_excluded_and_so_cannot_leak_through_it(self):
        """The one SecretStr in Settings lives under ``storage``, which the
        digest excludes: where events are stored cannot change what a replay
        computes. It therefore reaches neither the digest nor any message
        quoting one."""
        from pydantic import SecretStr

        settings = load_settings()
        secret = "hunter2-not-in-any-output"
        storage = settings.storage.model_copy(
            update={"postgres_dsn": SecretStr(f"postgresql://u:{secret}@h/db")}
        )
        changed = settings.model_copy(update={"storage": storage})
        digest = config_digest(changed.model_dump())
        assert secret not in digest
        assert digest == config_digest(settings.model_dump())

    async def test_a_mismatch_error_quotes_digests_not_settings(self):
        """The refusal names both digests. Digests, never the values behind
        them -- so no setting, secret or otherwise, can leak through it."""
        from pydantic import SecretStr

        settings = load_settings()
        secret = "hunter2-not-in-any-output"
        storage = settings.storage.model_copy(
            update={"postgres_dsn": SecretStr(f"postgresql://u:{secret}@h/db")}
        )
        material = settings.model_copy(
            update={"paper_initial_balance": 55_000.0, "storage": storage}
        )

        store = await _store()
        with pytest.raises(ReplayConfigMismatch) as excinfo:
            await _session(
                store, current_config_hash=config_digest(material.model_dump())
            ).open()
        message = str(excinfo.value)
        assert secret not in message
        assert "55000" not in message


class TestTheCliWiresItUp:
    def test_the_counterfactual_flag_exists_and_defaults_off(self):
        from replay.__main__ import build_parser

        args = build_parser().parse_args(["--session", "s1"])
        assert args.counterfactual is False

    def test_the_flag_sets_the_attribute_replay_reads(self):
        from replay.__main__ import build_parser

        args = build_parser().parse_args(["--session", "s1", "--counterfactual"])
        assert args.counterfactual is True
