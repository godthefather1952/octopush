#!/usr/bin/env bash
# Start a SHADOW rehearsal: real public market data, simulated execution.
#
# This script starts the SAME stack ./start-paper.sh starts, with two
# environment variables set:
#
#   TF_PROFILE=shadow   what the session is for
#   TF_FEED=live        public, read-only exchange feeds
#
# It does NOT set TF_MODE, and it cannot. Paper mode is enforced by
# assert_paper_mode() in scripts/lib/common.sh, which start-paper.sh calls
# before it touches the machine, and which refuses any other value from either
# the shell environment or .env. There is no flag, argument or variable here
# that changes that — this file adds a profile and a feed, and nothing else.
#
# WHAT A SHADOW SESSION IS
#   The whole platform — market data, strategy, agents, consensus, RUNE, VESKA
#   planning, PaperExecutor, OKAPI, MARIN — running against real public prices,
#   with extra observation recorded. It answers "what would this have done in
#   actual market conditions?".
#
# WHAT IT IS NOT
#   It is not live trading, and there is nothing in this build that could make
#   it live trading. Execution is PaperExecutor against PaperAccount. No order
#   reaches a venue, no authenticated endpoint is contacted, and no credential
#   exists to contact one with.

set -euo pipefail

TF_SHADOW_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# The profile and the feed. TF_MODE is deliberately absent: start-paper.sh
# asserts it, and setting it here — even to "paper" — would suggest this
# script has an opinion about a value it must never be able to influence.
export TF_PROFILE=shadow
export TF_FEED=live

if [[ -t 1 ]] && [[ "${NO_COLOR:-}" == "" ]] && [[ "${TERM:-dumb}" != "dumb" ]]; then
  SH_BOLD=$'\033[1m'; SH_WARN=$'\033[33m'; SH_INFO=$'\033[36m'; SH_RESET=$'\033[0m'
else
  SH_BOLD=""; SH_WARN=""; SH_INFO=""; SH_RESET=""
fi

printf '\n%sTrading Floor — SHADOW PROFILE%s\n\n' "$SH_BOLD" "$SH_RESET"
printf '  %sPUBLIC LIVE MARKET DATA%s   read-only exchange feeds\n' "$SH_INFO" "$SH_RESET"
printf '  %sPAPER EXECUTION ONLY%s      PaperExecutor, simulated fills\n' "$SH_INFO" "$SH_RESET"
printf '  %sNO REAL ORDERS%s            nothing reaches a venue\n' "$SH_WARN" "$SH_RESET"
printf '\n'
printf '  Trading mode ......... PAPER   (enforced; not settable here)\n'
printf '  Operational profile .. SHADOW\n'
printf '  Market feed .......... live    (public, read-only)\n'
printf '\n'
printf '  %sA shadow session rehearses what this platform would have done.%s\n' "$SH_BOLD" "$SH_RESET"
printf '  Its fills are a simulator'"'"'s estimate, not a venue'"'"'s report.\n'
printf '\n'

# Everything else — dependency checks, paper-mode enforcement, compose, health
# polling, reporting — is start-paper.sh's, unchanged. Duplicating it here
# would create a second launcher to keep in step with the first.
exec bash "${TF_SHADOW_DIR}/start-paper.sh" "$@"
