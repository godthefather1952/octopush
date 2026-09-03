#!/usr/bin/env bash
# Stop the paper-trading stack.
#
# Stopping an already-stopped stack is not an error: it says so and exits 0,
# because "make sure it is off" is a reasonable thing to ask twice.
#
# Recorded events survive by default. The named volumes hold the event store,
# which is the audit trail of every paper session — throwing it away needs to
# be asked for explicitly, with --volumes.

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"
require_repo_root
cd "$REPO_ROOT"

WIPE=0
for arg in "$@"; do
  case "$arg" in
    -v|--volumes) WIPE=1 ;;
    -h|--help)
      printf 'Usage: ./stop-paper.sh [--volumes]\n\n'
      printf '  --volumes   also delete the PostgreSQL and Redis volumes.\n'
      printf '              This erases recorded sessions. Not the default.\n\n'
      exit 0 ;;
    *)
      fail_with "Unknown option: ${arg}" \
        "./stop-paper.sh takes no arguments except --volumes." \
        "./stop-paper.sh --help"
      ;;
  esac
done

heading "Trading Floor — stopping"

if ! docker_ok; then
  check_line "Docker" "SKIP" "daemon not running"
  blank
  info "  Nothing to stop — the Docker daemon is not running, so the stack is not up."
  blank
  exit 0
fi

if [[ "$(running_service_count)" -eq 0 ]]; then
  check_line "Stack" "STOPPED" "already stopped"
  blank
  info "  Nothing to do."
  blank
  exit 0
fi

if (( WIPE )); then
  printf '  %sRemoving containers and volumes — recorded sessions will be erased.%s\n\n' "$C_WARN" "$C_RESET"
  compose down --volumes --remove-orphans
  blank
  check_line "Stack" "STOPPED" "volumes removed"
else
  compose down --remove-orphans
  blank
  check_line "Stack" "STOPPED" "volumes kept"
  dim "  Recorded sessions were preserved. Use ./stop-paper.sh --volumes to erase them."
fi

blank
info "  Start again with:  ./start-paper.sh"
blank
