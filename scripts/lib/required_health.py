"""Answer the question the startup scripts actually need: can it trade?

The /health endpoint's aggregate `status` spans every component, optional
ones included. LUMEN is optional, and its default provider is NullProvider,
which reports unavailable on purpose so that the no-intelligence path is the
tested path rather than an untried edge case. A default stack therefore
aggregates to OFFLINE while being entirely healthy for trading.

Prints HEALTHY when every component the strategy requires is healthy, or a
space-separated list of the ones that are not. Reads /health on stdin.

The required set is imported from the strategy rather than copied here, so
this cannot drift from what the platform actually gates on.
"""

from __future__ import annotations

import json
import sys


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        print("UNREACHABLE")
        return 1

    try:
        from strategies.cross_venue import REQUIRED_COMPONENTS
    except ImportError:
        print("UNKNOWN")
        return 1

    components = data.get("components", {})
    unhealthy = [
        name
        for name in REQUIRED_COMPONENTS
        if components.get(name, {}).get("status") != "HEALTHY"
    ]

    if unhealthy:
        print(" ".join(unhealthy))
        return 1
    print("HEALTHY")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
