#!/usr/bin/env bash
# What the stack is actually doing right now.
#
# Reports each service's own healthcheck and, when the application is
# reachable, the health its components report about themselves — which is a
# different and more useful question than "is the container running".

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"
require_repo_root
cd "$REPO_ROOT"

heading "Trading Floor — status"

if ! docker_ok; then
  check_line "Docker" "FAIL" "daemon unreachable"
  blank
  info "  The stack cannot be running: the Docker daemon is not up."
  blank
  info "  Start it, then:  ./start-paper.sh"
  blank
  exit 1
fi

info "Services"
blank
for service in postgres redis trading-floor; do
  case "$service" in
    postgres)      label="PostgreSQL" ;;
    redis)         label="Redis" ;;
    trading-floor) label="Trading Floor" ;;
  esac
  check_line "$label" "$(compose_health "$service")"
done

# --- application detail ------------------------------------------------------
blank
if curl -fsS --max-time 5 "${TF_API_URL}/health" >/dev/null 2>&1; then
  MODE="$(health_field mode || echo unknown)"
  STATUS="$(health_field status || echo unknown)"
  WARMED="$(health_field warmed_up || echo unknown)"

  REQUIRED="$(required_components_status || true)"

  info "Platform"
  blank
  # "Can it trade" first, because that is the question being asked. The
  # aggregate below includes optional components and is shown for
  # completeness, not as the verdict.
  if [[ "$REQUIRED" == "HEALTHY" ]]; then
    check_line "Can trade" "OK" "all required components healthy"
  else
    check_line "Can trade" "FAIL" "waiting on: ${REQUIRED}"
  fi
  check_line "Execution mode" "$([[ "$MODE" == "PAPER" ]] && echo OK || echo FAIL)" "$MODE"
  check_line "Warmed up" "$([[ "$WARMED" == "true" ]] && echo OK || echo WARMING)" "$WARMED"
  check_line "Aggregate" "$STATUS" "includes optional components"

  # Per-component health, straight from the endpoint. Rendered in Python
  # rather than with jq, which is not installed in a bare container.
  blank
  info "Components"
  blank
  curl -fsS --max-time 5 "${TF_API_URL}/health" 2>/dev/null \
    | "$(python_bin)" "${TF_LIB_DIR}/render_components.py" || true

  blank
  info "  Dashboard:  $(public_url "$TF_API_PORT")/"
  info "  Health:     $(public_url "$TF_API_PORT")/health"
else
  info "Platform"
  blank
  check_line "API" "UNREACHABLE" "${TF_API_URL}/health"
  blank
  if [[ "$(compose_health trading-floor)" == "STOPPED" ]]; then
    info "  The stack is not running."
    blank
    info "  Start it with:  ./start-paper.sh"
  else
    info "  The container is up but the API is not answering yet."
    blank
    info "  Watch it start:  ./logs.sh trading-floor"
  fi
fi

blank
