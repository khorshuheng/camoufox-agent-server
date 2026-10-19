# pi-camoufox — developer workflow
#
# `make` or `make help` lists every target. All Python work goes through uv.

SHELL := /bin/bash
.DEFAULT_GOAL := help

UV        ?= uv
HTTP_PORT ?= 8787
HOST      ?= 127.0.0.1
PROFILE   ?= $(HOME)/.camoufox-agent/profile
SETTLE_MS ?= 250
PY        := $(UV) run

# --- meta ---------------------------------------------------------------------

.PHONY: help
help: ## Show this help
	@awk 'BEGIN {FS = ":.*?## "; printf "\n\033[1mpi-camoufox\033[0m\n\n"} \
		/^[a-zA-Z_-]+:.*?## / {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2} \
		/^##@/ {printf "\n\033[1m%s\033[0m\n", substr($$0, 5)}' $(MAKEFILE_LIST)
	@echo ""

##@ Setup

.PHONY: install
install: ## Install dependencies into .venv (uv sync)
	@$(UV) sync

.PHONY: deps
deps: install ## Alias for install

.PHONY: browser
browser: ## Download the Camoufox browser build (uv run camoufox fetch)
	@$(PY) camoufox fetch

.PHONY: doctor
doctor: ## Show uv, Python, Camoufox and active browser state
	@echo "uv:       $$($(UV) --version)"
	@echo "python:   $$($(PY) python --version 2>&1)"
	@echo "ruff:     $$($(PY) ruff --version)"
	@$(PY) camoufox version

##@ Quality

.PHONY: lint
lint: ## Lint Python with ruff
	@$(PY) ruff check src tests examples scripts

.PHONY: lint-fix
lint-fix: ## Lint and apply safe fixes
	@$(PY) ruff check src tests examples scripts --fix

.PHONY: format
format: ## Format Python with ruff
	@$(PY) ruff format src tests examples scripts

.PHONY: format-check
format-check: ## Verify formatting without writing
	@$(PY) ruff format --check src tests examples scripts

.PHONY: syntax
syntax: ## Byte-compile every Python entry point
	@$(PY) python -m compileall -q src examples tests scripts
	@echo "syntax ok"

.PHONY: check
check: lint format-check syntax ## Run all static checks

##@ Test

.PHONY: test
test: ## Run the hermetic browser test suite headless
	@$(PY) pytest

.PHONY: test-headed
test-headed: ## Run the suite with a visible browser window
	@SMOKE_HEADED=1 $(PY) pytest

.PHONY: test-verbose
test-verbose: ## Run tests with verbose output
	@$(PY) pytest -vv -s

##@ Run

.PHONY: start
start: ## Start the server with a headed browser (default)
	@$(PY) pi-camoufox --http-port $(HTTP_PORT) --host $(HOST) \
		--profile "$(PROFILE)" --settle-ms $(SETTLE_MS)

.PHONY: start-headless
start-headless: ## Start the server without a window
	@$(PY) pi-camoufox --http-port $(HTTP_PORT) --host $(HOST) \
		--profile "$(PROFILE)" --headless --settle-ms $(SETTLE_MS)

.PHONY: start-virtual
start-virtual: ## Start a headed browser on a private Xvfb display (Linux)
	@$(PY) pi-camoufox --http-port $(HTTP_PORT) --host $(HOST) \
		--profile "$(PROFILE)" --virtual-display --settle-ms $(SETTLE_MS)

.PHONY: warm
warm: ## Visit common sites to warm the persistent profile (cookies/history)
	@$(PY) python scripts/warm_profile.py http://127.0.0.1:$(HTTP_PORT)

.PHONY: demo
demo: ## Run the example agent loop against a running server
	@$(PY) python examples/agent_loop.py http://127.0.0.1:$(HTTP_PORT)

.PHONY: health
health: ## Print /health from a running server
	@curl -sS http://127.0.0.1:$(HTTP_PORT)/health | $(PY) python -m json.tool

.PHONY: actions
actions: ## Print the action vocabulary
	@curl -sS http://127.0.0.1:$(HTTP_PORT)/actions | $(PY) python -m json.tool

##@ Housekeeping

.PHONY: clean
clean: ## Remove caches and transient files
	@rm -rf .pytest_cache .ruff_cache
	@find . -type d -name __pycache__ -not -path './.venv/*' -prune -exec rm -rf {} +
	@rm -f *.log
	@echo "cleaned"

.PHONY: clean-profile
clean-profile: ## Remove the browser profile (logs you out of everything)
	@rm -rf "$(PROFILE)"
	@echo "removed profile $(PROFILE)"

.PHONY: kill
kill: ## Stop the server listening on HTTP_PORT (never leaves processes behind)
	@-lsof -ti tcp:$(HTTP_PORT) | xargs kill 2>/dev/null || true
	@echo "stopped listener on $(HTTP_PORT)"

.PHONY: reset
reset: kill clean install ## Stop, clean caches, reinstall
	@echo "reset complete"

.PHONY: ci
ci: install check test ## What CI should run
