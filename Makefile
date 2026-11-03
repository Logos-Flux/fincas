help: ## Show this help message
	@echo "Fincas Developer Commands:"
	@grep -E '^[a-zA-Z0-9_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

# --- Docker Environment Commands ---

up: ## Start all development containers in background (PostgreSQL, Valkey, Web)
	docker compose up -d

down: ## Stop all development containers
	docker compose down

restart: down up ## Restart development containers

logs: ## Tail web service logs
	docker compose logs -f web

build: ## Build / rebuild container images
	docker compose build

compose-migrate: ## Run migrations inside docker container
	docker compose exec web python manage.py migrate

compose-seed: ## Seed demo data inside docker container
	docker compose exec web python manage.py seed_demo_data

compose-shell: ## Open Django shell inside docker container
	docker compose exec web python manage.py shell

PYTHON ?= $(if $(wildcard .venv/bin/python),.venv/bin/python,python3)

# --- Local Python Environment Commands ---

venv-setup: ## Install dependencies into active python environment
	pip install -r requirements/dev.txt

.PHONY: help up down restart logs build compose-migrate compose-seed compose-shell migrate makemigrations test check seed shell tripwire venv-setup lint run package-check r12 storefront-install storefront-dev storefront-build harness-setup harness harness-synth harness-storefront harness-clean findings-check

run: ## Start local Django development server on port 8000
	$(PYTHON) manage.py runserver 0.0.0.0:8000

migrate: ## Run local migrations (SQLite by default, or DATABASE_URL if set)
	$(PYTHON) manage.py migrate

makemigrations: ## Detect model changes and generate migrations
	$(PYTHON) manage.py makemigrations

check: findings-check ## Run Django system checks and verify migrations (plus the findings log in the dev repo)
	$(PYTHON) manage.py check
	$(PYTHON) manage.py makemigrations --check

findings-check: ## Validate docs/findings-log.md (dev repo only; skipped where the log is absent)
	@if [ -f docs/findings-log.md ]; then $(PYTHON) -I scripts/findings_check.py; \
	else echo "findings-check: docs/findings-log.md not present, skipped"; fi

test: ## Run full test suite with test settings
	$(PYTHON) manage.py test --settings=config.settings.test

seed: ## Seed rich generic demonstration data
	$(PYTHON) manage.py seed_demo_data

shell: ## Open interactive Django shell
	$(PYTHON) manage.py shell

tripwire: ## Run repository isolation tripwire check
	bash scripts/tripwire.sh

lint: ## Run code style and linting checks
	$(PYTHON) -m ruff check .

package-check: ## Build the wheel, install it in a fresh venv, run a downstream-style project on it
	PYTHON=$(PYTHON) bash scripts/check_package_install.sh

r12: ## Journey R12: docs/getting-started.md on a clean dir, timed, to a first invoice (R12_FLAGS="--db sqlite")
	bash scripts/release/r12_fresh_install.sh $(R12_FLAGS)

release-tree-check: ## Every working document is listed in scripts/release/dev-only.txt (CI gate)
	bash scripts/release/public_tree.sh --check

release-tree: ## Build the squashed public release branch: make release-tree VERSION=0.4.0 [FROM=ref]
	bash scripts/release/public_tree.sh --build $(VERSION) $(if $(FROM),--from $(FROM),)

storefront-install: ## Install storefront frontend dependencies
	cd storefront && bun install

storefront-dev: ## Run Qwik City storefront in development mode
	cd storefront && bun run dev

storefront-build: ## Build production storefront bundle
	cd storefront && bun run build


# --- Release harness (docs/release-testing.md) ---
# Flags are the interface of scripts/harness/run.sh; these variables are the
# make-side spelling: JOURNEY=R1 STEP=n FROM_STEP=n DB=<name> KEEP_DB=1
# HARNESS_RETRIES=0 SYNTH_DB=golden_synth PROVENANCE_DIR=<scramble scripts dir>.
HARNESS_FLAGS = $(if $(JOURNEY),--journey $(JOURNEY)) $(if $(STEP),--step $(STEP)) $(if $(FROM_STEP),--from-step $(FROM_STEP)) \
	$(if $(DB),--db $(DB)) $(if $(KEEP_DB),--keep-db) $(if $(HARNESS_RETRIES),--retries $(HARNESS_RETRIES))

harness-setup: ## Install what the harness needs (venv deps, Playwright + Chromium, bun install); idempotent
	@test -f config/settings/harness.py || { echo "config/settings/harness.py is missing; see docs/release-testing.md"; exit 2; }
	$(PYTHON) -m pip install -q -r requirements/dev.txt
	cd storefront && bun install --frozen-lockfile && bunx playwright install chromium

harness: ## Run the back-office journeys on a fresh seed (JOURNEY=R1 STEP=n DB=<name> KEEP_DB=1 HARNESS_RETRIES=0)
	PYTHON=$(PYTHON) bash scripts/harness/run.sh $(HARNESS_FLAGS)

harness-synth: ## Clone SYNTH_DB (pg_dump|pg_restore), overlay the role logins, run the journeys, drop the clone
	@test -n "$(SYNTH_DB)" || { echo "set SYNTH_DB=<source database>, e.g. golden_synth (docs/release-testing.md)"; exit 2; }
	PYTHON=$(PYTHON) bash scripts/harness/run.sh --synth $(SYNTH_DB) $(if $(PROVENANCE_DIR),--provenance-dir $(PROVENANCE_DIR)) $(HARNESS_FLAGS)

harness-storefront: ## Run R6 against a built storefront on a fixed port (not built yet; R12 is `make r12`)
	@echo "harness-storefront is not built yet: R6 comes with phase 1 (docs/release-testing.md); R12 is 'make r12'"; exit 2

harness-clean: ## Drop every golden_harness_a_* database left by failed or --keep-db runs
	PYTHON=$(PYTHON) bash scripts/harness/run.sh --cleanup
