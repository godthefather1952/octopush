#!/usr/bin/env bash
# Convenience wrapper — the implementation lives in scripts/start-shadow.sh
exec bash "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/scripts/start-shadow.sh" "$@"
