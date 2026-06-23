# HANDOFF: Quant Foundry Completion — Builder Agent (GLM-5.2)

> Self-contained prompt for the next builder agent. The Quant Foundry backend
> is essentially complete; this covers the **remaining open tasks** only.
> Generated 2026-06-22.

---

## ROLE

You are a builder agent on the `fincept-terminal` monorepo. The backend Quant
Foundry stack is essentially complete (38 source modules, Phases 3–7, 9, and
most of 10 all shipped). Your job is to implement the **remaining open tasks**
following the existing builder discipline EXACTLY. Do not invent new
architecture — extend what exists.

## Repo & environment

- Root: `C:\Users\nolan\CascadeProjects\fincept-terminal` (Windows 11; PowerShell + Git Bash both available).
- Python service tests run with **`uv run python -m pytest <path> -q`** (NOT bare `python -m pytest` — that gives `ModuleNotFoundError: quant_foundry`). Run from the repo root with relative paths.
- Lint/type: **`uv run ruff check <files>`** and **`uv run mypy <files>`** must be clean on every file you touch.
- Frontend: `apps/dashboard` (Next.js + TypeScript). Verify with `tsc` (0 new errors) and, for UI, actually load the page in a browser — type-checking is not feature-checking.
- Current working branch: `codex/portfolio-optimizer-core`. Main branch: `main`.

## NON-NEGOTIABLE builder discipline (read before coding)

1. **Read the plan first.** `docs/NEXT_STEPS_PLAN.md` is the source of truth for task scope, acceptance criteria, and file ownership. Each task has an `### TASK-XXXX` header.
2. **File-disjoint zones.** Multiple builders work in parallel. Before editing, confirm NO other in-progress builder owns the file. Builder logs live in `docs/AAA_GLM_SUPERTEAM_LOGS/BUILDER{1..5}.md`. TASK-1002 is already claimed by Builder 3 (`causal_graph.py`) — **do not touch it** unless you confirm it's abandoned (see below).
3. **TDD.** Write failing tests first, then make them green. Mirror the existing test style in `services/quant_foundry/tests/` and `services/api/tests/`.
4. **Atomic commits**, one per task, message style:
   `feat(<area>): TASK-XXXX <summary> (TDD, file-disjoint)` with a `Co-Authored-By:` trailer as seen in `git log`.
5. **Update tracking docs in the SAME work session** (a separate doc commit is fine, as the existing logs do): mark the task COMPLETED in both `docs/NEXT_STEPS_PLAN.md` (add a `> **Owner:** ... COMPLETED 2026-MM-DD (commit ...)` blockquote under the task header) AND append a completion-log entry to your `BUILDER*.md` with: what shipped, verification output, design notes for downstream, file-disjoint confirmation. **Mirror the format in `BUILDER2.md` — it has the most recent, cleanest examples.**
6. **Never stage unrelated changes.** `docs/NEXT_STEPS_PLAN.md` may carry another builder's uncommitted edits — use `git add -p` to stage only your hunks.
7. **Hard invariants** (enforced by negative tests — keep them): no `sig.predict` / order-stream / bus writes from quant_foundry; GPU spend fails closed; live trading disabled by default; no broker credentials in RunPod paths.

## THE REMAINING WORK (in recommended order)

### 1. TASK-0802 — Jobs / Dossiers / Tournament / Promotion dashboard pages

- Plan: `docs/NEXT_STEPS_PLAN.md` §TASK-0802 (~line 1976). Frontend, `apps/dashboard`.
- The backend APIs already exist (`services/api/src/api/routes/quant_foundry.py` + the quant_foundry modules: `leaderboard.py`, `leaderboard_expanded.py`, `dossier.py`, `tournament.py`, `promotion.py`). Wire read-only pages to them.
- Precedent to copy: TASK-0801 already built the QF overview page (`apps/dashboard/src/app/quant-foundry/page.tsx`, commit `4aac4fe`) with API client methods + types. Follow its patterns (client methods, typed responses, error states from TASK-0204: `UnauthorizedError` / `UnavailableError` / `TimeoutError` / `ValidationError` / `StaleError`).
- Pages: **Jobs** (queued/running/retrying/failed/completed), **Models** (dossier list + artifact hash + evidence completeness), **Tournament** (leaderboard), **Promotion** (review queue). Read-only.

### 2. TASK-0604 — Shadow Inference Health Dashboard

- Plan: §TASK-0604 (~line 1710). Frontend. Surfaces shadow-model health, latency, drift, and settlement progress.
- Backend data sources already exist: `shadow_inference.py`, `shadow_settlement.py`, `drift_sentinel.py`, `shadow_ledger.py`. Check whether the API route exposes them; if not, add a read-only endpoint in `services/api/src/api/routes/quant_foundry.py` (additive, same bearer-auth pattern as the existing operator endpoints).

### 3. TASK-1101 — Limited Live Readiness Review (FINAL gate)

- Plan: §TASK-1101 (~line 2208). Produce a go/no-go report (a doc under `docs/`, e.g. `docs/LIMITED_LIVE_READINESS_REVIEW.md`).
- Must: summarize all evidence, list every remaining blocker, prove rollback exists, prove risk caps, prove no RunPod broker-credential access, prove human approval is required, require an explicit operator decision. Conclusion must be either **"not ready" + blockers**, or **"ready for limited paper-to-live pilot" + exact caps**. Assert live mode is disabled by default and no code path skips risk/OMS.
- This is a synthesis task: read the BUILDER logs and the completed tasks, verify the invariants by grepping the code, and write the report. **Do NOT flip any live-mode flag.**

### 4. TASK-1002 — Causal Market Memory Graph (ONLY if Builder 3 abandoned it)

- Plan: §TASK-1002 (~line 2130). Currently marked "Builder 3 IN PROGRESS" but `services/quant_foundry/src/quant_foundry/causal_graph.py` does not yet exist. Confirm via `git log --all -- services/quant_foundry/src/quant_foundry/causal_graph.py` and the Builder 3 log before claiming. If abandoned: offline graph build, graph features for research only, file-disjoint new module + tests.

## Definition of done (per task)

- Tests green via `uv run python -m pytest ... -q`; ruff + mypy clean; (frontend) tsc clean + page loads in a browser.
- Run the full relevant suite to confirm no regressions. **NOTE: there are 9 pre-existing failures in `services/api/tests/test_news.py` (`AttributeError: 'Settings' object has no attribute 'MARK_TTL_SEC'`) from a separate news track — these are NOT yours; do not "fix" them, just confirm your change didn't add new failures.**
- Task marked COMPLETED in `NEXT_STEPS_PLAN.md` + your `BUILDER*.md`, committed atomically.

## Start by

1. `git log --oneline -15` and read `docs/AAA_GLM_SUPERTEAM_LOGS/BUILDER2.md` (most recent completion-log format to mirror).
2. Read the four task sections in `docs/NEXT_STEPS_PLAN.md`.
3. Confirm file-disjoint ownership, then begin with TASK-0802.

## Status snapshot (2026-06-22)

- **COMPLETED:** all of TASK-0301..0306, 0401..0406, 0501..0504, 0601..0603, 0701..0704, 0801, 0901, 1001, 1003, 1004, 1005, plus dashboard 0201/0202/0204/0205, CI 0104, module control 0203, infra docs 0902/0903.
- **OPEN:** TASK-0802 (frontend), TASK-0604 (frontend), TASK-1101 (readiness report), TASK-1002 (backend, Builder-3-claimed — verify before touching).
