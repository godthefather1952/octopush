#!/usr/bin/env bash
# Convenience wrapper — the implementation lives in scripts/status.sh
exec bash "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/scripts/status.sh" "$@"
