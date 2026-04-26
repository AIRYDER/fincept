# Fincept Terminal — top-level developer commands.
# All recipes are POSIX/bash. On Windows use WSL2, Git Bash, or a make port.

SHELL := bash
.SHELLFLAGS := -eu -o pipefail -c
.DEFAULT_GOAL := help
.PHONY: help dev stop restart logs status test test-cov lint format typecheck \
        build clean nuke install-hooks db-shell redis-cli env

PYTHON_DIRS := libs services

## --- Bootstrap -------------------------------------------------------------

help: ## Show this help.
	@awk 'BEGIN {FS = ":.*?## "} /^[a-zA-Z_-]+:.*?## / {printf "  \033[36m%-15s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

env: ## Copy .env.example to .env if missing.
	@test -f .env || cp .env.example .env
	@echo "✓ .env ready"

dev: env ## Start docker stack + sync Python + JS deps.
	docker compose up -d
	uv sync --all-packages --all-groups
	pnpm install
	@echo "✓ dev stack up: postgres :5432  redis :6379  minio :9000 (console :9001)"

stop: ## Stop docker stack (preserves volumes).
	docker compose stop

restart: ## Restart docker stack.
	docker compose restart

logs: ## Tail docker logs.
	docker compose logs -f --tail=100

status: ## Show docker service status.
	docker compose ps

install-hooks: ## Install git pre-commit hooks.
	uv run pre-commit install
	@echo "✓ pre-commit hooks installed"

## --- Quality gates ---------------------------------------------------------

test: ## Run pytest across all workspace packages (exit 5 = no tests collected, treat as ok during scaffold).
	@uv run pytest; ec=$$?; if [ $$ec -eq 5 ]; then echo "(no tests collected — ok during scaffold)"; exit 0; else exit $$ec; fi

test-cov: ## Run pytest with coverage report.
	@uv run pytest --cov=libs --cov=services --cov-report=term-missing --cov-report=html; ec=$$?; if [ $$ec -eq 5 ]; then exit 0; else exit $$ec; fi

lint: ## Lint Python + JS.
	uv run ruff check $(PYTHON_DIRS)
	-pnpm -r lint

format: ## Auto-format Python + JS.
	uv run ruff check --fix $(PYTHON_DIRS)
	uv run ruff format $(PYTHON_DIRS)
	-pnpm -r exec prettier --write .

typecheck: ## Run mypy.
	uv run mypy $(PYTHON_DIRS)

ci: lint typecheck test ## Run the same gates CI runs.

## --- Build & deploy --------------------------------------------------------

build: ## Build all wheels + JS bundles.
	uv build --all-packages
	pnpm -r build

## --- Utilities -------------------------------------------------------------

db-shell: ## Open psql on the dev database.
	docker compose exec postgres psql -U fincept -d fincept

redis-cli: ## Open redis-cli on the dev redis.
	docker compose exec redis redis-cli

clean: ## Stop docker, drop volumes, remove venvs and node_modules.
	docker compose down -v
	rm -rf .venv node_modules .pytest_cache .mypy_cache .ruff_cache htmlcov dist build
	find . -type d -name __pycache__ -prune -exec rm -rf {} +

nuke: clean ## Same as clean but also wipes uv cache.
	uv cache clean
