#!/usr/bin/env bash
# Run the test suite.
#
#   ./test.sh                 the whole suite
#   ./test.sh tests/unit      one directory, or any other pytest argument
#
# The event-store and bus conformance suites cover PostgreSQL and Redis too,
# but only when a server is reachable — otherwise they skip. If the stack is
# up, this script points them at it so those parameters really run, and says
# which mode it used rather than leaving a green run ambiguous.

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"
require_repo_root
cd "$REPO_ROOT"

PY="$(python_bin)"
[[ -z "$PY" ]] && fail_with "Python 3.11+ is not available" \
  "The test suite runs on your machine, not in a container." \
  "bash scripts/setup-codespace.sh"

if ! "$PY" -c 'import pytest' >/dev/null 2>&1; then
  fail_with "pytest is not installed" \
    "The development dependencies have not been installed in this environment." \
    "bash scripts/setup-codespace.sh"
fi

heading "Trading Floor — tests"

# Point the backend suites at the running stack when there is one. Compose
# publishes PostgreSQL on 5432 and Redis on 6379.
BACKENDS="in-process only (postgres and redis parameters will skip)"
if docker_ok && [[ "$(compose_health postgres)" == "HEALTHY" ]]; then
  export TF_TEST_POSTGRES_DSN="${TF_TEST_POSTGRES_DSN:-postgresql://trading:trading@127.0.0.1:5432/trading_floor}"
  BACKENDS="with live PostgreSQL"
  if [[ "$(compose_health redis)" == "HEALTHY" ]]; then
    export TF_TEST_REDIS_URL="${TF_TEST_REDIS_URL:-redis://127.0.0.1:6379/0}"
    BACKENDS="with live PostgreSQL and Redis"
  fi
fi
check_line "Backends" "OK" "$BACKENDS"
check_line "Mode" "OK" "TF_MODE=paper"
blank

if [[ $# -gt 0 ]]; then
  TF_MODE=paper "$PY" -m pytest "$@"
else
  TF_MODE=paper "$PY" -m pytest tests -q -p no:cacheprovider
fi
