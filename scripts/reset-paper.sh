#!/usr/bin/env bash
# Erase the paper-trading development data and start clean.
#
# The only script in this repository that deletes anything. It stops the
# stack, removes the Docker volumes holding the event store and the Redis
# append-only file, and leaves the environment ready for a fresh start —
# the volumes are recreated empty the next time ./start-paper.sh runs.
#
# It says exactly what will be destroyed and asks first, because the thing
# being deleted is a testing session's entire history and there is no undo.
# `--yes` skips the question for scripted use.

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"
require_repo_root
cd "$REPO_ROOT"

# Paper mode is asserted even here. Nothing in this repository runs without
# it, and a destructive command is the last place to make an exception.
assert_paper_mode

ASSUME_YES=0
for arg in "$@"; do
  case "$arg" in
    -y|--yes) ASSUME_YES=1 ;;
    -h|--help)
      printf '\nUsage: ./reset-paper.sh [--yes]\n\n'
      printf '  Stops the stack and DELETES all paper-session data:\n'
      printf '    · the PostgreSQL event store (recorded sessions, event history)\n'
      printf '    · paper orders and fills\n'
      printf '    · replay data read back from those events\n'
      printf '    · the Redis append-only file\n\n'
      printf '  --yes   do not ask for confirmation (for scripts)\n\n'
      printf '  To stop without deleting anything:  ./stop-paper.sh\n\n'
      exit 0 ;;
    *)
      fail_with "Unknown option: ${arg}" \
        "./reset-paper.sh takes no arguments except --yes." \
        "./reset-paper.sh --help" ;;
  esac
done

heading "Trading Floor — RESET"

if ! docker_ok; then
  fail_with "The Docker daemon is not running" \
    "The data lives in Docker volumes, which cannot be removed while the daemon is down." \
    "Start Docker Desktop (macOS/Windows), or on Linux:  sudo systemctl start docker"
fi

# --- say precisely what is about to be lost ----------------------------------
PRESENT="$(data_volumes_present)"
if [[ "$PRESENT" -eq 0 ]]; then
  check_line "Data volumes" "OK" "none exist — nothing to erase"
  blank
  info "  Nothing to do. The next ./start-paper.sh will begin from empty."
  blank
  exit 0
fi

printf '  %sThis will permanently delete your paper-session data.%s\n\n' "$C_FAIL$C_BOLD" "$C_RESET"
info "  To be erased:"
blank
info "    · PostgreSQL event store — every recorded session and its event history"
info "    · paper orders and fills"
info "    · replay data, which is read back from those events"
info "    · the Redis append-only file"
blank
info "  Docker volumes:"
while read -r name; do
  if docker volume inspect "$name" >/dev/null 2>&1; then
    printf '    · %s\n' "$name"
  fi
done < <(data_volume_names)
blank
dim "  There is no undo. To stop without deleting anything, run ./stop-paper.sh"
blank

# --- confirm -----------------------------------------------------------------
if (( ! ASSUME_YES )); then
  if [[ ! -t 0 ]]; then
    # Non-interactive and no --yes: refusing is the only safe reading. A
    # pipeline that meant to erase can say so explicitly.
    fail_with "Refusing to erase data without confirmation" \
      "This is not an interactive terminal, so the confirmation question cannot be asked and cannot be assumed answered." \
      "./reset-paper.sh --yes    # if you really mean it"
  fi
  printf '  %sType%s reset %sto confirm, or anything else to cancel: %s' \
    "$C_BOLD" "$C_RESET$C_BOLD" "$C_RESET" "$C_RESET"
  read -r REPLY || REPLY=""
  if [[ "$REPLY" != "reset" ]]; then
    blank
    info "  Cancelled. Nothing was deleted."
    blank
    exit 1
  fi
  blank
fi

# --- stop, then erase --------------------------------------------------------
info "Stopping the stack..."
compose down --remove-orphans >/dev/null 2>&1 || true
check_line "Stack" "STOPPED"

# `compose down --volumes` only removes volumes compose still knows about, so
# each is removed by name as well. Both paths are tolerant of an absent
# volume, which is what makes a repeated reset harmless.
compose down --volumes --remove-orphans >/dev/null 2>&1 || true

REMOVED=0
while read -r name; do
  if docker volume inspect "$name" >/dev/null 2>&1; then
    if docker volume rm "$name" >/dev/null 2>&1; then
      REMOVED=$(( REMOVED + 1 ))
    fi
  else
    REMOVED=$(( REMOVED + 1 ))
  fi
done < <(data_volume_names)

REMAINING="$(data_volumes_present)"
if [[ "$REMAINING" -eq 0 ]]; then
  check_line "Data volumes" "OK" "removed"
else
  check_line "Data volumes" "FAIL" "${REMAINING} still present"
  fail_with "Some volumes could not be removed" \
    "Docker refused to delete them, usually because a container outside this stack is still using one." \
    "docker ps -a                      # find what is holding them
       docker volume ls                  # list the volumes"
fi

# The local SQLite file is a separate store, used when the stack is not
# running. It is developer data too, so it goes with the rest — but only
# here, never on stop.
if [[ -f "$REPO_ROOT/data/trading_floor.db" ]]; then
  rm -f "$REPO_ROOT"/data/trading_floor.db*
  check_line "Local SQLite store" "OK" "removed"
fi

blank
printf '%s  Reset complete.%s\n\n' "$C_OK$C_BOLD" "$C_RESET"
info "  The next start creates empty volumes and applies the migration afresh."
blank
info "  Start clean:  ./start-paper.sh"
blank
