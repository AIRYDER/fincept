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

**Foundation implementation is partially complete.** The repo now has a uv Python workspace, pnpm dashboard workspace, Docker dev stack, Makefile commands, quality config, contract docs, build-order specs, and implemented library packages for `fincept-core`, `fincept-bus`, and `fincept-db`. The next engineering step is to finish `fincept-tools`, harden CI around live Redis/Postgres service containers, and keep all execution paths paper-only until the OMS/risk gates exist.

## Local automation commands

The repo now includes Windows-friendly wrappers for the repetitive setup and verification flows that were previously split across README notes, CI, and workflow docs:

- `powershell -ExecutionPolicy Bypass -File .\scripts\dev-setup.ps1` — copies `.env` from `.env.example` if needed, starts Docker services, syncs the uv workspace, installs pnpm dependencies, and installs pre-commit hooks.
- `powershell -ExecutionPolicy Bypass -File .\scripts\preflight.ps1` — mirrors the current local CI/parity flow: Docker up, `uv sync`, `pnpm install`, Python lint/format/typecheck, Alembic upgrade, pytest coverage run, JS workspace checks, and a gitleaks pre-commit scan.
- `powershell -ExecutionPolicy Bypass -File .\scripts\task-check.ps1 -PackagePath libs/fincept-core -PytestPath libs/fincept-core/tests` — runs the task-level verification loop used throughout `spec/tasks` and `spec/prompts`: `pytest`, `ruff check`, and `mypy` for one package or slice. Add `-Sync` when a fresh `uv sync` is part of the workflow.

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
