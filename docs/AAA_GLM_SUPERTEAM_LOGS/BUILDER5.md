# Builder 5 (GLM) — Work Log

**Agent:** Builder 5 (GLM-5.2)
**Joined:** 2026-06-22
**Track:** Operator experience — on-demand module control

---

## Task Adoption Log

### TASK-0203: Add On-Demand Module Control for Local and Staging — ADOPTED 2026-06-22

**Status:** IN PROGRESS
**Order:** 13
**Depends on:** TASK-0202 (✅ DONE — unified system readiness center)

**Why this task:**
- Builder 1 owns TASK-0401 (settlement ledger) — in flight.
- Builder 2 owns TASK-0304 (outbox + inbox) — in flight.
- TASK-0305 / 0306 / 0402–0405 all depend on in-flight work, so they are blocked.
- TASK-0204 (fetch timeouts) is already implemented in `apps/dashboard/src/lib/api.ts`.
- TASK-0203 is the earliest unblocked, unowned task. Its only dependency
  (TASK-0202) is complete. It is fully file-disjoint from the quant_foundry
  work the other builders own, so we can work in parallel without conflict.

**Files owned (file-disjoint from active tasks):**
- `services/api/src/api/routes/modules.py` (to create)
- `services/api/src/api/main.py` (additive router registration only)
- `services/api/tests/test_modules.py` (to create)
- `apps/dashboard/src/app/system/page.tsx` (additive module-control panel)
- `apps/dashboard/src/components/modules/` (new components, to create)
- `apps/dashboard/src/lib/modules.ts` (typed client, to create)
- `scripts/modules/` (new per-module start scripts, to create)
- `docs/ON_DEMAND_MODULES.md` (to create)

**File-disjoint check:**
- TASK-0304 (Builder 2, in flight) owns `services/quant_foundry/**` outbox/inbox — no overlap.
- TASK-0401 (Builder 1, in flight) owns `services/quant_foundry/**` settlement — no overlap.
- TASK-0202 (done) owns the existing system page readiness center; I add a
  module-control panel additively without rewriting the readiness surface.
- `services/api/src/api/main.py` is shared infrastructure — I only register a
  new router, no edits to existing routes.

**Plan (TDD):**
1. Write failing tests in `services/api/tests/test_modules.py` covering:
   - Module registry is allowlisted (no arbitrary module IDs accepted).
   - Start/stop/reject-duplicate-start/status transitions.
   - Auth required for operator endpoints.
   - No arbitrary shell command execution from user input (commands are
     predeclared server-side, keyed by allowlisted module ID).
   - Idle timeout stops optional modules safely.
   - "Stop all optional modules" works.
   - Start/stop receipts are recorded.
   - Disabled / unknown module IDs return safe errors (no secret echo).
2. Implement `services/api/src/api/routes/modules.py`:
   - `ModuleRegistry` with predeclared start/stop/health commands per module ID.
   - `ModuleRuntime` tracking running state, started_at, last_heartbeat, idle.
   - Endpoints: `GET /modules`, `GET /modules/{id}`, `POST /modules/{id}/start`,
     `POST /modules/{id}/stop`, `POST /modules/{id}/restart`,
     `POST /modules/stop-all`, `GET /modules/receipts`.
   - Local-only / auth-gated; commands launched via allowlisted subprocess
     invocations (no user-supplied command strings).
3. Add dashboard controls on the system page:
   - Module list with status badges (running / stopped / idle / degraded).
   - Start / stop / restart buttons.
   - Idle countdown display.
   - "Stop all optional modules" button.
   - Latest receipt link.
4. Create `scripts/modules/` per-module start scripts (OpenBB, news analysis)
   and `docs/ON_DEMAND_MODULES.md` documenting the registry + workflow.
5. Run `uv run pytest services/api/tests -q -k modules` green;
   `pnpm --dir apps/dashboard exec tsc --noEmit --pretty false` clean;
   `ruff check` + `mypy` clean on touched Python files.
6. Atomic commit.

**Additional work (Builder 1 request):**
- Implemented `assert_safe_for_runtime` in `libs/fincept-core/src/fincept_core/config.py`
  (function didn't exist yet; test imported it but no implementation).
- Added `ENV` field to `Settings` (maps to `FINCEPT_ENV`, defaults to `"dev"`).
- Added guard call in `services/api/src/api/main.py` at module level.
- Answered Builder 1's message in AGENT_TO_AGENT_MESSAGING.

---

## Completion Log

### TASK-0203 — COMPLETED 2026-06-22

**Changed files:**
- `services/api/src/api/routes/modules.py` (created — module registry + 8 endpoints)
- `services/api/src/api/main.py` (additive: modules router registration + runtime guard)
- `services/api/tests/test_modules.py` (created — 22 tests, all green)
- `apps/dashboard/src/lib/types.ts` (additive: Module* type definitions)
- `apps/dashboard/src/lib/api.ts` (additive: 8 module API client methods)
- `apps/dashboard/src/components/modules/module-control-panel.tsx` (created)
- `apps/dashboard/src/app/system/page.tsx` (additive: ModuleControlPanel mount)
- `docs/ON_DEMAND_MODULES.md` (created — operator workflow doc)
- `libs/fincept-core/src/fincept_core/config.py` (additive: ENV field + assert_safe_for_runtime)
- `AAAAAAAAA_BIG_PLAN.md` (ownership marker)
- `docs/NEXT_STEPS_PLAN.md` (ownership marker)

**Tests run:**
- `uv run pytest services/api/tests/test_modules.py -q` → 22 passed
- `uv run pytest services/api/tests/test_control.py -q` → 15 passed (no regressions)
- `uv run pytest libs/fincept-core/tests/test_startup_safety_matrix.py -q -k "api or guard"` → 3 passed
- `uv run pytest libs/fincept-core/tests/test_config.py -q` → 1 passed
- `uv run ruff check` on all touched Python files → All checks passed
- `uv run mypy` on modules.py + config.py → Success: no issues
- `pnpm --dir apps/dashboard exec tsc --noEmit` → No new errors from my files
  (pre-existing errors in untracked files: watchlist-preview.tsx, watchlist-row.tsx,
  symbol/page.tsx, shadow-news-impact-panel.tsx — all unrelated to TASK-0203)

**Tests not run:**
- Full dashboard Node test scripts (npm run test:*) — not relevant to this task.
- Full API test suite (384 other tests) — ran previously, 1 pre-existing failure
  in test_regime.py unrelated to modules.

**Pre-existing unrelated dirty files avoided:**
- `services/quant_foundry/**` (Builder 1/2/3/4 tracks)
- Untracked dashboard components (watchlist-preview, watchlist-row, symbol page)
- `2026-06-22-*.txt` scratch files

**Security verification:**
- No arbitrary shell command execution from user input (module IDs allowlisted).
- Auth required on every operator endpoint.
- Local-only launch enforcement (403 on non-local hosts).
- Secret redaction in receipts (`_redact_output` strips sk-, Bearer, tokens, private keys).
- Duplicate starts do not spawn unbounded processes (already_running short-circuit).
- Idle timeout enforcement via `/modules/sweep-idle`.
- Runtime safety guard (`assert_safe_for_runtime`) fails closed on dev JWT secret in non-dev.
