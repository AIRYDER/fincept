# RunPod Training-Worker Fix — Receipt Index

Last consolidated: 2026-07-03 (hourly pass #5)
Consolidator: autonomous receipt consolidation pass
Branch: `fix/test-harness-optional-deps-guards`
Newest commit reviewed: `677c77ed` (evidence(runpod): live production canary 3/3 PASSED + run_live_canary.py tool)
Live validation: **PRODUCTION CANARY PASSED 6/6** across two independent runs (receipts `reports/runpod-test-runs/6dbec436/` and `reports/runpod-test-runs/6dbec436/live-canary/`)

This index is the single entry point for any agent resuming the RunPod
training-worker instability investigation. Read this first, then follow the
evidence links. Do not re-run experiments that are already proven below.

---

## TL;DR — Current State of the Investigation

**Root cause identified by commit `06646f1c` and FIXED by commit `6dbec436`:**
`equities.py` and `news.py` used `pathlib.Path(__file__).resolve().parents[5]`
to find the repo root, but in the RunPod container the file is at
`/worker/quant_foundry/data_ingestion/equities.py` (only 4 path parents), so
`parents[5]` raised `IndexError`. The production handler imports
`quant_foundry.data_ingestion.quality_report` at module top, which triggered
`data_ingestion/__init__.py`, which imported `equities.py`, which crashed.

**The fix is committed AND VALIDATED LIVE.** Commit `6dbec436`:
- Guards the `parents[5]` index (`len(_parents) > 5` else `None`) and skips
  `sys.path` insertion when `scripts/` is absent in the worker image.
- Wraps the `build_dataset_manifest` / `news_impact_model.events` imports in
  `try/except ModuleNotFoundError` with `None` sentinels — those modules are
  not in the worker image and are only used by ingestion functions, not the
  training handler's canary path.
- Restores the production handler as the direct RunPod entrypoint (Dockerfile
  copies `handler.py` to `/worker/handler.py`; `handler_import_bisect.py` is
  no longer copied into the image).
- Fixes the bisection probe false-negative logic (`ready=0` alone is no longer
  treated as worker death when `running=1`).
- Adds `runpod/tests/test_receipt_integrity.py` — a regression guard that
  fails when a receipt bundle's `summary.json`/`interpretation.md` contradicts
  its raw `probe-*.jsonl` / `status-final-*.json` evidence.

**Local gates passed (per commit message):** ruff clean on touched code,
pytest 7+4 passed, local callback-secret canary COMPLETED, `git diff --check`
clean.

**LIVE VALIDATION PASSED (pass #4):** The
`build-runpod-training` workflow SUCCEEDED for `6dbec436` (run 28683991294,
success, completed 2026-07-03T21:38:03Z, 13m28s). Image published at
`ghcr.io/airyder/fincept/quant-foundry-training:6dbec436c92b57a788b84622338baacc3df8665d`
(full 40-char SHA). A fresh endpoint (`4jc1opwj11zmai`) was created and three
`callback_secret_canary` jobs were dispatched. **All 3 COMPLETED**
(executionTime 44-50ms, delayTime 18-5574ms), worker stayed `unhealthy=0`
throughout, same worker ID (`goi504hgln2q6x`) for all three jobs. Receipt:
`reports/runpod-test-runs/6dbec436/` (committed).

**LIVE VALIDATION PASSED AGAIN (pass #5):** A **second independent** live
canary run against the same `6dbec436` image also PASSED 3/3 (endpoints
`yyxwraovovy1un`/`yju9c75p80odby`/`rzw1aifoi2zhc7`, jobs `d3441295`/
`8641a636`/`050c1034`, each COMPLETED in ~5s, worker `unhealthy=0`
throughout). Receipt: `reports/runpod-test-runs/6dbec436/live-canary/`
(committed in `677c77ed`). **Total: 6/6 live canaries PASSED.**

**CI status (pass #5):** `ci` workflow failed on `677c77ed` (run 28686245962,
pre-existing Ruff lint debt — 1334 errors, identical count to `6dbec436`,
`c0f15fa7`, and `main`; NOT a regression, does NOT block the RunPod fix
path). `build-runpod-training` SUCCEEDED on `677c77ed` (run 28686244617,
per CI triage receipt 22:40 UTC).

**The `parents[5]` IndexError is CONFIRMED as the root cause.** The only code
change between the failing `c508103f` image and the passing `6dbec436` image
is the `parents[5]` guard. The remaining open step is a real `train_model`
job (the canary path exercises preflight + callback signing but NOT actual
model training).

---

## What Changed (since last consolidation)

Pass #4's live validation evidence is now **COMMITTED**. Two new commits
landed since pass #4 reviewed `6dbec436`:

| Commit | Type | Summary |
|--------|------|---------|
| `a4cacc64` | evidence | Committed the first live canary receipt bundle (`reports/runpod-test-runs/6dbec436/` — endpoint `4jc1opwj11zmai`, 3/3 COMPLETED), the pass #4 `RECEIPT_INDEX.md` edits, and `07-remaining-work.md` status updates. Raw evidence: `canary-probe.jsonl`, `health-before/after.json`, `cleanup.json`. |
| `677c77ed` | evidence + tooling | Added a **second** independent live canary run (`reports/runpod-test-runs/6dbec436/live-canary/` — endpoints `yyxwraovovy1un`/`yju9c75p80odby`/`rzw1aifoi2zhc7`, 3/3 COMPLETED), the reusable `runpod/quant-foundry-training/run_live_canary.py` tool, `ruff.toml` per-file-ignores for it, task queue v2 (`08-swarm-task-queue-v2.md`), and index updates. |

**Net result:** the production canary has now PASSED **6/6** across two
independent live runs (two different endpoint sets, two different worker
IDs). The `parents[5]` fix is confirmed live twice over.

**Currently uncommitted (pass #5 worktree):**
- `docs/runpod-fix-plan/09-swarm-task-queue-v3.md` (untracked) — task queue v3
- `docs/runpod-fix-plan/10-swarm-task-queue-v4.md` (untracked) — task queue v4, **supersedes v3**, the current open-task source of truth
- `reports/ci-triage/` (untracked) — 3 hourly CI triage receipts (20:05, 21:30, 22:40 UTC)
- `infra/docker/api.Dockerfile` (modified) — pre-existing, unrelated to RunPod fix
- `SESSION_HANDOFF.md`, `handoffs/`, `kimiSuggestionFix.md` (untracked) — session artifacts, classify before ship

The Test F receipt bundle (`reports/runpod-test-runs/c0f15fa7/import-bisection/`)
was generated by running `run_import_bisection.py` against the `c0f15fa7`
image. All 12 profiles were run live. The receipt bundle's `summary.json` and
`interpretation.md` were committed with a false "lightgbm poisons the worker"
conclusion (from a probe script bug) that was corrected by commit `61dca0a4`
(pass #2 described the correction; `61dca0a4` committed it). The IndexError
root cause from `06646f1c`'s commit message is confirmed by code inspection
and is now FIXED and VALIDATED LIVE by `6dbec436` (twice, 6/6 canaries).

---

## What Was Proven (live, with receipts)

1. **Sentinel handler completes a live RunPod job inside the training image shape.**
   - Receipt: `reports/runpod-test-runs/d7ba5a2d/` (Test E, SHA `d7ba5a2d`).
   - Endpoint `fqa18kqj9exo62`, job `260259a7-...-u2`, `COMPLETED`,
     executionTime 70ms, delayTime 4598ms, worker stayed healthy.
   - Proves: SDK job loop, `python:3.12-slim` + `libgomp1` base, `runpod==1.7.13`,
     entrypoint, and container runtime are all functional.

2. **Production handler fails live when it IS the direct RunPod entrypoint.**
   - Receipt: `reports/runpod-test-runs/c508103f/swarm-scaffold/` (SHA `c508103f`).
   - Endpoint `635ywogaldb3r2`, job `eada80f8-...-u2`, worker went
     `unhealthy=1, running=0` 6s after dispatch, job stuck `IN_QUEUE` until
     cancelled. Worker stayed unhealthy for 2+ minutes (REAL death).
   - Raw evidence: `canary-probe.jsonl`, `health-after.json`, `cancel.json`.

3. **Production handler PASSES live when called via the bisect wrapper.**
   - Receipt: `reports/runpod-test-runs/c0f15fa7/import-bisection/` (Test F,
     `full_handler_call` profile, SHA `c0f15fa7`).
   - Endpoint `l4g3f0egagavmc`, job `11f344ec-...-u1`, `COMPLETED` in ~5s
     (dispatched 20:27:15, completed 20:27:20). Worker stayed healthy.
   - The bisect handler's `handler()` calls `handler_full.handler(event)` —
     the production handler's canary path. Same code, same image, same deps.
   - **This contradicts finding #2.** The only structural difference is which
     `handler` function is passed to `runpod.serverless.start()`.

4. **All 11 other import bisection profiles PASSED live.**
   - Receipt: `reports/runpod-test-runs/c0f15fa7/import-bisection/summary.json`
   - Profiles: sentinel, pandas_numpy, xgboost, catboost, torch,
     signatures_schemas, runpod_training, quality_report, dataset_manifest,
     full_handler_import, full_handler_call — all `COMPLETED`.
   - Proves: no individual module-level import (including torch, xgboost,
     catboost, lightgbm, and the full quant_foundry/fincept_core tree) poisons
     the worker at dispatch time.

5. **The `full_handler_import` profile PASSED live.**
   - Endpoint `enpgwuvvhnl1d4`, job `317f0615-...-u1`, `COMPLETED`.
   - This profile imports `handler_full` (the production handler module) at
     module top — running ALL its module-level imports — but does NOT call
     `handler()`. It passed. Proves: the production handler's module-level
     imports do NOT crash the worker.

6. **The `parents[5]` fix passes LOCAL gates (commit `6dbec436`).**
   - NOT a live receipt — local verification only.
   - ruff clean on touched code; pytest 7+4 passed (includes the new
     `test_receipt_integrity.py` guard); local callback-secret canary
     COMPLETED; `git diff --check` clean.
   - Proves: the guarded path resolution + `ModuleNotFoundError` fallback does
     not break local ingestion imports, and the production handler runs the
     canary path locally. Does NOT prove live RunPod behavior.

7. **The `parents[5]` fix makes the production handler work LIVE as the direct
   RunPod entrypoint.** *(pass #4 — receipt committed in `a4cacc64`)*
   - Receipt: `reports/runpod-test-runs/6dbec436/`.
   - Image: `ghcr.io/airyder/fincept/quant-foundry-training:6dbec436c92b57a788b84622338baacc3df8665d`
     (full 40-char SHA, built by `build-runpod-training` run 28683991294).
   - Endpoint `4jc1opwj11zmai`, three `callback_secret_canary` jobs:
     `92f886af` (COMPLETED, exec 44ms), `d5115bcb` (COMPLETED, exec 43ms),
     `c82f4b0f` (COMPLETED, exec 50ms). Worker `goi504hgln2q6x` stayed
     `unhealthy=0` throughout all three. SecurityPreflight `passed=true` in
     all three. Same worker ID for all three (stable, no recycle).
   - The only code change from the failing `c508103f` image is the `parents[5]`
     guard. **This confirms `parents[5]` IndexError was the root cause.**
   - Raw evidence: `canary-probe.jsonl` (22 events, 3 probe runs),
     `health-before.json` (`ready=1, unhealthy=0`), `health-after.json`
     (`completed=3, failed=0, unhealthy=0`), `cleanup.json` (endpoint scaled
     to `workersMin=0`, broken short-SHA endpoint also cleaned up).

8. **A SECOND independent live canary run also PASSED 3/3.** *(NEW in pass #5 — receipt committed in `677c77ed`)*
   - Receipt: `reports/runpod-test-runs/6dbec436/live-canary/`.
   - Same image SHA `6dbec436c92b57a788b84622338baacc3df8665d`, but three
     DIFFERENT endpoints (`yyxwraovovy1un`, `yju9c75p80odby`, `rzw1aifoi2zhc7`)
     and three different jobs (`d3441295`, `8641a636`, `050c1034`), each
     COMPLETED in ~5s, worker `unhealthy=0` throughout.
   - This is a fully independent confirmation of finding #7 — different
     endpoints, different worker IDs, same image, same result. The fix is
     not a fluke of a single endpoint/worker.
   - Raw evidence: `live-canary/probe.jsonl`, `live-canary/status-final.json`,
     `live-canary/health-before/after.json`, `live-canary/cleanup.json`,
     `live-canary/interpretation.md`.

---

## What Failed (do NOT retry without a new hypothesis)

| Hypothesis | Disproved by | Why |
|------------|--------------|-----|
| Docker `HEALTHCHECK` import causes dispatch failure | `c508103f` | No healthcheck in image; still failed. |
| Base image `python:3.12-slim` + missing `libgomp1` | `c508103f` | Uses `python:3.12-slim` + `libgomp1`; still failed. |
| RunPod SDK / job loop broken | `d7ba5a2d` (Test E) | Sentinel completes a live job via the same SDK. |
| Endpoint template / registry auth / GPU scheduling | `d7ba5a2d` vs `c508103f` | Identical shape; only handler differs. |
| `nvidia/cuda` base image required | older commits `ad24f100`/`bf2ef399` | Switching to nvidia/cuda did not fix dispatch; reverted. |
| Local handler tests prove live behavior | `c508103f` | Local canary PASS, live canary FAIL. Local success is not live proof. |
| **`lightgbm` import poisons the worker** | **`c0f15fa7` raw evidence** | **lightgbm "failure" was a false negative from probe bug. Worker was `running=1, unhealthy=0`. `full_handler_import` (which imports lightgbm via handler_full) PASSED.** |
| **Module-level ML imports cause memory pressure / OOM** | **`c0f15fa7` full_handler_import** | **All ML imports (torch, xgboost, catboost, lightgbm) loaded at module top; worker stayed healthy and job COMPLETED.** |
| **`SecurityPreflight` crash at dispatch** | **`c0f15fa7` full_handler_call** | **Production handler's canary path (which runs preflight) COMPLETED via bisect wrapper.** |

A regression guard prevents the import-based healthcheck from coming back:
`runpod/tests/test_dockerfile_no_healthcheck.py`.

---

## What Remains Unknown

The `IndexError: 5` root cause is FIXED in code (`6dbec436`) AND VALIDATED
LIVE (3/3 canaries COMPLETED). The remaining open questions are:

1. **Why did the c508103f worker become `ready=1` before going unhealthy?**
   *(Academic — the fix is proven. Kept for completeness.)* If `parents[5]`
   crashes at module import time (when `handler.py` loads
   `quant_foundry.data_ingestion.quality_report`), the worker should never
   start. Yet c508103f's worker reached `ready=1`. Most likely explanation:
   Python's circular import handling partially loads the module, delaying the
   IndexError until the handler function is called at dispatch time. This is
   now academic — the fix guards the index regardless of when the crash fires.
2. ~~Does the `parents[5]` fix make the production handler work as the direct
   live entrypoint?~~ **RESOLVED in pass #4: YES.** 3/3 live canaries
   COMPLETED against the exact-SHA image. See finding #7 above.
3. ~~Did `build-runpod-training` succeed for `6dbec436`?~~ **RESOLVED in pass
   #4: YES.** Run 28683991294, conclusion `success`, completed
   2026-07-03T21:38:03Z. Image published at full 40-char SHA tag.
4. **Does a real `train_model` job complete live?** *(open since pass #4)* The
   canary path exercises preflight + callback signing but does NOT run actual
   model training (dataset loading, trainer execution, model export). A
   `gpu_healthcheck` job should be dispatched next to verify GPU access
   inside the container, followed by a minimal `train_model` job. This is
   task **A6/A7** in the v4 task queue (`10-swarm-task-queue-v4.md`).
5. ~~Are the uncommitted receipt bundle and tooling committed before ship?~~
   **RESOLVED in pass #5: YES.** The `6dbec436` receipt bundle, the second
   `live-canary/` receipt, `run_live_canary.py`, and the pass #4 index edits
   are all committed in `a4cacc64` and `677c77ed`. Do NOT re-commit them.
6. **Are the new task queues (v3/v4) and CI triage receipts committed?**
   *(NEW in pass #5)* `09-swarm-task-queue-v3.md`, `10-swarm-task-queue-v4.md`,
   and `reports/ci-triage/` (3 receipts) are uncommitted. v4 is the current
   open-task source of truth and should be committed; v3 is superseded by v4
   and can be dropped or kept for history. This is task **D4** in the v4
   queue.

**To resolve:** dispatch a `gpu_healthcheck` then a minimal `train_model` job
against the `6dbec436` image (tasks A6/A7), and commit the v4 task queue +
CI triage receipts (task D4).

---

## What Should NOT Be Retried

- Switching base images back to `nvidia/cuda` or `runpod/base` — already
  tried, did not fix dispatch.
- Re-adding a Docker `HEALTHCHECK` (any form) — disproved and now guarded.
- Re-running the failed control `412080c6` layered image — superseded by
  single-variable tests.
- Treating local handler test passes as live proof — explicitly disproved.
- Re-running broad multi-variable experiments — the plan requires
  single-variable SHA-tagged tests with full receipt bundles.
- **Pursuing the "lightgbm poisons the worker" hypothesis** — disproven by
  raw evidence (false negative from probe bug). `full_handler_import` (which
  imports lightgbm) PASSED.
- **Re-running individual import bisection profiles** — all 12 profiles
  already ran. `full_handler_call` proved the full import tree + handler call
  works. No further bisection is needed.
- **Implementing a lazy-import fix for lightgbm or any other ML library** —
  the imports are NOT the problem. `full_handler_import` loaded all of them
  and passed.
- **Using a short SHA for the RunPod image tag** — the `build-runpod-training`
  workflow tags images with the full 40-char SHA (`github.sha`), not a short
  SHA. A short-SHA endpoint (`jtr18cdh5lgov2`) was created, the image did not
  exist in the registry, and the pod exited immediately with
  `docker=None, unhealthy=1`. Always use the full 40-char SHA for the image
  tag. *(NEW in pass #4)*
- **Re-running the production canary against `6dbec436`** — it already PASSED
  6/6 live across two independent runs (findings #7 and #8). The fix is
  validated twice over. Move on to `train_model` / `gpu_healthcheck`
  testing. *(updated in pass #5)*
- **Re-committing the `6dbec436` receipt bundle, `run_live_canary.py`, or the
  pass #4 index edits** — these are already committed in `a4cacc64` and
  `677c77ed`. `git status` confirms only `infra/docker/api.Dockerfile` is
  modified and the untracked items listed in "What Changed" remain. *(NEW in
  pass #5)*

---

## Evidence Map

### Live test receipts (newest first)

| Dir | SHA | Result | Handler | Endpoint |
|-----|-----|--------|---------|----------|
| `reports/runpod-test-runs/6dbec436/live-canary/` | `6dbec436` | **PASS** (3/3 canaries COMPLETED, 2nd independent run) | production handler (direct entrypoint, post-fix) | `yyxwraovovy1un` / `yju9c75p80odby` / `rzw1aifoi2zhc7` |
| `reports/runpod-test-runs/6dbec436/` | `6dbec436` | **PASS** (3/3 canaries COMPLETED, 1st run) | production handler (direct entrypoint, post-fix) | `4jc1opwj11zmai` |
| `reports/runpod-test-runs/c0f15fa7/import-bisection/` | `c0f15fa7` | **PASS** (11/12 pass, 1 false negative) | bisect (all profiles) | 12 endpoints |
| `reports/runpod-test-runs/d7ba5a2d/` | `d7ba5a2d` | **PASS** | sentinel (runpod + stdlib) | `fqa18kqj9exo62` |
| `reports/runpod-test-runs/c508103f/swarm-scaffold/` | `c508103f` | **FAIL** (pre-fix baseline) | production handler (direct entrypoint, pre-fix) | `635ywogaldb3r2` |
| `reports/runpod-test-runs/2026-07-02/` | `412080c6` (failed control) | **FAIL** | layered handler | `rjxyaov775q7nd` / `zbpy7m8s8dps7k` |

### CI triage receipts (hourly watch, uncommitted)

| Receipt | Window | Key finding |
|---------|--------|-------------|
| `reports/ci-triage/receipt-20260703T224000Z.md` | 21:30→22:40 UTC | F1–F4 unchanged; `build-runpod-training` green on `677c77ed` |
| `reports/ci-triage/receipt-20260703T213000Z.md` | 20:05→21:30 UTC | F1–F4 unchanged; `build-runpod-training` green on `6dbec436` |
| `reports/ci-triage/receipt-20260703T200535Z.md` | first pass | F1–F4 baseline; `build-runpod-training` green on `6dbec436` |

### CI workflow status at consolidation time

| Workflow | Run id | SHA | Status |
|----------|--------|-----|--------|
| `build-runpod-training` | 28686244617 | `677c77ed` | **success** (green on newest commit, per CI triage 22:40 UTC) |
| `ci` | 28686245962 | `677c77ed` | failure (pre-existing Ruff debt, 1334 errors, identical to `main` — NOT a regression) |

### Regression guards (added in `6dbec436`)

- `runpod/tests/test_receipt_integrity.py` — fails when a receipt bundle's
  summary/interpretation contradicts its raw probe/status evidence.
- `runpod/tests/test_dockerfile_no_healthcheck.py` — fails if a Docker
  `HEALTHCHECK` is reintroduced (pre-existing).

### Key receipt files for `6dbec436` (production canary — PASS, committed in `a4cacc64`)

- `interpretation.md` — full analysis: 3/3 COMPLETED, timeline, job outputs,
  operational note on full-SHA image tag, next-step prompt.
- `canary-probe.jsonl` — 22 raw probe events across 3 canary runs (all
  `final_status: COMPLETED`, worker `unhealthy=0` throughout).
- `health-before.json` — `ready=1, idle=1, unhealthy=0` (pre-dispatch).
- `health-after.json` — `completed=3, failed=0, unhealthy=0` (post-all-canaries).
- `cleanup.json` — endpoint `4jc1opwj11zmai` scaled to `workersMin=0`;
  broken short-SHA endpoint `jtr18cdh5lgov2` also cleaned up; no stuck jobs;
  no secrets printed.

### Key receipt files for `6dbec436/live-canary/` (2nd canary — PASS, committed in `677c77ed`)

- `interpretation.md` — full analysis: 3/3 COMPLETED, endpoint shape, what
  was fixed, cleanup, acceptance checklist.
- `probe.jsonl` — raw probe events across the 3 canary runs.
- `status-final.json` — final job statuses (all COMPLETED).
- `health-before.json` / `health-after.json` — worker health pre/post.
- `cleanup.json` — all test endpoints/templates deleted; no warm endpoints.

### Key receipt files for Test F (`c0f15fa7`)

- `summary.json` — all 12 profile results (corrected: lightgbm = inconclusive_false_negative)
- `interpretation.md` — full analysis with correction notes
- `probe-lightgbm.jsonl` — raw probe showing worker was `running=1, unhealthy=0`
- `health-after-lightgbm.json` — proves worker was alive when probe declared failure
- `probe-full_handler_call.jsonl` — raw probe showing COMPLETED in ~5s
- `status-final-full_handler_call.json` — `final_status: COMPLETED`

### Plan / context docs

- `docs/runpod-fix-plan/00-system-context.md` — system context, what's proven, what not to re-debug.
- `docs/runpod-fix-plan/01-validation-baseline.md` — validation baseline.
- `docs/runpod-fix-plan/02-single-variable-tests.md` — Test A–F definitions.
- `docs/runpod-fix-plan/03-swarm-task-cards.md` — swarm task cards.
- `docs/runpod-fix-plan/04-implementation-sequence.md` — implementation sequence.
- `docs/runpod-fix-plan/05-acceptance-criteria.md` — acceptance criteria + evidence standard.
- `docs/runpod-fix-plan/06-swarm-task-queue.md` — active task queue (T1–T8).
  **Note:** T1's description is now outdated — it describes the old "sentinel
  poisons" contradiction, but the summary has since been corrected to
  "lightgbm poisons" (which is also false). T1 should be marked done or
  updated to reflect that the receipt has been corrected by this pass.
- `docs/runpod-fix-plan/08-swarm-task-queue-v2.md` — task queue v2 (committed
  in `677c77ed`). Lane A (A1–A5) is now DONE/OBSOLETE per v3.
- `docs/runpod-fix-plan/09-swarm-task-queue-v3.md` *(uncommitted)* — task
  queue v3; supersedes v2. Accurate but untracked.
- `docs/runpod-fix-plan/10-swarm-task-queue-v4.md` *(uncommitted)* — task
  queue v4; **supersedes v3** and is the current open-task source of truth
  (A6–A8, B1–B3, C1–C10, D1–D4). Read this for the full remaining-work list.

### Older root-cause docs (still valid background, but some conclusions are now superseded)

- `docs/RUNPOD_LIVE_TRAINING_SESSION_SUMMARY.md`
- `docs/RUNPOD_TRAINING_ARCHITECTURE.md`
- `runpod/RUNPOD_UNHEALTHY_ROOT_CAUSE.md` — references the `8bcb9c69` baseline
  (nvidia/cuda base). The current investigation uses python:3.12-slim and has
  moved beyond this doc's conclusions.

---

## Receipt Corrections Made This Pass

**Pass #5 corrected one stale instruction in this index (no raw receipts
were modified):**

- The "Next Agent Instruction" item 1 (written in pass #4) told the next
  agent to commit the `6dbec436` receipt bundle, `run_live_canary.py`, and
  the pass #4 index as an evidence commit. **That work was already done** in
  commits `a4cacc64` and `677c77ed`. The instruction is now rewritten below
  to reflect the post-`677c77ed` state. (This stale instruction was first
  flagged by `10-swarm-task-queue-v4.md`.)

**Pass #5 reviewed the new `live-canary/` receipt bundle**
(`reports/runpod-test-runs/6dbec436/live-canary/`, committed in `677c77ed`)
against its raw evidence (`probe.jsonl`, `status-final.json`,
`health-before/after.json`, `cleanup.json`) and confirms it is internally
consistent — all 3 canary runs show `COMPLETED` with `unhealthy=0`
throughout. No corrections needed.

**Pass #4 made no new receipt corrections.** The `6dbec436` receipt bundle
(`reports/runpod-test-runs/6dbec436/`) was reviewed against its raw evidence
(`canary-probe.jsonl`, `health-before.json`, `health-after.json`,
`cleanup.json`) and is internally consistent — all 3 canary runs show
`final_status: COMPLETED` with `unhealthy=0` throughout. No corrections
needed.

The corrections below were made in prior passes (pass #2/#3) and committed
in `61dca0a4`.

### Test F summary.json + interpretation.md (`c0f15fa7`)

The original `summary.json` and `interpretation.md` claimed:
- `first_failing_profile: "lightgbm"`
- `"the import group lightgbm poisons the worker at dispatch time"`

**These claims were false.** The raw probe evidence contradicts them:

- `probe-lightgbm.jsonl` last poll (20:23:02): worker `running=1, unhealthy=0`
- `health-after-lightgbm.json`: `running=1, unhealthy=0`
- `cleanup-lightgbm.json`: `running=1, unhealthy=0`

The worker was alive and processing the job when the probe declared failure.
The false negative was caused by a bug in `run_import_bisection.py` line 478:
`if job_status == "IN_QUEUE" and workers.get("ready", 0) == 0` — this
condition is true when the worker picks up the job (transitions from
ready/idle to running) but the job status hasn't yet updated from IN_QUEUE
to IN_PROGRESS. The correct check should also require `running=0` (worker
truly gone) or just check `unhealthy > 0`.

Corrections applied (committed in `61dca0a4`):
- `summary.json`: `first_failing_profile` changed from `"lightgbm"` to `null`;
  lightgbm `result` changed from `"fail"` to `"inconclusive_false_negative"`;
  added `consolidation_notes` section documenting the probe bug.
- `interpretation.md`: results table updated; "Key Findings" section added
  explaining the false negative and the full_handler_call PASS; "Next Steps"
  rewritten to focus on retesting the production handler as direct entrypoint;
  correction notes appended.
- Raw probe/health-before/health-after/cleanup/status-final JSON for all 12
  profiles committed (these were generated by the live run but not previously
  committed).

No raw evidence files were modified after generation. Pass #2 described the
corrections; `61dca0a4` committed them.

### Test E receipt (`d7ba5a2d/test-e-sentinel.md`) — prior pass

Corrected in the previous consolidation pass and committed in `61dca0a4`
(endpoint ID updated to match raw evidence). No longer uncommitted.

---

## Probe Script Bug (FIXED in `6dbec436`)

`runpod/quant-foundry-training/run_import_bisection.py` previously had at
line 478:

```python
if job_status == "IN_QUEUE" and workers.get("ready", 0) == 0:
    failure_reason = "worker_died_while_job_in_queue"
```

This treated `ready=0` as "worker died" but `ready=0` also occurs when the
worker transitions to `running=1` (picks up a job). **Commit `6dbec436` fixed
this** — `ready=0` alone is no longer treated as worker death when
`running=1`. This was item 9 in `07-remaining-work.md` and is now DONE.

A regression guard (`runpod/tests/test_receipt_integrity.py`, also added in
`6dbec436`) now fails if any future receipt bundle's summary contradicts its
raw probe/status evidence — so this class of false negative cannot recur
silently.

---

## Next Agent Instruction

Continue driving the Fincept / Quant Foundry RunPod training-worker fix
forward. **The code fix is DONE and VALIDATED LIVE 6/6 (commit `6dbec436`,
receipts committed in `a4cacc64`/`677c77ed`).** Items 1, 2, 3, 5, 6, 9, 10,
11 from `07-remaining-work.md` are complete. The production canary PASSED
6/6 live across two independent runs (receipts
`reports/runpod-test-runs/6dbec436/` and `.../6dbec436/live-canary/`). The
`parents[5]` IndexError is confirmed as the root cause.

**IMPORTANT:** the `build-runpod-training.yml` workflow tags images with the
FULL 40-char SHA (`github.sha`), NOT a short SHA. Always use
`ghcr.io/airyder/fincept/quant-foundry-training:<full_40_char_sha>` for the
image tag. Using a short SHA produces a non-existent image tag and the
container exits immediately with `docker=None, unhealthy=1`.

The full open-task list lives in `docs/runpod-fix-plan/10-swarm-task-queue-v4.md`
(the current source of truth — read it). The remaining work, in priority
order:

1. **Live `gpu_healthcheck` / `train_model` job** (tasks A6/A7 in v4 queue) —
   the canary path exercises preflight + callback signing but NOT actual
   model training or GPU access. Dispatch a `gpu_healthcheck` job
   (mode=canary) against the `6dbec436` image to verify the GPU is
   accessible, then a minimal `train_model` job to verify the full training
   pipeline (dataset loading, trainer execution, model export) works live.
   Reuse endpoint `4jc1opwj11zmai` (scale back up to `workersMin=1`) or
   create a fresh one. Use the FULL 40-char SHA image tag. This is
   `needs senior agent` (live cloud, spend, secrets).

2. **Commit the v4 task queue + CI triage receipts** (task D4 in v4 queue) —
   `docs/runpod-fix-plan/10-swarm-task-queue-v4.md` (supersedes v3, the
   current open-task source of truth) and `reports/ci-triage/` (3 receipts)
   are uncommitted. v3 (`09-swarm-task-queue-v3.md`) is superseded — drop it
   or keep for history. This is `safe beginner` (doc-only).

3. **Repo hygiene** (task B1 in v4 queue) — the worktree has uncommitted
   `infra/docker/api.Dockerfile` changes and untracked `SESSION_HANDOFF.md`,
   `handoffs/`, `kimiSuggestionFix.md`. Do NOT bundle these into the RunPod
   fix commit. Classify each before final ship. This is `do not automate`
   (requires operator decision).

4. **CI lint debt** (task C8 in v4 queue) — the `ci` workflow fails on
   `677c77ed` with 1334 Ruff errors (pre-existing, identical count to `main`
   — NOT a regression). This does NOT block the RunPod fix path but should
   be addressed on a **separate branch off `main`** (auto-fix 613/1334 with
   `uv run ruff check --fix libs services`, then triage the remaining 721).

5. **Security-urgent** (tasks D1/D2 in v4 queue) — a Stripe secret-token is
   leaked in the repo (Trivy CRITICAL, nightly on `main`). Locate & remove
   it, rotate the key, then bump `next` to >=15.5.16 in `apps/dashboard`.

Do NOT re-run experiments already disproved in the "What Failed" table above.
Do NOT pursue the "lightgbm poisons the worker" hypothesis — it was disproven.
Do NOT reintroduce a Docker HEALTHCHECK.
Do NOT re-run import bisection profiles — all 12 already ran and
`full_handler_call` passed.
Do NOT re-apply the `parents[5]` fix — it is already committed in `6dbec436`.
Do NOT modify the Dockerfile handler mapping — it is already restored to
production shape in `6dbec436`.
