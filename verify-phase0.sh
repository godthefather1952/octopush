#!/usr/bin/env bash
# Convenience wrapper — the implementation lives in scripts/verify-phase0.sh
exec bash "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/scripts/verify-phase0.sh" "$@"
