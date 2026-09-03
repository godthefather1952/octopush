#!/usr/bin/env bash
# Stop the paper-trading stack. Never deletes data.
#
# This script has no destructive mode — not a flag, not an argument. Stopping
# and erasing are different intentions, and putting them behind the same
# command means one mistyped word costs a testing session. Everything a
# session produced survives: the event store, recorded sessions, event
# history, paper orders and fills, and the replay data read back from them.
#
# To delete that deliberately, there is ./reset-paper.sh, which says what it
# will destroy and asks first.
#
# Stopping an already-stopped stack is not an error: "make sure it is off" is
# a reasonable thing to ask twice.

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"
require_repo_root
cd "$REPO_ROOT"

for arg in "$@"; do
  case "$arg" in
    -h|--help)
      printf '\nUsage: ./stop-paper.sh\n\n'
      printf '  Stops the stack. Takes no arguments.\n'
      printf '  Your recorded sessions and event history are preserved.\n\n'
      printf '  To erase them deliberately:  ./reset-paper.sh\n\n'
      exit 0 ;;
    -v|--volumes|--wipe|--clean)
      # This used to be an option here. Refusing by name is friendlier than
      # "unknown option" to anyone who learned the old spelling.
      fail_with "./stop-paper.sh no longer deletes data" \
        "Stopping and erasing are separate commands now, so that stopping can never cost you a testing session by accident." \
        "./stop-paper.sh      # stop, keeping everything
       ./reset-paper.sh     # erase deliberately, with confirmation" ;;
    *)
      fail_with "Unknown option: ${arg}" \
        "./stop-paper.sh takes no arguments." \
        "./stop-paper.sh --help" ;;
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
  info "  Nothing to do. Your data is untouched."
  blank
  exit 0
fi

# No --volumes. Containers go, named volumes stay.
compose down --remove-orphans

blank
check_line "Stack" "STOPPED"
check_line "Your data" "OK" "preserved"

blank
info "  Recorded sessions, event history, orders and fills are all still there."
info "  Starting again continues from where you left off."
blank
info "  Start again:  ./start-paper.sh"
dim "  Erase data:   ./reset-paper.sh   (asks first)"
blank
