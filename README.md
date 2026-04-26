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

**Foundation scaffold exists.** The repo now has a uv Python workspace, pnpm dashboard workspace, Docker dev stack, Makefile commands, quality config, contract docs, build-order specs, and empty package/service modules. The next engineering step is still Phase F implementation: fill `fincept-core`, `fincept-bus`, and `fincept-db` from `spec/CONTRACTS.md`, then make `make ci` meaningful beyond scaffold validation.

## Local progression snapshot — 2026-04-26

- `spec/BUILD_ORDER.md` correctly marks Task 001 complete; Tasks 002-006 are still open.
- Service directories exist for ingestor, features, agents, orchestrator, risk, OMS, portfolio, API, backtester, and jobs, but they currently contain package stubs only.
- `docs/ROADMAP.md` remains strategically useful, but the active path should now narrow from blueprint selection to "prove the Foundation phase with executable contracts."
- `featuresmenu.md` is the working backlog for new innovative features and skill-deepening recommendations.
