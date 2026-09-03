#!/usr/bin/env bash
# Convenience wrapper — the implementation lives in scripts/test.sh
exec bash "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/scripts/test.sh" "$@"
