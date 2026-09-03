# Developer workflow for the trading floor.
#
# Every target delegates to the script of the same name in scripts/, so there
# is one implementation and three ways to reach it: make, ./trading-floor, or
# the script directly. All of them run in paper mode.

.PHONY: help setup paper start stop restart reset status logs test verify lint typecheck clean

.DEFAULT_GOAL := help

help:  ## Show this help
	@echo ""
	@echo "  Trading Floor — paper mode"
	@echo ""
	@grep -E '^[a-z-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "    make %-10s %s\n", $$1, $$2}'
	@echo ""
	@echo "  Equivalent:  ./trading-floor <command>   or   ./start-paper.sh"
	@echo ""

setup:  ## Install dependencies and check the environment
	@bash scripts/setup-codespace.sh

paper:  ## Start the paper-trading stack
	@bash scripts/start-paper.sh

start: paper  ## Alias for 'make paper'

stop:  ## Stop everything — your data is preserved
	@bash scripts/stop-paper.sh

reset:  ## DELETE all paper-session data, then leave it clean (asks first)
	@bash scripts/reset-paper.sh

restart:  ## Stop, then start
	@bash scripts/stop-paper.sh
	@bash scripts/start-paper.sh

status:  ## What each service is doing right now
	@bash scripts/status.sh

logs:  ## Follow the logs
	@bash scripts/logs.sh

test:  ## Run the test suite
	@bash scripts/test.sh

verify:  ## Run the Phase 0 Docker infrastructure verification
	@bash scripts/verify-phase0.sh

lint:  ## Run ruff
	@python3 -m ruff check .

typecheck:  ## Run mypy on core/, which is gated at zero errors
	@python3 -m mypy --ignore-missing-imports core

clean:  ## Remove build artefacts and verification logs (keeps recorded sessions)
	@rm -rf .verify-logs .pytest_cache .ruff_cache
	@find . -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
	@echo "  Cleaned. Recorded sessions in ./data and the Docker volumes were kept."
