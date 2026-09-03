#!/usr/bin/env bash
# Prepare a development environment for the trading floor.
#
# Wired to the devcontainer's postCreateCommand, and safe to run by hand at
# any time: every step checks before it acts, so a second run reports the
# same state rather than duplicating or damaging anything.
#
# It never writes secrets, never overwrites an existing .env, and never
# modifies a machine it does not own — on a developer's own Linux or macOS
# box a missing system dependency is reported, not installed.

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"
require_repo_root
cd "$REPO_ROOT"

ENVIRONMENT="$(detect_environment)"
WARNINGS=0
warn() { printf '  %s! %s%s\n' "$C_WARN" "$1" "$C_RESET"; WARNINGS=$(( WARNINGS + 1 )); }

heading "Trading Floor — environment setup"

# --- 1. environment ----------------------------------------------------------
case "$ENVIRONMENT" in
  codespaces)  check_line "Environment" "OK" "GitHub Codespaces" ;;
  devcontainer) check_line "Environment" "OK" "dev container" ;;
  linux)       check_line "Environment" "OK" "local Linux" ;;
  macos)       check_line "Environment" "OK" "local macOS" ;;
  *)
    check_line "Environment" "UNKNOWN" "$(uname -s)"
    warn "Unrecognised platform — nothing will be installed system-wide."
    ;;
esac
check_line "Repository" "OK" "$REPO_ROOT"

# --- 2. python ---------------------------------------------------------------
PY="$(python_bin)"
if [[ -z "$PY" ]]; then
  fail_with \
    "No suitable Python found" \
    "This project requires Python 3.11 or newer (pyproject.toml: requires-python >= 3.11). Nothing on PATH satisfies that." \
    "In Codespaces, rebuild the container (Command Palette → Codespaces: Rebuild Container). Locally, install Python 3.11+ and re-run: bash scripts/setup-codespace.sh"
fi
PY_VERSION="$("$PY" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])')"
check_line "Python" "OK" "$PY_VERSION"

# --- 3. project dependencies -------------------------------------------------
# Installed from pyproject.toml — the project's own declared source — with the
# extras the stack actually reaches: postgres for the event store,
# intelligence for LUMEN, dev for the test tooling.
printf '  %sInstalling project dependencies (this takes a minute the first time)...%s\n' "$C_DIM" "$C_RESET"
INSTALL_LOG="$(mktemp)"
if "$PY" -m pip install --quiet --disable-pip-version-check \
      -e ".[dev,postgres,intelligence]" >"$INSTALL_LOG" 2>&1; then
  check_line "Dependencies" "OK" "pyproject.toml [dev,postgres,intelligence]"
else
  printf '\n%s\n' "$(tail -25 "$INSTALL_LOG")"
  fail_with \
    "Installing project dependencies failed" \
    "pip could not install the project. The last 25 lines of its output are above." \
    "Check network access, then re-run:  ${PY} -m pip install -e '.[dev,postgres,intelligence]'"
fi
rm -f "$INSTALL_LOG"

# --- 4. imports --------------------------------------------------------------
# Importing is the real check: a package can install and still be unusable.
MISSING=""
for module in pydantic fastapi uvicorn websockets httpx redis asyncpg anthropic pytest ruff; do
  "$PY" -c "import ${module}" 2>/dev/null || MISSING="${MISSING} ${module}"
done
if [[ -z "$MISSING" ]]; then
  check_line "Imports" "OK" "10 packages"
else
  check_line "Imports" "FAIL" "missing:${MISSING}"
  fail_with \
    "Some dependencies did not import" \
    "Installed but not importable:${MISSING}" \
    "${PY} -m pip install -e '.[dev,postgres,intelligence]' --force-reinstall"
fi

# The project's own package must import too, or nothing else will work.
if ( cd "$REPO_ROOT" && TF_MODE=paper "$PY" -c 'import core, agents, execution, storage' 2>/dev/null ); then
  check_line "Project package" "OK"
else
  check_line "Project package" "FAIL"
  fail_with \
    "The trading floor package does not import" \
    "'import core' failed from the repository root." \
    "cd ${REPO_ROOT} && ${PY} -m pip install -e '.[dev,postgres,intelligence]'"
fi

# --- 5. docker ---------------------------------------------------------------
# Reported, never installed by this script. In Codespaces the docker-in-docker
# dev container feature provides it; on a developer machine, installing Docker
# is the developer's decision, not a setup script's.
if docker_ok; then
  check_line "Docker" "OK" "$(docker version --format '{{.Server.Version}}' 2>/dev/null || echo 'daemon up')"
elif have docker; then
  check_line "Docker" "FAIL" "CLI present, daemon unreachable"
  if environment_is_disposable; then
    warn "The Docker daemon is not responding. Rebuild the container: Command Palette → Codespaces: Rebuild Container."
  else
    warn "Docker is installed but the daemon is not running. Start Docker Desktop (macOS) or: sudo systemctl start docker (Linux)."
  fi
else
  check_line "Docker" "FAIL" "not installed"
  if environment_is_disposable; then
    warn "Docker is missing from this container. The docker-in-docker feature in .devcontainer/devcontainer.json should provide it — rebuild the container."
  else
    warn "Docker is not installed. Install Docker Desktop (macOS/Windows) or Docker Engine (Linux). This script will not modify your machine."
  fi
fi

if compose_ok; then
  check_line "Docker Compose" "OK" "$(compose version --short 2>/dev/null || echo v2)"
else
  check_line "Docker Compose" "FAIL" "not available"
  warn "Docker Compose is unavailable; ./start-paper.sh cannot start the stack without it."
fi

# --- 6. .env -----------------------------------------------------------------
# Copied from the committed example only when absent. Never overwritten, and
# never populated with generated credentials — the example contains no secrets
# and the one real credential (the database password) belongs to a local
# development container.
if [[ -f "$REPO_ROOT/.env" ]]; then
  check_line ".env" "OK" "already present, left untouched"
elif [[ -f "$REPO_ROOT/.env.example" ]]; then
  cp "$REPO_ROOT/.env.example" "$REPO_ROOT/.env"
  check_line ".env" "OK" "created from .env.example"
else
  check_line ".env" "SKIP" "no .env.example to copy"
fi

# --- 7. directories ----------------------------------------------------------
mkdir -p "$REPO_ROOT/data"
check_line "Data directory" "OK" "./data"

# --- 8. executable bits ------------------------------------------------------
# Git preserves these, but a fresh checkout on a filesystem that does not can
# leave the entrypoints unrunnable, which reads as "the script is broken".
chmod +x "$REPO_ROOT"/scripts/*.sh "$REPO_ROOT"/trading-floor 2>/dev/null || true
chmod +x "$REPO_ROOT"/*.sh 2>/dev/null || true
check_line "Entrypoints" "OK" "executable"

# --- 9. sanity checks --------------------------------------------------------
# Cheap, offline, and they exercise the things a broken environment breaks
# first: configuration loading and the paper boundary.
if ( cd "$REPO_ROOT" && TF_MODE=paper "$PY" -c \
      'from core.config import load_settings; s = load_settings(); assert s.mode.value == "PAPER"' 2>/dev/null ); then
  check_line "Configuration" "OK" "loads, mode=PAPER"
else
  check_line "Configuration" "FAIL"
  fail_with \
    "The configuration could not be loaded" \
    "load_settings() raised. Most often this is an invalid value in .env — the error names the variable." \
    "cd ${REPO_ROOT} && ${PY} -c 'from core.config import load_settings; load_settings()'"
fi

if ( cd "$REPO_ROOT" && TF_MODE=live "$PY" -c 'from core.config import load_settings; load_settings()' >/dev/null 2>&1 ); then
  fail_with \
    "The paper-mode boundary is not being enforced" \
    "TF_MODE=live was accepted. That must never happen: this build has no exchange order-submission implementation, and silently accepting the request would misrepresent what it does." \
    "Do not use this environment. Re-run the test suite: ${PY} -m pytest tests/contract/test_config_validation.py -q"
fi
check_line "Paper boundary" "OK" "non-paper modes rejected"

if ( cd "$REPO_ROOT" && "$PY" -m pytest tests/unit -q -x --no-header -p no:cacheprovider >/dev/null 2>&1 ); then
  check_line "Unit tests" "OK" "fast suite passes"
else
  check_line "Unit tests" "FAIL"
  warn "The fast unit suite did not pass. Diagnose with: ${PY} -m pytest tests/unit -q"
fi

# --- report ------------------------------------------------------------------
blank
if (( WARNINGS == 0 )); then
  printf '%s  Setup complete.%s\n\n' "$C_OK$C_BOLD" "$C_RESET"
  info "  Next:  ./start-paper.sh"
else
  printf '%s  Setup finished with %d warning(s).%s\n\n' "$C_WARN$C_BOLD" "$WARNINGS" "$C_RESET"
  info "  Python tooling is ready. Resolve the warnings above before ./start-paper.sh,"
  info "  which needs Docker to bring up PostgreSQL, Redis and the application."
fi
blank
