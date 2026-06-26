# System Improvement Report

Generated: 2026-06-21

Scope: local repository audit of `C:\Users\nolan\CascadeProjects\fincept-terminal`. This report is based on repository files, local git state, focused source inspection, and targeted validation commands. It does not include hosted deployment proof, full Docker runtime proof, live broker/provider proof, or a browser-driven smoke run.

Validation performed during this audit:

- `npm run test:shadow-news-impact` from `apps/dashboard` passed: 3 tests.
- `npm run test:source-health` from `apps/dashboard` passed: 5 tests.
- `npm run test:strategy-readiness` from `apps/dashboard` passed: 4 tests.
- `pnpm --dir apps/dashboard exec tsc --noEmit --pretty false` passed.
- `uv run pytest services/api/tests/test_news_impact.py -q` passed: 6 tests.

Validation not performed:

- Full `scripts/preflight.ps1`.
- Docker Compose boot.
- Full API route smoke matrix.
- Browser smoke run.
- Live provider, broker, or hosted deployment checks.

## 1. Executive Summary

Fincept Terminal has a strong architecture direction: a Python service workspace, a Next.js dashboard, Timescale/Postgres-backed market data, Redis streams, typed contracts, focused tests, and an increasingly useful documentation set. The repo is healthiest where recent work has been narrowed into explicit, read-only proof surfaces: shadow news-impact UI, source-health checks, strategy-readiness checks, and API tests for `/news-impact`.

The largest current risk is not that the system lacks pieces. The risk is that safety invariants are unevenly enforced across processes. `assert_safe_for_runtime()` exists and is used by the API, but the other long-running service entrypoints inspected during this audit start from `get_settings()` without the same fail-closed guard. That weakens the promise that unsafe runtime configuration is rejected consistently before services touch Redis, streams, schedulers, or broker-adjacent code.

The second major risk is file boundary handling. The authenticated backtest endpoint accepts `bars_path` as an absolute or relative path and only checks that the file exists. That is too broad for an API route. It should be restricted to approved data roots and file types before any parsing.

The third major risk is release hygiene. The repo has many meaningful modified files plus large untracked local/tool directories. That may be normal during active development, but it makes broad staging or release decisions unsafe until changes are grouped, ignored, or intentionally reviewed.

Highest-leverage next work:

1. Apply the runtime safety guard to every service entrypoint and add a startup safety matrix test.
2. Lock down the backtest file path boundary with allowlisted roots and traversal tests.
3. Create a structured verification receipt command that records source-health, strategy-readiness, shadow-news-impact, API slice tests, and route-smoke results.

## 2. Current System Map

### Frontend

- Main dashboard app: `apps/dashboard`.
- Framework: Next.js app router, React, TypeScript.
- Key routes include:
  - `apps/dashboard/src/app/page.tsx`
  - `apps/dashboard/src/app/news-impact-lab/page.tsx`
  - `apps/dashboard/src/app/portfolio-builder/page.tsx`
  - `apps/dashboard/src/app/positions/page.tsx`
  - `apps/dashboard/src/app/watchlist/page.tsx`
  - `apps/dashboard/src/app/symbol/[symbol]/page.tsx`
  - `apps/dashboard/src/app/system/page.tsx`
  - `apps/dashboard/src/app/api/portfolio-report/route.ts`
- Dashboard API client:
  - `apps/dashboard/src/lib/api.ts`
  - `apps/dashboard/src/lib/auth.ts`
  - `apps/dashboard/src/lib/ws.ts`
- Current frontend auth model:
  - `apps/dashboard/src/lib/auth.ts` stores JWTs in `localStorage`.
  - `apps/dashboard/src/lib/ws.ts` passes the JWT to WebSocket streams through a `token` query parameter.
  - `apps/dashboard/README.md` documents this as a v1 model and says Phase H should move to httpOnly cookies plus OAuth.

### Backend

- Main API service:
  - `services/api/src/api/main.py`
  - `services/api/src/api/auth.py`
  - `services/api/src/api/ws.py`
  - `services/api/src/api/routes/`
- Long-running services:
  - `services/ingestor/src/ingestor/main.py`
  - `services/orchestrator/src/orchestrator/main.py`
  - `services/oms/src/oms/main.py`
  - `services/strategy_host/src/strategy_host/main.py`
- Agents:
  - `services/agents/src/agents/__init__.py`
  - Includes implemented agents plus `pairs`, which is explicitly listed as a stub.
- Core config:
  - `libs/fincept-core/src/fincept_core/config.py`
  - `Settings` centralizes runtime configuration.
  - `assert_safe_for_runtime(settings)` rejects unsafe defaults outside dev/local/test.
- Shared database package:
  - `libs/fincept-db/src/fincept_db/`

### Data/Storage

- Timescale/Postgres schema and migrations:
  - `libs/fincept-db/src/fincept_db/migrations/0001_initial.py`
  - `libs/fincept-db/src/fincept_db/migrations/0002_features.py`
  - `libs/fincept-db/src/fincept_db/migrations/0003_provider_data.py`
- Market bars and coverage:
  - `libs/fincept-db/src/fincept_db/bars.py`
  - `services/api/src/api/routes/data.py`
- Provider data:
  - `libs/fincept-db/src/fincept_db/provider_data.py`
  - `0003_provider_data.py`
- Redis streams and service coordination are assumed by multiple services.
- Docker Compose local services:
  - `docker-compose.yml` defines TimescaleDB, Redis, and MinIO.

### External APIs

- Broker/provider surfaces:
  - Alpaca integration in `services/oms/src/oms/alpaca/`.
  - News sync and marks in `services/oms/src/oms/alpaca/news_sync.py` and `services/oms/src/oms/alpaca/marks.py`.
- LLM portfolio report surface:
  - `apps/dashboard/src/app/api/portfolio-report/route.ts`
  - Uses OpenAI or Anthropic server-side environment variables.
- Dashboard env examples:
  - `apps/dashboard/.env.example`
  - Root `.env.example`

### Automation Scripts

- Main preflight:
  - `scripts/preflight.ps1`
- Targeted task check:
  - `scripts/task-check.ps1`
- Startup:
  - `scripts/start.ps1`
  - `scripts/dev-setup.ps1`
- Dashboard targeted tests:
  - `apps/dashboard/scripts/run-shadow-news-impact-tests.cjs`
  - `apps/dashboard/scripts/run-source-health-tests.cjs`
  - `apps/dashboard/scripts/run-strategy-readiness-tests.cjs`

### Deployment/Runtime Assumptions

- Local development expects Docker services from `docker-compose.yml`.
- Python workspace is managed by `uv` through `pyproject.toml` and `uv.lock`.
- Dashboard uses `pnpm` via `apps/dashboard/package.json`.
- CI is defined under `.github/workflows/`.
- Runtime safety currently depends on each service choosing to call shared config guards.

### Testing Setup

- Python:
  - `pyproject.toml` configures pytest, ruff, mypy, coverage, and warnings-as-errors.
  - Long, GPU, and live tests are excluded by default.
- Dashboard:
  - `apps/dashboard/package.json` defines many targeted test scripts.
  - `typecheck` uses `tsc --noEmit`.
- CI:
  - `.github/workflows/ci.yml`
  - `.github/workflows/nightly.yml`
  - `.github/workflows/build-images.yml`

### Docs Structure

- Current docs live under `docs/`.
- Strong local understanding pack:
  - `docs/project-understanding/`
- Current roadmap:
  - `docs/ROADMAP.md`
- Risk register:
  - `docs/RISKS.md`
- Feature menu:
  - `featuresmenu.md`
- This report:
  - `docs/SYSTEM_IMPROVEMENT_REPORT.md`

## 3. What Is Working Well

- The repo has a clear monorepo shape. `pyproject.toml` declares a `uv` workspace across `libs/*` and `services/*`, while the dashboard has its own `apps/dashboard/package.json`.
- Runtime configuration is centralized in `libs/fincept-core/src/fincept_core/config.py`.
- A fail-closed runtime guard already exists in `assert_safe_for_runtime(settings)`.
- The API applies the runtime guard during lifespan startup in `services/api/src/api/main.py`.
- The database layer has real migrations and indexes, including Timescale hypertables and provider-data indexing.
- The shadow news-impact surface is intentionally read-only. `services/api/src/api/routes/news_impact.py` says it does not publish trades or signals, and `apps/dashboard/src/components/news-impact/shadow-news-impact-panel.tsx` labels the panel as shadow-only.
- The current shadow-news-impact tests pass and verify that the UI does not expose trade-driving controls.
- Source-health and strategy-readiness tests pass and provide focused proof for important dashboard readiness slices.
- API tests for `services/api/tests/test_news_impact.py` pass.
- The dashboard has visible mock badges and warnings on several mock/demo routes instead of silently presenting fixtures as live data.
- `scripts/task-check.ps1` provides a useful targeted validation pattern for Python package work.
- Existing docs are much stronger than a typical early system: roadmap, risk register, project-understanding docs, and feature menu all exist.
- The LLM portfolio report route reads provider keys from server-side env vars, not `NEXT_PUBLIC_*`, and returns generic provider failure diagnostics instead of raw provider exceptions.
- Several integrations are intentionally bounded with read-only or shadow labels, which is the right safety posture for a trading-adjacent system.

## 4. Critical Issues

### CRIT-001: Runtime safety guard is not consistently applied across service entrypoints

- Area: Security, reliability, trading safety, runtime configuration.
- Evidence:
  - `libs/fincept-core/src/fincept_core/config.py` defines `assert_safe_for_runtime(settings)` and rejects the default `FINCEPT_JWT_SECRET` outside `dev`, `local`, or `test`.
  - `services/api/src/api/main.py` calls `assert_safe_for_runtime(settings)` during API startup.
  - Repository search found that same guard call only in the API startup path.
  - `services/strategy_host/src/strategy_host/main.py` calls `get_settings()` and starts Redis/supervisor work without the shared guard.
  - `services/orchestrator/src/orchestrator/main.py` calls `get_settings()` and starts Redis consumers/producers without the shared guard.
  - `services/oms/src/oms/main.py` calls `get_settings()`, selects OMS mode, opens Redis, and starts heartbeat without the shared guard.
  - `services/ingestor/src/ingestor/main.py` calls `get_settings()`, opens Redis, and starts heartbeat without the shared guard.
  - `services/strategy_host/src/strategy_host/main.py` also documents that the single-leader invariant is deferred and currently relies on dev startup launching exactly one process.
- Why it matters:
  - Safety is currently process-dependent. The API fails closed on unsafe runtime defaults, but other long-running services can start without the same check.
  - In a trading-adjacent system, inconsistent startup policy can allow unsafe secrets, wrong environments, or duplicate strategy-host processes to reach Redis streams before the operator notices.
  - If a non-API process is deployed independently, the API guard does not protect it.
- Recommended fix:
  - Apply `assert_safe_for_runtime(settings)` in every service entrypoint immediately after `settings = get_settings()` and before any Redis, scheduler, stream, broker, or heartbeat initialization.
  - Add a small service startup safety matrix test that imports or exercises the startup config guard for API, ingestor, orchestrator, OMS, and strategy host.
  - Add a documented single-leader guard for strategy host before any production or staging-like runtime.
- Implementation steps:
  1. Add the guard to `services/ingestor/src/ingestor/main.py`, `services/orchestrator/src/orchestrator/main.py`, `services/oms/src/oms/main.py`, and `services/strategy_host/src/strategy_host/main.py`.
  2. Keep failures sanitized: log that runtime safety validation failed, but do not echo secrets.
  3. Add focused tests under the relevant service test folders or a shared config test folder.
  4. Add a startup matrix receipt section to docs or automation so operators can see which processes enforce the guard.
  5. Add a strategy-host leadership design note before enabling multi-process deployment.
- Acceptance criteria:
  - All long-running service entrypoints call `assert_safe_for_runtime(settings)` before opening Redis or starting work.
  - Tests fail if a service entrypoint removes the guard.
  - Starting any service with `ENV=prod` and the default dev JWT secret fails before side effects.
  - No secret values appear in startup error output.
- Tests needed:
  - Unit test for `assert_safe_for_runtime` behavior in dev/local/test versus staging/prod.
  - Service startup guard tests using monkeypatched settings.
  - Targeted `uv run pytest` slice for the config/service startup tests.
  - Optional PowerShell smoke that starts each service with invalid prod-like env and confirms fail-closed behavior.
- Risk: Medium implementation risk, high operational risk if left unresolved. The code change should be small but touches startup paths.

### CRIT-002: Backtest endpoint accepts broad user-controlled file paths

- Area: API security, file handling, data boundary enforcement.
- Evidence:
  - `services/api/src/api/routes/backtest.py` documents that `bars_path` may be relative to the current working directory or absolute.
  - The route creates `pathlib.Path(body.bars_path)` and checks only whether it exists before passing it to backtest logic.
  - `docs/RISKS.md` already flags overly broad file paths for backtest/training workflows.
- Why it matters:
  - An authenticated caller can probe filesystem paths and potentially cause the API to parse unintended local files.
  - Absolute paths couple API behavior to machine layout and make hosted/runtime behavior harder to reason about.
  - File parsing errors can expose internal path details or operational assumptions.
- Recommended fix:
  - Restrict backtest file reads to approved data roots, resolve canonical paths, enforce prefix checks, and allow only expected file extensions such as `.parquet`.
  - Prefer logical dataset IDs over raw paths for user-facing APIs.
- Implementation steps:
  1. Add settings for allowed backtest data roots, with safe local defaults.
  2. Normalize `bars_path` with `Path.resolve()`.
  3. Reject absolute paths outside the approved roots.
  4. Reject traversal attempts such as `../`.
  5. Reject unsupported extensions.
  6. Return sanitized 400/403 errors without filesystem internals.
  7. Add tests for allowed files, missing files, traversal, absolute outside-root paths, and wrong extensions.
- Acceptance criteria:
  - Valid fixture files under approved roots still work.
  - `../`, absolute system paths, and unsupported suffixes are rejected before parsing.
  - Error responses do not reveal host-specific absolute paths.
  - Existing backtest tests pass.
- Tests needed:
  - API unit tests for path validation.
  - Regression tests for existing backtest happy path.
  - Negative tests for traversal and absolute path probes.
- Risk: Medium. Tightening file boundaries may break local workflows that depended on arbitrary absolute paths, so provide a migration note and settings escape hatch only for local/dev.

### CRIT-003: Dirty worktree and untracked tool artifacts make release decisions unsafe

- Area: Release hygiene, maintainability, supply chain, accidental commit prevention.
- Evidence:
  - `git status --short` shows a broad dirty tree with many modified files across dashboard, API, core, tests, docs, and scripts.
  - Untracked/local tool directories include `.opencode/`, `.playwright-cli/`, `.worktrees/`, `.devin/dialectic-repo/`, `.bridgespace/`, and generated logs.
  - `git diff --stat` excluding the largest local tool dirs still showed 48 changed files with about 1602 insertions and 311 deletions.
  - `.gitignore` ignores `.env` and some local directories, but not all of the local tool-state directories observed during this audit.
- Why it matters:
  - Broad `git add` is risky because unrelated local artifacts can be committed accidentally.
  - Large untracked directories make review and automation slower and noisier.
  - It is hard to distinguish shippable product changes from local tool state.
- Recommended fix:
  - Create a release-hygiene pass that classifies changed files into intentional product/docs changes, local-only artifacts, generated artifacts, and unknowns.
  - Update `.gitignore` for tool-state directories that should never be committed.
  - Avoid broad staging until the tree is intentionally partitioned.
- Implementation steps:
  1. Run `git status --short` and export a categorized working-tree inventory.
  2. Inspect each untracked top-level directory before ignoring it.
  3. Add ignore rules for confirmed local-only tool state.
  4. Leave product/docs files untouched unless a separate implementation task owns them.
  5. Add a release checklist item requiring a clean or intentionally bucketed status before PR/commit.
- Acceptance criteria:
  - `git status --short` is understandable at a glance.
  - Known local tool state is ignored.
  - No generated logs/caches are staged.
  - Release or PR scope can be described without guessing.
- Tests needed:
  - No runtime tests required for ignore-only changes.
  - Run `git check-ignore -v` on each newly ignored local artifact pattern.
  - Run `git status --short` after cleanup.
- Risk: Low implementation risk, high release-risk reduction.

### CRIT-004: Full-system verification is not yet a reproducible release gate

- Area: CI, testing, runtime reliability, release confidence.
- Evidence:
  - Focused tests passed for shadow-news-impact, source-health, strategy-readiness, dashboard typecheck, and API news-impact tests.
  - `scripts/preflight.ps1` exists and attempts a broad local gate, but this audit did not run it because it can be heavy and depends on local Docker/runtime state.
  - Existing docs note that route smoke proof is stale and that `/data/coverage` has historically been a timeout risk.
  - No single current receipt captures dashboard tests, API tests, route smoke, Docker readiness, and provider/live-proof status together.
- Why it matters:
  - The repo can have green slices while still being unready for a local demo, staging push, or broker-adjacent run.
  - Operators need to know which proof is code-level, which is local runtime proof, and which is hosted/live proof.
  - Without a structured receipt, repeated audits rediscover the same uncertainty.
- Recommended fix:
  - Add a structured verification receipt command that runs safe targeted checks by default and records skipped heavy/live checks explicitly.
  - Keep full preflight available, but do not make local heavy checks the only way to prove progress.
- Implementation steps:
  1. Add a script such as `scripts/verification-receipt.ps1`.
  2. Include dashboard source-health, strategy-readiness, shadow-news-impact, typecheck, and API news-impact tests.
  3. Add optional flags for Docker, browser smoke, live provider checks, and full preflight.
  4. Write a timestamped Markdown or JSON receipt under `reports/verification/`.
  5. Include command, exit code, duration, and skipped reason for each check.
- Acceptance criteria:
  - Running the receipt command produces a durable file.
  - The receipt clearly distinguishes passed, failed, skipped, and not-run checks.
  - The default command is safe for local development and does not require live credentials.
  - A future release can cite a receipt instead of prose.
- Tests needed:
  - Smoke test the receipt command on a clean local checkout.
  - Unit-test any parser/formatter if implemented in Python or TypeScript.
  - Verify non-zero child command failures are reflected in the receipt and exit code.
- Risk: Low. This is automation around existing tests, with high coordination value.

## 5. High Priority Improvements

### HIGH-001: Make the shadow news-impact receipt durable and API-complete

- Area: ML/shadow-model safety, API contracts, dashboard validation.
- Current state: Shadow news-impact has a read-only API and a dashboard panel. Focused dashboard tests and API tests pass.
- Problem: The current proof is split across test output and docs. The `/news-impact/signals` endpoint silently skips malformed Redis rows and does not report a skipped count or parse quality receipt.
- Evidence:
  - `services/api/src/api/routes/news_impact.py` labels the surface experimental and read-only.
  - The signals route deserializes Redis stream entries and skips malformed rows.
  - `apps/dashboard/scripts/run-shadow-news-impact-tests.cjs` runs a focused UI test but does not write a durable receipt.
  - `apps/dashboard/src/components/news-impact/shadow-news-impact-panel.tsx` shows shadow-only copy and avoids trade controls.
- Impact: Shadow-model readiness is harder to review, and malformed stream data can be invisible unless logs are inspected.
- Recommended improvement: Add `skipped_count`, `raw_count`, `accepted_count`, and a sanitized parse-warning field to the API response. Add a receipt-writing test runner that captures UI and API checks.
- Implementation approach:
  1. Extend the API response schema for `/news-impact/signals`.
  2. Add tests for malformed rows.
  3. Update the dashboard panel only if new fields need visible operator treatment.
  4. Add `reports/shadow-news-impact/` receipt output.
- Estimated effort: Medium.
- Risk level: Low to medium.
- Dependencies: Existing API tests and dashboard shadow panel tests.
- Acceptance criteria:
  - Malformed stream entries are counted and do not break the endpoint.
  - The UI still has no trade-driving controls.
  - A durable receipt records command output and summary counts.
- Suggested tests:
  - `uv run pytest services/api/tests/test_news_impact.py -q`
  - `npm run test:shadow-news-impact`

### HIGH-002: Build a mock-route atlas and replace the first mock-heavy route with a service-backed contract

- Area: Product readiness, UX trust, data contracts.
- Current state: Several dashboard routes are clearly labeled as mock/demo, which is good. Mock usage is spread across `positions`, `watchlist`, `symbol/[symbol]`, overview preview components, and demo surfaces.
- Problem: There is no single route atlas that tells operators which screens are live, mocked, hybrid, or demo-only.
- Evidence:
  - `apps/dashboard/src/lib/mock-data.ts` defines mock data expectations.
  - `apps/dashboard/src/app/positions/page.tsx` uses `MockBadge`.
  - `apps/dashboard/src/app/watchlist/page.tsx` includes an explicit mock replacement note.
  - `apps/dashboard/src/app/symbol/[symbol]/page.tsx` uses multiple `MockBadge` surfaces.
  - `apps/dashboard/src/components/overview/watchlist-preview.tsx` uses mock data.
- Impact: Users can mistake designed screens for production workflows, and engineers lack a prioritized path from fixtures to service-backed data.
- Recommended improvement: Generate or maintain a route atlas with columns for route, data source, mock badge presence, backend dependency, readiness, and replacement contract. Start by replacing one high-visibility mock route, such as watchlist or symbol snapshot.
- Implementation approach:
  1. Add `docs/dashboard-route-atlas.md` or a generated report under `reports/`.
  2. Use `rg`/AST scan to find `MockBadge`, `mock-data`, and inline fixture notes.
  3. Define the first backend contract for watchlist or symbol summary.
  4. Add API and UI tests for that route.
- Estimated effort: Medium.
- Risk level: Low.
- Dependencies: Backend data availability and route contract choice.
- Acceptance criteria:
  - Every dashboard route has a live/mock/hybrid/demo status.
  - One route moves from fixture-only to service-backed data.
  - Mock badges remain visible for any remaining fixture data.
- Suggested tests:
  - Dashboard route unit tests for the selected screen.
  - API contract tests for the selected endpoint.
  - Typecheck.

### HIGH-003: Time-box and receipt `/data/coverage` route behavior

- Area: API reliability, data readiness, route smoke.
- Current state: `/data/coverage` has structured error rows for some failures and uses database coverage helpers, but docs still treat route smoke as stale and call out a historical timeout.
- Problem: A slow coverage query can make system readiness checks unreliable, and stale smoke evidence keeps reappearing in docs.
- Evidence:
  - `services/api/src/api/routes/data.py` implements `/data/coverage`.
  - `libs/fincept-db/src/fincept_db/bars.py` implements `read_bar_coverage`.
  - Existing project-understanding docs mention route-smoke staleness and `/data/coverage` timeout risk.
- Impact: Data-readiness UI and preflight checks can be blocked by a single slow endpoint.
- Recommended improvement: Add endpoint-level time bounds, pagination or symbol limits, and a route-smoke receipt that captures duration and row counts.
- Implementation approach:
  1. Define acceptable limits for symbols/lookback/timeframe coverage.
  2. Add request validation and defaults.
  3. Add timeout handling with sanitized errors.
  4. Add a smoke script that records duration and response shape.
- Estimated effort: Medium.
- Risk level: Medium, because data readiness behavior may change.
- Dependencies: Database fixture or local test database.
- Acceptance criteria:
  - Route returns bounded responses under expected local data sizes.
  - Slow database reads fail with a useful, sanitized status.
  - Smoke proof is durable and repeatable.
- Suggested tests:
  - API tests with small fixtures.
  - Route smoke against local test database.
  - Regression test for timeout behavior.

### HIGH-004: Move dashboard auth away from localStorage and query-string WebSocket tokens

- Area: Auth, browser security, session reliability.
- Current state: Dashboard auth stores JWTs in `localStorage`; WebSocket auth uses a query-string token.
- Problem: Tokens in `localStorage` are exposed to XSS, and query-string tokens can leak through browser, proxy, or server logs.
- Evidence:
  - `apps/dashboard/src/lib/auth.ts` documents localStorage-backed JWT storage.
  - `apps/dashboard/src/lib/ws.ts` builds `/ws/stream?token=...`.
  - `services/api/src/api/ws.py` accepts WebSocket auth through headers or query parameters.
  - `apps/dashboard/README.md` already states Phase H should use httpOnly cookies and OAuth.
- Impact: Acceptable for early local development, but not appropriate for staging or production.
- Recommended improvement: Introduce httpOnly secure cookies for dashboard sessions, CSRF protection for state-changing routes, and a WebSocket auth handshake that avoids token query strings.
- Implementation approach:
  1. Keep existing local dev auth behind a development mode if needed.
  2. Add cookie-based session issuance from the API or dashboard backend.
  3. Add CSRF tokens for state-changing browser requests.
  4. Update WebSocket handshake to use cookies or a short-lived one-time token exchanged through a POST response.
- Estimated effort: Large.
- Risk level: Medium to high.
- Dependencies: Auth provider decision and deployment domain/cookie model.
- Acceptance criteria:
  - JWTs are not stored in `localStorage` in staging/prod mode.
  - WebSocket URLs do not contain bearer tokens.
  - Existing local dev auth remains documented.
  - State-changing routes have CSRF coverage.
- Suggested tests:
  - Auth unit tests.
  - Browser/session integration tests.
  - WebSocket auth tests.

### HIGH-005: Harden CI and container supply chain defaults

- Area: CI/CD, supply chain, reproducibility.
- Current state: CI exists and includes useful checks, including gitleaks. Some actions and images use mutable versions.
- Problem: Mutable action refs and image tags reduce reproducibility and increase supply-chain exposure.
- Evidence:
  - `.github/workflows/ci.yml` uses `astral-sh/setup-uv@v3` with `version: latest`.
  - `.github/workflows/ci.yml` runs `pnpm install --frozen-lockfile=false`.
  - `.github/workflows/nightly.yml` uses `aquasecurity/trivy-action@master`.
  - `docker-compose.yml` uses `timescale/timescaledb:latest-pg16` and `minio/minio:latest`.
  - `.github/workflows/build-images.yml` publishes a `latest` image tag.
- Impact: Builds can change without repo changes, and production-adjacent image selection can be ambiguous.
- Recommended improvement: Pin actions to SHAs or stable immutable versions, restore frozen lockfile installs where possible, and avoid relying on `latest` tags outside local dev.
- Implementation approach:
  1. Inventory all workflow actions and container images.
  2. Pin high-risk mutable refs.
  3. Decide whether `latest` image publishing is dev-only or remove it.
  4. Move local-only Docker tags into a clearly documented local profile if needed.
- Estimated effort: Medium.
- Risk level: Medium, because lockfile strictness may reveal dependency drift.
- Dependencies: Lockfile cleanup.
- Acceptance criteria:
  - CI action refs are pinned or intentionally justified.
  - Dashboard install uses a frozen lockfile unless documented otherwise.
  - Images used for deploy are immutable SHA or version tags.
- Suggested tests:
  - CI dry run where available.
  - Local `pnpm --dir apps/dashboard install --frozen-lockfile` after lockfile alignment.
  - Workflow lint if available.

## 6. Medium Priority Improvements

### MED-001: Add a structured preflight receipt instead of console-only validation

- Area: Developer workflow, release confidence, automation.
- Current state: `scripts/preflight.ps1` runs many valuable checks but primarily emits console output.
- Problem: Console proof is not durable, hard to cite, and hard to compare between runs.
- Evidence:
  - `scripts/preflight.ps1` runs Docker, Python checks, migrations, pytest, dashboard lint/typecheck/test/build, and gitleaks.
  - Existing docs repeatedly distinguish between code-level proof and runtime proof.
- Impact: Handoffs require prose summaries instead of machine-readable receipts.
- Recommended improvement: Add JSON/Markdown receipt output with status, command, duration, exit code, and skipped reason.
- Implementation approach: Extend preflight or add a wrapper that invokes safe check groups and writes `reports/preflight/<timestamp>.md`.
- Estimated effort: Medium.
- Risk level: Low.
- Dependencies: None beyond existing scripts.
- Acceptance criteria: Receipt is written on pass and fail; failures preserve enough detail to debug.
- Suggested tests: Run the receipt script with one passing command and one forced failing command in a test mode.

### MED-002: Add timeouts and cancellation to dashboard API clients and LLM provider calls

- Area: Reliability, UX, external API readiness.
- Current state: `apps/dashboard/src/lib/api.ts` centralizes fetch behavior but does not apply a standard timeout. `apps/dashboard/src/app/api/portfolio-report/route.ts` calls OpenAI/Anthropic without an explicit request timeout.
- Problem: Slow API or LLM calls can hang user flows and make local demos feel broken.
- Evidence:
  - `apps/dashboard/src/lib/api.ts` builds fetch requests with `cache: "no-store"` and bearer headers, but no `AbortController` timeout.
  - `apps/dashboard/src/app/api/portfolio-report/route.ts` calls provider APIs directly with server-side API keys.
- Impact: Unbounded waits produce poor UX and make error recovery inconsistent.
- Recommended improvement: Add a shared timeout helper and consistent retry/no-retry rules for idempotent requests.
- Implementation approach: Use `AbortController`, provider-specific timeout config, and user-friendly errors.
- Estimated effort: Medium.
- Risk level: Low.
- Dependencies: Decide default timeout budgets.
- Acceptance criteria: API calls fail with consistent typed errors after timeout; UI displays useful states.
- Suggested tests: Unit tests for timeout behavior and provider failure handling.

### MED-003: Add provider evidence redaction, retention, and replay policy

- Area: Provider integrations, auditability, privacy.
- Current state: Alpaca/news sync and marks have useful cache/TTL behavior. Provider proof is not yet a standardized ledger.
- Problem: Provider diagnostics can become either too thin to audit or too raw to store safely.
- Evidence:
  - `services/oms/src/oms/alpaca/news_sync.py` uses TTL/caps and API concurrency limits.
  - `services/oms/src/oms/alpaca/marks.py` writes TTL'd Redis marks.
  - Existing docs mention provider evidence redaction/storage as a follow-up.
- Impact: Live-provider readiness is harder to prove safely.
- Recommended improvement: Add a provider evidence receipt schema with redacted request metadata, response summary, timestamp, provider, dataset, and retention class.
- Implementation approach: Store summaries in `provider_data` or reports, never raw secrets or full sensitive payloads.
- Estimated effort: Medium to large.
- Risk level: Medium.
- Dependencies: Retention policy decision.
- Acceptance criteria: Receipts prove provider calls happened without exposing credentials or sensitive data.
- Suggested tests: Redaction unit tests and provider fixture replay tests.

### MED-004: Reconcile documentation status with implementation state

- Area: Documentation, roadmap accuracy, onboarding.
- Current state: Docs are useful but contain some historical status sections and "needs verification" notes.
- Problem: New contributors can confuse historical stubs with current implementation.
- Evidence:
  - `README.md` has a strong current status section, but also older sections describing some components as stubs.
  - `services/agents/src/agents/__init__.py` shows many implemented agents and one explicit `pairs` stub.
  - `docs/project-understanding/06-current-status.md` is more current than some root README sections.
- Impact: Planning and implementation order can drift.
- Recommended improvement: Add a short "status authority" note that explains which docs are canonical and which are historical.
- Implementation approach: Update README and docs index after the next implementation slice, not during risky code changes.
- Estimated effort: Small.
- Risk level: Low.
- Dependencies: None.
- Acceptance criteria: Setup, status, roadmap, and risks agree on current source-of-truth.
- Suggested tests: Documentation link check if available.

### MED-005: Add resource limits to streaming/WebSocket infrastructure

- Area: Reliability, real-time UX, Redis usage.
- Current state: `services/api/src/api/ws.py` creates a Redis client per WebSocket stream and loops while connected.
- Problem: Without explicit connection limits, idle timeouts, and backpressure policy, real-time streams can become expensive under load.
- Evidence:
  - `services/api/src/api/ws.py` opens Redis for `/ws/stream`.
  - The route supports dynamic stream subscription through path data.
- Impact: A small number of bad clients can increase Redis and API resource usage.
- Recommended improvement: Add connection caps, subscription allowlists, idle timeout, and metrics.
- Implementation approach: Track active connections in app state, reject unknown streams, and close idle clients gracefully.
- Estimated effort: Medium.
- Risk level: Medium.
- Dependencies: Desired stream list and observability choice.
- Acceptance criteria: Unknown streams reject; idle clients disconnect; active stream count is observable.
- Suggested tests: WebSocket unit/integration tests with fake Redis.

## 7. Low Priority / Nice-To-Have Improvements

### LOW-001: Add a dashboard command palette for operator workflows

- Area: UX, productivity.
- Current state: Many useful routes and receipts exist or are planned, but navigation can become dense.
- Problem: Operators may need to jump quickly between system health, source health, strategy readiness, receipts, and labs.
- Evidence:
  - Dashboard routes include system, receipts, research, risk, strategies, news labs, and portfolio builder.
- Impact: Lower discoverability as the product grows.
- Recommended improvement: Add a keyboard-accessible command palette that links to major workflows and recent receipts.
- Implementation approach: Start with static route entries, then add dynamic recent receipts.
- Estimated effort: Medium.
- Risk level: Low.
- Dependencies: Route atlas.
- Acceptance criteria: Keyboard shortcut opens palette; entries are searchable; no route behavior changes.
- Suggested tests: Component tests and accessibility checks.

### LOW-002: Add lightweight SEO/social metadata for public-facing docs or demo pages

- Area: Product polish, sharing.
- Current state: The app is primarily an internal/operator dashboard, so SEO is not a core risk.
- Problem: Public demo pages or generated reports may share poorly if Open Graph metadata is absent.
- Evidence: Dashboard is route-rich and includes report-like experiences, but this audit did not find a dedicated metadata strategy.
- Impact: Low for internal workflows; moderate for public demos.
- Recommended improvement: Add per-page metadata only for public/demo surfaces that need sharing.
- Implementation approach: Use Next.js metadata exports and avoid adding SEO noise to internal authenticated routes.
- Estimated effort: Small.
- Risk level: Low.
- Dependencies: Public route decision.
- Acceptance criteria: Shared public demo links render useful title/description/preview.
- Suggested tests: Metadata snapshot tests if already used.

### LOW-003: Create ADRs for major safety decisions

- Area: Architecture documentation.
- Current state: Safety decisions are spread across README, roadmap, risks, and project-understanding docs.
- Problem: Important decisions such as shadow-only ML, paper-only OMS defaults, and route-mock policy are not all captured as immutable decisions.
- Evidence:
  - `docs/RISKS.md`
  - `docs/ROADMAP.md`
  - `docs/project-understanding/`
- Impact: Future contributors may reopen settled decisions.
- Recommended improvement: Add `docs/adr/` with short records for runtime safety, shadow-model promotion, auth migration, and mock-data policy.
- Implementation approach: Start with four ADRs, each under 1 page.
- Estimated effort: Small.
- Risk level: Low.
- Dependencies: None.
- Acceptance criteria: ADRs link to source files and current tests.
- Suggested tests: Markdown link check if available.

## 8. Feature Roadmap

### FEATURE-001: Unified System Readiness Center

- User value: Operators can see whether the system is safe to run, demo, or promote without reading multiple docs and terminal logs.
- Problem solved: Readiness proof is scattered across source-health, strategy-readiness, shadow-news-impact, route smoke, docs, and manual notes.
- Proposed behavior: A dashboard page and generated receipt summarize checks as passed, failed, skipped, or stale.
- UI changes: Add or extend `apps/dashboard/src/app/system/page.tsx` or `apps/dashboard/src/app/receipts/page.tsx` with readiness cards, last receipt time, and links to detailed reports.
- Backend changes: Add a read-only endpoint that serves latest verification receipts from a safe reports directory, or generate static JSON consumed by the dashboard.
- Data/schema changes: Optional JSON receipt schema under `reports/verification/`.
- API/integration changes: None required for v1 beyond serving local receipt files safely.
- Automation opportunity: `scripts/verification-receipt.ps1` writes machine-readable receipts after local checks.
- Implementation steps:
  1. Define receipt schema.
  2. Add default safe check runner.
  3. Add dashboard display for latest receipt.
  4. Add stale/failed/skipped states.
- Acceptance criteria:
  - Latest readiness receipt is visible.
  - Skipped live checks are explicit.
  - Failed checks include command and exit code.
  - No secrets are written to receipts.
- Tests needed:
  - Receipt schema tests.
  - Dashboard component tests.
  - Smoke test for missing receipt empty state.
- Priority: High.

### FEATURE-002: Dashboard Route Atlas and Mock Replacement Queue

- User value: Users and developers can tell which screens are live, mock, hybrid, or demo-only.
- Problem solved: Mock usage is visible in components but not managed as a product queue.
- Proposed behavior: A generated or maintained atlas lists each route, data source, backend contract, mock badge status, and replacement priority.
- UI changes: Optional internal page under system/receipts; v1 can be Markdown only.
- Backend changes: Add contracts for the first replacement route, likely watchlist or symbol snapshot.
- Data/schema changes: Depends on selected route; watchlist likely needs user/watchlist schema, while symbol snapshot may reuse market data.
- API/integration changes: Add or extend read-only API endpoint for selected route.
- Automation opportunity: Script scans for `MockBadge`, `mock-data`, and `MOCK:` comments.
- Implementation steps:
  1. Generate route atlas.
  2. Pick one replacement route.
  3. Define API response schema.
  4. Replace fixture usage while keeping fallback labels.
- Acceptance criteria:
  - Every route has a readiness status.
  - One mock-heavy route is backed by an API contract.
  - Mock badges remain where fixture data remains.
- Tests needed:
  - Atlas script test.
  - API contract test.
  - Dashboard route test.
- Priority: High.

### FEATURE-003: Shadow Model Promotion Dossier

- User value: Researchers can review whether a model is ready to remain shadow-only, expand scope, or be retired.
- Problem solved: Shadow model evidence is split between experiments, API output, dashboard panels, and ad hoc tests.
- Proposed behavior: A dossier page/report collects evaluation metrics, calibration, error analysis, skipped stream rows, UI proof, and non-trading safety checks.
- UI changes: Extend `apps/dashboard/src/app/news-impact-lab/page.tsx` or add a receipt detail view.
- Backend changes: Add read-only access to model evaluation summaries and stream parse quality.
- Data/schema changes: Optional model receipt schema under `reports/shadow-news-impact/`.
- API/integration changes: Extend `/news-impact/status` and `/news-impact/signals` with receipt metadata.
- Automation opportunity: One command runs experiment fixture checks, API tests, UI tests, and writes a promotion dossier.
- Implementation steps:
  1. Define promotion dossier schema.
  2. Extend API parse-quality metadata.
  3. Add report writer.
  4. Display latest dossier in dashboard.
- Acceptance criteria:
  - Dossier states shadow-only status clearly.
  - No trade controls or sizing fields are introduced.
  - Model evidence is reproducible from a command.
- Tests needed:
  - API tests for parse quality.
  - Dashboard shadow panel tests.
  - Report generation tests.
- Priority: High.

### FEATURE-004: Provider Evidence Ledger

- User value: Operators can prove what external data was fetched, when, and from which provider without exposing secrets.
- Problem solved: Provider readiness and freshness are hard to audit safely.
- Proposed behavior: Store redacted provider request/response summaries with freshness, dataset, symbol, provider, and retention metadata.
- UI changes: Add provider evidence section to source-health or system route.
- Backend changes: Add provider receipt writer and read-only query endpoint.
- Data/schema changes: Use or extend provider-data tables; add retention class and redaction summary if needed.
- API/integration changes: Alpaca/news sync and future providers write receipts.
- Automation opportunity: Nightly provider freshness report and alerts on stale data.
- Implementation steps:
  1. Define redaction rules.
  2. Add receipt writer.
  3. Add local fixture replay.
  4. Add freshness UI.
- Acceptance criteria:
  - Receipts never contain API keys.
  - Provider call summaries are queryable.
  - Stale provider data is visible.
- Tests needed:
  - Redaction tests.
  - Provider fixture tests.
  - Source-health dashboard tests.
- Priority: Medium.

### FEATURE-005: Service-Backed Paper Spine Replay

- User value: Developers can replay a paper trading path through real service boundaries rather than a mostly standalone script.
- Problem solved: Paper-spine proof exists, but the system still needs clearer evidence across orchestrator, strategy host, OMS, Redis, and dashboard surfaces.
- Proposed behavior: A safe replay mode emits synthetic market events, strategy decisions, paper orders, OMS marks, and dashboard receipts without live broker effects.
- UI changes: Add replay receipts to `apps/dashboard/src/app/receipts/page.tsx`.
- Backend changes: Add a bounded replay harness around existing services.
- Data/schema changes: Optional replay run table or report files.
- API/integration changes: None for live brokers; must remain paper-only.
- Automation opportunity: Scheduled or manual paper-spine regression.
- Implementation steps:
  1. Define replay fixture and expected events.
  2. Run through service boundaries with paper mode enforced.
  3. Capture Redis stream and OMS mark receipts.
  4. Expose latest run in dashboard.
- Acceptance criteria:
  - Replay cannot place live orders.
  - Events cross expected service boundaries.
  - Receipt shows decisions, orders, and marks.
- Tests needed:
  - Service integration test with fake Redis or local Redis.
  - Paper-only guard tests.
  - Dashboard receipt display tests.
- Priority: Medium.

### FEATURE-006: Runtime Safety Matrix

- User value: Operators know which processes are safe to launch in each environment.
- Problem solved: Runtime safety enforcement is currently uneven and not visible.
- Proposed behavior: A generated matrix lists service entrypoints, required env vars, fail-closed checks, broker mode, and startup receipt status.
- UI changes: Optional system page matrix.
- Backend changes: Add a shared startup receipt helper.
- Data/schema changes: JSON receipt schema.
- API/integration changes: None.
- Automation opportunity: CI test that fails if a service lacks the safety guard.
- Implementation steps:
  1. Apply config guard to all services.
  2. Add service safety tests.
  3. Generate matrix.
  4. Link matrix from docs and dashboard.
- Acceptance criteria:
  - Every service has a documented safety status.
  - Tests fail on missing guard.
  - Matrix includes broker/live-mode restrictions.
- Tests needed:
  - Config guard tests.
  - Startup matrix generation test.
- Priority: High.

## 9. Automation Opportunities

- Repo health checks:
  - Create `scripts/repo-health.ps1` to summarize git status, untracked top-level directories, lockfile drift, ignored local artifacts, and risky file names.
- Test runners:
  - Combine `npm run test:source-health`, `npm run test:strategy-readiness`, `npm run test:shadow-news-impact`, `pnpm --dir apps/dashboard exec tsc --noEmit --pretty false`, and selected API tests into a receipt-writing safe default command.
- Data validation:
  - Add coverage route smoke with duration, symbol count, timeframes, and database row counts.
  - Add provider freshness validation using `provider_data` and Redis marks.
- Report generation:
  - Generate shadow-news-impact promotion dossiers.
  - Generate dashboard route/mock atlas.
  - Generate runtime safety matrix.
- Dependency updates:
  - Add a controlled dependency update workflow that runs lockfile-strict installs and targeted tests.
- Changelog generation:
  - Generate a local changelog from grouped commits or release notes after the dirty tree is partitioned.
- Lint/type checks:
  - Keep dashboard typecheck and Python ruff/mypy slices in fast local receipts.
- Backup/export flows:
  - Add export scripts for provider receipt summaries, model dossiers, and paper-spine replay receipts.
- API sync jobs:
  - Schedule provider freshness checks and source-health receipts.
- Monitoring alerts:
  - Add stale data alerts, Redis stream lag alerts, failed startup guard alerts, and provider error-rate alerts.

## 10. API and Integration Opportunities

### Integration/API name: Provider Evidence API

- Use case: Read redacted provider call summaries and freshness state.
- Where it fits in the system: `services/api/src/api/routes/` plus provider-data storage in `libs/fincept-db`.
- Required credentials/env vars: None for read-only receipts; provider writers require existing provider credentials.
- Risks: Accidentally storing raw payloads, secrets, or account identifiers.
- Recommended implementation path: Define a redacted receipt schema first; write fixture tests before connecting live providers.

### Integration/API name: Watchlist/Symbol Snapshot API

- Use case: Replace mock-heavy watchlist and symbol pages with service-backed data.
- Where it fits in the system: New or extended API route in `services/api/src/api/routes/`; dashboard client in `apps/dashboard/src/lib/api.ts`.
- Required credentials/env vars: None if backed by local market data; provider credentials only if live enrichment is enabled.
- Risks: Slow or missing data can degrade dashboards unless empty/loading/error states are explicit.
- Recommended implementation path: Start read-only, fixture-backed, and typed; add provider/live enrichment later.

### Integration/API name: Verification Receipt API

- Use case: Serve latest local verification receipts to the dashboard.
- Where it fits in the system: API read-only route or dashboard server route reading safe report files.
- Required credentials/env vars: None.
- Risks: Serving arbitrary files if report path boundaries are too broad.
- Recommended implementation path: Serve only from an allowlisted reports directory with file suffix checks and path prefix validation.

### Integration/API name: Auth Provider/OAuth

- Use case: Replace localStorage token auth before staging/prod use.
- Where it fits in the system: API auth, dashboard session handling, WebSocket handshake.
- Required credentials/env vars: OAuth client ID/secret, cookie secret, redirect URLs.
- Risks: Session bugs, CSRF gaps, and local developer friction.
- Recommended implementation path: Introduce cookie sessions behind a feature flag, test state-changing routes, then migrate WebSocket auth.

### Integration/API name: Observability Exporter

- Use case: Export service health, route timings, Redis stream lag, and provider freshness.
- Where it fits in the system: `libs/fincept-core` tracing/logging helpers and service entrypoints.
- Required credentials/env vars: Depends on backend choice, such as OTLP endpoint.
- Risks: Logging sensitive data or adding too much cardinality.
- Recommended implementation path: Start with local structured logs and receipt files; add external observability later.

## 11. Testing and Quality Plan

### Unit Tests

- Config safety:
  - Test `libs/fincept-core/src/fincept_core/config.py` for dev/local/test allowed behavior and staging/prod failure with default secrets.
- Path validation:
  - Test backtest path allowlist, traversal rejection, suffix checks, and sanitized errors.
- Provider redaction:
  - Test that provider receipts cannot include API keys or bearer tokens.
- Dashboard API timeout helper:
  - Test timeout and typed error behavior.

Suggested commands:

```powershell
uv run pytest libs/fincept-core/tests -q
uv run pytest services/api/tests -q
```

### Integration Tests

- API news-impact:

```powershell
uv run pytest services/api/tests/test_news_impact.py -q
```

- Backtest API path boundary tests once added:

```powershell
uv run pytest services/api/tests -q -k backtest
```

- Service startup safety matrix:

```powershell
uv run pytest services -q -k runtime_safety
```

### End-to-End Tests

- Add a browser smoke for:
  - login/dev auth
  - system page
  - receipts page
  - news-impact lab
  - watchlist or symbol page after mock replacement
- Keep it optional locally until stable:

```powershell
pnpm --dir apps/dashboard exec playwright test
```

### Smoke Tests

- Safe default receipt:

```powershell
pwsh ./scripts/verification-receipt.ps1
```

- Full preflight when Docker/local services are expected:

```powershell
pwsh ./scripts/preflight.ps1
```

### Regression Tests

- Keep focused dashboard regressions:

```powershell
npm run test:shadow-news-impact
npm run test:source-health
npm run test:strategy-readiness
```

Run from `apps/dashboard`.

### Data Validation Tests

- Add coverage route fixture tests.
- Add provider freshness fixture tests.
- Add stream malformed-row tests for news impact.

### Build Checks

```powershell
pnpm --dir apps/dashboard exec tsc --noEmit --pretty false
pnpm --dir apps/dashboard build
```

### Type/Lint Checks

```powershell
uv run ruff check .
uv run mypy libs services
pnpm --dir apps/dashboard lint
pnpm --dir apps/dashboard exec tsc --noEmit --pretty false
```

## 12. Security and Reliability Review

### Secrets Handling

- Good:
  - `.env.example` uses placeholder dev values.
  - Dashboard provider keys for portfolio reports are server-side env vars, not `NEXT_PUBLIC_*`.
  - CI includes gitleaks.
- Needs improvement:
  - The default JWT secret must be rejected consistently across all service entrypoints, not just API.
  - Local `.env` exists in the working tree environment and is ignored, but release hygiene should still avoid broad staging.
  - Workflow and receipt scripts should redact env values by default.

### Env Vars

- Good:
  - Central settings live in `libs/fincept-core/src/fincept_core/config.py`.
- Needs improvement:
  - Create an env var reference table with runtime mode, default, required environments, and secret/non-secret classification.
  - Add startup receipts that record which required vars are present without values.

### Auth

- Good:
  - API routes use `require_user` where inspected.
- Needs improvement:
  - `apps/dashboard/src/lib/auth.ts` stores JWTs in `localStorage`.
  - `apps/dashboard/src/lib/ws.ts` sends WebSocket tokens in the URL query string.
  - `services/api/src/api/auth.py` returns `invalid token: {exc}`; sanitize this before production because decoder messages can reveal too much about token failure modes.

### Permissions

- Needs improvement:
  - Backtest file access should use explicit allowlisted roots.
  - Verification receipt serving must use prefix checks if exposed through an API.

### API Key Exposure

- Good:
  - Portfolio report provider errors are generic.
- Needs improvement:
  - Provider receipt work must include redaction tests before live integration.

### Data Validation

- Good:
  - Pydantic models are used in API surfaces.
- Needs improvement:
  - Backtest path validation is too broad.
  - News-impact malformed stream rows should be counted.

### Dependency Risk

- Good:
  - Lockfiles exist.
  - CI/nightly includes dependency and container scanning.
- Needs improvement:
  - Avoid mutable action refs and image tags.
  - Restore strict dashboard lockfile install once lockfile state is stable.

### Unsafe File Handling

- High concern:
  - `services/api/src/api/routes/backtest.py` accepts arbitrary absolute/relative `bars_path` values.

### Logging Sensitive Data

- Needs improvement:
  - `services/api/src/api/training.py` writes command arguments to logs. Current args appear path/config oriented, but future sensitive args should be redacted.
  - Token decoder errors should not be returned to clients.

### Backup/Restore

- Needs improvement:
  - Add backup/export flow for receipts, provider evidence, model dossiers, and local database snapshots.

### Rollback Safety

- Needs improvement:
  - Use immutable image tags for deployable builds.
  - Add a release checklist with last known good verification receipt.

## 13. Performance Review

- Slow startup:
  - `scripts/preflight.ps1` is comprehensive and may be too heavy for frequent use. Add a safe fast receipt command and reserve full preflight for release.
- Unnecessary network calls:
  - LLM portfolio report provider calls do not appear to have explicit timeouts or caching.
- Inefficient loops:
  - No obvious critical inefficient loop was found in inspected code. Some stream and coverage paths need bounded behavior under larger data.
- Large bundle size:
  - This audit did not run bundle analysis. Add bundle analysis after route readiness work.
- Repeated computation:
  - Coverage and source-health style endpoints should cache or bound repeated expensive checks.
- Unindexed queries:
  - Bar coverage query uses indexed dimensions in `libs/fincept-db/src/fincept_db/bars.py`. Continue validating with realistic row counts.
- Memory-heavy operations:
  - Backtest runs synchronously in the API process and should move to a bounded worker/job model for larger files.
- Poor caching:
  - Dashboard API client uses `cache: "no-store"` globally. That is safe for trading-adjacent freshness but may be wasteful for static or receipt-like data.

Recommended performance actions:

1. Add route timing receipts for `/data/coverage`, `/news-impact/status`, `/news-impact/signals`, and core dashboard APIs.
2. Add `AbortController` timeouts to dashboard fetches.
3. Move long-running backtests out of synchronous API handlers.
4. Add a bundle-size check only after the dashboard route atlas is stable.

## 14. UX / Product Review

- Confusing screens:
  - Mock/demo screens are labeled, but the overall route readiness status is not centralized.
- Missing states:
  - Some readiness and provider workflows need explicit stale/skipped/not-run states, not only pass/fail.
- Unclear errors:
  - API client errors are typed, but user-facing errors should distinguish auth, unavailable backend, stale data, and validation failures.
- Missing review queues:
  - Shadow model evidence needs a promotion/review dossier.
  - Provider evidence needs a reviewable freshness queue.
- Missing confirmation steps:
  - Any future route that can affect orders, model promotion, or provider sync should require confirmation and audit logging.
- Lack of audit trail:
  - Verification, provider calls, model promotion, and paper-spine replay need durable receipts.
- Weak onboarding:
  - README is strong but has historical/stale sections. A current "start here" should point to the canonical local setup and safety docs.
- Manual steps that should be automated:
  - Route mock inventory.
  - Runtime safety matrix.
  - Shadow model receipt.
  - Source-health and strategy-readiness receipt.

## 15. Documentation Improvements

- Setup guide:
  - Keep `README.md` setup instructions, but add a short "fast local validation" path versus "full preflight" path.
- Env var reference:
  - Add a table covering root and dashboard env vars, secret classification, default, and required environment.
- Architecture overview:
  - Add one diagram showing dashboard, API, Redis streams, TimescaleDB, services, agents, and external providers.
- API docs:
  - Add endpoint readiness and safety labels: live, read-only, shadow, mock-backed, state-changing.
- Troubleshooting guide:
  - Add sections for Redis unavailable, Timescale unavailable, dashboard auth failure, provider credential missing, and route smoke timeout.
- Deployment guide:
  - Document immutable image tags, env safety guard, rollback, and verification receipts.
- Contribution guide:
  - Add "do not broad-stage local artifacts" and "run targeted receipt before handing off" guidance.
- Data model docs:
  - Document bars, features, provider_data, audit_log, strategies, and expected retention.

## 16. Recommended Implementation Order

### Phase 1: Stabilize

Must-do fixes to keep the system running:

1. Apply runtime safety guard to every service entrypoint.
2. Lock down backtest file path handling.
3. Partition dirty worktree and ignore local-only tool artifacts.
4. Add a safe verification receipt runner for current focused tests.
5. Sanitize token error responses.

### Phase 2: Make Development Safer

Tests, linting, validation, docs, CI:

1. Add service startup safety matrix tests.
2. Add backtest path boundary tests.
3. Harden CI action/image pinning.
4. Add route-smoke receipt with `/data/coverage` timing.
5. Add env var reference and canonical docs index.

### Phase 3: Improve Core Workflows

UX, automation, data flow, reliability:

1. Build dashboard route/mock atlas.
2. Replace first mock-heavy route with a service-backed read-only endpoint.
3. Add dashboard API timeouts and consistent error states.
4. Add provider evidence redaction and freshness receipts.
5. Add WebSocket connection limits and stream allowlists.

### Phase 4: Add High-Leverage Features

New features and integrations:

1. Unified System Readiness Center.
2. Shadow Model Promotion Dossier.
3. Provider Evidence Ledger.
4. Service-Backed Paper Spine Replay.
5. Runtime Safety Matrix dashboard view.

### Phase 5: Optimize and Scale

Performance, deployment, monitoring, advanced automation:

1. Move long-running backtests to worker/job execution.
2. Add route and provider latency metrics.
3. Add bundle-size and frontend performance checks.
4. Add immutable deploy artifact and rollback receipts.
5. Add scheduled provider freshness and verification monitors.

## 17. Top 10 Highest-Leverage Tasks

### 1. Apply runtime safety guards to all service entrypoints

- Task: Add `assert_safe_for_runtime(settings)` to ingestor, orchestrator, OMS, and strategy host before side effects.
- Why it is high leverage: One small pattern makes every process fail closed consistently.
- Estimated effort: Small to medium.
- Expected impact: High security and reliability improvement.
- Files likely touched:
  - `services/ingestor/src/ingestor/main.py`
  - `services/orchestrator/src/orchestrator/main.py`
  - `services/oms/src/oms/main.py`
  - `services/strategy_host/src/strategy_host/main.py`
  - related tests
- Acceptance criteria: Every service rejects unsafe prod-like defaults before Redis or stream startup.

### 2. Restrict backtest file paths to approved roots

- Task: Add path allowlist validation for `bars_path`.
- Why it is high leverage: Removes a clear trust-boundary weakness from an API route.
- Estimated effort: Medium.
- Expected impact: High security and reliability improvement.
- Files likely touched:
  - `services/api/src/api/routes/backtest.py`
  - `services/api/tests/`
  - `libs/fincept-core/src/fincept_core/config.py` if settings are added
- Acceptance criteria: Traversal and outside-root paths fail safely; valid fixtures still pass.

### 3. Create a safe verification receipt command

- Task: Add a receipt-writing script for focused tests and typechecks.
- Why it is high leverage: Converts scattered proof into durable, reviewable evidence.
- Estimated effort: Medium.
- Expected impact: High development and release confidence.
- Files likely touched:
  - `scripts/verification-receipt.ps1`
  - `reports/verification/`
  - docs references
- Acceptance criteria: Command writes pass/fail/skipped receipt and exits non-zero on required failures.

### 4. Add runtime safety matrix tests

- Task: Add tests that fail when service startup paths omit the runtime guard.
- Why it is high leverage: Prevents regression of the most important safety invariant.
- Estimated effort: Medium.
- Expected impact: High.
- Files likely touched:
  - service test folders
  - shared config tests
- Acceptance criteria: Removing a guard fails tests.

### 5. Add shadow news-impact API parse-quality metadata

- Task: Return accepted/skipped/raw counts for stream parsing and include them in receipts.
- Why it is high leverage: Makes shadow model data quality auditable.
- Estimated effort: Medium.
- Expected impact: Medium to high.
- Files likely touched:
  - `services/api/src/api/routes/news_impact.py`
  - `services/api/tests/test_news_impact.py`
  - `apps/dashboard/src/components/news-impact/shadow-news-impact-panel.tsx` if shown
- Acceptance criteria: Malformed rows are counted and visible to tests or UI.

### 6. Generate dashboard mock-route atlas

- Task: Create a route inventory of live/mock/hybrid/demo surfaces.
- Why it is high leverage: Turns product readiness into a manageable queue.
- Estimated effort: Medium.
- Expected impact: Medium to high.
- Files likely touched:
  - `docs/dashboard-route-atlas.md`
  - optional script under `scripts/` or `apps/dashboard/scripts/`
- Acceptance criteria: Every dashboard route has readiness and data-source status.

### 7. Time-box `/data/coverage`

- Task: Add bounds and smoke receipt for coverage endpoint performance.
- Why it is high leverage: Removes a known readiness-smoke blocker.
- Estimated effort: Medium.
- Expected impact: Medium.
- Files likely touched:
  - `services/api/src/api/routes/data.py`
  - `libs/fincept-db/src/fincept_db/bars.py`
  - API tests
- Acceptance criteria: Coverage route has predictable duration or sanitized timeout behavior.

### 8. Add dashboard API and provider-call timeouts

- Task: Add timeout helpers to dashboard API and LLM provider calls.
- Why it is high leverage: Prevents hung UX during backend/provider slowness.
- Estimated effort: Medium.
- Expected impact: Medium.
- Files likely touched:
  - `apps/dashboard/src/lib/api.ts`
  - `apps/dashboard/src/app/api/portfolio-report/route.ts`
  - related tests
- Acceptance criteria: Slow calls fail with typed, user-friendly errors.

### 9. Harden CI mutable refs and lockfile behavior

- Task: Pin mutable workflow refs and restore strict dashboard lockfile installs.
- Why it is high leverage: Improves reproducibility and supply-chain posture.
- Estimated effort: Medium.
- Expected impact: Medium.
- Files likely touched:
  - `.github/workflows/ci.yml`
  - `.github/workflows/nightly.yml`
  - `.github/workflows/build-images.yml`
  - `docker-compose.yml` if local image tags are pinned
- Acceptance criteria: Mutable refs are removed or explicitly justified; lockfile install is deterministic.

### 10. Add provider evidence redaction tests

- Task: Define provider evidence schema and redaction tests before live receipt storage.
- Why it is high leverage: Enables provider readiness without leaking secrets.
- Estimated effort: Medium.
- Expected impact: Medium.
- Files likely touched:
  - `services/oms/src/oms/alpaca/`
  - `libs/fincept-db/src/fincept_db/provider_data.py`
  - API/dashboard source-health surfaces
- Acceptance criteria: Provider receipts prove freshness without storing credentials or sensitive payloads.

## 18. Suggested Codex Implementation Prompts

### Prompt 1: Apply runtime safety guards to all service entrypoints

Goal: Make every long-running service fail closed with the same runtime config guard used by the API.

Files to inspect:

- `libs/fincept-core/src/fincept_core/config.py`
- `services/api/src/api/main.py`
- `services/ingestor/src/ingestor/main.py`
- `services/orchestrator/src/orchestrator/main.py`
- `services/oms/src/oms/main.py`
- `services/strategy_host/src/strategy_host/main.py`

Constraints:

- Do not change runtime behavior beyond fail-closed config validation.
- Do not log secret values.
- Keep changes small and reviewable.

Implementation steps:

1. Import `assert_safe_for_runtime` where needed.
2. Call it immediately after `settings = get_settings()` and before Redis, streams, schedulers, broker clients, or heartbeat startup.
3. Add focused tests proving unsafe prod-like defaults fail before side effects.
4. Update docs only if needed to mention the new invariant.

Tests to run:

- `uv run pytest libs/fincept-core/tests -q`
- Targeted service startup/config tests added in this change.

Expected output:

- Code patch, tests, and a short summary of which services now enforce the guard.

### Prompt 2: Lock down backtest file path handling

Goal: Prevent the backtest API from reading arbitrary local paths.

Files to inspect:

- `services/api/src/api/routes/backtest.py`
- `services/api/tests/`
- `libs/fincept-core/src/fincept_core/config.py`
- `docs/RISKS.md`

Constraints:

- Preserve existing valid local fixture workflows.
- Reject traversal and outside-root absolute paths.
- Return sanitized client errors.

Implementation steps:

1. Add approved backtest data roots to settings or route-local config.
2. Resolve paths and enforce prefix checks.
3. Allow only expected suffixes such as `.parquet`.
4. Add positive and negative tests.
5. Update risk/docs notes if the risk is reduced.

Tests to run:

- `uv run pytest services/api/tests -q -k backtest`
- Any existing backtest-related test slice.

Expected output:

- Backtest path guard, tests, and acceptance summary.

### Prompt 3: Create a verification receipt runner

Goal: Add a safe local command that runs current focused checks and writes a durable receipt.

Files to inspect:

- `scripts/preflight.ps1`
- `scripts/task-check.ps1`
- `apps/dashboard/package.json`
- `apps/dashboard/scripts/run-shadow-news-impact-tests.cjs`
- `apps/dashboard/scripts/run-source-health-tests.cjs`
- `apps/dashboard/scripts/run-strategy-readiness-tests.cjs`
- `services/api/tests/test_news_impact.py`

Constraints:

- Default command must be safe and not require live credentials.
- Record skipped heavy/live checks explicitly.
- Do not rewrite full preflight unless necessary.

Implementation steps:

1. Add `scripts/verification-receipt.ps1`.
2. Run dashboard source-health, strategy-readiness, shadow-news-impact, dashboard typecheck, and API news-impact tests.
3. Capture command, status, duration, and exit code.
4. Write Markdown and/or JSON under `reports/verification/`.
5. Exit non-zero when a required check fails.

Tests to run:

- `pwsh ./scripts/verification-receipt.ps1`

Expected output:

- Receipt file path plus summary of passed, failed, and skipped checks.

### Prompt 4: Add shadow news-impact parse-quality metadata and receipt

Goal: Make the shadow news-impact API and UI proof auditable when Redis stream rows are malformed.

Files to inspect:

- `services/api/src/api/routes/news_impact.py`
- `services/api/tests/test_news_impact.py`
- `apps/dashboard/src/components/news-impact/shadow-news-impact-panel.tsx`
- `apps/dashboard/scripts/run-shadow-news-impact-tests.cjs`

Constraints:

- Keep the surface read-only and shadow-only.
- Do not add trade controls or sizing fields.
- Sanitize errors and file paths.

Implementation steps:

1. Add raw/accepted/skipped counts to the signals response.
2. Add API tests for malformed stream rows.
3. Update UI only if the metadata should be visible.
4. Make the dashboard test runner write a small receipt.

Tests to run:

- `uv run pytest services/api/tests/test_news_impact.py -q`
- `npm run test:shadow-news-impact` from `apps/dashboard`.

Expected output:

- API metadata, tests, and receipt path.

### Prompt 5: Generate the dashboard mock-route atlas

Goal: Create a route/data-source atlas so we know which dashboard screens are live, mock, hybrid, or demo-only.

Files to inspect:

- `apps/dashboard/src/app/`
- `apps/dashboard/src/components/`
- `apps/dashboard/src/lib/mock-data.ts`
- `featuresmenu.md`
- `docs/ROADMAP.md`

Constraints:

- Do not replace mock data yet unless the atlas script requires a tiny helper.
- Preserve existing `MockBadge` labels.
- Ground every route status in file evidence.

Implementation steps:

1. Scan for `MockBadge`, `mock-data`, `MOCK:`, `placeholder`, and route files.
2. Create `docs/dashboard-route-atlas.md`.
3. Include route, source files, data source status, backend dependency, replacement priority, and suggested tests.
4. Recommend the first route to convert to service-backed data.

Tests to run:

- `pnpm --dir apps/dashboard exec tsc --noEmit --pretty false`
- Optional script test if an atlas generator is added.

Expected output:

- Markdown atlas and top replacement recommendation.

## 19. Final Recommendation

Do the stabilizing work first. The safest next move is to enforce runtime safety across every service entrypoint, lock down the backtest file path boundary, and add a receipt-writing verification command. Those three tasks reduce security risk, runtime drift, and handoff ambiguity without changing core product behavior.

After that, move into product-readiness work: generate the dashboard mock-route atlas, convert one mock-heavy route to a service-backed read-only contract, and make shadow-model/provider evidence durable. That sequence keeps the whole system running while turning the strongest existing slices into repeatable proof.
