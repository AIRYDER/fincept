# Codebase Map

## Top-Level Directory Tree

```text
fincept-terminal/
+-- apps/
|   +-- dashboard/
+-- libs/
|   +-- fincept-bus/
|   +-- fincept-core/
|   +-- fincept-db/
|   +-- fincept-sdk/
|   +-- fincept-tools/
+-- services/
|   +-- agents/
|   +-- api/
|   +-- backtester/
|   +-- features/
|   +-- ingestor/
|   +-- jobs/
|   +-- oms/
|   +-- orchestrator/
|   +-- portfolio/
|   +-- risk/
|   +-- strategy_host/
+-- scripts/
+-- docs/
+-- spec/
+-- experiments/
+-- data/
+-- models/
+-- reports/
+-- docker-compose.yml
+-- Makefile
+-- pyproject.toml
+-- pnpm-workspace.yaml
+-- apps/dashboard/package.json
```

## Practical Map

| Path | Purpose | Notes |
|---|---|---|
| `README.md` | Main repo orientation and local commands. | Contains current status snapshots and key validation commands. |
| `pyproject.toml` | Root uv workspace config. | Lists all Python workspace members and pytest/coverage config. |
| `uv.lock` | Python dependency lock. | Tracked despite `.gitignore` entries because of `!uv.lock`. |
| `pnpm-workspace.yaml` | JS workspace config. | Dashboard is the only observed pnpm package. |
| `apps/dashboard/package.json` | Dashboard scripts and dependencies. | Next.js 14, React, Tailwind, Radix, TanStack Query, Zustand. |
| `docker-compose.yml` | Local infra stack. | Timescale/Postgres, Redis, MinIO with dev credentials. |
| `Makefile` | POSIX/WSL developer commands. | `dev`, `test`, `test-cov`, `lint`, `typecheck`, `build`. |
| `.github/workflows/ci.yml` | CI pipeline. | Python lint/typecheck/test with services, JS checks, gitleaks. |
| `.env.example` | Example local environment. | Modified in current dirty worktree. `.env` exists locally but is ignored. |
| `libs/fincept-core/src/fincept_core/` | Core schemas, events, settings, strategy config, prediction log, clocks, IDs. | Source of shared contracts. |
| `libs/fincept-bus/src/fincept_bus/` | Redis Streams wrapper and stream constants. | Used by services for event publication/consumption. |
| `libs/fincept-db/src/fincept_db/` | Async database layer and migrations. | Includes Alembic versions under `migrations/versions`. |
| `libs/fincept-tools/src/fincept_tools/` | Typed tools for data, analytics, execution, research. | OpenBB/Exa tools are read-oriented; execution tools should remain paper-first. |
| `libs/fincept-sdk/src/fincept_sdk/` | Strategy SDK. | Tests exist under `libs/fincept-sdk/tests`. |
| `services/ingestor/src/ingestor/` | Market data ingestion, normalization, writing, quality monitoring. | Venue adapters include Binance, Coinbase, Kraken, EOD equity. |
| `services/features/src/features/` | Feature transforms and stores. | Online Redis and offline Parquet concepts appear in code/docs. |
| `services/agents/src/agents/` | Predictive and information agents. | GBM, regime, sentiment, news-alpha, news-impact, and enrichment modules. |
| `services/orchestrator/src/orchestrator/` | Consensus, allocation, decisions, router. | Converts predictions to decisions/order intents. |
| `services/risk/src/risk/` | Pre-trade checks and risk state. | Part of paper-spine proof. |
| `services/oms/src/oms/` | Order processing, paper fills, Alpaca adapter. | `OMS_ROUTER` selects sim or Alpaca path. |
| `services/portfolio/src/portfolio/` | Fill consumption and position state. | Publishes/serves portfolio snapshots. |
| `services/api/src/api/` | FastAPI app, routes, auth, background schedulers, WebSocket. | Local API default is port 8010 via scripts/dashboard config. |
| `services/strategy_host/src/strategy_host/` | Strategy runner and supervisor. | Uses persistent strategy configs from `fincept-core`. |
| `services/backtester/src/backtester/` | Historical replay, broker, costs, strategies, reporting. | API exposes synchronous backtest runs. |
| `services/jobs/src/jobs/` | Cron-style jobs. | Less prominent than other services in this audit. |
| `apps/dashboard/src/app/` | Next.js App Router pages. | Routes include markets, models, predictions, orders, positions, risk, research, system, receipts, and more. |
| `apps/dashboard/src/lib/api.ts` | Typed dashboard REST client. | Mirrors FastAPI routes and throws structured `ApiError`. |
| `apps/dashboard/src/lib/auth.ts` | LocalStorage JWT auth state. | Prototype auth; Phase H replacement planned. |
| `scripts/*.ps1` | Windows local automation. | Setup, preflight, task-check, start/status/stop, feature start/stop. |
| `scripts/*.py` | Proof, ingestion, backtest, smoke, and sync helpers. | Includes paper spine, route smoke, OpenBB proof. |
| `docs/` | Planning, audits, system docs, roadmap, risks, UI notes. | Some docs are current; some are historical planning context. |
| `spec/` | Contract-first implementation specifications. | Contains architecture, build order, prompts, task specs. |
| `experiments/news-impact-model/` | Experimental news-impact model surface. | Connected to API/dashboard in shadow/read-only style. |
| `reports/` | Generated proof receipts and walk-forward artifacts. | Mostly ignored by git; useful for current evidence. |
| `models/` | Model artifacts and active pointers. | Many artifacts should stay generated/ignored unless promoted to fixtures. |
| `data/` | Local generated data and prediction logs. | Do not assume all data is tracked or safe to commit. |

## Main Entry Points

| Entry Point | Command or File | Notes |
|---|---|---|
| API app | `services/api/src/api/main.py` | Uvicorn target is `api.main:app`; README uses port 8010. |
| Dashboard dev | `pnpm --filter @fincept/dashboard dev` | Runs Next.js on port 3000. |
| Local repo start | `powershell -ExecutionPolicy Bypass -File .\scripts\start.ps1` | Windows local orchestration helper. |
| Dev setup | `scripts/dev-setup.ps1` or `make dev` | Docker + Python/JS dependency setup. |
| Broad preflight | `scripts/preflight.ps1` | CI-parity local checks, heavier than task-level checks. |
| Task check | `scripts/task-check.ps1 -PackagePath ... -PytestPath ...` | Narrow pytest + ruff + mypy loop. |
| Paper proof | `uv run python scripts/paper_spine_replay.py` | Writes paper-spine receipts. |
| Route smoke | `uv run python scripts/route_smoke.py --base-url http://127.0.0.1:8010` | Probes API routes and writes route-smoke receipts. |
| OpenBB proof | `uv run python scripts/openbb_live_proof.py --symbol NVDA` | Probes OpenBB route readiness. |

## Tests

Tests are distributed beside packages:

- `libs/*/tests`
- `services/*/tests`
- `apps/dashboard/scripts/run-*-tests.cjs`
- component tests such as `apps/dashboard/src/components/news-impact/*.test.tsx`

Root pytest config lives in `pyproject.toml`.

## Generated or Local Artifacts

Likely generated/local:

- `.venv/`, `node_modules/`, `.next/`, caches.
- `.env` and other `.env.*` files except `.env.example`.
- `reports/openbb-live/`, `reports/paper-spine/`, `reports/route-smoke/`.
- `data/predictions/`, `data/training_runs/`, selected data parquet files.
- `models/news_alpha_predictor/`, `models/t3/`, local model artifacts.
- `tmp_*.out`, `tmp_*.err`.

These are ignored in `.gitignore`; do not stage them casually.

## Files That Appear Historical, Obsolete, or Duplicated

- `PROJECT_OVERVIEW.md`, `IMPLEMENTATION.md`, `featuresmenu.md`, and many older
  docs are useful context but may lag behind current code.
- `docs/ROADMAP.md` contains historical snapshots and should not be treated as a
  single current checklist.
- `spec/BUILD_ORDER.md` has some task status drift relative to implemented
  agents and dashboard routes.
- `docs/SYSTEM_OVERVIEW.md` is more current than some older spec docs, but it
  still contains planned steps and should be verified against source.
