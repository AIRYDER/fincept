# Fincept Terminal

An internal, AI-agentic stock & crypto trading platform.

This repo contains:

1. **Planning artifacts** (`docs/`) — what to build, why, in what order, with what risks.
2. **Implementation spec** (`spec/`) — contract-first task specs that any coding model can execute one at a time to actually build the system.
3. **`IMPLEMENTATION.md`** — the bridge: how to use the spec/ to drive a coding model.

## Planning docs (`docs/`)

- `docs/BLUEPRINT.md` — Full original blueprint (vision, features, tech stack, 5-phase plan).
- `docs/ROADMAP.md` — **Pragmatic phased roadmap** mapping blueprint to achievable milestones with realistic timelines, staffing, and cut-scope recommendations.
- `docs/TASKS.md` — Story-pointed ticket breakdown for project management.
- `docs/RISKS.md` — Risk register with likelihood × impact and mitigations.
- `docs/DECISIONS.md` — Architecture Decision Records (ADRs).

## Implementation spec (`spec/`)

- `spec/ARCHITECTURE.md` — One-page mental model + module boundaries.
- `spec/LAYOUT.md` — Every file in the repo, one line each.
- `spec/CONTRACTS.md` — All schemas, events, interfaces — copy-pasteable Python.
- `spec/BUILD_ORDER.md` — Sequenced task graph with phase-level checkpoints.
- `spec/PROMPTS.md` — How to feed a task to a coding model and the new-task template.
- `spec/tasks/TASK-*.md` — Atomic implementation units (currently 11 authored + ~30 templated).

## How to use this repo

1. Read `docs/ROADMAP.md` first — it tells you what's realistic vs. aspirational in the blueprint.
2. Pick a phase milestone from `docs/TASKS.md`, create a GitHub Project board, and import tickets.
3. Revisit `docs/DECISIONS.md` before committing to any foundational tech choice — several are marked `STATUS: open`.

## Current status

**Foundation through Phase U is implemented at the package level, and the local app surface has expanded into ML lifecycle management, multi-agent operations, and operator workflows.** The repo has implemented core/bus/db/tools libraries, seven agent services (gbm_predictor, regime, sentiment, sentiment_features, information_enricher, news_alpha_predictor, news_outcome_labeler), orchestrator, risk gate, paper OMS (with Alpaca adapter), portfolio service, full ML deployment vertical (train → promote → hot-reload → shadow → predict → log), strategy-host, and API/dashboard routes covering strategies, orders, research, news impact, models, predictions, reconciliation, datasource registry, symbol search, signal cockpit experiments, and the AI portfolio builder. The next engineering step is to prove these connected flows with end-to-end paper-spine replay receipts, port-8010 route smoke tests, and guarded shadow reports before treating the checked roadmap boxes as production proof.

## Local automation commands

The repo now includes Windows-friendly wrappers for the repetitive setup and verification flows that were previously split across README notes, CI, and workflow docs:

- `powershell -ExecutionPolicy Bypass -File .\scripts\dev-setup.ps1` — copies `.env` from `.env.example` if needed, starts Docker services, syncs the uv workspace, installs pnpm dependencies, and installs pre-commit hooks.
- `powershell -ExecutionPolicy Bypass -File .\scripts\preflight.ps1` — mirrors the current local CI/parity flow: Docker up, `uv sync`, `pnpm install`, Python lint/format/typecheck, Alembic upgrade, pytest coverage run, JS workspace checks, and a gitleaks pre-commit scan.
- `powershell -ExecutionPolicy Bypass -File .\scripts\task-check.ps1 -PackagePath libs/fincept-core -PytestPath libs/fincept-core/tests` — runs the task-level verification loop used throughout `spec/tasks` and `spec/prompts`: `pytest`, `ruff check`, and `mypy` for one package or slice. Add `-Sync` when a fresh `uv sync` is part of the workflow.
- `pnpm --filter @fincept/dashboard dev` — runs the Next.js dashboard at `http://localhost:3000` when you only need the UI.
- `pnpm --dir apps/dashboard exec tsc --noEmit --pretty false` — quick dashboard TypeScript check after UI edits.
- `uv run python scripts/paper_spine_replay.py` — generates a deterministic paper-spine replay receipt proving data → feature → signal → decision → risk → paper order/fill → portfolio update → audit trail. Outputs to `reports/paper-spine/`.
- `uv run python scripts/openbb_live_proof.py --symbol NVDA` — probes the Fincept OpenBB routes, including `/research/openbb/readiness`, and writes a live receipt under `reports/openbb-live/`.

For WSL/Git Bash users, the existing `Makefile` remains available. On Windows without `make`, prefer the PowerShell scripts above.

## Local progression snapshot — 2026-04-26

- `spec/BUILD_ORDER.md` correctly marks Task 001 complete; Tasks 002-006 are still open.
- Service directories exist for ingestor, features, agents, orchestrator, risk, OMS, portfolio, API, backtester, and jobs, but they currently contain package stubs only.
- `docs/ROADMAP.md` remains strategically useful, but the active path should now narrow from blueprint selection to "prove the Foundation phase with executable contracts."
- `featuresmenu.md` is the working backlog for new innovative features and skill-deepening recommendations.

## Local progression snapshot — 2026-04-27

- `spec/BUILD_ORDER.md` now marks Tasks 001-004 complete: monorepo scaffold, `fincept-core`, `fincept-bus`, and `fincept-db`.
- Local tests passed with `uv run pytest libs -q`: 29 passed, 11 skipped. The skipped tests require Postgres/Timescale or a real Redis latency assertion, so CI service-container validation is still the next proof point.
- `.github/workflows/ci.yml` now includes Python lint/typecheck, Python tests with Redis and Timescale services, JS lint/typecheck/test/build, coverage artifact upload, and gitleaks scanning.
- New local workflows exist for GHCR image builds and nightly long/security/dependency scans, but service Dockerfiles and long-test coverage are still future work.
- `fincept-tools` remains the next unchecked foundation task and should be implemented before any agent or OMS work.

## Local progression snapshot — 2026-04-28

- `libs/fincept-tools` is present in the uv workspace, but it is still a stub package with only `pyproject.toml` and `src/fincept_tools/__init__.py`.
- Treat `TASK-005-fincept-tools` as implementation-ready: the folder and workspace wiring exist, but the registry, typed tool protocol, audit wrapper, and paper execution guard do not.
- Keep `TASK-006` open until the Redis/Timescale-backed CI path and local `scripts/preflight.ps1` run are recorded without skips.

## Local progression snapshot — 2026-04-30

- `spec/BUILD_ORDER.md` now marks `TASK-005-fincept-tools` and `TASK-006` complete, and many downstream tasks in data ingestion, backtesting, agents, risk, OMS, API, dashboard, and Alpaca paper-broker integration are checked off.
- `libs/fincept-tools` now contains typed protocol, registry, data tools, analytics tools, paper-only execution tools, and 81 package tests.
- `scripts/task-check.ps1` now runs pytest with the workspace package selected and fails on nonzero native command exits; this fixed a wrapper gap where a pytest import failure could be followed by a misleading "Task check passed" message.
- Verified locally: `powershell -ExecutionPolicy Bypass -File .\scripts\task-check.ps1 -PackagePath libs/fincept-tools -PytestPath libs/fincept-tools/tests` passed with 81 pytest tests, Ruff clean, and Mypy clean.
- Remaining proof gap: the broad `scripts/preflight.ps1`, Redis/Timescale-backed checks, paper OMS replay, API/dashboard integration, and any live-execution boundaries were not run in this snapshot.

## Local progression snapshot — 2026-05-02

- Current local deltas add strategy configuration persistence, a strategy-host service, richer order/strategy API behavior, dashboard strategy/order/research/news-impact surfaces, OpenBB/Exa research tools, symbol search, OpenBB health history, rate limiting, and an experimental news-impact bridge.
- `scripts/start.ps1`, `status.ps1`, and `stop.ps1` now default the API to port `8010` with clearer port-owner checks; `NEXT_PUBLIC_API_URL` follows that port in the dashboard client.
- New docs such as `docs/datasources.md`, `docs/howthisworks.md`, `docs/openbb-research-handoff.md`, `docs/portfoliooptimizer.md`, and `docs/uirecommendations.md` need to be kept aligned with `docs/ROADMAP.md` and `featuresmenu.md`.
- The main proof gap shifted from package implementation to connected-system evidence: run `scripts/preflight.ps1`, then add one replay that proves data -> feature -> model -> decision -> risk -> order -> fill -> portfolio with an audit trail.

## Local progression snapshot — 2026-05-07

- The codebase has advanced substantially since the last snapshot. The agent layer now includes seven implemented agents (up from two): `gbm_predictor`, `regime_agent`, `sentiment_agent`, `sentiment_features`, `information_enricher`, `news_alpha_predictor`, and `news_outcome_labeler`. The `regime` and `pairs` agents listed as stubs in earlier snapshots are partially resolved — `regime_agent` is fully implemented with FRED-based rule heuristics; `pairs` remains a stub.
- The dashboard has expanded to include `/predictions`, `/signal-cockpit-demo`, `/reconciliation`, `/portfolio-builder`, `/news-lab`, `/news-impact-lab`, and `/optimizer` pages, in addition to the previously documented surfaces.
- `/optimizer` now redirects to `/portfolio-builder`; the portfolio builder includes deterministic allocation plus AI report provider selection for Auto, GPT-5.5, and Claude Opus 4.7.
- Two open ADRs are now resolved in code: ADR-0006 (feature store: custom Redis online + Parquet offline, not Feast) and ADR-0009 (datasource routing: registry in `services/api/src/api/routes/data.py` with safety tier, health mode, and coverage tracking). These should be promoted from "open" to "accepted" in `docs/DECISIONS.md`.
- The `docs/SYSTEM_OVERVIEW.md` is the most current architectural reference (last updated 2026-05-05) and should be consulted alongside this README for component-level detail.
- Outstanding proof gaps are now narrower: `scripts/paper_spine_replay.py` produces a local deterministic paper-spine receipt with one accepted paper order and one low-limit risk rejection, and OpenBB now has a readiness route that separates API reachability from provider-specific quote/fundamental failures. The port-8010 route smoke receipt, coverage error safety, OpenBB live-provider receipt, and live service-container proof still need recording before expanding agent autonomy or live-brokerage assumptions.
- `docs/datasources.md` is the routing document for datasource registry work. Keep future provider work tied to safety tier, health check, return shape, and operator proof instead of adding one-off dashboard calls.

## UI documentation map

- `apps/dashboard/README.md` — dashboard-specific routes, run commands, verification, and troubleshooting.
- `docs/agent-ui-analysis/README.md` — design rationale for the signal cockpit, safety-state, evidence-stack, and structured AI rail concepts.
- `docs/uirecommendations.md` — broader UI improvement backlog and recommendations.
- `docs/portfoliooptimizer.md` — portfolio optimizer and AI report workflow notes.
