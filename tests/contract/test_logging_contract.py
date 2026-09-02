"""Log records must be alignable with the events they describe — P0-L5, P0-H3.

Two defects were found here. The first (P0-H3) was that ``ts`` reported the
zero-padded *day of month* where milliseconds belonged — ``%03d`` is not a
strftime millisecond directive — in local time while labelled ``Z``. Three
lines 250ms apart all read ``.002Z``.

The second (P0-L5) is subtler and survives that fix: ``record.created`` is
host wall-clock time. During a replay, events carry replay time and logs
carry the time the replay happened to run, so the two cannot be put on one
timeline — which is exactly what someone debugging a replay needs to do.
The fix does not replace ``ts``; a log line still says when it was really
emitted. It adds the platform clock's reading alongside it.
"""

from __future__ import annotations

import json
import logging
import re

import pytest

from core.clock import ManualClock
from core.logging import JsonFormatter, clock_context

START_MS = 1_700_000_000_000

RFC3339_UTC = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")


def make_record(**extra) -> logging.LogRecord:
    record = logging.LogRecord(
        name="test.logger", level=logging.INFO, pathname="p", lineno=1,
        msg="probe", args=(), exc_info=None,
    )
    record.__dict__.update(extra)
    return record


def emit(record: logging.LogRecord) -> dict:
    return json.loads(JsonFormatter().format(record))


class TestWallClockTimestamps:
    def test_the_timestamp_is_rfc3339_utc_with_real_milliseconds(self):
        record = make_record()
        record.created = 1_700_000_000.25
        assert RFC3339_UTC.match(emit(record)["ts"])

    def test_milliseconds_are_milliseconds_not_the_day_of_month(self):
        """The P0-H3 regression, pinned.

        1_700_000_000.25 is 2023-11-14T22:13:20.250Z. The old formatter
        rendered `.014Z` — the 14th of the month.
        """
        record = make_record()
        record.created = 1_700_000_000.25
        assert emit(record)["ts"] == "2023-11-14T22:13:20.250Z"

    def test_lines_milliseconds_apart_are_distinguishable(self):
        stamps = []
        for offset in (0.0, 0.25, 0.5, 0.75):
            record = make_record()
            record.created = 1_700_000_000.0 + offset
            stamps.append(emit(record)["ts"])
        assert len(set(stamps)) == 4, stamps

    def test_the_z_suffix_really_means_utc(self):
        """It was local time wearing a Z."""
        record = make_record()
        record.created = 0.0
        assert emit(record)["ts"] == "1970-01-01T00:00:00.000Z"


class TestLogsCarryPlatformTime:
    def test_without_a_clock_no_platform_time_is_claimed(self):
        """Absent is honest; a wrong number is not."""
        assert "clock_ms" not in emit(make_record())

    def test_a_bound_clock_appears_on_every_record(self):
        clock = ManualClock(start_ms=START_MS)
        with clock_context(clock):
            payload = emit(make_record())
        assert payload["clock_ms"] == START_MS

    def test_platform_time_follows_the_clock_not_the_host(self):
        clock = ManualClock(start_ms=START_MS)
        with clock_context(clock):
            first = emit(make_record())["clock_ms"]
            clock.advance(5_000)
            second = emit(make_record())["clock_ms"]
        assert second - first == 5_000

    def test_replay_logs_align_with_replay_events(self):
        """The property the field exists for.

        Under a manual clock, `clock_ms` tracks logical time while `ts`
        tracks the host. A replay of a 2023 recording run today logs both,
        so a line can be placed against the event it describes.
        """
        clock = ManualClock(start_ms=START_MS)
        with clock_context(clock):
            record = make_record()
            record.created = 1_900_000_000.0  # "now", years after the recording
            payload = emit(record)

        assert payload["clock_ms"] == START_MS
        assert payload["ts"].startswith("2030-"), payload["ts"]
        assert payload["clock_ms"] != int(record.created * 1000)

    def test_the_binding_is_restored_afterwards(self):
        clock = ManualClock(start_ms=START_MS)
        with clock_context(clock):
            pass
        assert "clock_ms" not in emit(make_record())

    def test_the_binding_is_restored_after_an_exception(self):
        clock = ManualClock(start_ms=START_MS)
        with pytest.raises(RuntimeError), clock_context(clock):
            raise RuntimeError("boom")
        assert "clock_ms" not in emit(make_record())

    def test_a_broken_clock_does_not_take_down_logging(self):
        """Logging must never be the thing that fails."""

        class Broken:
            def now_ms(self):
                raise RuntimeError("clock exploded")

        with clock_context(Broken()):
            payload = emit(make_record())
        assert payload["message"] == "probe"
        assert "clock_ms" not in payload


class TestStructuredFields:
    def test_extra_fields_are_merged_at_the_top_level(self):
        payload = emit(make_record(venue="VENUE_A", symbol="BTC-USD"))
        assert payload["venue"] == "VENUE_A"
        assert payload["symbol"] == "BTC-USD"

    def test_a_record_is_always_valid_json(self):
        payload = emit(make_record(weird=object()))
        assert isinstance(payload["weird"], str)
