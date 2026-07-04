# Fincept Swarm Task Queue v8

Last generated: 2026-07-04 (refresh of v7 after CI triage receipt #5 landed in worktree)
Branch: `fix/test-harness-optional-deps-guards`
HEAD: `6e85f44c` (evidence(runpod): A6 live gpu_healthcheck PASSED — RTX 4090 visible)
Source of truth for prior state: `docs/runpod-fix-plan/RECEIPT_INDEX.md` (pass #8, **uncommitted** in worktree)
Supersedes: `docs/runpod-fix-plan/13-swarm-task-queue-v7.md`

This queue **supersedes v7**. v7 was accurate as of pass #8 (A6 done). Since v7 was
generated, **no new commits have landed** (`git log 6e85f44c..HEAD` is empty) and
**no task has been completed**. The only worktree delta is one new untracked file:
CI triage receipt #5 (`reports/ci-triage/receipt-20260704T063000Z.md`, 06:30 UTC,
UNCHANGED status — no new CI runs in a 2h28m window; re-verifies F2
`api.Dockerfile` one-liner as correct). v8 therefore:

- **Carries forward every v7 task card unchanged** (all still open, all still
  accurate). v7's task cards are reproduced verbatim below so smaller agents do
  not cross-reference v7.
- **Expands D7** from 7 files to **8 files** (adds receipt #5 to the
  consolidation commit set).
- **Updates the worktree list, status rollup, dependency graph, blocked list, and
  recommended next assignment** to reflect the receipt #5 delta.
- v7 itself is now untracked and belongs in D7's commit set (v7 already listed
  itself; v8 lists both v7 and v8).

**What landed since v7:** nothing committed. One new untracked file (CI triage
receipt #5). The production image SHA is unchanged at
`6dbec436c92b57a788b84622338baacc3df8665d`.

Read `RECEIPT_INDEX.md` first for what is already proven. Do NOT re-run
experiments listed in its "What Should NOT Be Retried" table.

---

## What changed since v7 (delta)

- **Code:** none. No new commits. HEAD is still `6e85f44c`.
- **Commits:** none since v7.
- **Tasks DONE:** none. All v7 tasks remain open.
- **Tasks superseded:** none. v7's task set carries forward unchanged.
- **Worktree (current `git status --short`, post-v7):**
  - `docs/runpod-fix-plan/RECEIPT_INDEX.md` — **modified** (pass #7 + pass #8
    consolidation). Durable evidence, uncommitted. **D7 commits it.**
  - `docs/runpod-fix-plan/11-swarm-task-queue-v5.md` — **modified** (pass #7
    edits marking D4/D5 done). **D7 commits it.**
  - `docs/runpod-fix-plan/12-swarm-task-queue-v6.md` — **untracked** (v6 task
    queue; A6 section STALE; kept for history). **D7 commits it.**
  - `docs/runpod-fix-plan/13-swarm-task-queue-v7.md` — **untracked** (v7 task
    queue; superseded by v8 but kept for history). **D7 commits it.**
  - `docs/runpod-fix-plan/14-swarm-task-queue-v8.md` — **untracked** (this file,
    the new source of truth). **D7 commits it.**
  - `reports/runpod-test-runs/3098f11f/RECEIPT.md` — **untracked** (pass #7
    receipt). **D7 commits it.**
  - `reports/runpod-test-runs/6e85f44c/RECEIPT.md` — **untracked** (pass #8
    receipt — A6 done). **D7 commits it.**
  - `reports/ci-triage/receipt-20260704T040200Z.md` — **untracked** (CI triage
    #4, 04:02 UTC; UNCHANGED). **D7 commits it.**
  - `reports/ci-triage/receipt-20260704T063000Z.md` — **untracked, NEW since v7**
    (CI triage #5, 06:30 UTC; UNCHANGED — no new CI runs in 2h28m window;
    re-verifies F2 `api.Dockerfile` one-liner as correct). **D7 commits it.**
  - `infra/docker/api.Dockerfile` — **modified** (the C9 `COPY experiments`
    fix). Still uncommitted. **C9 still OPEN.**
  - `SESSION_HANDOFF.md`, `handoffs/`, `kimiSuggestionFix.md` — **untracked**
    (B1 items). Still present. **B1 still OPEN.**
- **`.gitignore`:** still contains `/.tmp_*.py` (line 128) but NO `.tmp_*.json`
  rule. **B2 unchanged.**

---

## One-paragraph state brief

The RunPod training-worker dispatch failure was root-caused to an unguarded
`parents[5]` index in `equities.py`/`news.py` (only 4 path parents exist in
the container). Commit `6dbec436` fixed it (guarded index +
`ModuleNotFoundError` fallback), restored the production handler as the direct
RunPod entrypoint, fixed the bisection probe false-negative logic, and added a
receipt-integrity guard test. The `build-runpod-training` workflow SUCCEEDED;
the image is published as
`ghcr.io/airyder/fincept/quant-foundry-training:6dbec436c92b57a788b84622338baacc3df8665d`.
**The live production-handler canary PASSED 6/6** across two independent runs
(commits `a4cacc64` + `677c77ed`) **AND the live GPU healthcheck PASSED**
(commit `6e85f44c` — RTX 4090, 24 GB VRAM, CUDA 550.144.03, xgboost/catboost
GPU true, lightgbm GPU false). The canary + gpu_healthcheck paths exercise
preflight + callback signing + GPU visibility but NOT actual model training —
so the single critical open live step is **A7** (a minimal `train_model` job:
dataset loading + trainer execution + model export). Pass #7 + pass #8
consolidated the index and recorded A6 done; that consolidation (plus the v6/v7
task queues, two receipts, and CI triage #4 + #5) is **uncommitted** (task D7,
now 8 files). No new commits have landed since `6e85f44c`.

---

## v7 Queue Status Rollup (all tasks)

| v7 Task | v8 Status | Note |
|---------|-----------|------|
| A6 live `gpu_healthcheck` | **DONE** | PASSED live in `6e85f44c` — RTX 4090 visible. Do not re-run. |
| A7 live minimal `train_model` | **OPEN** → A7 | Unchanged. Only operator spend awareness blocks it. |
| A8 consolidate A6/A7 receipts | **OPEN** → A8 | A6 portion doable now; A7 portion conditional on A7. |
| B1 classify dirty worktree | **OPEN** → B1 | **NARROWED** — D4/D5 done; remaining: `SESSION_HANDOFF.md`, `handoffs/`, `kimiSuggestionFix.md`, `api.Dockerfile`, pass #7+#8 consolidation set (now + receipt #5). |
| B2 gitignore scratch | **OPEN** → B2 | **REVISED** — `.tmp_*.py` rule exists; only `.tmp_*.json` remains. |
| B3 AGENTS.md | **OPEN** → B3 | unchanged (confirmed `AGENTS.md` still missing). |
| C1 RUNPOD_INIT_TIMEOUT default | **OPEN** → C1 | unchanged. |
| C2 Dockerfile dead code | **OPEN** → C2 | unchanged; needs operator decision. |
| C3 callback secret default | **OPEN** → C3 | unchanged. |
| C4 README sync | **OPEN** → C4 | unchanged. |
| C5 dead diagnostic files | **OPEN** → C5 | unchanged. |
| C6 training endpoint script | **OPEN** → C6 | unchanged. |
| C7 cuda-test SHA pins | **OPEN** → C7 | unchanged. |
| C8 ruff lint debt | **OPEN** → C8 | unchanged; separate branch off main. |
| C9 commit api.Dockerfile fix | **OPEN** → C9 | unchanged; blocked by B1. |
| C10 job timeout doc | **OPEN** → C10 | unchanged. |
| D1 Stripe secret removal | **OPEN** → D1 | unchanged; security-urgent. |
| D2 next CVE bump | **OPEN** → D2 | unchanged. |
| D3 pip-audit refresh | **OPEN** → D3 | unchanged. |
| D7 commit pass #7+#8 consolidation | **OPEN** → D7 | **EXPANDED** — now 8 files (adds CI triage receipt #5 + v8 itself). |

---

## Task Status Legend

- `safe beginner` — read-only or doc-only, no live cloud, no secrets, no production code.
- `focused bugfix` — narrow code/config change with a clear root cause; no live cloud.
- `needs senior agent` — touches live RunPod (secrets, spend, endpoints), production handler logic, security boundaries, or large-scope refactors requiring judgment.
- `do not automate` — requires explicit operator decision; do not run without human approval.

---

## Active Automations (do not duplicate)

- **`build-runpod-training` workflow** — green for `677c77ed`/`6dbec436`. The
  `6e85f44c` commit is evidence-only (no Dockerfile/handler/workflow change),
  so no new build was triggered and the image SHA is unchanged at
  `6dbec436`. Do not re-trigger the build unless a new code commit lands on
  this branch.
- **Receipt-integrity guard** (`runpod/tests/test_receipt_integrity.py`) and
  **no-healthcheck guard** (`runpod/tests/test_dockerfile_no_healthcheck.py`)
  are in place — do not re-add them.
- **Import bisection** (`run_import_bisection.py` / `handler_import_bisect.py`)
  is complete (all 12 profiles ran). Do not spawn a parallel bisection.
- **`run_live_canary.py`** and **`run_gpu_healthcheck.py`** reusable tools now
  exist (`runpod/quant-foundry-training/`). Use them for live canary / GPU
  healthcheck work; do not write competing ad-hoc scripts.
- **Hourly CI triage** is producing receipts under `reports/ci-triage/` (5
  receipts now: 20:05, 21:30, 22:40 UTC on 2026-07-03, and 04:02 + 06:30 UTC on
  2026-07-04; #1–#3 committed in `748eef6c`, #4 + #5 uncommitted → D7). Do not
  start a competing triage; D-lane tasks consume those receipts.

---

# Lane A — Live Validation (critical path)

The canary path is proven (6/6) and the GPU is proven accessible (A6 done).
The remaining live unknown is the **full training pipeline** (dataset loading
+ trainer execution + model export) — task A7.

## A6 — Live `gpu_healthcheck` job against `6dbec436` — DONE

**Status:** needs senior agent (was) → **DONE**
**Outcome:** PASSED live in commit `6e85f44c`. GPU is accessible inside the
production `6dbec436` container: `gpu_capable=true`,
`gpu_model=NVIDIA GeForce RTX 4090`, `gpu_count=1`, `gpu_memory_mb=24564`
(~24 GB VRAM), `nvidia_smi_available=true`, `cuda_version=550.144.03`,
`driver_version=550.144.03`. Library GPU flags: `xgboost_gpu=true`,
`catboost_gpu=true`, `lightgbm_gpu=false` (CPU-only lightgbm build — not a
failure, just a dispatcher flag). Worker `dzy1mxoua2ojqb` stayed
`unhealthy=0` throughout. Endpoint `6hl6v67nybijwy` created fresh, scaled
down + deleted after. Receipt: `reports/runpod-test-runs/6dbec436/gpu-healthcheck/`
(committed in `6e85f44c`).

**Do NOT re-run A6.** The GPU is proven accessible. The next live step is A7.

---

## A7 — Live minimal `train_model` job against `6dbec436`

**Status:** needs senior agent
**Objective:** Verify the full training pipeline (dataset loading, trainer
execution, model export) works live by dispatching a minimal `train_model` job
against the exact-SHA production image.

**Context:**
A6 PASSED — the GPU is accessible (RTX 4090, 24 GB). A7 is no longer blocked
by A6; it is blocked only by operator awareness of spend. Use a minimal
dataset so the job completes within the handler's
`QUANT_FOUNDRY_TRAINING_DEADLINE_SECONDS=1800` and RunPod's job timeout. Same
endpoint/image/redaction discipline as A6. **The RunPod default serverless
job timeout is 600s** — ensure the endpoint template sets a timeout ≥ 1860s,
or the platform will kill the job with `TIMED_OUT` before the handler's signed
failure envelope runs (see C10). **Use the FULL 40-char SHA image tag**
(`6dbec436c92b57a788b84622338baacc3df8665d`); a short SHA produces a
non-existent image and the container exits immediately (proven by the broken
`jtr18cdh5lgov2` endpoint).

This spends RunPod GPU time and uses secrets (`RUNPOD_API_KEY`,
`QUANT_FOUNDRY_CALLBACK_SECRET`, registry auth id). Do NOT run without
operator awareness of spend.

**Files allowed:**
- `reports/runpod-test-runs/6dbec436/train-model/` (new receipt bundle)

**Files forbidden:**
- `runpod/quant-foundry-training/Dockerfile` (production-shaped; do not change)
- `runpod/quant-foundry-training/handler.py` (production handler; do not touch)
- `services/quant_foundry/src/quant_foundry/data_ingestion/equities.py`
- `services/quant_foundry/src/quant_foundry/data_ingestion/news.py`
- inference worker files, UI, app, product files
- `.github/workflows/**`

**Commands to run:**
```powershell
$sha = "6dbec436c92b57a788b84622338baacc3df8665d"
$image = "ghcr.io/airyder/fincept/quant-foundry-training:$sha"
# Create a fresh endpoint via runpod/quant-foundry-training/run_live_canary.py
# or scripts/runpod_create_smoke_endpoint.py; ensure template timeout >= 1860s.
$endpoint = "<fresh-id>"
# Payload shape: minimal train_model task with a tiny dataset manifest.
# Exact payload TBD by the senior agent from handler.py's train_model branch.
$train_payload = '{"input":{"task":"train_model","mode":"canary","job_id":"qf:train:6dbec436:001",...}}'
uv run python scripts/runpod_smoke_probe.py --endpoint-id $endpoint --image-tag $image --interval 5 --timeout 1900 --payload-json $train_payload
```
Then capture `/health` after completion, scale the endpoint down, and record
cleanup.

**Acceptance criteria:**
- Job reaches `COMPLETED` with a model artifact reference (or a signed
  failure envelope if the handler fail-closed intentionally — record which).
- Worker remains `unhealthy=0` after completion.
- No secrets printed in the receipt.
- Endpoint scaled down or deleted after the test.
- Receipt bundle written under `reports/runpod-test-runs/6dbec436/train-model/`.

**Rollback plan:**
- No code rollback (probe-only on an already-built image).
- Scale down the endpoint even on early exit.
- If spend or rate limits are hit, stop and report; do not retry blindly.

**Evidence required:**
- `reports/runpod-test-runs/6dbec436/train-model/` receipt bundle
  (endpoint id, redacted settings, health before, `/run` response, status
  probe JSONL, final status JSON, health after, cleanup, short interpretation).
- One-line interpretation: PASS or FAIL with the terminal status and whether
  an artifact was produced.

**Blocked by:** Operator awareness of spend (do not automate the spend
decision). NOT blocked by A6 (A6 is DONE). Not blocked by any code task.

---

## A8 — Consolidate A7 receipts + update RECEIPT_INDEX (conditional)

**Status:** safe beginner
**Objective:** After A7, write the consolidated interpretation and update
`RECEIPT_INDEX.md` so the next agent reads a single current index reflecting
the live training-pipeline result. (The A6 portion is already consolidated in
the pass #8 index edit — D7 commits that baseline.)

**Context:**
`RECEIPT_INDEX.md` (pass #8, uncommitted) lists the canary as PASS (6/6) and
the GPU healthcheck as PASS (A6 done), but the training-pipeline row as NOT
YET TESTED. This task flips it based strictly on the raw A7 evidence.
Coordinate with D7 (which commits the pass #7 + pass #8 baseline) — A8 lands
after D7 so the index history stays linear.

**Files allowed:**
- `docs/runpod-fix-plan/RECEIPT_INDEX.md`
- `reports/runpod-test-runs/6dbec436/train-model/interpretation.md` (new)

**Files forbidden:**
- Raw probe/health/cleanup JSON files (immutable evidence — do not edit).
- All source code, Dockerfiles, workflows.
- Other receipt dirs.

**Commands to run:**
```powershell
git status --short
Get-Content reports/runpod-test-runs/6dbec436/train-model/status-final-*.json
Get-Content reports/runpod-test-runs/6dbec436/train-model/probe-*.jsonl
uv run pytest runpod/tests/test_receipt_integrity.py -q
```

**Acceptance criteria:**
- `RECEIPT_INDEX.md` Evidence Map row for the train-model result is updated
  with the endpoint id and PASS/FAIL.
- No claims contradict the raw probe evidence.
- `runpod/tests/test_receipt_integrity.py` still passes.

**Rollback plan:**
```powershell
git restore docs/runpod-fix-plan/RECEIPT_INDEX.md reports/runpod-test-runs/6dbec436/train-model/interpretation.md
```

**Evidence required:**
- `git diff` of the index.
- `pytest runpod/tests/test_receipt_integrity.py -q` output (pass).

**Blocked by:** A7 (must run first) + D7 (so the index baseline is committed first).

---

# Lane B — Repo Hygiene

## B1 — Classify remaining dirty worktree items

**Status:** do not automate
**Objective:** With the operator, decide the disposition of every untracked
and unrelated-modified item in the worktree so the final commit set is
reviewable and safe.

**Context:**
`git status` (post-`6e85f44c`, post-v7) shows:
- `docs/runpod-fix-plan/RECEIPT_INDEX.md` — **modified** (pass #7 + pass #8
  consolidation: records D4/D5 done, A6 done, "Newest commit reviewed"
  `6e85f44c`). **Durable evidence — candidate for committing (see D7).**
- `docs/runpod-fix-plan/11-swarm-task-queue-v5.md` — **modified** (pass #7
  edits marking D4/D5 done). **Durable evidence — candidate for D7.**
- `docs/runpod-fix-plan/12-swarm-task-queue-v6.md` — **untracked** (v6 task
  queue; A6 section now STALE). **Durable evidence — candidate for D7**
  (kept for history; v8 is the new source of truth).
- `docs/runpod-fix-plan/13-swarm-task-queue-v7.md` — **untracked** (v7 task
  queue; superseded by v8). **Durable evidence — candidate for D7**
  (kept for history).
- `docs/runpod-fix-plan/14-swarm-task-queue-v8.md` — **untracked** (v8 task
  queue, the current source of truth). **Candidate for D7.**
- `reports/runpod-test-runs/3098f11f/RECEIPT.md` — **untracked** (pass #7
  receipt). **Candidate for D7.**
- `reports/runpod-test-runs/6e85f44c/RECEIPT.md` — **untracked** (pass #8
  receipt — A6 done). **Candidate for D7.**
- `reports/ci-triage/receipt-20260704T040200Z.md` — **untracked** (CI triage
  #4, 04:02 UTC; UNCHANGED status). **Candidate for D7.**
- `reports/ci-triage/receipt-20260704T063000Z.md` — **untracked, NEW since
  v7** (CI triage #5, 06:30 UTC; UNCHANGED status; re-verifies F2
  `api.Dockerfile` one-liner as correct). **Candidate for D7.**
- `infra/docker/api.Dockerfile` — **modified**, adds `COPY experiments
  experiments`. This is a fix for the F2 `build-images` / `build (api)`
  failure (the `experiments/news-impact-model` workspace path dep can't
  resolve without the dir in build context). **Likely a real fix — see C9.**
  Keep separate from the RunPod fix.
- `SESSION_HANDOFF.md` — **untracked, STALE**. Predates the `parents[5]` fix;
  describes the superseded bisect-`handler()` approach and old commit
  `d15482ff`. Misleading if read as current.
- `handoffs/2026-07-03_01-51_fix-runpod-training-crash/` — **untracked,
  STALE**. Same superseded narrative as `SESSION_HANDOFF.md`.
- `kimiSuggestionFix.md` — **untracked**. 15-item config/deployment hygiene
  audit. Source material for Lane C tasks. Keep as a reference doc or move
  into `docs/`.

(Already committed and NOT in the worktree: D4's `reports/ci-triage/`
receipts #1–#3, D5's pass #5 index + v3/v4/v5 task queues, A6's
`gpu-healthcheck/` receipt bundle. Do NOT re-commit any of those.)

Do NOT delete or commit anything without explicit operator approval per item.

**Files allowed:**
- Only after operator approval: `SESSION_HANDOFF.md`, `handoffs/`,
  `kimiSuggestionFix.md`, and the D7 consolidation set
  (`docs/runpod-fix-plan/RECEIPT_INDEX.md`,
  `docs/runpod-fix-plan/11-swarm-task-queue-v5.md`,
  `docs/runpod-fix-plan/12-swarm-task-queue-v6.md`,
  `docs/runpod-fix-plan/13-swarm-task-queue-v7.md`,
  `docs/runpod-fix-plan/14-swarm-task-queue-v8.md`,
  `reports/runpod-test-runs/3098f11f/RECEIPT.md`,
  `reports/runpod-test-runs/6e85f44c/RECEIPT.md`,
  `reports/ci-triage/receipt-20260704T040200Z.md`,
  `reports/ci-triage/receipt-20260704T063000Z.md`).

**Files forbidden:**
- `infra/docker/api.Dockerfile` (handled by C9, not here).
- All tracked source code, Dockerfiles, workflows.
- Any file under `reports/runpod-test-runs/6dbec436/` (immutable live evidence).

**Commands to run (triage only, no deletion/commit yet):**
```powershell
git status --short
Get-Content SESSION_HANDOFF.md -TotalCount 5
Get-Content kimiSuggestionFix.md -TotalCount 5
git diff --stat docs/runpod-fix-plan/RECEIPT_INDEX.md docs/runpod-fix-plan/11-swarm-task-queue-v5.md
ls handoffs/
ls reports/runpod-test-runs/3098f11f/ reports/runpod-test-runs/6e85f44c/
ls reports/ci-triage/
```

**Acceptance criteria:**
- Operator has reviewed the list and approved a per-item
  commit / delete / move / keep-as-untracked decision.
- Only approved actions are taken.
- A note is left (commit message or a receipt) listing each decision.
- No secrets or local-only scratch artifacts are staged accidentally.

**Rollback plan:**
- Deleted untracked files are NOT recoverable via git. If unsure, move to a
  local backup dir outside the repo instead of deleting.

**Evidence required:**
- The operator's approved disposition list.
- `git status --short` after actions showing only intended changes.

---

## B2 — Add `.tmp_*.json` scratch pattern to `.gitignore`

**Status:** focused bugfix
**Objective:** Prevent future `.tmp_*.json` scratch files (e.g. the
`.tmp_canary_payload{,2,3}.json` files the `6dbec436` live run produced) from
cluttering `git status` or being committed by accident.

**Context:**
`.gitignore` already contains `/.tmp_*.py` (line 128) — the `.py` half of v5's
B2 is effectively done. Only the `.tmp_*.json` rule remains. A narrow rule
keeps future diagnostic work out of the tracked set without affecting any
committed file.

**Files allowed:**
- `.gitignore`

**Files forbidden:**
- All source code, Dockerfiles, workflows.
- Do not add `SESSION_HANDOFF.md` or `handoffs/` to `.gitignore` here — those
  are intentional handoff artifacts whose disposition is B1's job.

**Commands to run:**
```powershell
git diff -- .gitignore
git check-ignore -v .tmp_canary_payload.json 2>$null  # may not exist; that's fine
git status --short
```

**Acceptance criteria:**
- `.gitignore` gains a rule matching `.tmp_*.json` (and `.tmp_*` generally if
  the operator prefers one broad rule — but it must not blanket-ignore
  `reports/` or `handoffs/`).
- No existing tracked file becomes ignored (`git status` shows no surprises).

**Rollback plan:**
```powershell
git restore .gitignore
```

**Evidence required:**
- `git diff` of `.gitignore`.
- `git status --short` showing no unintended ignores.

---

## B3 — Create `AGENTS.md` with project rules

**Status:** safe beginner
**Objective:** Capture the hard-won operational knowledge (base image
selection, no Docker HEALTHCHECK, direct entrypoint, init timeout, full-SHA
image tag) so future agents avoid re-running disproved experiments.

**Context:**
The repo has `PROJECT_OVERVIEW.md` but no `AGENTS.md` (confirmed missing).
Operational knowledge is scattered across commits,
`RUNPOD_UNHEALTHY_ROOT_CAUSE.md`, the README, and `RECEIPT_INDEX.md`. An
`AGENTS.md` consolidates the "do not re-do" rules in one place agents read
first.

**Files allowed:**
- `AGENTS.md` (new, repo root)

**Files forbidden:**
- All other files. Do not modify `PROJECT_OVERVIEW.md`, the README, or any
  existing doc here.

**Commands to run:**
```powershell
git status --short
Select-String -Path PROJECT_OVERVIEW.md -Pattern "HEALTHCHECK|base image|entrypoint" -SimpleMatch
```

**Acceptance criteria:**
- `AGENTS.md` exists at the repo root with concise rules:
  - Do not change the RunPod training base image to `pytorch/pytorch` or
    `runpod/base` without re-running the layer-0 probe (disproved).
  - Do not reintroduce a Docker `HEALTHCHECK` (disproved + regression-guarded).
  - Always set `RUNPOD_INIT_TIMEOUT >= 900` for CUDA training endpoints.
  - Keep `ENTRYPOINT ["python", "-u", "/worker/handler.py"]` (direct).
  - Always use the FULL 40-char SHA for the RunPod training image tag
    (`github.sha`); a short SHA produces a non-existent image and the
    container exits immediately.
  - Never commit `RUNPOD_API_KEY`, `QUANT_FOUNDRY_CALLBACK_SECRET`, or
    endpoint IDs in scripts/docs.
  - Local handler test passes are NOT live proof (disproved by `c508103f`).
  - Do not pursue the "lightgbm/ML imports poison the worker" hypothesis
    (disproved by Test F `full_handler_import`).
  - Do not re-apply the `parents[5]` fix or re-run import bisection — both
    done in `6dbec436`.
  - Do not re-run A6 (GPU healthcheck) — PASSED live in `6e85f44c` (RTX 4090).
- Rules are derived from `RECEIPT_INDEX.md` "What Should NOT Be Retried".

**Rollback plan:**
```powershell
git rm AGENTS.md
```

**Evidence required:**
- File content of `AGENTS.md`.
- One-line confirmation each rule maps to a disproved hypothesis in
  `RECEIPT_INDEX.md`.

---

# Lane C — Configuration / Deployment Hygiene

Source: `kimiSuggestionFix.md` (15-item audit). Items below are the ones safe
to dispatch to smaller agents. Items requiring security-boundary judgment or
large-scope refactors are marked `needs senior agent`.

## C1 — Add `RUNPOD_INIT_TIMEOUT` to endpoint creation script defaults

**Status:** focused bugfix
**Objective:** Prevent new CUDA training endpoints from being marked unhealthy
during cold start by setting an explicit init timeout.

**Context:**
`scripts/runpod_create_smoke_endpoint.py` does not set `RUNPOD_INIT_TIMEOUT`.
Large CUDA images take ~8–12 min to cold-start; RunPod's default is 7 min.
The diagnostic history shows endpoints only became healthy after manually
injecting 900s. New training endpoints created from this script will regress.

**Files allowed:**
- `scripts/runpod_create_smoke_endpoint.py`

**Files forbidden:**
- `runpod/quant-foundry-training/Dockerfile`
- `handler.py`, any source code
- `.github/workflows/**`

**Commands to run:**
```powershell
uv run ruff check scripts/runpod_create_smoke_endpoint.py
uv run python -c "import ast; ast.parse(open('scripts/runpod_create_smoke_endpoint.py').read())"
```

**Acceptance criteria:**
- Training-shaped endpoint creation injects `RUNPOD_INIT_TIMEOUT=900` by
  default (or via a `--training` flag / dedicated script — see C6).
- CPU/smoke endpoints keep a lower default (e.g., 600) or the platform default.
- Ruff passes.

**Rollback plan:**
```powershell
git restore scripts/runpod_create_smoke_endpoint.py
```

**Evidence required:**
- `git diff` of the script.
- `ruff check` output (clean).

---

## C2 — Resolve Dockerfile dead code (entrypoint/preflight/gosu/trainer)

**Status:** needs senior agent
**Objective:** Eliminate the mixed-state Dockerfile where `entrypoint.sh`,
`preflight.py`, `gosu`, and the `trainer` user are built but never executed
because the `ENTRYPOINT` is direct Python.

**Context:**
The Dockerfile creates `/worker/preflight.py`, `/worker/entrypoint.sh`, a
non-root `trainer` user, and installs `gosu`, then sets
`ENTRYPOINT ["python", "-u", "/worker/handler.py"]`. The preflight/chown/
privilege-drop logic never runs. This is a security-boundary decision: either
restore the entrypoint wrapper (Option A) or remove the dead code and rely on
handler-level `SecurityPreflight.run()` at request time (Option B). The
previous session showed `gosu` drop caused 40s unhealthy exits, so GPU
device permissions must be verified if Option A is chosen. Do NOT decide
without the operator.

**Files allowed:**
- `runpod/quant-foundry-training/Dockerfile`
- `runpod/quant-foundry-training/README.md` (to document the chosen option)

**Files forbidden:**
- `handler.py` (do not change handler logic here)
- `equities.py`, `news.py` (already fixed)
- inference worker files, UI, app, product files
- `.github/workflows/**`

**Commands to run:**
```powershell
Select-String -Path runpod/quant-foundry-training/Dockerfile -Pattern "preflight|entrypoint|gosu|trainer|ENTRYPOINT" -Context 1
uv run ruff check runpod/quant-foundry-training
```

**Acceptance criteria:**
- The Dockerfile is internally consistent: either the entrypoint wrapper runs
  preflight+chown+gosu AND GPU permissions are verified, OR the dead code is
  removed and the README documents request-time preflight as the sole boundary.
- No mixed state remains.
- A live canary (A6 shape) still passes after the change.

**Rollback plan:**
```powershell
git restore runpod/quant-foundry-training/Dockerfile runpod/quant-foundry-training/README.md
```

**Evidence required:**
- `git diff` of the Dockerfile + README.
- Operator's chosen option (A or B) recorded.
- Live canary receipt on the changed image (separate from A6).

**Blocked by:** Operator decision on entrypoint strategy. Do not automate.

---

## C3 — Align `QUANT_FOUNDRY_CALLBACK_SECRET` default (Dockerfile vs README)

**Status:** focused bugfix
**Objective:** Resolve the contradiction where the Dockerfile sets
`ENV QUANT_FOUNDRY_CALLBACK_SECRET=""` (guaranteed to fail) while the README
documents a dev default that doesn't exist.

**Context:**
The handler's `_get_callback_secret()` fails closed on empty/missing secret.
A default that is guaranteed to fail is worse than no default. Either align
the Dockerfile with the README's dev placeholder, or remove the default and
update the README to say "Required; no default."

**Files allowed:**
- `runpod/quant-foundry-training/Dockerfile`
- `runpod/quant-foundry-training/README.md`

**Files forbidden:**
- `handler.py` (do not change the fail-closed logic)
- `equities.py`, `news.py`
- inference worker files, UI, app, product files

**Commands to run:**
```powershell
Select-String -Path runpod/quant-foundry-training/Dockerfile -Pattern "QUANT_FOUNDRY_CALLBACK_SECRET"
Select-String -Path runpod/quant-foundry-training/README.md -Pattern "callback.secret|CALLBACK_SECRET"
```

**Acceptance criteria:**
- Dockerfile and README agree: either both show the dev placeholder, or both
  say "Required; no default; set via RunPod template env."
- No production secret is committed.
- A startup-time warning is documented (implementation in handler is C2-adjacent,
  not required here).

**Rollback plan:**
```powershell
git restore runpod/quant-foundry-training/Dockerfile runpod/quant-foundry-training/README.md
```

**Evidence required:**
- `git diff` of both files.
- Confirmation no secret value is committed.

---

## C4 — Sync README with current Dockerfile

**Status:** safe beginner
**Objective:** Update the training README so it describes the actual image
(base image, no HEALTHCHECK, direct Python entrypoint) instead of the old
`pytorch/pytorch` + HEALTHCHECK + gosu description.

**Context:**
The README describes the old image. Operators following it will create broken
endpoints or expect behavior that no longer exists. This is doc-only. Note:
C2 and C3 also touch the README; coordinate so the three tasks don't conflict
(C4 should land after C2/C3, or be merged into them).

**Files allowed:**
- `runpod/quant-foundry-training/README.md`

**Files forbidden:**
- `Dockerfile`, `handler.py`, all source code
- Other READMEs

**Commands to run:**
```powershell
Select-String -Path runpod/quant-foundry-training/Dockerfile -Pattern "FROM|ENTRYPOINT|HEALTHCHECK" -Context 0
git diff -- runpod/quant-foundry-training/README.md
```

**Acceptance criteria:**
- README "Image", "Build", "RunPod deployment configuration", and "GPU
  requirements" sections match the current Dockerfile.
- No references to a Docker `HEALTHCHECK` (removed + regression-guarded).
- Sample RunPod template JSON includes `RUNPOD_INIT_TIMEOUT=900` (cross-ref C1).
- Base-image rationale links to `runpod/RUNPOD_UNHEALTHY_ROOT_CAUSE.md`.

**Rollback plan:**
```powershell
git restore runpod/quant-foundry-training/README.md
```

**Evidence required:**
- `git diff` of the README.
- One-line confirmation each section matches the current Dockerfile.

**Coordinate with:** C2, C3, C10 (all touch the README).

---

## C5 — Move/delete dead diagnostic handler files

**Status:** focused bugfix
**Objective:** Remove or relocate the dead diagnostic handler files so they
cannot be accidentally wired as the production handler.

**Context:**
`handler_layered.py` (recursive import fallback → `RecursionError` if
re-copied as `handler.py`), `handler_diagnostic.py`, `handler_minimal.py`,
and `Dockerfile.minimal` are dead code. `runpod/quant-foundry-cuda-test/
Dockerfile.nvidia` is redundant (same base as the main Dockerfile).

**Files allowed:**
- `runpod/quant-foundry-training/handler_layered.py`
- `runpod/quant-foundry-training/handler_diagnostic.py`
- `runpod/quant-foundry-training/handler_minimal.py`
- `runpod/quant-foundry-training/Dockerfile.minimal`
- `runpod/quant-foundry-cuda-test/Dockerfile.nvidia`
- `runpod/quant-foundry-training/diagnostics/` (new, if moving instead of deleting)

**Files forbidden:**
- `handler.py`, `handler_import_bisect.py` (keep — the latter is the
  diagnostic referenced by `RECEIPT_INDEX.md` Test F; do not delete)
- `run_live_canary.py`, `run_gpu_healthcheck.py` (keep — reusable live tools)
- `Dockerfile` (production-shaped; do not change)
- inference worker files

**Commands to run:**
```powershell
git status --short
Select-String -Path .github/workflows/*.yml -Pattern "handler_layered|handler_diagnostic|handler_minimal|Dockerfile.minimal|Dockerfile.nvidia"
```

**Acceptance criteria:**
- Dead files are either moved to `diagnostics/` or deleted (operator preference).
- If `handler_layered.py` is kept, its recursive fallback is guarded against
  re-loading `handler.py` as `handler_full`.
- No workflow or Dockerfile references the moved/deleted paths.
- `git status` shows only intended changes.

**Rollback plan:**
```powershell
git restore <moved files>
# deleted files recoverable from git history if they were tracked
```

**Evidence required:**
- `git status --short` + `git diff --stat` of the move/delete.
- Confirmation no workflow/Dockerfile references the old paths.

---

## C6 — Create dedicated training endpoint creation script

**Status:** focused bugfix
**Objective:** Add `scripts/runpod_create_training_endpoint.py` with
training-specific defaults so training endpoints are created consistently
rather than via ad-hoc scripts.

**Context:**
Only `scripts/runpod_create_smoke_endpoint.py` exists. Training-specific env
vars (`QUANT_FOUNDRY_CALLBACK_SECRET`, `QUANT_FOUNDRY_USE_REAL_TRAINER`,
`RUNPOD_INIT_TIMEOUT`) are easy to forget. This task may partially overlap
with C1; if C1 adds a `--training` flag to the smoke script instead, this
task becomes "document the training flag" and is downgraded to safe beginner.

**Files allowed:**
- `scripts/runpod_create_training_endpoint.py` (new)

**Files forbidden:**
- `scripts/runpod_create_smoke_endpoint.py` (C1 owns it; do not duplicate)
- `Dockerfile`, `handler.py`, source code
- `.github/workflows/**`

**Commands to run:**
```powershell
uv run ruff check scripts/runpod_create_training_endpoint.py
uv run python -c "import ast; ast.parse(open('scripts/runpod_create_training_endpoint.py').read())"
```

**Acceptance criteria:**
- Script defaults: `container_disk_gb=40`, `gpu_ids=ADA_24`, `idle_timeout=300`,
  `scaler_type=QUEUE_DELAY`, `scaler_value=4`, `RUNPOD_INIT_TIMEOUT=900`.
- Requires `QUANT_FOUNDRY_CALLBACK_SECRET` via `--callback-secret` or env.
- `--production` flag sets `QUANT_FOUNDRY_TRAINING_MODE=production`.
- Ruff passes.

**Rollback plan:**
```powershell
git rm scripts/runpod_create_training_endpoint.py
```

**Evidence required:**
- New file content.
- `ruff check` output (clean).

**Coordinate with:** C1 (avoid duplicating the init-timeout logic).

---

## C7 — Pin cuda-test workflow actions to SHA

**Status:** safe beginner
**Objective:** Eliminate the supply-chain inconsistency where
`build-runpod-training.yml` pins actions by SHA but
`build-runpod-cuda-test.yml` uses mutable `@v3`/`@v4` tags.

**Context:**
`build-runpod-cuda-test.yml` uses `actions/checkout@v4`,
`docker/setup-buildx-action@v3`, `docker/login-action@v3`,
`docker/build-push-action@v6`. The training workflow already pins by SHA.
Mixing pinning styles makes the repo harder to audit and exposes the
cuda-test workflow to tag-retag attacks.

**Files allowed:**
- `.github/workflows/build-runpod-cuda-test.yml`

**Files forbidden:**
- `.github/workflows/build-runpod-training.yml` (already pinned; reference only)
- All source code, Dockerfiles, other workflows.

**Commands to run:**
```powershell
Select-String -Path .github/workflows/build-runpod-training.yml -Pattern "actions/|docker/" | Select-Object -First 10
Select-String -Path .github/workflows/build-runpod-cuda-test.yml -Pattern "actions/|docker/"
git diff -- .github/workflows/build-runpod-cuda-test.yml
```

**Acceptance criteria:**
- cuda-test workflow actions pinned to the same SHA versions used in the
  training workflow (or newer SHAs the operator approves).
- No `@v3`/`@v4` mutable tags remain in the cuda-test workflow.
- `git diff` shows only pin changes.

**Rollback plan:**
```powershell
git restore .github/workflows/build-runpod-cuda-test.yml
```

**Evidence required:**
- `git diff` of the workflow.
- Confirmation no mutable tags remain.

---

## C8 — Triage pre-existing CI lint debt on a separate branch

**Status:** needs senior agent
**Objective:** Reduce the 1334 Ruff errors blocking the `ci` workflow, on a
**separate branch** so the churn does not mix with the RunPod fix.

**Context:**
`ci` workflow fails on `uv run ruff check libs services` with 1334 errors
(613 auto-fixable). Identical count on `main` since 2026-06-28 — pre-existing
debt, NOT a regression from this branch. Confirmed unchanged across all five
CI triage receipts (20:05, 21:30, 22:40 UTC on 2026-07-03, and 04:02 + 06:30
UTC on 2026-07-04). Concentrated in `services/quant_foundry/`. Rule families: S112,
I001, B905, UP037, RUF046, S301, F841, B017, UP035, B904, SIM108. This is
large-scope and risks behavioral changes from `--unsafe-fixes`; needs
judgment and review. Do NOT commit alongside the RunPod fix.

**Files allowed:**
- `libs/**`, `services/**` (on a NEW branch off `main`, not this branch)

**Files forbidden:**
- `runpod/quant-foundry-training/**` (RunPod fix lane)
- `infra/docker/api.Dockerfile` (C9)
- `.github/workflows/**` (unless adjusting the ruff config)

**Commands to run (on the new branch):**
```powershell
git checkout main
git checkout -b fix/ruff-lint-debt
uv run ruff check --fix libs services
uv run ruff check libs services
uv run pytest -q
```

**Acceptance criteria:**
- New branch off `main` (not this branch).
- Auto-fixes applied; remaining errors triaged and fixed or `noqa`-annotated
  with justification.
- `uv run ruff check libs services` passes.
- `uv run pytest -q` passes (no behavioral regressions from the fixes).
- PR opened against `main`, not against this branch.

**Rollback plan:**
```powershell
git checkout fix/test-harness-optional-deps-guards
git branch -D fix/ruff-lint-debt  # if the branch is unwanted
```

**Evidence required:**
- `ruff check` output (clean) on the new branch.
- `pytest -q` output (pass).
- PR link.

---

## C9 — Commit the `api.Dockerfile` `COPY experiments` fix

**Status:** focused bugfix
**Objective:** Commit the already-made fix to `infra/docker/api.Dockerfile`
that resolves the F2 `build-images` / `build (api)` failure, on its own
commit separate from the RunPod fix.

**Context:**
`git status` shows `infra/docker/api.Dockerfile` modified: adds
`COPY experiments experiments`. The F2 failure (`uv sync --frozen` cannot
resolve the `experiments/news-impact-model` workspace path dep because the
dir is absent from the build context) is fixed by this line. Re-verified as
correct by CI triage receipt #5 (06:30 UTC). This is unrelated to the RunPod
training-worker fix and must ship as its own commit so the RunPod fix review
stays clean.

**Files allowed:**
- `infra/docker/api.Dockerfile`

**Files forbidden:**
- Everything else. Do not `git add -A`. Stage only this one file.
- Do not bundle with any RunPod-fix commit.

**Commands to run:**
```powershell
git diff -- infra/docker/api.Dockerfile
git add infra/docker/api.Dockerfile
git commit -m "fix(api): copy experiments dir into Docker build context for news-impact-model workspace dep"
git status --short
# verify the build-images workflow passes (optional, after push):
gh workflow run build-images.yml --ref fix/test-harness-optional-deps-guards
```

**Acceptance criteria:**
- Commit contains exactly one file.
- Commit message states the fix rationale (workspace path dep resolution).
- `git status` no longer lists `api.Dockerfile` as modified.
- (Optional) `build-images` / `build (api)` job passes after the commit.

**Rollback plan:**
```powershell
git revert HEAD
```

**Evidence required:**
- `git show --stat HEAD` output.
- `git status --short` showing the file is no longer dirty.

**Coordinate with:** B1 (B1 triages the file's disposition; C9 implements
the "commit it" outcome if the operator approves).

---

## C10 — Document RunPod job timeout in the template/README

**Status:** safe beginner
**Objective:** Document that the RunPod endpoint template must set a job
timeout ≥ `QUANT_FOUNDRY_TRAINING_DEADLINE_SECONDS + 60` so the platform
does not kill real training jobs before the handler's deadline logic runs.

**Context:**
The handler enforces `QUANT_FOUNDRY_TRAINING_DEADLINE_SECONDS=1800`, but
RunPod's default serverless job timeout is 600s. A real training job could
be killed by the platform with a `TIMED_OUT` before the handler's signed
failure envelope runs. This is directly relevant to A7.

**Files allowed:**
- `runpod/quant-foundry-training/README.md`

**Files forbidden:**
- `Dockerfile`, `handler.py`, source code
- Other READMEs

**Commands to run:**
```powershell
Select-String -Path runpod/quant-foundry-training/README.md -Pattern "timeout|DEADLINE|RUNPOD_TIMEOUT"
git diff -- runpod/quant-foundry-training/README.md
```

**Acceptance criteria:**
- README documents that the template should set `RUNPOD_TIMEOUT` (or
  per-job timeout) to ≥ 1860s.
- Dispatcher must pass the same deadline in the request.
- No code changes.

**Rollback plan:**
```powershell
git restore runpod/quant-foundry-training/README.md
```

**Evidence required:**
- `git diff` of the README.

**Coordinate with:** C4 (both touch the README; merge or sequence).

---

# Lane D — CI / Security Debt (from `reports/ci-triage/`)

Source: `reports/ci-triage/receipt-20260703T200535Z.md`,
`receipt-20260703T213000Z.md`, `receipt-20260703T224000Z.md` (committed in
`748eef6c`), and `receipt-20260704T040200Z.md` + `receipt-20260704T063000Z.md`
(uncommitted → D7). These are pre-existing failures on `main`, independent of
the RunPod fix branch, but the security item has real-world urgency. None
block the RunPod fix path. All five receipts confirm F1–F4 unchanged.

## D1 — Locate and remove the leaked Stripe secret (CRITICAL, security)

**Status:** needs senior agent
**Objective:** Find and remove the leaked Stripe secret token flagged by the
nightly Trivy scan, then rotate the key. This is the only CI-debt item with
real-world urgency.

**Context:**
The `nightly` workflow's Trivy filesystem scan reports
`Total: 1 (CRITICAL: 1)`: a **Stripe secret-token** leaked in the repo. This
is a CRITICAL secret exposure, not just a CVE. The exact location is TBD —
the senior agent must run `trivy fs --scanners secret --severity CRITICAL .`
locally to pinpoint it. After removal, the Stripe key MUST be rotated
(operator action — do not automate the rotation).

**Files allowed:**
- The file(s) containing the leaked secret (TBD by the scan).
- `.gitignore` if needed to prevent re-add (coordinate with B2).

**Files forbidden:**
- Do NOT commit the secret value in any new file, receipt, or commit message.
- `runpod/quant-foundry-training/**` (RunPod fix lane)
- `services/quant_foundry/**` (C8 owns lint there)

**Commands to run:**
```powershell
trivy fs --scanners secret --severity CRITICAL .
# then, after identifying the file:
git diff -- <identified-file>
```

**Acceptance criteria:**
- The leaked Stripe secret token is removed from the repo.
- A follow-up rotation of the Stripe key is requested from the operator
  (recorded as a note; do not perform the rotation yourself).
- Re-running `trivy fs --scanners secret --severity CRITICAL .` shows 0
  CRITICAL secrets.
- No secret value appears in the commit diff or message.

**Rollback plan:**
- Do NOT `git revert` a secret-removal commit — that would re-add the secret.
  If the removal broke a legitimate reference, fix forward with the rotated key.

**Evidence required:**
- `trivy fs --scanners secret --severity CRITICAL .` output before and after.
- `git diff` of the removal (with the secret value redacted in the receipt).
- Operator confirmation that key rotation is scheduled/done.

**Blocked by:** Operator awareness (security incident). Do not automate the
rotation. The removal itself can proceed once the location is confirmed.

---

## D2 — Bump `next` to clear the 5 HIGH CVEs

**Status:** focused bugfix
**Objective:** Clear the 5 HIGH Next.js CVEs (CVE-2026-44573 info disclosure,
CVE-2026-44578 SSRF, 3 GHSA DoS) flagged by the nightly Trivy scan.

**Context:**
`apps/dashboard` pins `next` 14.2.35. Trivy reports 5 HIGH CVEs against it.
Bump to >=15.5.16 (or 16.2.5) per the triage receipt. This is a frontend
dependency bump — verify the dashboard still builds.

**Files allowed:**
- `apps/dashboard/package.json`
- `apps/dashboard/package-lock.json` (or `pnpm-lock.yaml` / `yarn.lock` — use whichever the repo uses)

**Files forbidden:**
- `runpod/quant-foundry-training/**`
- `services/**`, `libs/**`
- `.github/workflows/**`

**Commands to run:**
```powershell
Select-String -Path apps/dashboard/package.json -Pattern '"next"'
# then bump and install per the repo's package manager
# e.g. npm install next@>=15.5.16 --save
# then:
npm run build  # or the repo's build command for apps/dashboard
```

**Acceptance criteria:**
- `next` is bumped to a version with no known HIGH CVEs (>=15.5.16 or 16.2.5).
- `apps/dashboard` build succeeds.
- Re-running Trivy on the dashboard shows 0 HIGH Next.js CVEs.

**Rollback plan:**
```powershell
git restore apps/dashboard/package.json apps/dashboard/package-lock.json
```

**Evidence required:**
- `git diff` of the package files.
- Build output (success).
- Trivy re-scan output showing the Next.js CVEs cleared.

---

## D3 — Refresh vulnerable Python deps (`uv lock --upgrade` + pip-audit)

**Status:** needs senior agent
**Objective:** Clear the 53 known Python vulnerabilities (15 packages) flagged
by the nightly pip-audit, starting with `certifi 2023.11.17`.

**Context:**
The `nightly` workflow's `uv run pip-audit --ignore-vuln GHSA-4xh5-x5gv-qwph`
reports 53 known vulnerabilities in 15 packages. `uv lock --upgrade` refreshes
vulnerable packages. Some upgrades may be breaking (workspace-wide) — needs
judgment and a full test run. Do NOT introduce floating ranges; pin to vetted
versions.

**Files allowed:**
- `uv.lock`
- `pyproject.toml` (only if a floor pin must move)

**Files forbidden:**
- `runpod/quant-foundry-training/Dockerfile` (RunPod fix lane — coordinate if a pin there must move)
- `apps/dashboard/**` (D2 owns frontend)
- `.github/workflows/**` (unless adjusting the ignore list with justification)

**Commands to run:**
```powershell
uv lock --upgrade
uv run pip-audit --ignore-vuln GHSA-4xh5-x5gv-qwph
uv run pytest -q
```

**Acceptance criteria:**
- `uv lock --upgrade` completes; vulnerable packages refreshed (certifi >= 2024.7.4 and the other 14).
- `pip-audit` count drops to a justified residual (unfixable ones `--ignore-vuln`-annotated with rationale).
- `uv run pytest -q` passes (no behavioral regressions).
- No floating ranges (`latest`, `*`, unbounded `>=`) introduced.

**Rollback plan:**
```powershell
git restore uv.lock pyproject.toml
```

**Evidence required:**
- `git diff --stat` of `uv.lock`.
- `pip-audit` output before and after.
- `pytest -q` output (pass).

---

## D7 — Commit the pass #7 + pass #8 consolidation set (EXPANDED in v8)

**Status:** safe beginner
**Objective:** Commit the uncommitted pass #7 + pass #8 consolidation set so
the investigation's single entry point and receipt history stop lagging behind
the worktree and reflect that D4/D5 are done and A6 is done, once B1 approves
its disposition.

**Context:**
`git status` shows a consolidation set that has grown beyond v7's D7 scope.
v7's D7 covered 7 files; v8 adds CI triage receipt #5 (06:30 UTC, new since
v7) and v8 itself. The current uncommitted set is:
- `docs/runpod-fix-plan/RECEIPT_INDEX.md` (modified) — pass #7 + pass #8
  consolidation (records D4/D5 done, A6 done, "Newest commit reviewed"
  `6e85f44c`).
- `docs/runpod-fix-plan/11-swarm-task-queue-v5.md` (modified) — pass #7 edits
  marking D4/D5 done.
- `docs/runpod-fix-plan/12-swarm-task-queue-v6.md` (untracked) — v6 task
  queue (A6 section now STALE; kept for history; v8 is the new source of
  truth).
- `docs/runpod-fix-plan/13-swarm-task-queue-v7.md` (untracked) — v7 task
  queue (superseded by v8; kept for history).
- `docs/runpod-fix-plan/14-swarm-task-queue-v8.md` (untracked) — v8 task
  queue (this file — the current source of truth).
- `reports/runpod-test-runs/3098f11f/RECEIPT.md` (untracked) — pass #7
  receipt.
- `reports/runpod-test-runs/6e85f44c/RECEIPT.md` (untracked) — pass #8
  receipt (A6 done).
- `reports/ci-triage/receipt-20260704T040200Z.md` (untracked) — CI triage
  receipt #4 (04:02 UTC; UNCHANGED status).
- `reports/ci-triage/receipt-20260704T063000Z.md` (untracked) — CI triage
  receipt #5 (06:30 UTC; UNCHANGED status; NEW since v7).

The **committed** index still reads pass #5 ("Newest commit reviewed
`677c77ed`"); an agent reading only committed state would not know D4/D5
landed OR that A6 is done. This is durable evidence that should be committed,
not discarded. Do NOT re-commit the CI triage receipts #1–#3, the pass #5
index, the v3/v4/v5 task queues, or the `gpu-healthcheck/` receipt bundle
(all already committed in `748eef6c`/`3940271b`/`6e85f44c`).

**Files allowed:**
- `docs/runpod-fix-plan/RECEIPT_INDEX.md`
- `docs/runpod-fix-plan/11-swarm-task-queue-v5.md`
- `docs/runpod-fix-plan/12-swarm-task-queue-v6.md`
- `docs/runpod-fix-plan/13-swarm-task-queue-v7.md`
- `docs/runpod-fix-plan/14-swarm-task-queue-v8.md` (this file — commit alongside so the source of truth is current)
- `reports/runpod-test-runs/3098f11f/RECEIPT.md`
- `reports/runpod-test-runs/6e85f44c/RECEIPT.md`
- `reports/ci-triage/receipt-20260704T040200Z.md`
- `reports/ci-triage/receipt-20260704T063000Z.md`

**Files forbidden:**
- All other files. Do not `git add -A`.
- Do not bundle with any RunPod-fix code commit or with C9's `api.Dockerfile`.
- Do not re-commit `reports/ci-triage/receipt-20260703T*` (already committed).
- Do not re-commit `reports/runpod-test-runs/6dbec436/**` (already committed).
- Do not re-commit `docs/runpod-fix-plan/09-swarm-task-queue-v3.md`,
  `10-swarm-task-queue-v4.md` (already committed).

**Commands to run:**
```powershell
git status --short
git diff --stat docs/runpod-fix-plan/RECEIPT_INDEX.md docs/runpod-fix-plan/11-swarm-task-queue-v5.md
# verify no secrets in the consolidation set:
Select-String -Path docs/runpod-fix-plan/RECEIPT_INDEX.md,docs/runpod-fix-plan/11-swarm-task-queue-v5.md,docs/runpod-fix-plan/12-swarm-task-queue-v6.md,docs/runpod-fix-plan/13-swarm-task-queue-v7.md,docs/runpod-fix-plan/14-swarm-task-queue-v8.md,reports/runpod-test-runs/3098f11f/RECEIPT.md,reports/runpod-test-runs/6e85f44c/RECEIPT.md,reports/ci-triage/receipt-20260704T040200Z.md,reports/ci-triage/receipt-20260704T063000Z.md -Pattern "RUNPOD_API_KEY|QUANT_FOUNDRY_CALLBACK_SECRET|sk_live|rk_live"
git add docs/runpod-fix-plan/RECEIPT_INDEX.md docs/runpod-fix-plan/11-swarm-task-queue-v5.md docs/runpod-fix-plan/12-swarm-task-queue-v6.md docs/runpod-fix-plan/13-swarm-task-queue-v7.md docs/runpod-fix-plan/14-swarm-task-queue-v8.md reports/runpod-test-runs/3098f11f/RECEIPT.md reports/runpod-test-runs/6e85f44c/RECEIPT.md reports/ci-triage/receipt-20260704T040200Z.md reports/ci-triage/receipt-20260704T063000Z.md
git commit -m "evidence(runpod): commit pass #7+#8 consolidation (D4/D5/A6 done) + v6/v7/v8 task queues + receipts"
git status --short
```

**Acceptance criteria:**
- Commit contains exactly the nine consolidation files (no more, no less).
- Secret-scan of the staged files returns no hits.
- `git status` no longer lists any of the nine files as modified/untracked.
- The committed index now reads "Newest commit reviewed `6e85f44c`" (pass #8).
- `runpod/tests/test_receipt_integrity.py` still passes (the index edits do
  not contradict raw evidence).

**Rollback plan:**
```powershell
git revert HEAD
```

**Evidence required:**
- `git show --stat HEAD` output.
- Secret-scan output (clean).
- `pytest runpod/tests/test_receipt_integrity.py -q` output (pass).

**Blocked by:** B1's disposition approval for the consolidation set.

---

# Dependency Graph

```
A6 (gpu_healthcheck live) ── DONE (6e85f44c) ── do not re-run
A7 (train_model live)      ──> A8 (consolidate A7 receipts)
                               (A6 portion already consolidated in pass #8; D7 commits it)

B1 (classify dirty work) ─┬─> C9 (commit api.Dockerfile fix, if operator approves)
                          └─> D7 (commit pass #7+#8 consolidation set, if operator approves)
B2 (gitignore .tmp_*.json) # independent
B3 (AGENTS.md)            # independent

C1 (RUNPOD_INIT_TIMEOUT)  # independent of A; coordinate with C6
C2 (Dockerfile dead code) # needs operator decision; live canary after
C3 (callback secret default) # coordinate with C4
C4 (README sync)          # coordinate with C2, C3, C10
C5 (dead diagnostic files) # independent
C6 (training endpoint script) # coordinate with C1
C7 (cuda-test SHA pins)   # independent
C8 (ruff lint debt)       # separate branch off main; independent
C9 (api.Dockerfile fix)   # blocked by B1 disposition
C10 (job timeout doc)     # coordinate with C4

D1 (Stripe secret removal) # security-urgent; needs operator awareness for rotation
D2 (next CVE bump)        # independent
D3 (pip-audit refresh)    # independent; needs judgment
D7 (pass #7+#8 consolidation set) # blocked by B1 disposition
```

---

# Blocked Tasks

- **A7** — blocked on operator awareness of live GPU spend (do not automate
  the spend decision). NOT blocked by A6 (A6 is DONE). Not blocked by any
  code task.
- **A8** — blocked by A7 (must run first) + D7 (so the index baseline is
  committed first).
- **B1** — blocked on operator decision (do not automate).
- **C2** — blocked on operator entrypoint-strategy decision (do not automate
  the choice; implementation is senior agent).
- **C9** — blocked by B1's disposition of `api.Dockerfile` (operator must
  approve committing it).
- **D1** — blocked on operator awareness (security incident); the removal can
  proceed once the location is confirmed, but key rotation is operator-only.
- **D7** — blocked by B1's disposition of the pass #7 + pass #8
  consolidation set.
- **C8** — not blocked, but must run on a separate branch off `main`, not
  this branch.

---

# Recommended Next Assignment

1. **D1** (needs senior agent, security-urgent) — locate and remove the
   leaked Stripe secret token, then ask the operator to rotate the key. This
   is the only item with real-world urgency. Highest priority despite being
   senior-agent work. Not blocked by any code task — can start immediately
   once the operator is aware of the security incident.
2. **A7** (needs senior agent, ~15–30 min live) — dispatch the minimal
   `train_model` job against `6dbec436` to verify the full training pipeline
   (dataset loading + trainer execution + model export). A6 is DONE (GPU
   accessible, RTX 4090), so A7 is now the single critical live unknown on
   the critical path. → A8 (consolidate) if it passes. Ensure the endpoint
   template timeout ≥ 1860s (see C10).
3. **D7** (safe beginner, ~10 min) — commit the 9-file pass #7 + pass #8
   consolidation set (index, v5/v6/v7/v8 task queues, two receipts, CI triage
   #4 + #5) so the investigation's committed entry point stops lagging behind
   the worktree and reflects that A6 is done. Blocked only by B1's
   disposition approval — the highest-leverage unblock available to the
   operator right now.

Run B3 (and B2, C7, C10 — all safe-beginner/focused, independent, no live
cloud) in parallel as a swarm of small agents. Hand D1 and A7 to senior
agents with RunPod/security access. Lane C focused-bugfix tasks (C1, C3, C5,
C6, C9) can be dispatched in parallel once B1 lands the dispositions they
depend on. C8 and D3 run on separate branches off `main`. D7 should land
early once B1 approves, so the investigation's committed entry point stops
lagging behind the worktree and reflects that A6 is done.
