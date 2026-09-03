#!/usr/bin/env bash
# Convenience wrapper — the implementation lives in scripts/logs.sh
exec bash "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/scripts/logs.sh" "$@"
