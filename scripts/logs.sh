#!/usr/bin/env bash
# Follow the stack's logs.
#
#   ./logs.sh                  everything, followed
#   ./logs.sh trading-floor    one service
#   ./logs.sh --no-follow      the last 200 lines and exit

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"
require_repo_root
cd "$REPO_ROOT"

FOLLOW="--follow"
SERVICES=()
for arg in "$@"; do
  case "$arg" in
    --no-follow|-n) FOLLOW="" ;;
    -h|--help)
      printf 'Usage: ./logs.sh [--no-follow] [service ...]\n\n'
      printf '  Services: postgres  redis  trading-floor\n\n'
      exit 0 ;;
    *) SERVICES+=("$arg") ;;
  esac
done

if ! docker_ok; then
  fail_with "The Docker daemon is not running" \
    "There are no container logs to read because nothing is running." \
    "Start Docker, then:  ./start-paper.sh"
fi

if [[ "$(running_service_count)" -eq 0 ]]; then
  heading "Trading Floor — logs"
  check_line "Stack" "STOPPED"
  blank
  info "  Nothing is running, so there are no live logs."
  blank
  info "  Start it with:  ./start-paper.sh"
  info "  Logs from the last run:  ./logs.sh --no-follow"
  blank
  [[ -n "$FOLLOW" ]] && exit 0
fi

[[ -n "$FOLLOW" ]] && dim "  Following logs — press Ctrl-C to stop." && blank

# shellcheck disable=SC2086
compose logs --tail=200 $FOLLOW "${SERVICES[@]}"
