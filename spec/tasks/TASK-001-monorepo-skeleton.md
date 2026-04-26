# TASK-001 · Monorepo skeleton

**Phase:** F (Foundation) · **Depends on:** none · **Blocks:** everything

## Goal

Create the workspace structure, dev tooling, and local dependency stack so subsequent tasks have a place to live and a reliable way to run.

## Files to create

```
fincept-terminal/
├── Makefile
├── pyproject.toml                    # uv workspace root
├── pnpm-workspace.yaml
├── .python-version                   # "3.12"
├── .env.example
├── .gitignore
├── .pre-commit-config.yaml
├── docker-compose.yml
├── ruff.toml
├── mypy.ini
└── .github/workflows/ci.yml
```

## Exact contents

### `pyproject.toml`

```toml
[project]
name = "fincept-terminal"
version = "0.0.0"
requires-python = ">=3.12"

[tool.uv.workspace]
members = [
    "libs/fincept-core",
    "libs/fincept-bus",
    "libs/fincept-db",
    "libs/fincept-tools",
    "libs/fincept-sdk",
    "services/ingestor",
    "services/features",
    "services/agents",
    "services/orchestrator",
    "services/risk",
    "services/oms",
    "services/portfolio",
    "services/api",
    "services/backtester",
    "services/jobs",
]

[tool.uv.sources]
fincept-core = { workspace = true }
fincept-bus = { workspace = true }
fincept-db = { workspace = true }
fincept-tools = { workspace = true }
fincept-sdk = { workspace = true }
```

### `pnpm-workspace.yaml`

```yaml
packages:
  - "apps/*"
```

### `.python-version`

```text
3.12
```

### `.env.example`

Copy verbatim from `spec/CONTRACTS.md §10`.

### `.gitignore`

```gitignore
__pycache__/
*.py[cod]
.venv/
.env
node_modules/
.next/
dist/
build/
.pytest_cache/
.mypy_cache/
.ruff_cache/
*.egg-info/
.coverage
htmlcov/
.idea/
.vscode/
*.db
*.sqlite
data/local/
```

### `ruff.toml`

```toml
line-length = 100
target-version = "py312"

[lint]
select = ["E", "F", "I", "UP", "B", "SIM", "RUF"]
ignore = ["E501"]
```

### `mypy.ini`

```ini
[mypy]
python_version = 3.12
strict = True
warn_return_any = True
warn_unused_configs = True
disallow_untyped_defs = True
ignore_missing_imports = True
```

### `.pre-commit-config.yaml`

```yaml
repos:
  - repo: https://github.com/astral-sh/ruff-pre-commit
    rev: v0.7.0
    hooks:
      - id: ruff
        args: [--fix]
      - id: ruff-format
  - repo: https://github.com/pre-commit/mirrors-mypy
    rev: v1.13.0
    hooks:
      - id: mypy
        additional_dependencies: [pydantic>=2.9, types-redis]
```

### `docker-compose.yml`

```yaml
services:
  postgres:
    image: timescale/timescaledb:latest-pg16
    environment:
      POSTGRES_DB: fincept
      POSTGRES_USER: fincept
      POSTGRES_PASSWORD: fincept
    ports: ["5432:5432"]
    volumes: [pgdata:/var/lib/postgresql/data]
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U fincept"]
      interval: 5s
      retries: 10

  redis:
    image: redis:7-alpine
    command: redis-server --appendonly yes --maxmemory 2gb --maxmemory-policy noeviction
    ports: ["6379:6379"]
    volumes: [redisdata:/data]

  minio:
    image: minio/minio:latest
    command: server /data --console-address ":9001"
    environment:
      MINIO_ROOT_USER: fincept
      MINIO_ROOT_PASSWORD: fincept-minio-pw
    ports: ["9000:9000", "9001:9001"]
    volumes: [miniodata:/data]

volumes:
  pgdata:
  redisdata:
  miniodata:
```

### `Makefile`

```makefile
.PHONY: dev stop test lint typecheck build clean

dev:
	docker compose up -d
	uv sync --all-packages
	pnpm install

stop:
	docker compose down

test:
	uv run pytest

lint:
	uv run ruff check .
	pnpm -r lint

typecheck:
	uv run mypy libs services

build:
	uv build --all-packages
	pnpm -r build

clean:
	docker compose down -v
	rm -rf .venv node_modules
```

### `.github/workflows/ci.yml`

```yaml
name: ci
on: [push, pull_request]
jobs:
  python:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v3
        with:
          enable-cache: true
      - run: uv sync --all-packages
      - run: uv run ruff check .
      - run: uv run mypy libs services
      - run: uv run pytest
  js:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: pnpm/action-setup@v4
        with: { version: 9 }
      - uses: actions/setup-node@v4
        with: { node-version: 22, cache: pnpm }
      - run: pnpm install
      - run: pnpm -r lint
      - run: pnpm -r build
```

## Acceptance

Run these commands in order; each must succeed:

```bash
make dev
docker compose ps                # shows postgres, redis, minio healthy
uv --version                     # installed
pnpm --version                   # installed
make lint                        # no errors
make test                        # collects 0 tests, exits 0
```

## Out of scope

- Do NOT create package directories under `libs/` or `services/`. TASK-002 starts that work.
- Do NOT configure Vault or cloud secrets — use `.env` locally.
- Do NOT add Terraform or Kubernetes manifests.

## Done when

- [ ] All files above exist at the exact paths listed
- [ ] `make dev` succeeds on a fresh clone
- [ ] `make lint` exits 0
- [ ] CI workflow runs green on a branch push
