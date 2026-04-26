# TASK-006 · CI pipeline (GitHub Actions) — lint, typecheck, test, build matrix

**Phase:** F · **Depends on:** TASK-001 · **Blocks:** every PR after this point gates on green CI

## Goal

Reproducible CI for Python (uv workspace) + JS (pnpm workspace). Every PR runs lint, typecheck, unit tests against an ephemeral Redis + Timescale, and (on `main` push) builds Docker images. Tests required: every package under `libs/` and every service under `services/` runs `pytest` + `mypy` + `ruff`. JS workspace runs `eslint` + `tsc --noEmit` + `pnpm test`.

## Files to create

```
.github/
└── workflows/
    ├── ci.yml                # PR-triggered: lint + typecheck + test
    ├── build-images.yml      # main-push: build + push Docker images to GHCR
    └── nightly.yml           # nightly: long-tests, vulnerability scan, dependency check
```

## `ci.yml`

```yaml
name: ci

on:
  pull_request:
    branches: [main]
  push:
    branches: [main]

concurrency:
  group: ${{ github.workflow }}-${{ github.ref }}
  cancel-in-progress: true

env:
  PYTHON_VERSION: "3.12"
  NODE_VERSION: "22"
  PNPM_VERSION: "9"
  UV_VERSION: "0.5.5"

jobs:
  py-lint-typecheck:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v4
        with:
          version: ${{ env.UV_VERSION }}
          enable-cache: true
      - run: uv sync --frozen
      - name: ruff
        run: uv run ruff check libs services
      - name: mypy
        run: uv run mypy libs services

  py-test:
    runs-on: ubuntu-latest
    services:
      redis:
        image: redis:7-alpine
        ports: [6379:6379]
        options: >-
          --health-cmd "redis-cli ping"
          --health-interval 5s
          --health-timeout 3s
          --health-retries 5
      timescale:
        image: timescale/timescaledb:latest-pg16
        ports: [5432:5432]
        env:
          POSTGRES_USER: fincept
          POSTGRES_PASSWORD: fincept
          POSTGRES_DB: fincept_test
        options: >-
          --health-cmd "pg_isready -U fincept -d fincept_test"
          --health-interval 5s
          --health-timeout 3s
          --health-retries 10
    env:
      DATABASE_URL: postgresql+asyncpg://fincept:fincept@localhost:5432/fincept_test
      REDIS_URL: redis://localhost:6379/15
      TRADING_MODE: paper
      LOG_LEVEL: WARNING
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v4
        with:
          version: ${{ env.UV_VERSION }}
          enable-cache: true
      - run: uv sync --frozen
      - name: alembic upgrade
        run: uv run alembic -c libs/fincept-db/alembic.ini upgrade head
      - name: pytest
        run: uv run pytest -q --maxfail=5 --tb=short --cov=libs --cov=services
      - name: coverage threshold
        run: uv run python -c "import coverage; c=coverage.Coverage(); c.load(); assert c.report() >= 70.0, 'coverage below 70%'"

  js-lint-typecheck-test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: pnpm/action-setup@v4
        with:
          version: ${{ env.PNPM_VERSION }}
      - uses: actions/setup-node@v4
        with:
          node-version: ${{ env.NODE_VERSION }}
          cache: pnpm
      - run: pnpm install --frozen-lockfile
      - run: pnpm -r lint
      - run: pnpm -r typecheck
      - run: pnpm -r test --if-present
```

## `build-images.yml`

```yaml
name: build-images
on:
  push:
    branches: [main]
    paths:
      - "services/**"
      - "infra/docker/**"
      - ".github/workflows/build-images.yml"
permissions:
  contents: read
  packages: write
jobs:
  build:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        image: [ingestor, agents, api, orchestrator, risk, oms]
    steps:
      - uses: actions/checkout@v4
      - uses: docker/setup-buildx-action@v3
      - uses: docker/login-action@v3
        with:
          registry: ghcr.io
          username: ${{ github.actor }}
          password: ${{ secrets.GITHUB_TOKEN }}
      - uses: docker/build-push-action@v6
        with:
          context: .
          file: infra/docker/${{ matrix.image }}.Dockerfile
          push: true
          tags: |
            ghcr.io/${{ github.repository }}/${{ matrix.image }}:${{ github.sha }}
            ghcr.io/${{ github.repository }}/${{ matrix.image }}:latest
          cache-from: type=gha,scope=${{ matrix.image }}
          cache-to: type=gha,mode=max,scope=${{ matrix.image }}
```

## `nightly.yml`

```yaml
name: nightly
on:
  schedule:
    - cron: "0 7 * * *"  # 07:00 UTC daily
  workflow_dispatch: {}
jobs:
  long-tests:
    runs-on: ubuntu-latest
    timeout-minutes: 60
    services:
      redis: { image: "redis:7-alpine", ports: ["6379:6379"] }
      timescale:
        image: timescale/timescaledb:latest-pg16
        ports: [5432:5432]
        env: { POSTGRES_USER: fincept, POSTGRES_PASSWORD: fincept, POSTGRES_DB: fincept_test }
    env:
      DATABASE_URL: postgresql+asyncpg://fincept:fincept@localhost:5432/fincept_test
      REDIS_URL: redis://localhost:6379/15
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v4
        with: { version: 0.5.5, enable-cache: true }
      - run: uv sync --frozen
      - run: uv run alembic -c libs/fincept-db/alembic.ini upgrade head
      - run: uv run pytest -q -m "long" --timeout=1800 --tb=short
  vuln-scan:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: aquasecurity/trivy-action@master
        with:
          scan-type: fs
          severity: CRITICAL,HIGH
          exit-code: 1
          ignore-unfixed: true
  pip-audit:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v4
        with: { version: 0.5.5 }
      - run: uv sync --frozen
      - run: uv run pip-audit -r <(uv pip compile pyproject.toml)
```

## Dockerfile expectations

Each service has its own Dockerfile under `infra/docker/<svc>.Dockerfile`. Skeleton (referenced by `build-images.yml`):

```dockerfile
# infra/docker/ingestor.Dockerfile
FROM python:3.12-slim-bookworm AS base
ENV UV_PROJECT_ENVIRONMENT=/opt/venv UV_LINK_MODE=copy
RUN pip install --no-cache-dir uv==0.5.5
WORKDIR /app

FROM base AS builder
COPY pyproject.toml uv.lock ./
COPY libs ./libs
COPY services/ingestor ./services/ingestor
RUN uv sync --frozen --no-dev --package ingestor

FROM python:3.12-slim-bookworm AS runtime
COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /app /app
WORKDIR /app
ENV PATH="/opt/venv/bin:$PATH"
USER 1000:1000
CMD ["python", "-m", "ingestor.main"]
```

(Author Dockerfiles for ingestor, agents, api, orchestrator, risk, oms in this task. Same skeleton; different `--package` and final `CMD`.)

## Test markers convention

- Default `pytest` runs short tests (≤30s each).
- `@pytest.mark.long` for tests > 30s; only run in `nightly.yml`.
- `@pytest.mark.gpu` for tests requiring GPU; not run in CI by default.
- `@pytest.mark.live` for tests hitting external APIs; never run in CI.

Add `[tool.pytest.ini_options]` to root `pyproject.toml`:

```toml
[tool.pytest.ini_options]
markers = [
    "long: tests >30s, run only in nightly",
    "gpu: tests requiring GPU",
    "live: tests hitting external APIs (manual only)",
]
asyncio_mode = "auto"
addopts = "-ra --strict-markers -m 'not long and not gpu and not live'"
```

## Out of scope

- No deployment automation (no kubectl apply, no Argo). Image build only; deploys are Phase H.
- No release tagging / changelog automation.
- No code-signing / image-signing (defer to Phase H).
- No staged rollouts (defer to TASK-076).

## Done when

- [ ] All 3 workflow files exist
- [ ] Skeleton Dockerfiles exist for all 6 services
- [ ] PR opened against `main` triggers `ci.yml`; all jobs pass
- [ ] Push to `main` triggers `build-images.yml`; images appear in GHCR
- [ ] Nightly schedule produces a green run within 24h of merge
- [ ] `[tool.pytest.ini_options]` is in root `pyproject.toml`
- [ ] Coverage threshold of ≥70% reported by `py-test` job
