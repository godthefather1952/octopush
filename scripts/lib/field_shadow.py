"""Phase 12 SHADOW field-rehearsal checks: safety gate, then observation.

Read-only. Every request is a GET, and nothing here can start, stop, promote
or reconfigure anything.

THE SAFETY GATE COMES FIRST, AND IT IS NOT A DIAGNOSTIC
=======================================================
A shadow rehearsal is only a shadow rehearsal if the envelope holds: PAPER
mode, SHADOW profile, live public feed, PaperExecutor, no private venue
access, no real order submission. If any of those cannot be proven from the
running platform's own endpoints, this exits non-zero and the rehearsal is not
observed. "Probably fine" is not a safety argument.

Reads the API base URL from ``TF_API_URL``, which ``scripts/lib/common.sh``
defines from ``TF_API_PORT``. It deliberately has no default of its own -- a
second default is exactly the defect P12-T1 records, where a field script
polled a port it had invented for itself and reported the platform unreachable
while the platform was up and logging the real one.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

TIMEOUT_S = 10


def fetch(base: str, path: str) -> dict | None:
    try:
        with urllib.request.urlopen(f"{base}{path}", timeout=TIMEOUT_S) as response:
            return json.load(response)
    except (urllib.error.URLError, OSError, json.JSONDecodeError, ValueError):
        return None


def find_key(blob: object, key: str) -> object:
    """Locate one key anywhere in a nested response.

    The operations snapshot nests the session manifest, and pinning this to an
    exact path would make the check brittle against a reshaped response in a
    way that reads as a safety failure rather than a harness change.
    """
    if isinstance(blob, dict):
        if key in blob:
            return blob[key]
        for value in blob.values():
            found = find_key(value, key)
            if found is not None:
                return found
    elif isinstance(blob, list):
        for item in blob:
            found = find_key(item, key)
            if found is not None:
                return found
    return None


def line(label: str, ok: bool, detail: str = "") -> None:
    mark = "OK  " if ok else "FAIL"
    print(f"  {label:<34} {mark}  {detail}")


def safety_gate(base: str) -> bool:
    health = fetch(base, "/health")
    if health is None:
        line("API /health", False, "unreachable")
        return False

    ok = True

    mode = str(health.get("mode", "")).upper()
    good = mode == "PAPER"
    line("Trading mode is PAPER", good, mode or "absent")
    ok &= good

    profile = str(health.get("profile", "")).upper()
    good = profile == "SHADOW"
    line("Operational profile is SHADOW", good, profile or "absent")
    ok &= good

    feed = str(health.get("feed", "")).upper()
    good = feed == "LIVE"
    line("Market feed is live", good, feed or "absent")
    ok &= good

    executor = str(health.get("executor", ""))
    good = executor == "PaperExecutor"
    line("Executor is PaperExecutor", good, executor or "absent")
    ok &= good

    operations = fetch(base, "/api/operations")
    for key, want in (
        ("paper_executor", True),
        ("private_venue_access", False),
        ("real_order_submission", False),
    ):
        value = find_key(operations, key) if operations else None
        good = value is want
        line(f"Manifest {key} is {want}", good, "absent" if value is None else str(value))
        ok &= good

    pre_live = fetch(base, "/api/pre-live")
    for key in ("live_executor", "private_venue_connectivity", "credential_boundary"):
        value = find_key(pre_live, key) if pre_live else None
        good = value == "NOT_IMPLEMENTED"
        line(f"Pre-live {key}", good, str(value) if value is not None else "absent")
        ok &= good

    return bool(ok)


def observe(base: str, index: int, total: int) -> None:
    state = fetch(base, "/api/state") or {}
    health = fetch(base, "/health") or {}
    shadow = fetch(base, "/api/shadow") or {}

    print(f"\n  --- sample {index}/{total} ---")
    print(f"  session_id ......... {state.get('session_id', 'n/a')}")
    print(f"  ticks .............. {state.get('ticks', 'n/a')}")
    print(f"  warmed_up .......... {health.get('warmed_up', 'n/a')}")
    print(f"  aggregate status ... {health.get('status', 'n/a')}")

    venues = state.get("venues") or []
    if not venues:
        print("  venues ............. none reporting yet")
    for row in venues:
        print(
            f"  venue {row.get('venue', '?')}/{row.get('symbol', '?')}: "
            f"connected={row.get('connected')} "
            f"quality={row.get('quality')} "
            f"age_ms={row.get('age_ms')} "
            f"reconnects={row.get('reconnects')} "
            f"gaps={row.get('sequence_gaps')}"
        )

    components = health.get("components") or {}
    for name in ("TIDAL", "NORO", "ZEPHR", "RECORDER"):
        entry = components.get(name)
        if entry:
            print(f"  {name:<18} {entry.get('status')}  errors={entry.get('errors')}")

    print(f"  shadow enabled ..... {shadow.get('enabled', 'n/a')}")
    for key in (
        "observer_events_seen",
        "observer_failures",
        "observer_unattributable_events",
        "decisions_total",
        "paper_fills",
    ):
        if key in shadow:
            print(f"  {key} ".ljust(23, ".") + f" {shadow[key]}")

    readiness = shadow.get("readiness")
    if isinstance(readiness, dict):
        print(f"  shadow ready ....... {readiness.get('ready')}")
        codes = readiness.get("reason_codes")
        if codes:
            print(f"  reason codes ....... {', '.join(str(c) for c in codes)}")


def main() -> int:
    base = os.environ.get("TF_API_URL", "").rstrip("/")
    if not base:
        print(
            "TF_API_URL is not set. Run this through scripts/field-check-shadow.sh, "
            "which sources scripts/lib/common.sh.",
            file=sys.stderr,
        )
        return 2

    samples = int(os.environ.get("TF_FIELD_SAMPLES", "1"))
    mode = sys.argv[1] if len(sys.argv) > 1 else "all"

    if mode in ("safety", "all"):
        print("\nSafety envelope\n")
        if not safety_gate(base):
            print(
                "\nSAFETY CHECK FAILED - this is not a proven PAPER/SHADOW "
                "rehearsal. Stopping without observing.",
                file=sys.stderr,
            )
            return 1
        print("\n  Safety envelope proven.")

    if mode in ("observe", "all"):
        index = int(os.environ.get("TF_FIELD_SAMPLE_INDEX", "1"))
        observe(base, index, samples)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
