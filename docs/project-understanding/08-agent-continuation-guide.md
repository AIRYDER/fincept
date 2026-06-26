# Agent Continuation Guide

## First Files to Read

Read in this order:

1. `README.md`
2. `docs/project-understanding/00-overview.md`
3. `docs/project-understanding/07-risks-and-issues.md`
4. `docs/project-understanding/09-next-steps.md`
5. `docs/SYSTEM_OVERVIEW.md`
6. `spec/ARCHITECTURE.md`
7. `pyproject.toml`
8. `apps/dashboard/package.json`
9. The specific package or route files for your task.

For API/dashboard tasks, also read:

- `services/api/src/api/main.py`
- `services/api/src/api/auth.py`
- Relevant file under `services/api/src/api/routes/`
- `apps/dashboard/src/lib/api.ts`
- `apps/dashboard/src/lib/types.ts`
- Relevant dashboard page/component.

For trading-spine tasks, also read:

- `libs/fincept-core/src/fincept_core/schemas.py`
- `libs/fincept-bus/src/fincept_bus/streams.py`
- `scripts/paper_spine_replay.py`
- `reports/paper-spine/latest.json`

## Safe Work Strategy

- Preserve the current dirty worktree. Do not revert user changes.
- Keep patches small and reviewable.
- Prefer package-local or route-local changes before broad refactors.
- Inspect actual source before updating docs or claiming a feature exists.
- Keep live trading out of scope unless the user explicitly requests otherwise.
- Treat `.env`, credentials, generated reports, generated model artifacts, and
  local data as unsafe to commit by default.
- Use exact paths and explicit staging if asked to commit.
- Prefer existing scripts and patterns over new automation frameworks.

## Architecture Rules

- Shared schemas and contracts live in `libs/fincept-core`.
- Events move over `fincept-bus`/Redis Streams.
- Services should not import each other's runtime internals unless an existing
  local pattern already does so.
- Agents emit predictions/signals, not direct orders.
- Orchestrator creates decisions/order intents; risk checks them; OMS fills
  them; portfolio owns position state.
- API exposes read models and explicit control endpoints.
- Dashboard should use `apps/dashboard/src/lib/api.ts` and typed structures in
  `apps/dashboard/src/lib/types.ts`.
- Research/OpenBB/Exa/news-impact paths should stay read-only or shadow-only
  until receipt-backed governance exists.

## Validation Checklist

Choose the smallest validation set that covers your change.

Python package slice:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\task-check.ps1 -PackagePath libs/fincept-core -PytestPath libs/fincept-core/tests
```

API route slice:

```powershell
uv run pytest services/api/tests/test_data.py -q
uv run ruff check services/api
uv run mypy services/api
```

Dashboard slice:

```powershell
pnpm --dir apps/dashboard exec tsc --noEmit --pretty false
```

Integration/proof slice:

```powershell
uv run python scripts/paper_spine_replay.py
uv run python scripts/route_smoke.py --base-url http://127.0.0.1:8010
```

Full local preflight when needed:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\preflight.ps1
```

## Known Pitfalls

- `reports/route-smoke/route-smoke-20260506-211742.json` shows `/data/coverage`
  timing out. Do not assume route smoke is green.
- `.env` exists locally and is ignored. Do not read or stage it unless the user
  explicitly asks for local environment debugging.
- The dashboard auth token is in localStorage and is not production-grade.
- Backtest and model-training paths need careful approved-root validation.
- Roadmap docs contain historical snapshots; verify against source.
- Generated artifacts under `reports/`, `models/`, and `data/` can be large or
  local-only.
- `spec/BUILD_ORDER.md` may lag current implementation.
- Some UI surfaces are demos with placeholders; label them honestly.

## Recommended Next Agent Tasks

1. Bound `/data/coverage` latency and rerun route smoke.
2. Add approved-root validation for backtest input paths.
3. Add a dashboard route inventory smoke.
4. Promote deterministic paper-spine replay toward a service-backed profile.
5. Reconcile `spec/BUILD_ORDER.md` with implemented agents and current proof
   receipts.

## What Not to Touch Without Approval

- Live execution behavior or credentials.
- `.env` or any local secret files.
- Generated model artifacts or reports, unless the task is explicitly about
  receipts.
- Broad dependency upgrades.
- Public API response shapes without matching dashboard/client/test updates.
- Historical roadmap rewrites that are not tied to current code evidence.
