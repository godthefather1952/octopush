#!/usr/bin/env bash
# Phase 12 SHADOW field-rehearsal checks — safety gate, then live-feed observation.
#
# WHY THIS FILE EXISTS (P12-T1)
#   The first SHADOW field rehearsal reported
#
#       ERROR: Trading Floor API never became reachable
#
#   from an ad-hoc script that polled a loopback URL on a port of its own
#   invention. The API was up the whole time, listening on TF_API_PORT exactly
#   as the application logged. The harness was wrong, not the platform; the
#   exact wrong value is recorded in docs/phase12-shadow-validation-audit.md.
#
#   The fix is not a second correct default — it is having no second default at
#   all. This sources scripts/lib/common.sh and uses the TF_API_URL it already
#   defines, so this script's port cannot drift from the one every other script
#   uses. Override with TF_API_PORT, as everywhere else.
#
# USAGE
#   ./scripts/field-check-shadow.sh [SAMPLES] [INTERVAL_SECONDS]
#
#   Defaults to one sample. For the 5–10 minute rehearsal window:
#       ./scripts/field-check-shadow.sh 12 30
#
# It changes nothing. Every request is a GET, and there is no route here that
# could start, stop, promote or reconfigure a session.

set -euo pipefail

TF_FIELD_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/lib/common.sh
source "${TF_FIELD_DIR}/lib/common.sh"

SAMPLES="${1:-1}"
INTERVAL_S="${2:-30}"
export TF_API_URL TF_FIELD_SAMPLES="$SAMPLES"

py="$(python_bin)"
if [[ -z "$py" ]]; then
  printf 'No usable Python interpreter found.\n' >&2
  exit 1
fi

heading "Phase 12 SHADOW field check"
info "  API ................... ${TF_API_URL}"
info "  Samples ............... ${SAMPLES} every ${INTERVAL_S}s"

if ! wait_for_http "${TF_API_URL}/health" "${TF_FIELD_WAIT_S:-90}"; then
  blank
  check_line "API reachable" "FAIL" "${TF_API_URL}"
  blank
  printf 'The API did not answer at %s.\n' "${TF_API_URL}" >&2
  printf 'Check the stack with ./status.sh; if it listens elsewhere, set TF_API_PORT.\n' >&2
  exit 1
fi

# The safety envelope. A failure here stops the run rather than annotating it.
( cd "$REPO_ROOT" && "$py" "${TF_LIB_DIR}/field_shadow.py" safety )

for (( i = 1; i <= SAMPLES; i++ )); do
  export TF_FIELD_SAMPLE_INDEX="$i"
  ( cd "$REPO_ROOT" && "$py" "${TF_LIB_DIR}/field_shadow.py" observe ) || true
  if (( i < SAMPLES )); then
    sleep "$INTERVAL_S"
  fi
done

blank
info "Field check complete. Nothing was modified."
