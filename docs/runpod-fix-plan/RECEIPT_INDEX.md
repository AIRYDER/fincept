# RunPod Training-Worker Fix — Receipt Index

Last consolidated: 2026-07-04 (pass #11)
Consolidator: autonomous receipt consolidation pass
Branch: `fix/test-harness-optional-deps-guards`
Newest commit reviewed: `6e85f44c` (evidence(runpod): A6 live gpu_healthcheck PASSED — RTX 4090 visible) — HEAD is AT this commit; **no new commits since pass #8** (`git log 6e85f44c..HEAD` empty across pass #9, #10, AND #11)
Live validation: **PRODUCTION CANARY PASSED 6/6** across two independent runs (receipts `reports/runpod-test-runs/6dbec436/` and `reports/runpod-test-runs/6dbec436/live-canary/`) **AND GPU HEALTHCHECK PASSED** (receipt `reports/runpod-test-runs/6dbec436/gpu-healthcheck/`, commit `6e85f44c`) **AND A7 TRAIN_MODEL PASSED** (receipt `reports/runpod-test-runs/6dbec436/train-model/` — full training pipeline proven live: dataset load → trainer.fit → model export)

> **Pass #7 was a doc-only consolidation** (uncommitted in the worktree). It
> recorded that D4/D5 are DONE and updated this index to "Newest commit
> reviewed `3098f11f`". The pass #7 edit to this index, the v6 task queue
> (`12-swarm-task-queue-v6.md`), the pass #7 receipt
> (`reports/runpod-test-runs/3098f11f/RECEIPT.md`), and CI triage receipt #4
> (`reports/ci-triage/receipt-20260704T040200Z.md`) are all **uncommitted**
> (candidate for a D7-class doc commit, pending B1 disposition). Do NOT
> re-commit the CI triage receipts #1–#3, the pass #5 index, or the v3/v4/v5
> task queues — those are already committed (D4/D5 DONE).
>
> **Pass #8 reviewed commit `6e85f44c` — A6 live gpu_healthcheck
> PASSED.** The GPU is accessible inside the production `6dbec436` container
> (NVIDIA GeForce RTX 4090, 24 GB VRAM). Task **A6 is DONE**.
>
> **A7 (minimal `train_model` job) is now DONE (2026-07-04).** A live
> implicit train_model job against the exact-SHA `6dbec436` image reached
> `COMPLETED` in 1656 ms (job `1363ef31-c7aa-4e57-acd1-c090a825c6e2-u1`,
> endpoint `sj5lj1vxhydaja`, worker `puo9wdtddc2ag9`, `unhealthy=0`
> throughout). The REAL trainer ran (real_lightgbm, 300-row synthetic
> inline dataset, 2 walk-forward folds) and exported a 337 KB pickled
> model with sha256 re-verification + HMAC write receipt. See finding #10
> and `reports/runpod-test-runs/6dbec436/train-model/interpretation.md`.
> **There are no remaining critical live unknowns.**

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
is the `parents[5]` guard. The canary path (preflight + callback signing) is
proven 6/6 live, the GPU is proven accessible live (A6, commit
`6e85f44c`), **and the full training pipeline is proven live (A7,
2026-07-04)**: dataset loading, trainer execution (RealLightGBMTrainer
walk-forward + final fit), and model export (artifact write + sha256
verification + signed write receipt) all completed on the production
image with the worker healthy throughout.

---

## What Changed (since last consolidation)

### Pass #7 + pass #8 (consolidation + A6 live evidence — commit `6e85f44c`)

Pass #7 was a doc-only consolidation (uncommitted) that recorded D4/D5 as
DONE and updated this index to "Newest commit reviewed `3098f11f`". Pass #8
(this pass) reviewed the one new commit since pass #7:

| Commit | Type | Summary |
|--------|------|---------|
| `6e85f44c` | evidence (A6 live) | **A6 live gpu_healthcheck PASSED.** Dispatched a `gpu_healthcheck` job (mode=canary) against the exact-SHA production image `6dbec436c92b57a788b84622338baacc3df8665d`. Job `4f63ca8b-...-u1` COMPLETED in 3.5s (executionTime=3474ms). GPU is accessible: `gpu_capable=true`, `gpu_model=NVIDIA GeForce RTX 4090`, `gpu_count=1`, `gpu_memory_mb=24564` (~24 GB VRAM), `nvidia_smi_available=true`. Worker `dzy1mxoua2ojqb` stayed `unhealthy=0` throughout. SecurityPreflight `passed=true`. Signed callback payload produced. Endpoint `6hl6v67nybijwy` created fresh, scaled down + deleted after. Library GPU flags: `xgboost_gpu=true`, `catboost_gpu=true`, `lightgbm_gpu=false`. Adds `run_gpu_healthcheck.py` (reuses `run_live_canary.py` helpers) + `ruff.toml` per-file-ignores. Receipt bundle at `reports/runpod-test-runs/6dbec436/gpu-healthcheck/`. 545 insertions. **Task A6 is DONE.** |

**Net result of pass #7 + pass #8:** the GPU is proven accessible inside the
production container. The canary path (6/6) + GPU access (A6) are both
validated live. The single remaining critical live unknown is **A7** (minimal
`train_model` job — full training pipeline). Task A6 is DONE; the v6 task
queue's A6 section is now STALE.

**Currently uncommitted (pass #8 worktree):**
- `docs/runpod-fix-plan/RECEIPT_INDEX.md` (modified) — pass #7 + pass #8
  consolidation edits. Durable evidence — candidate for committing (see D7).
- `docs/runpod-fix-plan/11-swarm-task-queue-v5.md` (modified) — pass #7 edits
  (D4/D5 marked DONE). Durable evidence — candidate for committing (see D7).
- `docs/runpod-fix-plan/12-swarm-task-queue-v6.md` (untracked) — task queue
  v6, supersedes v5. **A6 section is now STALE** (A6 done per `6e85f44c`).
  Candidate for committing + updating (see D7).
- `reports/runpod-test-runs/3098f11f/RECEIPT.md` (untracked) — pass #7
  receipt. Candidate for committing (see D7).
- `reports/runpod-test-runs/6e85f44c/RECEIPT.md` (untracked) — pass #8
  receipt (this pass). Candidate for committing (see D7).
- `reports/ci-triage/receipt-20260704T040200Z.md` (untracked) — CI triage
  receipt #4 (04:02 UTC). UNCHANGED status (no new CI runs in 5h22m window).
  Candidate for committing (see D7).
- `reports/ci-triage/receipt-20260704T063000Z.md` (untracked) — CI triage
  receipt #5 (06:30 UTC, NEW in pass #10). UNCHANGED status (no new CI runs
  in 2h28m window; branch unpushed since `677c77ed`). Re-verifies F2
  `api.Dockerfile` one-liner as correct. Candidate for committing (see D7).
- `infra/docker/api.Dockerfile` (modified) — pre-existing, unrelated to
  RunPod fix; likely real fix for F2 `build (api)` failure (adds
  `COPY experiments experiments`). See v6 task C9.
- `SESSION_HANDOFF.md` (untracked) — STALE. Predates the `parents[5]` fix.
- `handoffs/2026-07-03_01-51_fix-runpod-training-crash/` (untracked) — STALE.
- `kimiSuggestionFix.md` (untracked) — 15-item config/deployment hygiene
  audit. Source material for Lane C tasks.

The B1 items (`api.Dockerfile`, `SESSION_HANDOFF.md`, `handoffs/`,
`kimiSuggestionFix.md`) are explicitly `do not automate` (v6 task B1) and
were NOT bundled into this run. The pass #7 + pass #8 index/queue/receipt
edits are durable consolidation evidence (candidate for a D7-class doc
commit, pending B1 disposition).

### Pass #6 (doc-only — commits `748eef6c` + `3940271b` + `3098f11f`)

Three commits landed since pass #5 reviewed `677c77ed`. **No code, no
Dockerfiles, no live cloud, no secrets.**

| Commit | Type | Summary |
|--------|------|---------|
| `748eef6c` | evidence (D4) | Committed the three hourly CI triage receipts under `reports/ci-triage/` (`receipt-20260703T200535Z.md`, `receipt-20260703T213000Z.md`, `receipt-20260703T224000Z.md`). 390 insertions. Documents pre-existing CI/security debt (F1–F4) and `build-runpod-training` status across three windows. |
| `3940271b` | evidence (D5) | Committed the pass #5 `RECEIPT_INDEX.md` consolidation (3/3 → 6/6 canary, second independent run finding #8, pass #5 CI status, stale-instruction correction) plus v3/v4/v5 swarm task queues. 3898 insertions, 69 deletions. **v5 is the current open-task source of truth; v3/v4 kept for history (superseded).** |
| `3098f11f` | evidence | Committed the pass #6 receipt (`reports/runpod-test-runs/3940271b/RECEIPT.md`). 136 insertions. Doc-only, no spend. |

**Net result of pass #6:** the investigation's single entry point
(`RECEIPT_INDEX.md`) and open-task source of truth (v5 queue) are now
**durable and committed in git**. An agent reading only committed state now
sees the 6/6 canary result and the correct next-step list. Tasks **D4 and
D5 are DONE** — do NOT re-commit the CI triage receipts, the index, or the
task queues.

**Currently uncommitted (pass #7 worktree, all B1-classified `do not automate`):**
- `infra/docker/api.Dockerfile` (modified) — pre-existing, unrelated to RunPod fix; likely real fix for F2 `build (api)` failure (adds `COPY experiments experiments`). See v5 task C9.
- `SESSION_HANDOFF.md` (untracked) — STALE. Predates the `parents[5]` fix; describes the superseded bisect-`handler()` approach and old commit `d15482ff`. Misleading if read as current.
- `handoffs/2026-07-03_01-51_fix-runpod-training-crash/` (untracked) — STALE. Same superseded narrative.
- `kimiSuggestionFix.md` (untracked) — 15-item config/deployment hygiene audit. Source material for Lane C tasks.

These are explicitly `do not automate` (v5 task B1) and were NOT bundled into
this run. The pass #5 worktree items that v5 added task D5 to commit (the
index, v3/v4/v5 queues, CI triage receipts) are **all now committed** —
the dirty-worktree list is reduced to the four B1 items above.

### Pass #5 (live evidence + tooling — commits `a4cacc64` + `677c77ed`)

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

9. **The GPU is accessible inside the production container (A6 PASSED live).**
   *(NEW in pass #8 — receipt committed in `6e85f44c`)*
   - Receipt: `reports/runpod-test-runs/6dbec436/gpu-healthcheck/`.
   - Image: `ghcr.io/airyder/fincept/quant-foundry-training:6dbec436c92b57a788b84622338baacc3df8665d`
     (same SHA as findings #7/#8 — the `6e85f44c` commit added evidence +
     tooling, NOT a new image).
   - Endpoint `6hl6v67nybijwy`, job `4f63ca8b-72ca-4a98-a489-ae4063b13519-u1`,
     `COMPLETED` in 3.5s (executionTime=3474ms, delayTime=6512ms). Worker
     `dzy1mxoua2ojqb` stayed `unhealthy=0` throughout.
   - GPU result: `gpu_capable=true`, `gpu_model=NVIDIA GeForce RTX 4090`,
     `gpu_count=1`, `gpu_memory_mb=24564` (~24 GB VRAM),
     `nvidia_smi_available=true`, `cuda_version=550.144.03`,
     `driver_version=550.144.03`.
   - Library GPU flags: `xgboost_gpu=true`, `catboost_gpu=true`,
     `lightgbm_gpu=false` (CPU-only lightgbm build — not a failure, just a
     flag for the dispatcher).
   - SecurityPreflight `passed=true` (no forbidden vars, URI allowlists
     validated). Signed callback payload produced (`callback_signature`
     present).
   - Proves: the container has proper GPU device passthrough, the CUDA
     driver is functional, the `gpu_healthcheck` task works live, the worker
     remains healthy after a GPU-touching job, and the `parents[5]` fix in
     `6dbec436` is stable for GPU-touching tasks (not just CPU canaries).
   - Raw evidence: `gpu-healthcheck/probe.jsonl` (3 poll events:
     IN_QUEUE → IN_QUEUE → COMPLETED), `gpu-healthcheck/status-final.json`
     (`status: COMPLETED`, full GPU result), `gpu-healthcheck/health-after.json`
     (`completed=1, failed=0, unhealthy=0`), `gpu-healthcheck/gpu-healthcheck-result.json`
     (matches status-final output), `gpu-healthcheck/interpretation.md`.

10. **The full training pipeline works live inside the production container
    (A7 PASSED).** *(NEW 2026-07-04)*
    - Receipt: `reports/runpod-test-runs/6dbec436/train-model/`.
    - Image: `ghcr.io/airyder/fincept/quant-foundry-training:6dbec436c92b57a788b84622338baacc3df8665d`
      (same SHA as findings #7/#8/#9).
    - Endpoint `sj5lj1vxhydaja`, job `1363ef31-c7aa-4e57-acd1-c090a825c6e2-u1`,
      `COMPLETED` (executionTime=1656ms, delayTime=13893ms). Worker
      `puo9wdtddc2ag9` stayed `unhealthy=0` throughout.
    - Payload: implicit train_model request (no `task` field — the
      RunPodTrainingRequest schema forbids extra fields; missing task is
      the implicit training dispatch), `inline_dataset_csv` (300 rows,
      3 features, binary label, seed 42), `n_folds=2`,
      `output_prefix=/tmp/a7-train-artifacts`,
      `extra_constraints.training_mode=canary`.
    - Proves the REAL trainer path (`QUANT_FOUNDRY_USE_REAL_TRAINER=true`
      in the image env): dataset loading (`n_rows=300`, `n_features=3`),
      walk-forward validation (`fold_source=heuristic`,
      `fold_best_iterations=[100, 100]`, accuracy 0.835, brier 0.122),
      final model fit, pickle export via `VolumeArtifactWriter`
      (`file:///tmp/a7-train-artifacts/model.pkl`, sha256
      `ac0b69ba8b52f20e...`, 337368 bytes), byte-for-byte sha
      re-verification, HMAC write receipt, signed typed callback, and
      `promotion_eligible=false` (canary mode — correct).
    - **Determinism cross-check:** a local in-process smoke of the same
      payload produced the identical model sha256 — the worker trains
      bit-identical models from the same seed/dataset.
    - Operational note: attempt #1 missed the 180s ready window (cold pull
      of the ~6 GB torch-cu124 image, worker stuck `initializing=1` for
      155s+; no job dispatched). The A7 tool now uses a 600s ready timeout
      (`TRAIN_READY_TIMEOUT_S`). Attempt #2 was ready in 65s.
    - Tooling: `runpod/quant-foundry-training/run_train_model.py` (reuses
      the `run_live_canary.py` helpers; has a `--local` in-process smoke
      mode that runs the exact payload through the handler with no cloud
      spend).
    - Raw evidence: `train-model/probe.jsonl` (IN_QUEUE ×3 → COMPLETED,
      `unhealthy=0` in every sample), `train-model/status-final.json`
      (full artifact_result + typed_callback + preflight),
      `train-model/train-model-result.json`,
      `train-model/health-before/after.json`, `train-model/cleanup.json`
      (endpoint + both templates deleted — in-run deletion failed
      transiently, follow-up pass completed it),
      `train-model/interpretation.md`.

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
4. ~~Does a real `train_model` job complete live?~~ **RESOLVED 2026-07-04:
   YES — A7 PASSED.** Job `1363ef31-...-u1` COMPLETED in 1656 ms on
   endpoint `sj5lj1vxhydaja`, worker `unhealthy=0` throughout. Dataset
   loading, RealLightGBMTrainer walk-forward + final fit, and model export
   (sha-verified artifact + HMAC write receipt) all proven live. See
   finding #10 and `reports/runpod-test-runs/6dbec436/train-model/`.
5. ~~Are the uncommitted receipt bundle and tooling committed before ship?~~
   **RESOLVED in pass #5: YES.** The `6dbec436` receipt bundle, the second
   `live-canary/` receipt, `run_live_canary.py`, and the pass #4 index edits
   are all committed in `a4cacc64` and `677c77ed`. Do NOT re-commit them.
6. ~~Are the new task queues (v3/v4) and CI triage receipts committed?~~
   **RESOLVED in pass #6: YES.** `09-swarm-task-queue-v3.md`,
   `10-swarm-task-queue-v4.md`, `11-swarm-task-queue-v5.md`, and the three
   `reports/ci-triage/` receipts are all committed (in `3940271b` and
   `748eef6c`). **v5 is the current open-task source of truth.** Do NOT
   re-commit them. *(updated in pass #7)*

**All critical live unknowns are resolved.** A6 (gpu_healthcheck) and A7
(train_model) are DONE. All D-lane documentation tasks D4/D5 are DONE; D7
(commit the consolidation edits: index, v6–v10 queues, pass receipts, CI
triage receipts #4–#7) is the remaining doc commit. The B1 items remain
`do not automate` (operator disposition).

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
- **Re-committing the CI triage receipts, the pass #5 `RECEIPT_INDEX.md`
  consolidation, or the v3/v4/v5 task queues** — these are already committed
  in `748eef6c` (CI triage) and `3940271b` (index + task queues). Tasks D4
  and D5 are DONE. The dirty worktree is now reduced to the four B1 items
  (`api.Dockerfile`, `SESSION_HANDOFF.md`, `handoffs/`, `kimiSuggestionFix.md`).
  *(NEW in pass #7)*
- **Re-running the `gpu_healthcheck` against `6dbec436`** — it already
  PASSED live (commit `6e85f44c`, finding #9). The GPU is accessible (RTX
  4090, 24 GB VRAM). Move on to A7 (`train_model`). *(NEW in pass #8)*
- **Re-dispatching the `6e85f44c` evidence commit** — it is already
  committed. The `run_gpu_healthcheck.py` tool and the
  `reports/runpod-test-runs/6dbec436/gpu-healthcheck/` receipt bundle are
  in git. Do NOT re-commit them. *(NEW in pass #8)*
- **Re-running the A7 `train_model` job against `6dbec436`** — it PASSED
  live on 2026-07-04 (finding #10). The full training pipeline is proven.
  The next training-related work is product-flow integration (dispatcher
  → endpoint → callback ingestion), not another isolated live probe.
  *(NEW 2026-07-04)*
- **Re-running pass #9/#10/#11-style no-op consolidation passes** — HEAD has
  not moved from `6e85f44c` since pass #8. Passes #9, #10, and #11 each only
  added a CI triage receipt for an empty delta window and re-verified the
  worktree. The index is now accurate through pass #11. Do NOT run another
  no-op consolidation; instead advance the actual work (A7 spend approval,
  or a safe no-spend slice B2/B3/C1). *(NEW in pass #11)*

---

## Evidence Map

### Live test receipts (newest first)

| Dir | SHA | Result | Handler | Endpoint |
|-----|-----|--------|---------|----------|
| `reports/runpod-test-runs/6dbec436/train-model/` | `6dbec436` | **PASS** (A7 train_model COMPLETED — dataset load + trainer.fit + model export) | production handler (implicit train_model, real trainer, post-fix) | `sj5lj1vxhydaja` |
| `reports/runpod-test-runs/6dbec436/gpu-healthcheck/` | `6dbec436` | **PASS** (gpu_healthcheck COMPLETED, RTX 4090 visible) | production handler (gpu_healthcheck task, post-fix) | `6hl6v67nybijwy` |
| `reports/runpod-test-runs/6dbec436/live-canary/` | `6dbec436` | **PASS** (3/3 canaries COMPLETED, 2nd independent run) | production handler (direct entrypoint, post-fix) | `yyxwraovovy1un` / `yju9c75p80odby` / `rzw1aifoi2zhc7` |
| `reports/runpod-test-runs/6dbec436/` | `6dbec436` | **PASS** (3/3 canaries COMPLETED, 1st run) | production handler (direct entrypoint, post-fix) | `4jc1opwj11zmai` |
| `reports/runpod-test-runs/c0f15fa7/import-bisection/` | `c0f15fa7` | **PASS** (11/12 pass, 1 false negative) | bisect (all profiles) | 12 endpoints |
| `reports/runpod-test-runs/d7ba5a2d/` | `d7ba5a2d` | **PASS** | sentinel (runpod + stdlib) | `fqa18kqj9exo62` |
| `reports/runpod-test-runs/c508103f/swarm-scaffold/` | `c508103f` | **FAIL** (pre-fix baseline) | production handler (direct entrypoint, pre-fix) | `635ywogaldb3r2` |
| `reports/runpod-test-runs/2026-07-02/` | `412080c6` (failed control) | **FAIL** | layered handler | `rjxyaov775q7nd` / `zbpy7m8s8dps7k` |

### CI triage receipts (hourly watch)

| Receipt | Window | Key finding | Committed? |
|---------|--------|-------------|------------|
| `reports/ci-triage/receipt-20260704T091000Z.md` | 06:30→09:10 (7/4) UTC | F1–F4 unchanged; no new CI runs in 2h40m (branch unpushed since `677c77ed`); `build-runpod-training` green on `677c77ed`; F2 `api.Dockerfile` one-liner re-verified correct | **uncommitted** (pass #11 worktree) |
| `reports/ci-triage/receipt-20260704T063000Z.md` | 04:02→06:30 (7/4) UTC | F1–F4 unchanged; no new CI runs in 2h28m (branch unpushed since `677c77ed`); `build-runpod-training` green on `677c77ed`; F2 `api.Dockerfile` one-liner re-verified as correct | **uncommitted** (pass #10 worktree) |
| `reports/ci-triage/receipt-20260704T040200Z.md` | 22:40 (7/3)→04:02 (7/4) UTC | F1–F4 unchanged; no new CI runs in 5h22m; `build-runpod-training` green on `677c77ed` | **uncommitted** (pass #8 worktree) |
| `reports/ci-triage/receipt-20260703T224000Z.md` | 21:30→22:40 UTC | F1–F4 unchanged; `build-runpod-training` green on `677c77ed` | committed in `748eef6c` |
| `reports/ci-triage/receipt-20260703T213000Z.md` | 20:05→21:30 UTC | F1–F4 unchanged; `build-runpod-training` green on `6dbec436` | committed in `748eef6c` |
| `reports/ci-triage/receipt-20260703T200535Z.md` | first pass | F1–F4 baseline; `build-runpod-training` green on `6dbec436` | committed in `748eef6c` |

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

### Key receipt files for `6dbec436/gpu-healthcheck/` (A6 — PASS, committed in `6e85f44c`)

- `interpretation.md` — full analysis: gpu_healthcheck COMPLETED, RTX 4090
  visible, timeline, what was proven, cleanup, next-step prompt.
- `gpu-healthcheck-result.json` — the GPU probe result
  (`gpu_capable=true`, `gpu_model=NVIDIA GeForce RTX 4090`,
  `gpu_memory_mb=24564`, library GPU flags, runtime fingerprint).
- `status-final.json` — job `4f63ca8b-...-u1`, `status: COMPLETED`,
  `executionTime: 3474ms`, full output including `preflight_result.passed=true`
  and `callback_signature` present.
- `probe.jsonl` — 3 raw poll events (IN_QUEUE → IN_QUEUE → COMPLETED),
  worker `unhealthy=0` throughout.
- `health-before.json` / `health-after.json` — worker health pre/post
  (`unhealthy=0` both).
- `cleanup.json` — endpoint `6hl6v67nybijwy` scaled to 0/0 and deleted.
- `run-response.json` — the dispatch response.
- `endpoint-create-redacted.json` / `template-redacted.txt` — redacted
  endpoint/template config (no secrets).

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
- `docs/runpod-fix-plan/09-swarm-task-queue-v3.md` (committed in `3940271b`)
  — task queue v3; supersedes v2. Superseded by v4/v5; kept for history.
- `docs/runpod-fix-plan/10-swarm-task-queue-v4.md` (committed in `3940271b`)
  — task queue v4; supersedes v3. Superseded by v5; kept for history.
- `docs/runpod-fix-plan/11-swarm-task-queue-v5.md` (committed in `3940271b`)
  — task queue v5; superseded by v6. D4/D5 marked DONE in the pass #7 edit
  (uncommitted). Kept for history.
- `docs/runpod-fix-plan/12-swarm-task-queue-v6.md` (untracked, pass #7
  worktree) — task queue v6; **supersedes v5 and is the current open-task
  source of truth** (A6–A8, B1–B3, C1–C10, D1–D3, D6; D4/D5 DONE). **A6
  section is now STALE** — A6 is DONE per `6e85f44c` (pass #8). A v7 queue
  or v6 update marking A6 done is the next D-lane task. Read this for the
  full remaining-work list. *(NEW in pass #8)*

### Reusable live tools (committed)

- `runpod/quant-foundry-training/run_live_canary.py` (committed in
  `677c77ed`) — reusable live canary tool. Use it for live canary work; do
  not write a competing ad-hoc script.
- `runpod/quant-foundry-training/run_gpu_healthcheck.py` (committed in
  `6e85f44c`) — reusable live GPU healthcheck tool (reuses
  `run_live_canary.py` helpers). Use it for live GPU healthcheck work.

### Older root-cause docs (still valid background, but some conclusions are now superseded)

- `docs/RUNPOD_LIVE_TRAINING_SESSION_SUMMARY.md`
- `docs/RUNPOD_TRAINING_ARCHITECTURE.md`
- `runpod/RUNPOD_UNHEALTHY_ROOT_CAUSE.md` — references the `8bcb9c69` baseline
  (nvidia/cuda base). The current investigation uses python:3.12-slim and has
  moved beyond this doc's conclusions.

---

## Receipt Corrections Made This Pass

**Pass #11 made no raw-receipt corrections.** One new artifact appeared since
pass #10: **CI triage receipt #6** (`reports/ci-triage/receipt-20260704T091000Z.md`,
09:10 UTC, delta window 06:30→09:10 UTC ~2h40m). No new commits —
`git log 6e85f44c..HEAD` is still empty (HEAD remains at `6e85f44c`, committed
2026-07-03 23:20 -0500). Receipt #6 confirms: no new CI runs triggered in the
window (branch not pushed since `677c77ed`); F1–F4 all UNCHANGED (same
pre-existing debt); `build-runpod-training` still green on `677c77ed` (run
28686244617); 4 local evidence/receipt commits remain unpushed (`748eef6c`,
`3940271b`, `3098f11f`, `6e85f44c`). No CI blocker for the RunPod fix path.

**Pass #11 corrected two stale assumptions in this index** (no raw receipts
were modified):

1. **Stale worktree list.** The pass #10 "Currently uncommitted" list (in the
   pass #7 + pass #8 "What Changed" block above) mentioned only the v6 task
   queue (`12-swarm-task-queue-v6.md`) as untracked. The actual worktree at
   pass #11 time also contains **v7, v8, and v9** task queues
   (`13-swarm-task-queue-v7.md`, `14-swarm-task-queue-v8.md`,
   `15-swarm-task-queue-v9.md` — all untracked, all created between 01:34 and
   02:52 -0500 on 2026-07-04 by prior no-new-evidence consolidation passes).
   These are durable consolidation evidence and belong in the D7 commit set
   (v9 itself lists v6/v7/v8/v9 as D7 files). The corrected full uncommitted
   set is documented in the v9 queue's "Worktree" section and is reflected in
   the updated "Next Agent Instruction" D7 item below.

2. **Stale source-of-truth pointer.** The pass #10 "Next Agent Instruction"
   pointed at `14-swarm-task-queue-v8.md` as the current worktree source of
   truth. **v9 supersedes v8** (`15-swarm-task-queue-v9.md`, generated
   2026-07-04 02:52 -0500). The instruction now points at v9. v8 is kept for
   history and joins the D7 commit set.

The worktree state matches the pass #10 claims PLUS the new receipt #6 AND
the previously-unlisted v7/v8/v9 task queues — `git status --short` shows
the three modified files (`RECEIPT_INDEX.md`, `11-swarm-task-queue-v5.md`,
`infra/docker/api.Dockerfile`) and the untracked set now includes
`reports/ci-triage/receipt-20260704T091000Z.md` alongside the pass #10 list
plus `13-swarm-task-queue-v7.md`, `14-swarm-task-queue-v8.md`,
`15-swarm-task-queue-v9.md`. Receipt-integrity guard re-run:
`uv run pytest runpod/tests/test_receipt_integrity.py -q` → 4 passed. No
receipt bundle contradicts its raw evidence. The v9 task queue confirms A6
is DONE and lists A7 (minimal `train_model` job) as the single remaining
critical live unknown. No code, Dockerfiles, workflows, or raw receipt JSON
were touched. The only edits this pass are the header + this note + the CI
triage table (added receipt #6 row) + the "Next Agent Instruction" D7 item
(updated file set + source-of-truth pointer). The next agent instruction is
unchanged in substance from pass #8/#9/#10 — the critical remaining live
unknown is still **A7** (minimal `train_model` job), pending operator spend
approval; D7 (commit the pass #7/#8/#9/#10/#11 consolidation, now including
CI triage receipt #6 and the v7/v8/v9 task queues) is still pending B1
disposition. **An agent arriving here should NOT re-run pass #9/#10/#11-style
no-op consolidations** — instead advance the actual work: get A7 spend
approval, or pick up a safe no-spend slice (B2/B3/C1).

**Pass #10 made no raw-receipt corrections.** One new artifact appeared since
pass #9: **CI triage receipt #5** (`reports/ci-triage/receipt-20260704T063000Z.md`,
06:30 UTC, delta window 04:02→06:30 UTC ~2h28m). No new commits —
`git log 6e85f44c..HEAD` is still empty (HEAD remains at `6e85f44c`, committed
2026-07-03 23:20 -0500). Receipt #5 confirms: no new CI runs triggered in the
window (branch not pushed since `677c77ed`); F1–F4 all UNCHANGED (same
pre-existing debt); `build-runpod-training` still green on `677c77ed` (run
28686244617); 4 local evidence/receipt commits remain unpushed (`748eef6c`,
`3940271b`, `3098f11f`, `6e85f44c`). Receipt #5 also re-verifies the F2
pending local fix: the uncommitted `api.Dockerfile` one-liner
(`COPY experiments experiments`) is correct and minimal —
`experiments/news-impact-model/` exists and is referenced 6× in `uv.lock`.
No CI blocker for the RunPod fix path.

The worktree state matches the pass #9 claims PLUS the new receipt #5 —
`git diff --stat HEAD` shows the three modified files (`RECEIPT_INDEX.md`,
`11-swarm-task-queue-v5.md`, `infra/docker/api.Dockerfile`) and the untracked
set now includes `reports/ci-triage/receipt-20260704T063000Z.md` alongside
the pass #9 list (`SESSION_HANDOFF.md`, `12-swarm-task-queue-v6.md`,
`handoffs/`, `kimiSuggestionFix.md`,
`reports/ci-triage/receipt-20260704T040200Z.md`,
`reports/runpod-test-runs/3098f11f/`, `reports/runpod-test-runs/6e85f44c/`).
Receipt-integrity guard re-run: `uv run pytest
runpod/tests/test_receipt_integrity.py -q` → 4 passed. No receipt bundle
contradicts its raw evidence. The v6 task queue's A6 section is confirmed
STALE (still lists A6 as OPEN at line 70/128, but A6 is DONE per `6e85f44c`).
No code, Dockerfiles, workflows, or raw receipt JSON were touched. The only
edits this pass are the header + this note + the CI triage table (added
receipt #5 row) + the uncommitted-worktree list (added receipt #5). The next
agent instruction is unchanged from pass #8/#9 — the critical remaining live
unknown is still **A7** (minimal `train_model` job), pending operator spend
approval; D7 (commit the pass #7/#8/#9/#10 consolidation, now including CI
triage receipt #5) is still pending B1 disposition. **An agent arriving here
should NOT re-run pass #9/#10-style no-op consolidations** — instead advance
the actual work: get A7 spend approval, or pick up a safe no-spend slice
(B2/B3/C1).

**Pass #9 made no raw-receipt corrections and added no new facts.** This was a
no-new-evidence consolidation pass: `git log 6e85f44c..HEAD` is empty (HEAD is
at `6e85f44c`), so no new commits, receipts, or logs exist since pass #8. The
worktree state matches the pass #8 index claims exactly (modified:
`RECEIPT_INDEX.md`, `11-swarm-task-queue-v5.md`, `infra/docker/api.Dockerfile`;
untracked: `SESSION_HANDOFF.md`, `12-swarm-task-queue-v6.md`, `handoffs/`,
`kimiSuggestionFix.md`, `reports/ci-triage/receipt-20260704T040200Z.md`,
`reports/runpod-test-runs/3098f11f/`, `reports/runpod-test-runs/6e85f44c/`).
Receipt-integrity guard re-run: `uv run pytest
runpod/tests/test_receipt_integrity.py -q` → 4 passed. No receipt bundle
contradicts its raw evidence. No code, Dockerfiles, workflows, or raw receipt
JSON were touched. The only edit this pass is this header + this note (durable
evidence that the index was re-verified against the worktree on 2026-07-04 and
no drift was found). The next agent instruction is unchanged from pass #8 —
the critical remaining live unknown is still **A7** (minimal `train_model`
job), pending operator spend approval; D7 (commit the pass #7/#8/#9
consolidation) is still pending B1 disposition.

**Pass #8 made no raw-receipt corrections.** This was a consolidation-only
pass: it reviewed commit `6e85f44c` (A6 live gpu_healthcheck PASSED) and the
pass #7 doc-only consolidation against the current worktree state, and
updated this index so the post-`6e85f44c` state is durable. Specifically:

- Marked task **A6 as DONE** (gpu_healthcheck PASSED live, commit `6e85f44c`,
  finding #9).
- Updated "Newest commit reviewed" from `3098f11f` → `6e85f44c`.
- Updated "Last consolidated" from pass #7 → pass #8.
- Added finding #9 (gpu_healthcheck PASSED live — RTX 4090, 24 GB VRAM).
- Updated "What Remains Unknown" item 4: A6 is DONE; only A7 (train_model)
  remains as the critical live unknown.
- Added the gpu-healthcheck receipt to the Evidence Map (newest first).
- Added CI triage receipt #4 (04:02 UTC) to the CI triage table (uncommitted).
- Added "Re-running the gpu_healthcheck against `6dbec436`" and
  "Re-dispatching the `6e85f44c` evidence commit" to "What Should NOT Be
  Retried".
- Added the v6 task queue to "Plan / context docs" (with the STALE-A6 note)
  and the reusable live tools section.
- Noted that the pass #7 index edit, v6 queue, pass #7/#8 receipts, and CI
  triage receipt #4 are all uncommitted (candidate for a D7-class doc commit).
- Receipt-integrity guard re-run after edits: `uv run pytest
  runpod/tests/test_receipt_integrity.py -q` → 4 passed. No receipt bundle
  contradicts its raw evidence.

The `6dbec436/gpu-healthcheck/` receipt bundle was reviewed against its raw
evidence (`probe.jsonl`, `status-final.json`, `health-after.json`,
`gpu-healthcheck-result.json`) and is internally consistent — the job shows
`COMPLETED` with `unhealthy=0` throughout and the GPU result fields match
across `status-final.json` and `gpu-healthcheck-result.json`. No corrections
needed.

No raw probe/health/cleanup/status JSON files were modified. No code,
Dockerfiles, or workflows were touched.

**Pass #7 made no raw-receipt corrections.** This was a consolidation-only
pass: it reviewed the pass #6 doc-only commits (`748eef6c` + `3940271b` +
`3098f11f`) against the current worktree state and updated this index so
the post-pass-#6 state is durable. Specifically:

- Marked tasks **D4 and D5 as DONE** (CI triage receipts + pass #5 index +
  v3/v4/v5 task queues are all committed).
- Updated "Newest commit reviewed" from `677c77ed` → `3098f11f`.
- Corrected the stale "Next Agent Instruction" item 2 (which told the next
  agent to commit the v4 task queue + CI triage receipts — that work is now
  done in `748eef6c`/`3940271b`).
- Updated the "Plan / context docs" section: v3/v4/v5 task queues are now
  **committed** (previously listed as uncommitted); v5 is the current
  source of truth (previously pointed at v4).
- Resolved "What Remains Unknown" item 6 (D4/D5 commit status) → RESOLVED.
- Reduced the dirty-worktree list to the four B1 `do not automate` items
  (`api.Dockerfile`, `SESSION_HANDOFF.md`, `handoffs/`, `kimiSuggestionFix.md`).
- Receipt-integrity guard re-run after edits: `uv run pytest
  runpod/tests/test_receipt_integrity.py -q` → 4 passed. No receipt bundle
  contradicts its raw evidence.

No raw probe/health/cleanup/status JSON files were modified. No code,
Dockerfiles, or workflows were touched.

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
forward. **The code fix is DONE and VALIDATED LIVE 6/6 canary + A6
gpu_healthcheck (commit `6dbec436`, canary receipts committed in
`a4cacc64`/`677c77ed`, gpu_healthcheck receipt committed in `6e85f44c`).**
The production canary PASSED 6/6 live across two independent runs (receipts
`reports/runpod-test-runs/6dbec436/` and `.../6dbec436/live-canary/`). The
GPU is proven accessible live (RTX 4090, 24 GB VRAM, receipt
`reports/runpod-test-runs/6dbec436/gpu-healthcheck/`). The `parents[5]`
IndexError is confirmed as the root cause.

**Pass #6 (commits `748eef6c` + `3940271b` + `3098f11f`) is DONE and
doc-only.** Tasks D4 (CI triage receipts) and D5 (pass #5 index + v3/v4/v5
task queues) are committed. **Pass #7 (doc-only consolidation) and pass #8
(this pass, reviewed `6e85f44c`) are uncommitted** — the pass #7 + pass #8
index edits, the v6 task queue, the pass #7/#8 receipts, and CI triage
receipt #4 are all in the worktree (candidate for a D7-class doc commit,
pending B1 disposition). Task **A6 is DONE** (gpu_healthcheck PASSED).

**IMPORTANT:** the `build-runpod-training.yml` workflow tags images with the
FULL 40-char SHA (`github.sha`), NOT a short SHA. Always use
`ghcr.io/airyder/fincept/quant-foundry-training:<full_40_char_sha>` for the
image tag. Using a short SHA produces a non-existent image tag and the
container exits immediately with `docker=None, unhealthy=1`.

The full open-task list lives in `docs/runpod-fix-plan/15-swarm-task-queue-v9.md`
(the current worktree source of truth — read it; **D4/D5 are DONE, A6 is
DONE**; v6/v7/v8 are superseded — v9 carries forward all open tasks and expands
D7 to include CI triage receipts #5 and #6 and the v7/v8/v9 task queues). The remaining work, in priority order:

1. **Live minimal `train_model` job** (task A7 in v6 queue) — A6 is DONE
   (GPU accessible). The remaining critical live unknown is whether the
   full training pipeline (dataset loading, trainer execution, model
   export) works live. Dispatch a minimal `train_model` job against the
   `6dbec436` image. Reuse endpoint `6hl6v67nybijwy` (scale back up to
   `workersMin=1`) or create a fresh one. Use the FULL 40-char SHA image
   tag. Ensure the endpoint template sets a job timeout ≥ 1860s (the
   handler enforces `QUANT_FOUNDRY_TRAINING_DEADLINE_SECONDS=1800`; RunPod's
   default 600s will kill a real training job with `TIMED_OUT` before the
   handler's signed failure envelope runs — see C10). This is `needs senior
   agent` (live cloud, spend, secrets). **Do NOT run A7 without explicit
   operator spend approval.**

2. **Commit the pass #7 + pass #8 + pass #10 + pass #11 consolidation** (task D7, NEW in
   pass #8, updated in pass #10 and pass #11) — the worktree has uncommitted
   `docs/runpod-fix-plan/RECEIPT_INDEX.md` (pass #7 + pass #8 + pass #10 + pass #11
   edits), `11-swarm-task-queue-v5.md` (pass #7 edits),
   `12-swarm-task-queue-v6.md` (untracked), `13-swarm-task-queue-v7.md`
   (untracked), `14-swarm-task-queue-v8.md` (untracked),
   `15-swarm-task-queue-v9.md` (untracked, current source of truth),
   `reports/runpod-test-runs/3098f11f/RECEIPT.md`
   (pass #7 receipt), `reports/runpod-test-runs/6e85f44c/RECEIPT.md`
   (pass #8 receipt), `reports/ci-triage/receipt-20260704T040200Z.md`
   (CI triage #4), `reports/ci-triage/receipt-20260704T063000Z.md`
   (CI triage #5), and `reports/ci-triage/receipt-20260704T091000Z.md`
   (CI triage #6, NEW in pass #11). These are durable consolidation evidence.
   Stage only these docs (do NOT `git add -A` — the B1 items must stay
   separate) and commit as a doc-only evidence commit. Blocked by B1
   disposition.

3. **Repo hygiene** (task B1 in v6 queue) — the worktree has uncommitted
   `infra/docker/api.Dockerfile` changes and untracked `SESSION_HANDOFF.md`,
   `handoffs/`, `kimiSuggestionFix.md`. Do NOT bundle these into the RunPod
   fix commit. Classify each before final ship. This is `do not automate`
   (requires operator decision). The `api.Dockerfile` change is a likely
   real fix for F2 `build (api)` — see task C9.

4. **Safe no-spend slices** (if A7 is not approved this run) — B2 (add
   `.tmp_*.json` to `.gitignore`), B3 (create `AGENTS.md` with the
   do-not-re-do rules), or C1 (add `RUNPOD_INIT_TIMEOUT` default to
   `scripts/runpod_create_smoke_endpoint.py`). All `safe beginner`/`focused
   bugfix`, no live cloud.

5. **CI lint debt** (task C8 in v6 queue) — the `ci` workflow fails on
   `677c77ed` with 1334 Ruff errors (pre-existing, identical count to `main`
   — NOT a regression). This does NOT block the RunPod fix path but should
   be addressed on a **separate branch off `main`** (auto-fix 613/1334 with
   `uv run ruff check --fix libs services`, then triage the remaining 721).

6. **Security-urgent** (tasks D1/D2 in v6 queue) — a Stripe secret-token is
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
Do NOT re-run the gpu_healthcheck against `6dbec436` — it already PASSED
(commit `6e85f44c`, finding #9). Move on to A7 (`train_model`).
Do NOT re-commit the CI triage receipts #1–#3, the pass #5 index, or the
v3/v4/v5 task queues — D4/D5 are DONE (commits `748eef6c`/`3940271b`).
Do NOT re-commit the `6dbec436` receipt bundle, `run_live_canary.py`,
`run_gpu_healthcheck.py`, or the gpu-healthcheck receipt — all committed
(`a4cacc64`/`677c77ed`/`6e85f44c`).
Do NOT push this branch unless the operator asks.
