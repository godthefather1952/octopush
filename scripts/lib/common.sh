#!/usr/bin/env bash
# Shared helpers for the developer-experience scripts.
#
# Every script in scripts/ sources this file and delegates to it, so the
# checks, the output format and — most importantly — the paper-mode
# enforcement exist once rather than in six slightly-different copies.
#
# This file is development tooling. It never imports, wraps or influences
# trading logic; the only thing it asserts about the platform is that it is
# running in paper mode, which it verifies rather than assumes.

set -euo pipefail

# --- repository root ---------------------------------------------------------
# Resolved from this file's own location, so every script works regardless of
# the directory it was invoked from.
TF_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${TF_LIB_DIR}/../.." && pwd)"
export REPO_ROOT

# --- the one setting this tooling is not allowed to vary ---------------------
# The incoming value is captured BEFORE being overwritten. Exporting
# TF_MODE=paper first and checking afterwards would read back our own value
# and silently discard an operator who asked for something else — which is
# safe, but is exactly the "intent ignored without a word" behaviour the
# platform itself was fixed not to have.
TF_REQUESTED_MODE="${TF_MODE:-paper}"
export TF_REQUESTED_MODE
export TF_MODE=paper

# --- presentation ------------------------------------------------------------
if [[ -t 1 ]] && [[ "${NO_COLOR:-}" == "" ]] && [[ "${TERM:-dumb}" != "dumb" ]]; then
  C_RESET=$'\033[0m'; C_BOLD=$'\033[1m'; C_DIM=$'\033[2m'
  C_OK=$'\033[32m';   C_WARN=$'\033[33m'; C_FAIL=$'\033[31m'; C_INFO=$'\033[36m'
else
  C_RESET=""; C_BOLD=""; C_DIM=""; C_OK=""; C_WARN=""; C_FAIL=""; C_INFO=""
fi

#: Width of the dotted leader in check lines.
TF_LABEL_WIDTH=22

heading() { printf '\n%s%s%s\n\n' "$C_BOLD" "$1" "$C_RESET"; }
info()    { printf '%s\n' "$1"; }
dim()     { printf '%s%s%s\n' "$C_DIM" "$1" "$C_RESET"; }
blank()   { printf '\n'; }

# check_line "Python" "OK"|"FAIL"|"HEALTHY"|... [detail]
# Renders:  Python .............. OK
check_line() {
  local label="$1" state="$2" detail="${3:-}" colour="$C_OK"
  case "$state" in
    OK|HEALTHY|PASS|RUNNING|READY) colour="$C_OK" ;;
    SKIP|PENDING|STOPPED|WARMING)  colour="$C_WARN" ;;
    *)                             colour="$C_FAIL" ;;
  esac
  local dots=""
  local n=$(( TF_LABEL_WIDTH - ${#label} ))
  (( n < 1 )) && n=1
  dots="$(printf '%*s' "$n" '' | tr ' ' '.')"
  printf '  %s %s %s%s%s' "$label" "$dots" "$colour" "$state" "$C_RESET"
  [[ -n "$detail" ]] && printf ' %s(%s)%s' "$C_DIM" "$detail" "$C_RESET"
  printf '\n'
}

# fail_with "what failed" "why" "what to run"
# The three things a diagnosis needs. Never a bare traceback.
fail_with() {
  printf '\n%s  FAILED: %s%s\n' "$C_FAIL$C_BOLD" "$1" "$C_RESET"
  printf '\n  %sWhy:%s  %s\n' "$C_BOLD" "$C_RESET" "$2"
  printf '\n  %sFix:%s  %s\n\n' "$C_BOLD" "$C_RESET" "$3"
  exit 1
}

# --- environment detection ---------------------------------------------------
# Codespaces is disposable and repository-controlled, so setup automates
# aggressively there. A developer's own machine is not, so setup reports
# missing system dependencies rather than changing the host.
detect_environment() {
  if [[ -n "${CODESPACES:-}" ]] || [[ -n "${CODESPACE_NAME:-}" ]]; then
    echo "codespaces"
  elif [[ -n "${REMOTE_CONTAINERS:-}" ]] || [[ -f /.dockerenv ]]; then
    echo "devcontainer"
  elif [[ "$(uname -s)" == "Linux" ]]; then
    echo "linux"
  elif [[ "$(uname -s)" == "Darwin" ]]; then
    echo "macos"
  else
    echo "unsupported"
  fi
}

# True where it is safe to install system-level things without asking.
environment_is_disposable() {
  local env; env="$(detect_environment)"
  [[ "$env" == "codespaces" || "$env" == "devcontainer" ]]
}

# --- tool discovery ----------------------------------------------------------
have() { command -v "$1" >/dev/null 2>&1; }

python_bin() {
  if [[ -n "${TF_PYTHON:-}" ]]; then echo "$TF_PYTHON"; return; fi
  for candidate in python3.13 python3.12 python3.11 python3 python; do
    if have "$candidate" && "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
      echo "$candidate"; return
    fi
  done
  echo ""
}

# Docker Compose v2 (`docker compose`) with a fallback to the v1 binary.
compose_cmd() {
  if docker compose version >/dev/null 2>&1; then
    echo "docker compose"
  elif have docker-compose; then
    echo "docker-compose"
  else
    echo ""
  fi
}

docker_ok()  { have docker && docker info >/dev/null 2>&1; }
compose_ok() { [[ -n "$(compose_cmd)" ]]; }

# --- compose wrapper ---------------------------------------------------------
# Always runs from the repository root with paper mode in the environment.
compose() {
  local cc; cc="$(compose_cmd)"
  [[ -z "$cc" ]] && fail_with \
    "Docker Compose is not available" \
    "Neither 'docker compose' nor 'docker-compose' is on PATH." \
    "In Codespaces, rebuild the container (Command Palette → Codespaces: Rebuild Container). Locally, install Docker Desktop or the compose plugin."
  ( cd "$REPO_ROOT" && TF_MODE=paper $cc "$@" )
}

# --- paper-mode enforcement --------------------------------------------------
# The convenience tooling must not be a route to anything but paper trading.
# Two independent checks: the shell environment, and the .env file compose
# and the app both read.
assert_paper_mode() {
  local requested="${TF_REQUESTED_MODE:-paper}"
  if [[ "${requested,,}" != "paper" ]]; then
    fail_with \
      "TF_MODE is set to '${requested}'" \
      "These scripts start paper trading only. This build contains no exchange order-submission implementation, so a non-paper mode cannot be honoured — and silently ignoring it would be worse than refusing." \
      "unset TF_MODE   # then re-run"
  fi

  if [[ -f "$REPO_ROOT/.env" ]]; then
    local from_file
    from_file="$(grep -E '^[[:space:]]*TF_MODE[[:space:]]*=' "$REPO_ROOT/.env" 2>/dev/null | tail -1 | cut -d= -f2- | tr -d ' "'"'"'' || true)"
    if [[ -n "$from_file" && "${from_file,,}" != "paper" ]]; then
      fail_with \
        ".env sets TF_MODE=${from_file}" \
        "The .env file asks for a mode this build cannot provide. Startup would abort anyway; refusing here says so before anything starts." \
        "Edit ${REPO_ROOT}/.env and set:  TF_MODE=paper"
    fi
  fi
  export TF_MODE=paper
}

# --- health polling ----------------------------------------------------------
#: Where the API listens. Overridable for a non-default port.
TF_API_PORT="${TF_API_PORT:-8080}"
TF_API_URL="http://localhost:${TF_API_PORT}"

# wait_for_http URL TIMEOUT_S — returns 0 once it answers 2xx.
wait_for_http() {
  local url="$1" timeout="${2:-90}" waited=0
  while (( waited < timeout )); do
    if curl -fsS --max-time 3 "$url" >/dev/null 2>&1; then return 0; fi
    sleep 2; waited=$(( waited + 2 ))
  done
  return 1
}

# Reads one top-level string field out of the /health JSON without needing jq.
health_field() {
  local field="$1" body
  body="$(curl -fsS --max-time 5 "${TF_API_URL}/health" 2>/dev/null || true)"
  [[ -z "$body" ]] && return 1
  printf '%s' "$body" \
    | tr ',' '\n' \
    | grep -m1 "\"${field}\"" \
    | sed -E 's/.*"'"${field}"'"[[:space:]]*:[[:space:]]*"?([^",}]*)"?.*/\1/' \
    | tr -d ' '
}

# required_components_status — HEALTHY, or a space-separated list of the
# required components that are not.
#
# The aggregate `status` field covers every component, including optional
# ones. LUMEN is optional and its default provider is NullProvider, which
# always reports unavailable on purpose — so a perfectly good default stack
# aggregates to OFFLINE. Reporting that as a failure would teach a new
# developer to ignore the health line, so the tooling asks the narrower and
# more useful question: can the platform trade?
required_components_status() {
  local body py
  body="$(curl -fsS --max-time 5 "${TF_API_URL}/health" 2>/dev/null || true)"
  [[ -z "$body" ]] && { echo "UNREACHABLE"; return 1; }
  py="$(python_bin)"
  [[ -z "$py" ]] && { echo "UNKNOWN"; return 1; }
  printf '%s' "$body" | ( cd "$REPO_ROOT" && "$py" "${TF_LIB_DIR}/required_health.py" )
}

# compose_health SERVICE — HEALTHY | UNHEALTHY | STARTING | RUNNING | STOPPED
# Uses the container's own healthcheck rather than "the process exists".
compose_health() {
  local service="$1" cid state health
  cid="$(compose ps -q "$service" 2>/dev/null | head -1 || true)"
  [[ -z "$cid" ]] && { echo "STOPPED"; return; }
  state="$(docker inspect -f '{{.State.Status}}' "$cid" 2>/dev/null || echo unknown)"
  health="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{end}}' "$cid" 2>/dev/null || true)"
  if [[ -n "$health" ]]; then
    case "$health" in
      healthy)   echo "HEALTHY" ;;
      starting)  echo "STARTING" ;;
      *)         echo "UNHEALTHY" ;;
    esac
  elif [[ "$state" == "running" ]]; then
    echo "RUNNING"
  else
    echo "STOPPED"
  fi
}

# wait_for_compose_health SERVICE TIMEOUT_S
wait_for_compose_health() {
  local service="$1" timeout="${2:-120}" waited=0 state
  while (( waited < timeout )); do
    state="$(compose_health "$service")"
    case "$state" in
      HEALTHY|RUNNING) return 0 ;;
      STOPPED)         : ;;
    esac
    sleep 2; waited=$(( waited + 2 ))
  done
  return 1
}

# running_service_count — how many compose services are actually up.
#
# `compose ps --services --filter status=running` prints one empty line when
# nothing is running, so piping it straight into `wc -l` yields 1 and every
# "is anything up?" check silently believes a service exists. Blank lines are
# dropped here, once, rather than in each caller.
running_service_count() {
  docker_ok || { echo 0; return; }
  compose ps --services --filter status=running 2>/dev/null | grep -c '[^[:space:]]' || true
}

# --- forwarded URLs ----------------------------------------------------------
# In Codespaces localhost is not the address a browser can reach, so print the
# forwarded host too rather than a URL that will not open.
public_url() {
  local port="$1"
  if [[ -n "${CODESPACE_NAME:-}" && -n "${GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN:-}" ]]; then
    echo "https://${CODESPACE_NAME}-${port}.${GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN}"
  else
    echo "http://localhost:${port}"
  fi
}

# --- misc --------------------------------------------------------------------
paper_balance() {
  local py; py="$(python_bin)"
  [[ -z "$py" ]] && { echo "unknown"; return; }
  ( cd "$REPO_ROOT" && TF_MODE=paper "$py" -c \
      'from core.config import load_settings; print(f"{load_settings().paper_initial_balance:,.2f}")' \
      2>/dev/null || echo "unknown" )
}

require_repo_root() {
  [[ -f "$REPO_ROOT/pyproject.toml" ]] || fail_with \
    "Could not locate the repository root" \
    "Expected pyproject.toml at ${REPO_ROOT}." \
    "Run the script from inside a clone of the repository."
}
