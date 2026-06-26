# Current Status

Audit date: 2026-06-01.

This file summarizes observed source, manifests, docs, and existing receipts.
It does not claim a fresh full test/preflight run.

## What Appears Complete

- Monorepo structure with Python uv workspace and pnpm dashboard workspace.
- Local Docker stack for Timescale/Postgres, Redis, and MinIO.
- Shared libraries for contracts, bus, database, tools, and SDK.
- Many service packages have concrete source and tests.
- FastAPI app includes major route groups and local CORS configuration.
- Dashboard includes broad operator page coverage and a typed API client.
- Deterministic paper-spine replay exists and latest receipt is passed.
- CI workflow exists for Python lint/typecheck/tests, JS checks, and gitleaks.
- Windows local automation wrappers exist for setup, preflight, task checks, and
  service start/status/stop.

## What Appears Incomplete

- Latest reviewed API route-smoke receipt is not green: `/data/coverage` timed
  out after about 5 seconds.
- OpenBB provider proof is degraded/unavailable for quote and dispatcher calls.
- Paper-spine replay is deterministic and fakeredis-backed, not a live service
  or Timescale-backed integration receipt.
- Production auth and multi-user authorization are not implemented.
- Some planned agents and roadmap tasks are still stubs or not reconciled with
  implemented surfaces.
- Production deployment manifests were not observed.
- Dashboard route inventory/browser smoke is not complete.

## What Appears Broken or Risky

- `/data/coverage` can exceed the smoke timeout according to
  `reports/route-smoke/route-smoke-20260506-211742.json`.
- `services/api/src/api/routes/backtest.py` accepts arbitrary existing
  `bars_path` values without an approved-root check.
- `FINCEPT_JWT_SECRET` has an unsafe dev default and dashboard auth stores JWTs
  in localStorage.
- The current worktree is dirty with many modified and untracked files; do not
  stage or revert broadly.
- Reports, model artifacts, data artifacts, and local logs are present in the
  tree and must be treated as generated/local unless deliberately promoted.

## What Can Be Run Today

Discovered commands:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\dev-setup.ps1
powershell -ExecutionPolicy Bypass -File .\scripts\preflight.ps1
powershell -ExecutionPolicy Bypass -File .\scripts\task-check.ps1 -PackagePath libs/fincept-core -PytestPath libs/fincept-core/tests
powershell -ExecutionPolicy Bypass -File .\scripts\start.ps1
powershell -ExecutionPolicy Bypass -File .\scripts\status.ps1
powershell -ExecutionPolicy Bypass -File .\scripts\stop.ps1
pnpm --filter @fincept/dashboard dev
pnpm --dir apps/dashboard exec tsc --noEmit --pretty false
uv run python scripts/paper_spine_replay.py
uv run python scripts/route_smoke.py --base-url http://127.0.0.1:8010
uv run python scripts/openbb_live_proof.py --symbol NVDA
```

POSIX/WSL Makefile commands:

```bash
make dev
make test
make test-cov
make lint
make typecheck
make build
```

## What Cannot Be Verified From This Pass

- Whether Docker, Redis, Timescale, MinIO, API, and dashboard are currently
  running.
- Whether `scripts/preflight.ps1` passes in the current dirty worktree.
- Whether dashboard pages render correctly in a browser.
- Whether latest modified news-impact and UI files typecheck.
- Whether OpenBB provider readiness has improved since the existing receipt.
- Whether current local `.env` contains valid credentials. `.env` was not read.

## Existing Verification Evidence

- `reports/paper-spine/latest.json`:
  - `status`: `passed`
  - uses fakeredis
  - live broker credentials not required
  - proves data, feature, model signal, decision, risk approved/rejected, order,
    fill, portfolio, and audit trail.

- `reports/route-smoke/route-smoke-20260506-211742.json`:
  - 8 of 9 probes passed
  - `/data/coverage` failed with `ReadTimeout`
  - `/research/openbb/health` returned structured unavailable state but counted
    as pass.

- `reports/openbb-live/openbb-live-20260505-151250.json`:
  - 1 of 3 probes passed
  - health probe returned structured unavailable
  - quote and generic dispatcher returned 503.

## Setup, Build, and Test Status

Fresh status from this audit:

- `git status --short` was run and shows many pre-existing modified and
  untracked files.
- No broad test suite, dashboard build, browser check, Docker start, or API
  route smoke was rerun in this pass.

Confidence level:

- High for repository structure, code organization, and existing receipt facts.
- Medium for runtime health because no live services were started.
- Low for current full-green build/test status until preflight is rerun.

## Automation Review Update - 2026-06-06

This local pass reviewed the worktree rather than GitHub. The branch is
`codex/portfolio-optimizer-core` at `9c1aba1`, with a large dirty tree spanning
dashboard UI, API routes, news-impact promotion work, data/provider hardening,
core schemas/config, OMS provider helpers, and docs.

### Material changes observed

| Area | Evidence | Current read |
|---|---|---|
| News-impact shadow lane | `NewsImpactSignal`, `NewsImpactHorizon`, `STREAM_SIG_NEWS_IMPACT`, `news_impact_agent`, `GET /news-impact/signals`, and `ShadowNewsImpactPanel` now exist locally. | The news-impact experiment is no longer only a standalone workbench; it has a read-only shadow-signal integration path. |
| Shadow-safety controls | Signal schemas carry no side, quantity, broker, venue, order, or sizing fields; focused tests assert those fields do not render in the dashboard panel. | This preserves the raw-signal boundary and should remain a non-negotiable promotion invariant. |
| Dashboard operator surface | New or modified surfaces include `/watchlist`, `/symbol/[symbol]`, `TradingChart`, watchlist rows, shared mock badges, LED dots, status widgets, and a broad shell/style refresh. | The dashboard is moving toward a dense trading terminal, but several new pages are still mock-backed and need route/API smoke before being treated as product-complete. |
| Runtime safety config | `FINCEPT_ENV`, production JWT default rejection, CORS allowlist, HTTP retry budget, Binance WS override, Alpaca data base URL, and mark TTL settings were added locally. | Staging/prod posture is improving, but service entrypoints should consistently call the fail-fast runtime safety check. |
| Data freshness semantics | `DataFreshness` now models realtime, delayed, cached, simulated, and stale responses. | This gives the UI a better provenance vocabulary; API responses and dashboard chips should converge on it. |
| Provider/data resilience | Local diffs touch Binance, EOD equity loading, daily EOD load, Alpaca marks/news sync, `/data`, and API tests. | The next verification should isolate whether these changes close the prior `/data/coverage` timeout and provider-readiness gaps. |
| Reviewability | The worktree has many unrelated modified and untracked files plus generated/local directories. | Do not ship this as one commit; split into docs, dashboard UI, news-impact integration, provider/data hardening, and local/generated cleanup slices. |

### Updated risk notes

- The most promising new work is the read-only `sig.news_impact` shadow lane,
  because it connects experiment, agent, API, and dashboard without creating an
  execution path.
- The largest current delivery risk is review scope: dashboard visuals,
  provider hardening, API schemas, experiments, and generated artifacts are all
  mixed in the same dirty tree.
- The largest verification gap is runtime proof. This pass did not start
  Docker, API, or dashboard services, and did not rerun preflight or route
  smoke.

## Automation Review Update - 2026-06-08

This pass again reviewed local files only. It did not use GitHub and did not
start Docker, API, dashboard, Redis, Timescale, or provider services.

### Material changes observed

| Area | Evidence | Current read |
|---|---|---|
| Worktree shape | `git status --short` shows broad modified and untracked files across docs, dashboard, API, agents, experiments, provider code, and tests. | Still not reviewable as one change; a review-slice ledger is now the first deliverable. |
| News-impact lane | `NewsImpactSignal`, `sig.news_impact`, `/news-impact/signals`, dashboard types, and shadow panel/tests are present locally. | Promising shadow-only path; add a mechanical no-order-field invariant before promotion work. |
| Mock terminal UX | `/watchlist`, `/symbol/[symbol]`, `/positions`, `MockBadge`, `mock-data.ts`, watchlist rows, sparkline/chart widgets, and terminal styling exist locally. | Useful operator direction, but each mock surface needs a route-atlas entry and replacement API contract. |
| Provider evidence | `/research/provider-data`, provider capture tests, OpenBB readiness layering, and Exa/OpenBB record builders are present. | Good foundation for an operator evidence ledger; needs redaction, retention, and stable failure semantics. |
| Freshness semantics | `DataFreshness`, freshness chips, mark TTL settings, and data/provider tests are visible locally. | Ready to become a strategy-readiness gate instead of only a display concept. |

## Automation Review Update - 2026-06-10

| Area | Current local evidence | Status |
|---|---|---|
| Review decomposition | `docs/review-slices/2026-06-10-local-worktree.md` now groups the dirty tree into docs, dashboard shell, mock terminal routes, news-impact shadow lane, API/runtime safety, provider resilience, and tooling slices. | Ready to use as the staging and validation map. |
| Commit boundary | The branch remains `codex/portfolio-optimizer-core` at `9c1aba1` with broad local modifications and untracked files. | Do not treat the tree as one review unit. |
| Highest-risk contracts | News-impact non-agency, provider-data redaction, runtime fail-closed checks, and `DataFreshness` readiness still need mechanical checks. | Prioritize tests/scripts over new product surface. |
| Mock terminal UX | `MockBadge` and `mock-data.ts` make mock-backed routes identifiable. | Next artifact should be a mock route atlas with replacement endpoint names. |
| Runtime safety | `FINCEPT_ENV`, JWT default rejection, CORS origin config, request IDs, retry helpers, and provider base URLs are visible locally. | Safer posture, but entrypoint coverage needs a matrix and tests. |

### Verification gap

No fresh preflight, route smoke, dashboard typecheck, Playwright smoke, or
provider live proof was run in this pass. Treat the observations above as
source/diff evidence, not runtime health evidence.

## Automation Review Update - 2026-06-14

This pass reviewed local files only and did not use GitHub. The branch is still
`codex/portfolio-optimizer-core` at `9c1aba1`.

| Area | Current local evidence | Status |
|---|---|---|
| Review decomposition | `docs/review-slices/2026-06-10-local-worktree.md` still matches the broad dirty tree shape. | Use it as the staging gate before any commit. |
| Shadow validation | `apps/dashboard/scripts/run-shadow-news-impact-tests.cjs` is present, and the news-impact slice still spans schemas, API route, tests, and dashboard panel files. | Promote the helper into the canonical shadow receipt command. |
| Workflow handoff | `.devin/workflows/phase-kickoff.md` is added while `.windsurf/workflows/phase-kickoff.md` is deleted. | Verify all agent kickoff references now target `.devin`. |
| Mock terminal UX | Mock-backed dashboard surfaces and `MockBadge` remain present across watchlist/symbol/positions/overview work. | The missing artifact is the mock route atlas. |
| Runtime/provider safety | `FINCEPT_ENV`, CORS/JWT safety, request IDs, retry helpers, provider base URLs, and freshness fields remain visible in local diffs. | Next validation should target redaction and freshness-readiness behavior, not add more UI. |

### Verification gap

No runtime services, preflight, lint, tests, route smoke, or browser checks were
run in this pass. Treat these notes as source/diff evidence only.

## Automation Review Update - 2026-06-21

This pass reviewed local files only and did not use GitHub. The branch remains
`codex/portfolio-optimizer-core` at `9c1aba1`, with the same broad dirty-tree
constraint as the prior automation runs.

| Area | Current local evidence | Status |
|---|---|---|
| Shadow signal contract | `NewsImpactSignal`, `STREAM_SIG_NEWS_IMPACT`, `/news-impact/signals`, API tests, dashboard API types, and `ShadowNewsImpactPanel` are present. | Strong read-only foundation; still needs a persisted UI+API receipt and malformed-row accounting. |
| Source health and readiness | `npm run test:source-health` and `npm run test:strategy-readiness` are now dashboard scripts. | These are ready to become a single provenance-to-launch policy receipt. |
| Mock terminal UX | `MockBadge` and `mock-data.ts` hits remain in `/positions`, `/watchlist`, `/symbol/[symbol]`, and overview watchlist preview. | The mock route atlas is still missing and should be generated before route conversion. |
| Workflow handoff | `.devin/workflows/phase-kickoff.md` remains added while `.windsurf/workflows/phase-kickoff.md` is deleted. | Add a reference check so the migration has proof. |
| Verification scope | This update inspected source and scripts; no full runtime stack, Docker, provider live proof, or browser smoke was run. | Treat this as documentation/source evidence, not a green runtime claim. |

## Automation Review Update - 2026-06-23

This pass reviewed local files only and did not use GitHub. The branch is
`codex/portfolio-optimizer-core` at `751d212`.

| Area | Current local evidence | Status |
|---|---|---|
| Quant Foundry | The recent commit stack adds service modules, tests, RunPod containers, dashboard pages, gateway/budget controls, settlement, promotion, conformal, drift, MoE, and causal-memory pieces. | This is now a first-class local vertical; it needs a dated release-readiness receipt before any production claim. |
| Live readiness | `docs/LIMITED_LIVE_READINESS_REVIEW.md` explicitly says the limited live path is not ready and lists blockers. | Keep live-capital, production, and GPU-spend claims blocked until those blockers have fresh proof. |
| Dashboard scope | `/quant-foundry/*` pages are present alongside older mock terminal/news-impact surfaces. | Add route smoke coverage instead of expanding UI surface again. |
| Provider evidence | Redaction and freshness receipt work landed in `libs/fincept-db` and API/provider tests. | Fold this into the release receipt so evidence safety is not a one-off proof. |
| Shadow helper script drift | `node scripts/run-shadow-news-impact-tests.cjs` passes directly, but `npm run test:shadow-news-impact` is no longer defined. | Re-add the npm alias or update future receipts to call the helper directly. |
| Worktree hygiene | Current tracked dirty state is `uv.lock`; many local/untracked artifacts remain. | Classify the lockfile delta and keep local tool/report artifacts out of product review slices unless intentionally promoted. |

### Updated risk notes

- The strongest new value is the Quant Foundry platform, but the current proof
  boundary is local tests/docs, not live training or live trading.
- The largest current risk is mixing many implemented ML modules with cloud,
  dashboard, and generated/local artifacts in one review story.
- The prior shadow news-impact safety queue still matters; it should not be
  hidden by the larger Quant Foundry work.
