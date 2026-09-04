"""Phase 2 Batch 2: the recorder tells the truth about what it kept.

Three defects met here:

**P2-7** — a failed flush destroyed the batch and called it ``events_lost``.
A database that was briefly unreachable therefore lost history permanently.

**P2-12** — the recorder buffered the caller's mutable ``Event`` object, so a
later bus subscriber could rewrite history that had already been accepted.

**Clean vs incomplete stop** — ``stop()`` ended the session whether or not
the final flush worked, producing rows that looked finished and were not.

Retaining failed batches introduces the opposite risk (unbounded memory
during an outage), so the bound and its fail-closed behaviour are tested
here too.
"""

from __future__ import annotations

import pytest

from core.bus import InMemoryEventBus
from core.clock import ManualClock
from core.events import Event, EventType
from storage import InMemoryEventStore, Recorder, RecorderAtCapacity
from storage.base import SessionStatus

START_MS = 1_700_000_000_000


def make_event(i: int = 0, **overrides) -> Event:
    fields: dict = {
        "type": EventType.SYSTEM_EVENT,
        "ts_ms": START_MS + i,
        "source": "TEST",
        "schema_name": "Probe",
        "payload": {"i": i},
        "sequence": i,
    }
    fields.update(overrides)
    return Event(**fields)


class FlakyStore(InMemoryEventStore):
    """A real store whose writes can be made to fail on demand."""

    def __init__(self) -> None:
        super().__init__()
        self.fail = False
        self.attempts = 0
        #: Set to commit the batch and THEN raise, simulating the case where
        #: the database succeeded but the caller never heard so.
        self.commit_then_fail = False

    async def append_many(self, session_id, events):
        self.attempts += 1
        if self.commit_then_fail:
            await super().append_many(session_id, events)
            raise RuntimeError("connection dropped after commit")
        if self.fail:
            raise RuntimeError("storage unavailable")
        await super().append_many(session_id, events)


@pytest.fixture
async def setup():
    clock = ManualClock(start_ms=START_MS)
    store = FlakyStore()
    rec = Recorder(store=store, clock=clock, buffer_size=100, flush_interval_ms=1_000)
    await rec.start()
    return rec, store, clock


# ==========================================================================
# Section 7 / 18 — a transient failure is not loss
# ==========================================================================


class TestTransientFailureIsNotLoss:
    async def test_a_failed_flush_retains_every_event(self, setup):
        rec, store, _ = setup
        store.fail = True
        for i in range(100):
            await rec.record(make_event(i))

        assert rec.unpersisted == 100, "the batch must still be held"
        assert rec.events_lost == 0, "a retryable failure is not loss"
        assert rec.failures == 1
        assert rec.consecutive_failures == 1
        assert rec.healthy is False
        assert rec.events_recorded == 0

    async def test_a_retry_after_recovery_records_everything(self, setup):
        rec, store, clock = setup
        store.fail = True
        for i in range(100):
            await rec.record(make_event(i))
        assert rec.unpersisted == 100

        store.fail = False
        clock.advance(2_000)
        await rec.flush_if_due()

        assert rec.events_recorded == 100
        assert rec.unpersisted == 0
        assert rec.events_lost == 0
        assert rec.failures == 1, "the failure really happened and is remembered"
        assert rec.consecutive_failures == 0
        assert rec.healthy is True
        assert await store.count(rec.session_id) == 100

    async def test_a_sustained_outage_keeps_accumulating_without_loss(self, setup):
        rec, store, clock = setup
        store.fail = True
        for round_ in range(5):
            for i in range(100):
                await rec.record(make_event(round_ * 100 + i))
            clock.advance(2_000)
            await rec.flush_if_due()

        assert rec.unpersisted == 500
        assert rec.events_lost == 0
        assert rec.consecutive_failures >= 5
        assert rec.healthy is False

        store.fail = False
        clock.advance(2_000)
        await rec.flush_if_due()
        assert rec.events_recorded == 500
        assert rec.unpersisted == 0
        assert await store.count(rec.session_id) == 500

    async def test_an_unknown_commit_outcome_does_not_duplicate(self, setup):
        """Section 8: storage committed, the caller heard an error.

        The recorder retries the identical batch, and the store's
        idempotent-retry rule converges on exactly one copy of each event.
        """
        rec, store, clock = setup
        store.commit_then_fail = True
        for i in range(50):
            await rec.record(make_event(i))
        clock.advance(2_000)
        await rec.flush_if_due()

        assert rec.unpersisted == 50, "the recorder believes the write failed"
        assert await store.count(rec.session_id) == 50, "but it actually landed"

        store.commit_then_fail = False
        clock.advance(2_000)
        await rec.flush_if_due()

        assert await store.count(rec.session_id) == 50, "no duplicates"
        assert rec.unpersisted == 0
        assert rec.events_lost == 0


# ==========================================================================
# Section 9 — the hard bound on pending memory
# ==========================================================================


class TestPendingIsBounded:
    async def test_the_bound_cannot_be_exceeded_during_an_outage(self):
        clock = ManualClock(start_ms=START_MS)
        store = FlakyStore()
        rec = Recorder(
            store=store,
            clock=clock,
            buffer_size=10,
            flush_interval_ms=1_000,
            max_pending_events=50,
        )
        await rec.start()
        store.fail = True

        accepted = 0
        refused = 0
        for i in range(500):
            try:
                await rec.record(make_event(i))
                accepted += 1
            except RecorderAtCapacity:
                refused += 1

        assert rec.unpersisted <= 50, "the hard bound must hold"
        assert rec.unpersisted == 50
        assert refused == 450
        assert rec.rejected_at_capacity == 450
        assert rec.healthy is False
        assert rec.events_lost == 0, "refusing new history is not losing old history"

    async def test_no_accepted_history_is_discarded_to_make_room(self):
        clock = ManualClock(start_ms=START_MS)
        store = FlakyStore()
        rec = Recorder(
            store=store, clock=clock, buffer_size=10, max_pending_events=20
        )
        await rec.start()
        store.fail = True
        for i in range(20):
            await rec.record(make_event(i))
        with pytest.raises(RecorderAtCapacity):
            await rec.record(make_event(999))

        store.fail = False
        clock.advance(2_000)
        await rec.flush_if_due()

        stored = [e.payload["i"] async for e in store.read(rec.session_id)]
        assert sorted(stored) == list(range(20)), (
            "the events accepted BEFORE the bound was hit must all survive"
        )

    async def test_capacity_refusal_reaches_the_publisher(self):
        """Fail closed through the bus: the event is never admitted."""
        clock = ManualClock(start_ms=START_MS)
        store = FlakyStore()
        bus = InMemoryEventBus(raise_on_handler_error=True)
        rec = Recorder(store=store, clock=clock, buffer_size=5, max_pending_events=5)
        await rec.start()
        rec.attach(bus)

        delivered: list[Event] = []
        bus.subscribe(lambda e: delivered.append(e) or _noop(), name="probe")

        store.fail = True
        for i in range(5):
            await bus.publish(make_event(i))
        await bus.drain()

        with pytest.raises(RecorderAtCapacity):
            await bus.publish(make_event(99))
        await bus.drain()

        assert all(e.payload["i"] != 99 for e in delivered), (
            "a publish the recorder refused must never be dispatched -- "
            "otherwise the platform acts on history it cannot record"
        )


# ==========================================================================
# Sections 10/11 — clean stop vs incomplete stop
# ==========================================================================


class TestFinalization:
    async def test_a_clean_recording_finalizes_complete(self, setup):
        rec, store, _ = setup
        for i in range(10):
            await rec.record(make_event(i))
        await rec.stop()

        info = await store.session(rec.session_id)
        assert info.status is SessionStatus.COMPLETE
        assert info.events_lost == 0
        assert info.failure_reason == ""
        assert await store.count(rec.session_id) == 10

    async def test_a_transient_failure_that_recovers_still_finalizes_complete(
        self, setup
    ):
        rec, store, clock = setup
        store.fail = True
        for i in range(10):
            await rec.record(make_event(i))
        clock.advance(2_000)
        await rec.flush_if_due()
        assert rec.unpersisted == 10

        store.fail = False
        await rec.stop()

        info = await store.session(rec.session_id)
        assert info.status is SessionStatus.COMPLETE, (
            "a session that recovered is complete; the failure was transient"
        )
        assert info.events_lost == 0
        assert await store.count(rec.session_id) == 10

    async def test_a_persistent_failure_finalizes_incomplete(self, setup):
        rec, store, _ = setup
        store.fail = True
        for i in range(10):
            await rec.record(make_event(i))

        # Let finalisation itself through, so the INCOMPLETE claim can land.
        async def only_appends_fail(session_id, events):
            raise RuntimeError("storage unavailable")

        store.append_many = only_appends_fail
        await rec.stop()

        info = await store.session(rec.session_id)
        assert info.status is SessionStatus.INCOMPLETE
        assert info.events_lost == 10
        assert "never durably confirmed" in info.failure_reason
        assert not info.is_verified_complete

    async def test_a_failed_finalization_leaves_the_session_open(self, setup):
        """Case (E): everything may be stored, but we could not say so.

        OPEN is honestly "unknown". COMPLETE would be a claim nothing backs.
        """
        rec, store, _ = setup
        for i in range(5):
            await rec.record(make_event(i))

        async def no_finalize(*args, **kwargs):
            raise RuntimeError("metadata write failed")

        store.finalize_session = no_finalize
        await rec.stop()

        info = await store.session(rec.session_id)
        assert info.status is SessionStatus.OPEN
        assert not info.is_verified_complete
        assert rec.healthy is False

    async def test_a_crash_before_stop_leaves_the_session_open(self, setup):
        """Section 20(A)/(B): no stop() at all."""
        rec, store, _ = setup
        for i in range(10):
            await rec.record(make_event(i))
        await rec.flush()
        # ...and the process dies here. Nothing finalises.

        info = await store.session(rec.session_id)
        assert info.status is SessionStatus.OPEN
        assert not info.is_verified_complete

    async def test_stop_is_idempotent(self, setup):
        rec, store, _ = setup
        await rec.record(make_event(0))
        await rec.stop()
        await rec.stop()
        info = await store.session(rec.session_id)
        assert info.status is SessionStatus.COMPLETE


# ==========================================================================
# Section 17 — P2-12: what is recorded is what was accepted
# ==========================================================================


class TestAcceptedEventsAreSnapshotted:
    async def test_mutating_the_event_after_acceptance_cannot_rewrite_history(
        self, setup
    ):
        rec, store, _ = setup
        original = make_event(1, payload={"value": "as accepted"})
        await rec.record(original)

        original.payload["value"] = "rewritten"
        original.payload["injected"] = True
        original.correlation_id = "tampered"
        original.source = "SOMEWHERE_ELSE"

        await rec.flush()

        stored = [e async for e in store.read(rec.session_id)]
        assert len(stored) == 1
        assert stored[0].payload == {"value": "as accepted"}
        assert stored[0].correlation_id is None
        assert stored[0].source == "TEST"

    async def test_a_later_bus_subscriber_cannot_rewrite_history(self):
        """The real shape of P2-12: the recorder is middleware, and every
        subscriber afterwards receives the same mutable instance.
        """
        clock = ManualClock(start_ms=START_MS)
        store = FlakyStore()
        bus = InMemoryEventBus(raise_on_handler_error=True)
        rec = Recorder(store=store, clock=clock, buffer_size=100)
        await rec.start()
        rec.attach(bus)

        async def vandal(event: Event) -> None:
            event.payload["value"] = "rewritten by a subscriber"

        bus.subscribe(vandal, name="vandal")

        await bus.publish(make_event(1, payload={"value": "as published"}))
        await bus.drain()
        await rec.flush()

        stored = [e async for e in store.read(rec.session_id)]
        assert stored[0].payload == {"value": "as published"}

    async def test_the_snapshot_is_deep(self, setup):
        rec, store, _ = setup
        nested = {"outer": {"inner": [1, 2, 3]}}
        original = make_event(1, payload=nested)
        await rec.record(original)

        original.payload["outer"]["inner"].append(999)
        await rec.flush()

        stored = [e async for e in store.read(rec.session_id)]
        assert stored[0].payload == {"outer": {"inner": [1, 2, 3]}}


async def _noop() -> None:
    return None
