#!/usr/bin/env bash
# Start the paper-trading development stack.
#
# PostgreSQL, Redis and the trading floor, via the repository's existing
# docker-compose.yml — this script adds no second way to run the services, it
# just drives the one that already exists and waits for it to actually be up.
#
# Paper mode is enforced twice before anything starts: once against the shell
# environment and once against .env. There is no flag, argument or environment
# variable that makes this script start anything else.

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"
require_repo_root
cd "$REPO_ROOT"

heading "Trading Floor — Paper Mode"

# --- refuse anything but paper, before touching the machine ------------------
assert_paper_mode

# --- environment -------------------------------------------------------------
info "Checking environment..."
blank

PY="$(python_bin)"
if [[ -n "$PY" ]]; then
  check_line "Python" "OK" "$("$PY" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])')"
else
  check_line "Python" "FAIL" "3.11+ not found"
  fail_with "Python 3.11 or newer is not available" \
    "The stack runs in containers, but this script reads the project's configuration to report your paper balance and to verify paper mode." \
    "bash scripts/setup-codespace.sh"
fi

if docker_ok; then
  check_line "Docker" "OK" "$(docker version --format '{{.Server.Version}}' 2>/dev/null || echo 'daemon up')"
else
  check_line "Docker" "FAIL" "daemon unreachable"
  if environment_is_disposable; then
    fail_with "The Docker daemon is not responding" \
      "This Codespace needs its own Docker daemon to run PostgreSQL, Redis and the application. The docker-in-docker dev container feature provides it." \
      "Command Palette (F1) → 'Codespaces: Rebuild Container', then re-run ./start-paper.sh"
  else
    fail_with "The Docker daemon is not responding" \
      "Docker is required to start the stack." \
      "Start Docker Desktop (macOS/Windows), or on Linux:  sudo systemctl start docker"
  fi
fi

if compose_ok; then
  check_line "Docker Compose" "OK" "$(compose version --short 2>/dev/null || echo v2)"
else
  check_line "Docker Compose" "FAIL"
  fail_with "Docker Compose is not available" \
    "The stack is defined in docker-compose.yml and cannot be started without it." \
    "In Codespaces, rebuild the container. Locally, install the Docker Compose plugin."
fi

if ( TF_MODE=paper "$PY" -c 'import core, fastapi, redis' >/dev/null 2>&1 ); then
  check_line "Project deps" "OK"
else
  check_line "Project deps" "FAIL" "not installed"
  fail_with "The project's Python dependencies are not installed" \
    "'import core' failed, so setup has not completed in this environment." \
    "bash scripts/setup-codespace.sh"
fi

# --- already running? --------------------------------------------------------
# Starting an already-started stack should be a no-op that says so, not a
# confusing second attempt.
if [[ "$(running_service_count)" -ge 3 ]]; then
  blank
  printf '  %sAlready running.%s Bringing any stopped service up and re-checking health.\n' "$C_WARN" "$C_RESET"
fi

# --- start -------------------------------------------------------------------
blank
info "Starting infrastructure..."
blank

BUILD_LOG="$REPO_ROOT/.verify-logs/start.log"
mkdir -p "$(dirname "$BUILD_LOG")"
if ! compose up -d --build >"$BUILD_LOG" 2>&1; then
  blank

  # Name the specific cause where it is identifiable, rather than printing
  # thirty lines of container output and leaving the reading to the developer.
  if grep -qiE 'Forbidden|403|failed to (resolve|copy)|no such host|dial tcp.*timeout|TLS handshake' "$BUILD_LOG"; then
    fail_with "Docker could not download the images the stack needs" \
      "The pull was refused by the network, not by Docker. This happens behind a proxy or firewall that blocks Docker Hub's content CDN — the registry API answers but the image layers do not download. Nothing is wrong with this repository." \
      "docker pull redis:7-alpine     # confirm the same refusal directly
       Then: use a network that permits Docker Hub, or configure a registry
       mirror in Docker's daemon.json. Full output: ${BUILD_LOG}"
  fi

  if grep -qiE 'address already in use|port is already allocated' "$BUILD_LOG"; then
    fail_with "A port the stack needs is already taken" \
      "Another process is listening on one of 8080, 5432 or 6379." \
      "./stop-paper.sh                       # if an older stack is still up
       lsof -i :${TF_API_PORT}                        # find what holds the port"
  fi

  if grep -qiE 'permission denied.*docker.sock|dial unix /var/run/docker.sock' "$BUILD_LOG"; then
    fail_with "This user cannot talk to the Docker daemon" \
      "The socket exists but is not accessible to you." \
      "In Codespaces, rebuild the container. Locally: sudo usermod -aG docker \$USER, then log out and back in."
  fi

  printf '%s\n' "$(tail -30 "$BUILD_LOG")"
  blank
  fail_with "The stack did not start" \
    "docker compose up failed. Its last 30 lines are above." \
    "cat ${BUILD_LOG}     # the full output
       ./stop-paper.sh      # clear the stack, then try again"
fi

# --- wait for health, then report what is actually true ----------------------
# Nothing below prints HEALTHY on the strength of a container existing: each
# line reflects that service's own healthcheck, and the application line waits
# for its HTTP endpoint to answer.
FAILED=0

for service in postgres redis; do
  label="$([[ $service == postgres ]] && echo PostgreSQL || echo Redis)"
  if wait_for_compose_health "$service" 90; then
    check_line "$label" "$(compose_health "$service")"
  else
    check_line "$label" "$(compose_health "$service")"
    FAILED=1
  fi
done

if wait_for_compose_health trading-floor 120 && wait_for_http "${TF_API_URL}/health" 120; then
  WARMED="$(health_field warmed_up || echo false)"
  REQUIRED="$(required_components_status || true)"

  if [[ "$REQUIRED" == "HEALTHY" ]]; then
    check_line "Trading Floor" "HEALTHY"
  elif [[ "$WARMED" != "true" ]]; then
    # Warm-up is a real state, not a fault: the platform does not trade until
    # every required component has reported healthy at least once.
    check_line "Trading Floor" "WARMING" "waiting on: ${REQUIRED}"
  else
    check_line "Trading Floor" "UNHEALTHY" "not healthy: ${REQUIRED}"
    FAILED=1
  fi

  # Optional components are reported, never counted as failures. With the
  # default configuration LUMEN is deliberately unavailable.
  OPTIONAL="$(health_field status || echo UNKNOWN)"
  if [[ "$REQUIRED" == "HEALTHY" && "$OPTIONAL" != "HEALTHY" ]]; then
    check_line "Intelligence" "SKIP" "LUMEN not configured — optional"
  fi
else
  check_line "Trading Floor" "$(compose_health trading-floor)" "health endpoint did not answer"
  FAILED=1
fi

if (( FAILED )); then
  blank
  fail_with "The stack started but did not become healthy" \
    "One or more services failed their health check. The container is up; the service inside it is not ready." \
    "./status.sh                      # what each service reports
       ./logs.sh trading-floor          # why the application is unhealthy
       ./stop-paper.sh && ./start-paper.sh"
fi

# --- report ------------------------------------------------------------------
MODE="$(health_field mode || echo PAPER)"
blank
printf '  %sExecution mode: %s%s\n' "$C_BOLD" "$MODE" "$C_RESET"
if [[ "$MODE" != "PAPER" ]]; then
  fail_with "The running platform did not report paper mode" \
    "The health endpoint reports mode=${MODE}. Everything in this build should make that impossible." \
    "./stop-paper.sh   # then report this: it is a defect, not a configuration problem"
fi

blank
info "  Dashboard:"
printf '    %s%s%s\n' "$C_INFO" "$(public_url "$TF_API_PORT")/" "$C_RESET"
blank
info "  API health:"
printf '    %s%s%s\n' "$C_INFO" "$(public_url "$TF_API_PORT")/health" "$C_RESET"
blank
info "  Metrics:"
printf '    %s%s%s\n' "$C_INFO" "$(public_url "$TF_API_PORT")/metrics" "$C_RESET"
blank
info "  Paper balance:"
printf '    %s USD\n' "$(paper_balance)"

if [[ -n "${CODESPACE_NAME:-}" ]]; then
  blank
  dim "  In Codespaces, open the PORTS tab and click the globe icon on port ${TF_API_PORT}"
  dim "  if the link above does not open directly."
fi

blank
printf '%s  Ready for testing.%s\n\n' "$C_OK$C_BOLD" "$C_RESET"
dim "  ./status.sh          what everything is doing"
dim "  ./logs.sh            follow the logs"
dim "  ./verify-phase0.sh   run the Phase 0 infrastructure verification"
dim "  ./stop-paper.sh      stop everything"
blank
