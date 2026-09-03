"""Render the per-component block of ./status.sh.

A separate file rather than a `python3 -c` one-liner: the inline version
needed escaped quotes inside an f-string, which is a syntax error before
Python 3.12 and silently printed nothing on 3.11 — the version the dev
container pins. Reads /health on stdin.
"""

from __future__ import annotations

import json
import sys

LABEL_WIDTH = 22

STATUS_COLOUR = {
    "HEALTHY": "\033[32m",
    "DEGRADED": "\033[33m",
    "OFFLINE": "\033[31m",
}
RESET = "\033[0m"
DIM = "\033[2m"


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        # The caller already reported that the API is unreachable; adding a
        # traceback here would bury that with something less useful.
        return 0

    components = data.get("components", {})
    if not components:
        print("  (none reported)")
        return 0

    colour = sys.stdout.isatty()
    for name, component in sorted(components.items()):
        status = component.get("status", "?")
        detail = component.get("detail") or ""
        errors = component.get("errors", 0)
        if errors:
            detail = f"{detail} · {errors} errors".strip(" ·").strip()

        dots = "." * max(1, LABEL_WIDTH - len(name))
        if colour:
            shade = STATUS_COLOUR.get(status, "")
            line = f"  {name} {dots} {shade}{status}{RESET}"
            if detail:
                line += f" {DIM}({detail}){RESET}"
        else:
            line = f"  {name} {dots} {status}"
            if detail:
                line += f" ({detail})"
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
