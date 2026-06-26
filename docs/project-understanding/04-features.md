# Features

Confidence levels reflect code evidence from this audit, not product readiness.

## Confirmed Working Features

### Python Monorepo and Local Automation

- Description: uv workspace, pnpm dashboard workspace, Docker stack, CI, and
  Windows/Makefile workflows.
- Related files: `pyproject.toml`, `pnpm-workspace.yaml`, `docker-compose.yml`,
  `Makefile`, `.github/workflows/ci.yml`, `scripts/*.ps1`.
- Confidence: high.
- Evidence: root manifests and CI files exist; README documents the commands.

### Core Schemas, Settings, and Event Primitives

- Description: shared Pydantic schemas, settings, event envelopes, IDs, clocks,
  logging, tracing, strategy config, and prediction log utilities.
- Related files: `libs/fincept-core/src/fincept_core`.
- Confidence: high.
- Evidence: code and tests under `libs/fincept-core/tests`.

### Redis Streams Event Bus

- Description: producer/consumer wrappers and canonical stream constants.
- Related files: `libs/fincept-bus/src/fincept_bus`.
- Confidence: high.
- Evidence: code and tests under `libs/fincept-bus/tests`.

### Database Layer

- Description: async SQLAlchemy engine, migrations, bars/ticks/features/provider
  data helpers.
- Related files: `libs/fincept-db/src/fincept_db`, `libs/fincept-db/alembic.ini`.
- Confidence: high.
- Evidence: migrations and tests under `libs/fincept-db/tests`.

### FastAPI Gateway

- Description: API app with data, orders, positions, strategies, news,
  news-impact, services, models, regime, backtest, research, control, and
  WebSocket routes.
- Related files: `services/api/src/api/main.py`, `services/api/src/api/routes`.
- Confidence: high.
- Evidence: routers are included in `api.main`; tests exist under
  `services/api/tests`.

### Next.js Operator Dashboard

- Description: App Router dashboard with pages for overview, markets, models,
  orders, positions, predictions, receipts, reconciliation, research, risk,
  strategies, system readiness, portfolio builder, and experimental labs.
- Related files: `apps/dashboard/src/app`, `apps/dashboard/src/lib/api.ts`.
- Confidence: high.
- Evidence: route folders and typed API client exist; dashboard README documents
  shipped pages.

### Deterministic Paper-Spine Replay

- Description: deterministic proof that a single fixture can flow through data,
  feature, signal, decision, risk, order, fill, portfolio, and audit trail.
- Related files: `scripts/paper_spine_replay.py`, `reports/paper-spine/latest.json`.
- Confidence: high.
- Evidence: latest receipt status is `passed` with 11 assertions true.

### Model Lifecycle Surfaces

- Description: model listing, training runs, promotion, rollback, active/shadow
  pointers, predictions, and feature importance.
- Related files: `services/api/src/api/routes/models.py`,
  `services/api/src/api/promotions.py`, `services/api/src/api/training.py`,
  dashboard model components.
- Confidence: high for API/UI surface; medium for end-to-end runtime proof.
- Evidence: API routes, tests, dashboard client methods, and docs exist.

### Strategy Config CRUD and Lifecycle

- Description: persistent strategy config records, history, start/stop/adopt
  actions, and strategy-host supervision.
- Related files: `libs/fincept-core/src/fincept_core/strategy_config.py`,
  `services/api/src/api/routes/strategies.py`, `services/strategy_host`.
- Confidence: high for config surface; medium for live strategy-host proof.
- Evidence: tests exist and API/dashboard routes reference this workflow.

## Partially Implemented Features

### Route Smoke Coverage

- Description: route smoke script and receipts exist, but latest reviewed receipt
  has one failure.
- Related files: `scripts/route_smoke.py`,
  `reports/route-smoke/route-smoke-20260506-211742.json`.
- Confidence: high.
- Evidence: latest receipt shows 8/9 passed and `/data/coverage` timed out.

### OpenBB Research Integration

- Description: API health and dispatcher routes exist; proof shows OpenBB backend
  or package unavailable for provider calls.
- Related files: `services/api/src/api/routes/research.py`,
  `libs/fincept-tools/src/fincept_tools/research/openbb.py`,
  `reports/openbb-live/openbb-live-20260505-151250.json`.
- Confidence: high.
- Evidence: health route returns structured unavailable; quote and dispatcher
  probes returned 503 in the latest proof.

### News Impact Model

- Description: experiment bridged into API/dashboard and an agent folder exists,
  but posture is still experimental/shadow-oriented.
- Related files: `experiments/news-impact-model`, `services/api/src/api/routes/news_impact.py`,
  `services/agents/src/agents/news_impact_agent`, `apps/dashboard/src/app/news-impact-lab`.
- Confidence: medium.
- Evidence: files exist and route smoke probes `/news-impact/status`; current
  git status shows many news-impact files modified or untracked.

### Live Service Orchestration

- Description: start/status/stop scripts exist for local service orchestration,
  but this audit did not verify running processes.
- Related files: `scripts/start.ps1`, `scripts/status.ps1`, `scripts/stop.ps1`.
- Confidence: medium.
- Evidence: scripts exist; recent route receipts indicate a port-8010 API was
  running in prior sessions, not in this audit.

### Portfolio Builder AI Report

- Description: deterministic allocation and server-side portfolio report route
  exist with OpenAI/Anthropic provider options.
- Related files: `apps/dashboard/src/features/portfolio-builder`,
  `apps/dashboard/src/app/api/portfolio-report/route.ts`.
- Confidence: medium.
- Evidence: code exists, but live provider calls require keys and were not
  tested in this audit.

## Planned or Implied Features

- Service-backed paper-spine replay with real Redis stream IDs and Timescale rows.
- Dashboard route inventory smoke for every page.
- Production-grade auth with OAuth/httpOnly cookies.
- Stronger secret management and live-capital governance.
- Kubernetes/staging deployment.
- Phase X/X+/Y/Z agent expansions such as options flow, on-chain analytics,
  drift monitoring, and richer LLM workflows.
- Provider health center and OpenBB readiness matrix.

Evidence: `docs/ROADMAP.md`, `docs/SYSTEM_OVERVIEW.md`, `spec/BUILD_ORDER.md`,
dashboard/system readiness code, and current proof gaps.

## Missing Features

- Multi-user authorization and ownership checks.
- Production auth/session model.
- Green latest route-smoke receipt.
- Service-backed end-to-end replay.
- Clear approved-root validation for all user-controlled file paths in
  backtest/model training paths.
- Documented production deployment manifests in the repo.
- Complete CI gate for dashboard route load/browser smoke.

## Deprecated or Unclear Features

- `pairs` agent is listed as a stub in `services/agents/src/agents/__init__.py`
  and `spec/BUILD_ORDER.md`.
- Polygon loader is intentionally a paid stub in
  `services/ingestor/src/ingestor/eod_equity.py`.
- Some roadmap tasks and older docs do not match current implemented surfaces.
- Some dashboard demo surfaces use placeholder/static data by design, such as
  the signal cockpit demo and portfolio-builder market data placeholders.
