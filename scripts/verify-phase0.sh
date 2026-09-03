#!/usr/bin/env bash
# Automates the Phase 0 Docker verification recorded in
# docs/docker-verification.md.
#
# That document was written because the image could not be built where the
# remediation was carried out: the sandbox proxy denied Docker Hub's blob CDN,
# so no base layer could be pulled. Every step below has a stated pass/fail
# expectation, so the result is a fact rather than a judgement call.
#
# Each check prints PASS or FAIL. A failure preserves its log and names the
# file, then the run continues so one broken step does not hide the rest.

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"
require_repo_root
cd "$REPO_ROOT"

IMAGE="trading-floor:phase0-verify"
PROBE="tf-phase0-probe"

# Run under a compose project of its own. Volumes are scoped to the project,
# so the stack this script brings up and tears down cannot reach the ones
# holding your recorded sessions — verification needs an empty database to
# check the migration schema and event persistence, and taking the
# developer's data to get one would be an unacceptable price.
export TF_COMPOSE_PROJECT="trading-floor-verify"
LOG_DIR="$REPO_ROOT/.verify-logs"
PASSED=0
FAILED=0
FAILED_STEPS=()

mkdir -p "$LOG_DIR"

pass() { printf '  %s[PASS]%s %s\n' "$C_OK" "$C_RESET" "$1"; PASSED=$(( PASSED + 1 )); }
fail() {
  printf '  %s[FAIL]%s %s' "$C_FAIL" "$C_RESET" "$1"
  [[ -n "${2:-}" ]] && printf ' %s— log: %s%s' "$C_DIM" "$2" "$C_RESET"
  printf '\n'
  FAILED=$(( FAILED + 1 )); FAILED_STEPS+=("$1")
}
skip() { printf '  %s[SKIP]%s %s %s(%s)%s\n' "$C_WARN" "$C_RESET" "$1" "$C_DIM" "$2" "$C_RESET"; }

cleanup() {
  docker rm -f "$PROBE" >/dev/null 2>&1 || true
  compose down --volumes --remove-orphans >/dev/null 2>&1 || true
}
trap cleanup EXIT

heading "PHASE 0 INFRASTRUCTURE VERIFICATION"

# --- preconditions -----------------------------------------------------------
if ! docker_ok; then
  fail_with "The Docker daemon is not available" \
    "Every check here builds or runs a container. Nothing can be verified without a daemon." \
    "In Codespaces: Command Palette (F1) → 'Codespaces: Rebuild Container'.
       Locally: start Docker Desktop, or  sudo systemctl start docker"
fi
if ! compose_ok; then
  fail_with "Docker Compose is not available" \
    "Steps 7 to 10 bring up the full stack, which is defined in docker-compose.yml." \
    "In Codespaces, rebuild the container. Locally, install the Docker Compose plugin."
fi

# The verification stack binds the same host ports as the development one, so
# they cannot both run. Refusing is better than fighting over a port and
# reporting a confusing failure four checks later.
if ( unset TF_COMPOSE_PROJECT; [[ "$(running_service_count)" -gt 0 ]] ); then
  fail_with "The development stack is running" \
    "Verification starts its own isolated stack, which needs the same host ports (8080, 5432, 6379). Your data is safe either way — the two stacks use separate volumes — but they cannot run at once." \
    "./stop-paper.sh      # stop it, keeping all your data
       ./verify-phase0.sh   # then verify"
fi

dim "  Image: ${IMAGE}   Logs: ${LOG_DIR}/"
blank

# --- 1. the image builds -----------------------------------------------------
info "Image"
blank
if docker build -t "$IMAGE" . >"$LOG_DIR/01-build.log" 2>&1; then
  pass "Docker image builds"
else
  fail "Docker image builds" "$LOG_DIR/01-build.log"
  # Nothing downstream can run without an image. Diagnose the most common
  # cause rather than cascading nine more failures.
  blank
  if grep -qiE '403|forbidden|failed to (resolve|copy)|no such host|tls' "$LOG_DIR/01-build.log"; then
    printf '  %sThe build could not pull its base image.%s\n' "$C_WARN" "$C_RESET"
    printf '  %sThis is a network or registry-policy problem, not a defect in the Dockerfile.%s\n\n' "$C_DIM" "$C_RESET"
  fi
  fail_with "The image did not build" \
    "Every remaining check needs it. The build output is in $LOG_DIR/01-build.log." \
    "tail -40 $LOG_DIR/01-build.log"
fi

# --- 2. dependencies present inside the image --------------------------------
if docker run --rm "$IMAGE" python -c \
     'import anthropic, asyncpg, fastapi, pydantic, redis, uvicorn, websockets' \
     >"$LOG_DIR/02-imports.log" 2>&1; then
  pass "Python dependencies available in the image"
else
  fail "Python dependencies available in the image" "$LOG_DIR/02-imports.log"
fi

# --- 3 & 4. console scripts installed ----------------------------------------
# These exist only if the project itself was installed, not merely copied.
if docker run --rm "$IMAGE" trading-floor --help >"$LOG_DIR/03-cli.log" 2>&1; then
  pass "CLI installed (trading-floor)"
else
  fail "CLI installed (trading-floor)" "$LOG_DIR/03-cli.log"
fi
if docker run --rm "$IMAGE" trading-floor-replay --help >"$LOG_DIR/04-replay-cli.log" 2>&1; then
  pass "Replay CLI installed (trading-floor-replay)"
else
  fail "Replay CLI installed (trading-floor-replay)" "$LOG_DIR/04-replay-cli.log"
fi

# --- 5. the paper boundary holds inside the image ----------------------------
# Expectation: NON-ZERO exit. A zero exit here is a boundary failure.
if docker run --rm -e TF_MODE=live "$IMAGE" \
     python -c 'from core.config import load_settings; load_settings()' \
     >"$LOG_DIR/05-live-rejected.log" 2>&1; then
  fail "Live mode rejected — TF_MODE=live was ACCEPTED" "$LOG_DIR/05-live-rejected.log"
else
  if grep -q "not supported" "$LOG_DIR/05-live-rejected.log"; then
    pass "Live mode rejected"
  else
    fail "Live mode rejected (exited non-zero, but not with the expected message)" "$LOG_DIR/05-live-rejected.log"
  fi
fi

# --- 6. non-root -------------------------------------------------------------
UID_IN_IMAGE="$(docker run --rm "$IMAGE" id -u 2>/dev/null || echo "?")"
if [[ "$UID_IN_IMAGE" == "10001" ]]; then
  pass "Container runs non-root (uid 10001)"
elif [[ "$UID_IN_IMAGE" == "0" ]]; then
  fail "Container runs non-root — it is running as root"
else
  fail "Container runs non-root — unexpected uid: ${UID_IN_IMAGE}"
fi

# --- 7. the full stack comes up ----------------------------------------------
blank
info "Stack"
blank
compose down --volumes --remove-orphans >/dev/null 2>&1 || true
if compose up -d --build >"$LOG_DIR/07-compose-up.log" 2>&1; then
  pass "Docker Compose stack starts"
else
  fail "Docker Compose stack starts" "$LOG_DIR/07-compose-up.log"
  fail_with "The stack did not start" \
    "Checks 8 to 11 all need it running." \
    "tail -40 $LOG_DIR/07-compose-up.log"
fi

# --- 8 & 9. services become healthy ------------------------------------------
for service in redis postgres; do
  label="$([[ $service == redis ]] && echo Redis || echo PostgreSQL)"
  if wait_for_compose_health "$service" 120 && [[ "$(compose_health "$service")" == "HEALTHY" ]]; then
    pass "${label} healthy"
  else
    compose logs "$service" >"$LOG_DIR/0${service}.log" 2>&1 || true
    fail "${label} healthy — reported $(compose_health "$service")" "$LOG_DIR/0${service}.log"
  fi
done

if wait_for_compose_health trading-floor 180 && wait_for_http "${TF_API_URL}/health" 180; then
  pass "API health endpoint answers"
  MODE="$(health_field mode || echo unknown)"
  if [[ "$MODE" == "PAPER" ]]; then
    pass "Running platform reports PAPER mode"
  else
    fail "Running platform reports PAPER mode — got '${MODE}'"
  fi
else
  compose logs trading-floor >"$LOG_DIR/09-trading-floor.log" 2>&1 || true
  fail "API health endpoint answers" "$LOG_DIR/09-trading-floor.log"
  fail "Running platform reports PAPER mode (not checked — API unreachable)"
fi

# --- 10. migration schema ----------------------------------------------------
# docker-compose mounts storage/migrations into the PostgreSQL container's
# entrypoint directory, so this exercises the path a new deployment takes.
# Expectation: exactly `events` and `sessions`. Any other table means the
# migration and postgres_store.MIGRATION_SQL have diverged again.
TABLES="$(compose exec -T postgres psql -tAq -U trading -d trading_floor \
  -c "select tablename from pg_tables where schemaname='public' order by tablename" \
  2>"$LOG_DIR/10-tables.log" | tr -d '\r' | grep -v '^$' | tr '\n' ' ' | sed 's/ $//')"
if [[ "$TABLES" == "events sessions" ]]; then
  pass "Migration schema (events, sessions)"
else
  printf 'tables reported: %s\n' "$TABLES" >>"$LOG_DIR/10-tables.log"
  fail "Migration schema — expected 'events sessions', got '${TABLES}'" "$LOG_DIR/10-tables.log"
fi

# --- 11. events actually persist ---------------------------------------------
# The stack has been running for a while by now, so the recorder should have
# flushed. Poll rather than assume: the buffer flushes on size or age.
EVENTS=0
for _ in $(seq 1 15); do
  EVENTS="$(compose exec -T postgres psql -tAq -U trading -d trading_floor \
    -c 'select count(*) from events' 2>>"$LOG_DIR/11-events.log" | tr -d '\r ' || echo 0)"
  [[ "${EVENTS:-0}" =~ ^[0-9]+$ ]] && (( EVENTS > 0 )) && break
  sleep 4
done
if [[ "${EVENTS:-0}" =~ ^[0-9]+$ ]] && (( EVENTS > 0 )); then
  pass "Event persistence (${EVENTS} events recorded)"
else
  compose logs trading-floor >"$LOG_DIR/11-trading-floor.log" 2>&1 || true
  fail "Event persistence — no rows in events" "$LOG_DIR/11-events.log"
fi

# --- result ------------------------------------------------------------------
blank
printf '%s  FINAL RESULT%s\n\n' "$C_BOLD" "$C_RESET"
printf '    %d passed, %d failed\n\n' "$PASSED" "$FAILED"

if (( FAILED == 0 )); then
  printf '%s  PHASE 0 DOCKER VERIFICATION: PASS%s\n\n' "$C_OK$C_BOLD" "$C_RESET"
  dim "  docs/docker-verification.md can be marked verified on this host."
  blank
  exit 0
fi

printf '%s  PHASE 0 DOCKER VERIFICATION: FAIL%s\n\n' "$C_FAIL$C_BOLD" "$C_RESET"
info "  Failed checks:"
for step in "${FAILED_STEPS[@]}"; do printf '    · %s\n' "$step"; done
blank
info "  Logs for every step are kept in ${LOG_DIR}/"
blank
exit 1
