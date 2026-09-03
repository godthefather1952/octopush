#!/usr/bin/env bash
# Convenience wrapper — the implementation lives in scripts/stop-paper.sh
exec bash "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/scripts/stop-paper.sh" "$@"
