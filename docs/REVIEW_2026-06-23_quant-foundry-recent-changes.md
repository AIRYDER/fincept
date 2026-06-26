# Code Review — Recent Quant Foundry Changes

- **Date:** 2026-06-23
- **Branch:** `codex/portfolio-optimizer-core`
- **Scope:** Recent feature commits on the Quant Foundry surface (TASK-0901, TASK-1001–1004, TASK-0704, TASK-0802, TASK-0604) plus their dashboard wiring.
- **Reviewer mode:** Read-only review. No code was modified.
- **Commits in scope:**
  - `6256cdf` wire budget guard into gateway, fail closed on GPU spend
  - `a88e8c2` TASK-1001 mixture-of-experts model router
  - `e272b6e` TASK-1003 conformal prediction risk gate
  - `22700a7` TASK-1004 adversarial drift sentinel
  - `808e7ab` TASK-1002 causal market memory graph
  - `e95c51f` TASK-0704 paper-only model pointer bridge
  - `8f3a589` TASK-0802 jobs/dossiers/tournament/promotion dashboard pages
  - `4233e64` TASK-0604 shadow inference health dashboard

## Summary

The recent work adds a coherent set of read-only operator surfaces on top of the Quant Foundry gateway: a hard monthly budget guard wired fail-closed into job creation, a causal market-memory graph model, and several dashboard pages (jobs, dossiers, tournament, promotion, shadow health) backed by bearer-auth HTTP endpoints. The work is disciplined about the "shadow-only / no trading writes" invariant and is consistently TDD with file-disjoint commits. Overall quality is high. A handful of correctness and maintainability issues are worth addressing before merge.

## Correctness

### Verified good

- **Budget guard fail-closed wiring is correct.** `gateway.create_job` reserves via `BudgetGuard.check_and_reserve` *before* `outbox.enqueue`, and rejected jobs are never enqueued (confirmed by `test_gateway_budget.py` — `gw.outbox.get(...)` is `None` for rejected jobs). The route maps `budget_exceeded` → 402 and `budget_kill_switch` → 429. Zero-cost jobs (`None`/`0`) are always allowed, preserving the local-mock loop. 18/18 tests pass.
- **Kill-switch detection string match is sound.** The gateway classifies the error via `"kill switch" in decision.reason`; `budget.py` emits `"budget kill switch is active; all paid jobs are blocked"`, so the substring match holds. (See follow-up #1 for a fragility note.)
- **Shadow health aggregation is null-safe.** `gateway.shadow_health()` returns zero counts + null metrics for both the disabled and empty-ledger states; `_percentile` uses linear interpolation and is only called when `latencies` is non-empty. The populated-ledger test asserts p50=20.0 and p95=29.0 for `[10,20,30]`, which matches the interpolation formula. No secrets or raw callback payloads leak into the response (explicit forbidden-key assertion in the test).
- **Causal graph models are frozen + `extra="forbid"`.** `CausalEdge.strength` is validated to `[0.0, 1.0]`; the builder dedupes nodes by `node_id` and rejects duplicate `(source, target, kind)` edges. `to_dict()` round-trips through JSON. A dedicated invariant test asserts no `sig.predict`/`order`/`producer` symbols are exposed on the model classes.
- **Auth model is consistent.** Operator endpoints use bearer (`require_user`); the callback endpoint uses HMAC headers and is explicitly *not* bearer-protected. Missing headers → 401, bad signature → 401, unknown job → 404.

### Issues found

1. **`BudgetGuard.record_spend` docstring contradicts its implementation (real bug).** <ref_snippet file="C:\Users\nolan\CascadeProjects\fincept-terminal\services\quant_foundry\src\quant_foundry\budget.py" lines="200-215" /> The docstring says *"Use a negative amount to adjust a prior over-reservation downward,"* but the body raises `ValueError` for `amount_cents < 0`. `test_budget.py::test_negative_amount_raises` confirms negatives are rejected, so the documented "adjust downward" capability does not exist. Either implement negative adjustments (with a guard against driving the monthly total below zero) or fix the docstring. This matters because `runpod_client.py` calls `record_spend(result.cost_cents)` to reconcile reservations against actual spend — over-reservations can never be corrected today.
2. **`formatTs` heuristic in the shadow health page can misrender.** <ref_snippet file="C:\Users\nolan\CascadeProjects\fincept-terminal\apps\dashboard\src\app\quant-foundry\shadow\page.tsx" lines="245-253" /> It treats `ts > 10_000_000_000` as nanoseconds and everything else as seconds. The gateway returns `float(max(r.ts_event))` directly from `ShadowPrediction.ts_event`. If `ts_event` is stored as unix-nanoseconds (the schema's `horizon_ns` is in nanoseconds, suggesting ns is the house unit), real values (~1.7e18) render correctly. But the test fixtures use `ts_event=1000..3000`, which the page would render as seconds → dates in 1970. Low severity (test-only data), but the heuristic is fragile; prefer an explicit unit contract from the backend rather than a magnitude guess.
3. **`shadow_health` always reports `settled_count: 0` and `circuit_breaker_state: "closed"`.** These are hardcoded with comments saying the upstream surfaces aren't wired yet. This is *intentional and documented*, not a bug, but the dashboard surfaces them as live values. Operators may read "circuit breaker: CLOSED / VERIFIED" as a real green signal when no drift data exists at all. Consider distinguishing "not wired" from "checked and healthy" (e.g. an explicit `unknown` state) once the drift/settlement surfaces ship.

## Maintainability

### Strengths

- **File-disjoint, TDD commits.** Each feature commit stages only its own files and ships tests alongside the implementation. Commit messages document invariants, defaults, and what was *intentionally not* done (e.g. "do NOT invent new storage here").
- **Consistent disabled-state pattern.** Every dashboard page handles three states identically: 503 (gateway absent) → disabled card, loading, error, empty. The `UnavailableError` + `status === 503` check is reused across `jobs`, `tournament`, `promotion`, and `shadow` pages. Good copy-paste consistency.
- **Type parity.** `QuantFoundryShadowHealth` in `types.ts` matches the gateway's response dict key-for-key, including the `"closed" | "open" | "half_open"` literal union.
- **Frozen pydantic models with `extra="forbid"`** throughout the causal graph module — schema drift fails loud.

### Concerns

1. **Dashboard page duplication.** The `EmptyState`, `MetricRow`, `formatNs`, and disabled-detection blocks are copy-pasted across `jobs/page.tsx`, `shadow/page.tsx`, `tournament/page.tsx`, `promotion/page.tsx`, and `models/page.tsx`. This is already ~5 copies. Extract a shared `QuantFoundryPageShell` / `EmptyState` / `formatNs` helper before the next page is added, or refactors will be painful.
2. **Error-code classification by substring.** `gateway.create_job` decides `budget_kill_switch` vs `budget_exceeded` via `"kill switch" in decision.reason`. This couples the gateway to the guard's prose. Prefer an explicit `BudgetDecision.code` enum field so the contract is structural, not lexical.
3. **Lazily-constructed singletons on the gateway.** `dossier_registry()`, `expanded_leaderboard()`, `promotion_queue()`, and `shadow_ledger_real()` each guard a `None`-cached instance. This is fine, but the gateway now has four separate lazy caches plus the eager `outbox`/`inbox`/`dispatcher`/`processor`. A small "what is constructed when" comment block near `__init__` would help future readers.
4. **Working-tree hygiene.** `git status` shows a large volume of untracked cruft not part of these commits: `AAAAAAAAA_BIG_PLAN.md`, `clipboard-1782171544577.png`, `2026-06-22-*.txt`, `Sisyphus_*.md`, `.omo/`, `.opencode/`, `.playwright-cli/`, `.worktrees/`, `session-db.md`, etc. None are staged, but they risk accidental commits and bloat the repo view. Consider `.gitignore` entries or removal.

## Follow-ups

1. **Resolve the `record_spend` negative-amount contract.** Either implement downward adjustment (clamped at zero monthly spend) or correct the docstring + remove the misleading "adjust a prior over-reservation downward" line. Add a test for whichever behavior is chosen. *(Correctness — should fix before relying on RunPod cost reconciliation.)*
2. **Add a structural error code to `BudgetDecision`** and have the gateway switch on it instead of substring-matching `reason`. *(Maintainability.)*
3. **Extract shared dashboard primitives** (`EmptyState`, `MetricRow`, `formatNs`, disabled/503 detection) into a shared module under `components/quant-foundry/` before adding more pages. *(Maintainability.)*
4. **Define an explicit timestamp-unit contract** for `ShadowPrediction.ts_event` and have the shadow page format against it, replacing the magnitude-guess heuristic. *(Correctness/UX.)*
5. **Distinguish "not wired" from "healthy"** for `circuit_breaker_state` and `settled_count` once the drift/settlement surfaces ship, so the dashboard doesn't present absent data as a green signal. *(Correctness/UX.)*
6. **Clean up untracked working-tree cruft** — add `.gitignore` entries for `.omo/`, `.opencode/`, `.playwright-cli/`, `.worktrees/`, `clipboard-*.png`, `session-db.md`, and remove or commit the stray `.md`/`.txt` scratch files. *(Hygiene.)*
7. **Run the full dashboard typecheck/lint** as part of review verification — this review ran the Python tests for `test_causal_graph.py` and `test_gateway_budget.py` (18 passed) but did not execute `tsc`/`eslint` or the API-route test suite (`test_quant_foundry_shadow.py`, `test_quant_foundry_dossiers.py`, `test_quant_foundry_budget.py`) due to environment setup scope. Recommend running those before merge.

## Verification performed

- `git log --oneline -20`, `git diff --stat main...HEAD`, `git status`.
- Read full source: `causal_graph.py`, `gateway.py`, `budget.py`, `api/routes/quant_foundry.py`, `quant-foundry/shadow/page.tsx`, `quant-foundry/jobs/page.tsx`, `lib/types.ts`, `lib/api.ts`.
- Read full tests: `test_causal_graph.py`, `test_gateway_budget.py`, `test_quant_foundry_shadow.py`.
- Installed `quant_foundry` editable and ran `pytest tests/test_causal_graph.py tests/test_gateway_budget.py` → **18 passed**.
- Did **not** modify any code.
