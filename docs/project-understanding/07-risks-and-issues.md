# Risks and Issues

## Critical

No critical production exploit was confirmed in this documentation pass.

Important caveat: this is not a full security penetration test, dependency
audit, or live environment review. The repo includes trading and credentialed
provider surfaces, so production clearance requires a dedicated security pass.

## High

### Prototype Auth Is Not Production-Safe

- Severity: High
- Evidence: `services/api/src/api/auth.py` uses HS256 bearer JWTs; `Settings.JWT_SECRET`
  defaults to `dev-only-change-me`; `apps/dashboard/src/lib/auth.ts` stores JWTs
  in `localStorage`.
- Impact: Stolen tokens or a default signing secret could allow unauthorized API
  access in any non-local environment.
- Suggested fix: Require non-default `FINCEPT_JWT_SECRET` at startup outside
  local/test mode, move dashboard auth to httpOnly cookies/OAuth, add token
  expiry and scopes, and document operator bootstrap.
- Files involved: `libs/fincept-core/src/fincept_core/config.py`,
  `services/api/src/api/auth.py`, `apps/dashboard/src/lib/auth.ts`.

### User-Controlled Backtest Path Needs Approved-Root Enforcement

- Severity: High
- Evidence: `services/api/src/api/routes/backtest.py` constructs
  `Path(body.bars_path)` and checks `exists()` before passing it to the runner.
  Existing risks docs also call out backtest/training file-path boundaries.
- Impact: An authenticated operator could cause the API to read arbitrary local
  files that parse as input, or at minimum probe filesystem existence.
- Suggested fix: Resolve paths against approved roots such as `data/` and
  `reports/`, require suffix/format checks, reject absolute paths unless
  explicitly allowlisted, and add traversal tests.
- Files involved: `services/api/src/api/routes/backtest.py`,
  `services/api/tests/test_backtest.py`, `docs/RISKS.md`.

### Latest Route Smoke Has `/data/coverage` Timeout

- Severity: High
- Evidence: `reports/route-smoke/route-smoke-20260506-211742.json` shows
  `data_coverage` failed with `ReadTimeout` at about 5015 ms.
- Impact: Operator datasource health can hang or fail smoke gates, making the
  current API/dashboard contract unreliable.
- Suggested fix: Bound DB calls, add batching/indexing if needed, return 200 or
  structured 503 inside the smoke timeout, and add timing spans for universe
  read, coverage read, and serialization.
- Files involved: `services/api/src/api/routes/data.py`,
  `fincept_db.bars.read_bar_coverage`, `scripts/route_smoke.py`,
  `services/api/tests/test_data.py`.

### Dirty Worktree and Generated Artifacts Increase Commit Risk

- Severity: High
- Evidence: `git status --short` shows many modified and untracked files across
  dashboard, API, experiments, scripts, docs, and agents. Generated reports and
  artifacts are present locally and many are ignored.
- Impact: A broad stage/commit could mix unrelated work, generated artifacts,
  or local-only files into one risky change.
- Suggested fix: Keep changes sliced; inspect every diff before staging; never
  use broad staging until secret and artifact sweeps are clean.
- Files involved: current worktree, `.gitignore`, `reports/`, `models/`,
  `data/`, `apps/dashboard`, `services/api`, `experiments/news-impact-model`.

## Medium

### Paper-Spine Proof Is Deterministic, Not Service-Backed

- Severity: Medium
- Evidence: `reports/paper-spine/latest.json` has `status=passed` but says
  `uses_fakeredis=true` and `redis_required=false`.
- Impact: The proof is valuable but does not prove live Redis stream IDs,
  Timescale persistence, API correlation IDs, dashboard rendering, or real
  service orchestration.
- Suggested fix: Keep deterministic proof as a fast gate, then add a
  service-backed replay profile with Docker services and captured receipts.
- Files involved: `scripts/paper_spine_replay.py`, `reports/paper-spine/`,
  service entrypoints.

### OpenBB Readiness Is Degraded

- Severity: Medium
- Evidence: `reports/openbb-live/openbb-live-20260505-151250.json` shows health
  returned structured unavailable and quote/dispatcher probes returned 503.
- Impact: Research pages can show degraded provider state; autonomous research
  relying on OpenBB should not proceed as if provider data is available.
- Suggested fix: Maintain a readiness matrix separating API process, package
  install, provider credentials, allowlist, and call result.
- Files involved: `services/api/src/api/routes/research.py`,
  `libs/fincept-tools/src/fincept_tools/research/openbb.py`,
  `apps/dashboard/src/app/research/page.tsx`.

### Synchronous Backtest Runs May Block Requests

- Severity: Medium
- Evidence: `services/api/src/api/routes/backtest.py` documents synchronous
  in-request backtest execution and uses an in-process `_RUN_LOCK`.
- Impact: Larger runs can tie up the API request path and degrade operator UX.
- Suggested fix: Move larger backtests to a background queue/run store while
  preserving the current API contract.
- Files involved: `services/api/src/api/routes/backtest.py`,
  `backtester.runner`, dashboard backtest page.

### Feature Launcher Executes Local PowerShell Scripts

- Severity: Medium
- Evidence: `services/api/src/api/routes/control.py` starts/stops features via
  `asyncio.create_subprocess_exec("pwsh", ... start_feature.ps1 ...)`.
- Impact: This is powerful process control behind an API route. It is currently
  limited to local clients and authenticated users, but would be dangerous if
  exposed beyond localhost.
- Suggested fix: Keep local-only enforcement, add explicit deployment guardrails,
  consider moving feature control behind a separate local supervisor, and audit
  all script arguments.
- Files involved: `services/api/src/api/routes/control.py`,
  `scripts/start_feature.ps1`, `scripts/stop_feature.ps1`.

### Roadmap and Build Order Drift

- Severity: Medium
- Evidence: `spec/BUILD_ORDER.md` does not fully reflect later implemented
  agents, dashboard pages, route receipts, and current proof gaps.
- Impact: Future agents may choose stale tasks or overclaim completion.
- Suggested fix: Reconcile `spec/BUILD_ORDER.md` and main docs after stabilizing
  route smoke and paper-spine proof.
- Files involved: `spec/BUILD_ORDER.md`, `docs/ROADMAP.md`,
  `docs/SYSTEM_OVERVIEW.md`.

## Low

### Dashboard Demo Surfaces Use Placeholders

- Severity: Low
- Evidence: Signal cockpit and portfolio-builder market data contain placeholder
  strings/static demo snapshots.
- Impact: Operators may confuse demo or static data for live evidence.
- Suggested fix: Label demo surfaces clearly and route them through the system
  readiness page.
- Files involved: `apps/dashboard/src/features/signal-cockpit-demo`,
  `apps/dashboard/src/features/portfolio-builder/marketDataService.ts`.

### Dev Credentials in Docker Compose

- Severity: Low
- Evidence: `docker-compose.yml` uses simple local credentials for Postgres and
  MinIO.
- Impact: Acceptable for local dev, unsafe if reused outside local networks.
- Suggested fix: Keep compose local-only and document that deployment must use
  secret-backed credentials.
- Files involved: `docker-compose.yml`, deployment docs.

### CORS Is Local-Dev Oriented

- Severity: Low
- Evidence: `services/api/src/api/main.py` allows local dashboard origins and
  notes production override is future work.
- Impact: Fine locally; incomplete for production origin policy.
- Suggested fix: Make allowed origins env-configured before staging/prod.
- Files involved: `services/api/src/api/main.py`, config docs.
