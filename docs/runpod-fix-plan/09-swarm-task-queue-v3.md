# Fincept Swarm Task Queue v3

Last generated: 2026-07-03 (post live-canary PASS)
Branch: `fix/test-harness-optional-deps-guards`
HEAD: `677c77ed` (evidence(runpod): live production canary 3/3 PASSED + run_live_canary.py tool)
Source of truth for prior state: `docs/runpod-fix-plan/RECEIPT_INDEX.md` (pass #4)

This queue **supersedes** `docs/runpod-fix-plan/08-swarm-task-queue-v2.md`. v2
was written before the live canary ran; its Lane A (A1–A5) is now DONE or
OBSOLETE. v3 reflects the post-`677c77ed` state: the `parents[5]` fix is
committed, the image is published, the **production canary PASSED 3/3 live**,
and the remaining work is **live training-pipeline validation + repo hygiene +
config/deployment hygiene + CI/security debt**.

Read `RECEIPT_INDEX.md` first for what is already proven. Do NOT re-run
experiments listed in its "What Should NOT Be Retried" table.

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
**The live production-handler canary PASSED 3/3** (commits `a4cacc64` +
`677c77ed`; receipt at `reports/runpod-test-runs/6dbec436/`). The canary path
exercises preflight + callback signing but NOT actual model training or GPU
access — so the single critical open live step is a `gpu_healthcheck` then a
minimal `train_model` job.

---

## v2 Queue Status Rollup (A1–A5, B1–B3, C1–C10)

| v2 Task | v3 Status | Note |
|---------|-----------|------|
| A1 commit pass #3 docs | **DONE** (`677c77ed`) | doc updates committed |
| A2 live production canary | **DONE** (`a4cacc64`/`677c77ed`) | 3/3 COMPLETED, worker healthy |
| A3 repeat canary for stability | **DONE** | 3/3 (was the same run as A2) |
| A4 consolidate receipt + index | **DONE** (`677c77ed`) | `RECEIPT_INDEX.md` pass #4 + interpretation.md |
| A5 failure isolation | **OBSOLETE** | canary PASSED, no failure to isolate |
| B1 classify dirty work | **OPEN** → B1 | worktree still dirty (see below) |
| B2 gitignore scratch | **OPEN** → B2 | still valuable |
| B3 AGENTS.md | **OPEN** → B3 | still valuable |
| C1 RUNPOD_INIT_TIMEOUT | **OPEN** → C1 | unchanged |
| C2 Dockerfile dead code | **OPEN** → C2 | needs operator decision |
| C3 callback secret default | **OPEN** → C3 | unchanged |
| C4 README sync | **OPEN** → C4 | unchanged |
| C5 dead diagnostic files | **OPEN** → C5 | unchanged |
| C6 training endpoint script | **OPEN** → C6 | unchanged |
| C7 cuda-test SHA pins | **OPEN** → C7 | unchanged |
| C8 ruff lint debt | **OPEN** → C8 | unchanged |
| C9 api.Dockerfile COPY experiments | **OPEN** → C9 | unchanged |
| C10 job timeout doc | **OPEN** → C10 | unchanged |

### Stale instruction in RECEIPT_INDEX.md (note, not a task)

`RECEIPT_INDEX.md` "Next Agent Instruction" item 1 says to commit the
`6dbec436` receipt bundle, `run_live_canary.py`, and the pass #4 index. **That
is already done** in commits `a4cacc64` and `677c77ed`. `git status` confirms
only `infra/docker/api.Dockerfile` is modified and the 4 untracked items
remain. Do NOT re-commit the receipts. (A doc-only correction of that stale
instruction is folded into D1 below.)

---

## Task Status Legend

- `safe beginner` — read-only or doc-only, no live cloud, no secrets, no production code.
- `focused bugfix` — narrow code/config change with a clear root cause; no live cloud.
- `needs senior agent` — touches live RunPod (secrets, spend, endpoints), production handler logic, security boundaries, or large-scope refactors requiring judgment.
- `do not automate` — requires explicit operator decision; do not run without human approval.

---

## Active Automations (do not duplicate)

- **`build-runpod-training` workflow** — green for `677c77ed`/`6dbec436`. Do
  not re-trigger the build unless a new commit lands on this branch.
- **Receipt-integrity guard** (`runpod/tests/test_receipt_integrity.py`) and
  **no-healthcheck guard** (`runpod/tests/test_dockerfile_no_healthcheck.py`)
  are in place — do not re-add them.
- **Import bisection** (`run_import_bisection.py` / `handler_import_bisect.py`)
  is complete (all 12 profiles ran). Do not spawn a parallel bisection.
- **`run_live_canary.py`** reusable canary tool now exists
  (`runpod/quant-foundry-training/run_live_canary.py`). Use it for live canary
  work; do not write a competing ad-hoc script.
- **Hourly CI triage** is producing receipts under `reports/ci-triage/`. Do
  not start a competing triage; D-lane tasks consume those receipts.

---

# Lane A — Live Validation (critical path)

The canary path is proven. The remaining live unknown is the **full training
pipeline** (GPU access + dataset loading + trainer execution + model export).

## A6 — Live `gpu_healthcheck` job against `6dbec436`

**Status:** needs senior agent
**Objective:** Verify the GPU is accessible inside the production container by
dispatching a `gpu_healthcheck` job (mode=canary) against the exact-SHA
production image.

**Context:**
The canary path exercises preflight + callback signing but does NOT touch the
GPU. A `gpu_healthcheck` job confirms CUDA/driver/runtime visibility inside the
container before spending on a full `train_model` run. Reuse endpoint
`4jc1opwj11zmai` (scale back up to `workersMin=1`) or create a fresh one. **Use
the FULL 40-char SHA image tag** — the workflow tags with `github.sha`, not a
short SHA; a short-SHA tag produces a non-existent image and the container
exits immediately with `docker=None, unhealthy=1` (proven by the broken
`jtr18cdh5lgov2` endpoint in the `6dbec436` receipt).

This spends RunPod GPU time and uses secrets (`RUNPOD_API_KEY`,
`QUANT_FOUNDRY_CALLBACK_SECRET`, registry auth id). Do NOT run without
operator awareness of spend.

**Files allowed:**
- `reports/runpod-test-runs/6dbec436/gpu-healthcheck/` (new receipt bundle)

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
# Reuse 4jc1opwj11zmai scaled back up, or create a fresh endpoint via
# runpod/quant-foundry-training/run_live_canary.py or scripts/runpod_create_smoke_endpoint.py
$endpoint = "4jc1opwj11zmai"  # or fresh id
$payload = '{"input":{"task":"gpu_healthcheck","mode":"canary","job_id":"qf:gpu-hc:6dbec436:001"}}'
uv run python scripts/runpod_smoke_probe.py --endpoint-id $endpoint --image-tag $image --interval 5 --timeout 240 --payload-json $payload
```
Then capture `/health` after completion, scale the endpoint down, and record
cleanup.

**Acceptance criteria:**
- Job reaches `COMPLETED` with a GPU-visible result (e.g. CUDA device count,
  driver version, or equivalent) in the output.
- Job does not stay `IN_QUEUE`.
- Worker remains `unhealthy=0` after completion.
- No secrets printed in the receipt.
- Endpoint scaled down or deleted after the test.
- Receipt bundle written under `reports/runpod-test-runs/6dbec436/gpu-healthcheck/`.

**Rollback plan:**
- No code rollback (probe-only on an already-built image).
- Scale down the endpoint even on early exit.
- If spend or rate limits are hit, stop and report; do not retry blindly.

**Evidence required:**
- `reports/runpod-test-runs/6dbec436/gpu-healthcheck/` receipt bundle
  (endpoint id, redacted settings, health before, `/run` response, status
  probe JSONL, final status JSON, health after, cleanup, short interpretation).
- One-line interpretation: PASS or FAIL with the GPU result.

**Blocked by:** Operator awareness of spend (do not automate the spend
decision). Not blocked by any code task.

---

## A7 — Live minimal `train_model` job against `6dbec436` (conditional)

**Status:** needs senior agent
**Objective:** Verify the full training pipeline (dataset loading, trainer
execution, model export) works live by dispatching a minimal `train_model` job.

**Context:**
Only runs if A6 PASSES (GPU is accessible). Use a minimal dataset so the job
completes within the handler's `QUANT_FOUNDRY_TRAINING_DEADLINE_SECONDS=1800`
and RunPod's job timeout. Same endpoint/image/redaction discipline as A6. The
RunPod default serverless job timeout is 600s — ensure the endpoint template
sets a timeout ≥ 1860s, or the platform will kill the job with `TIMED_OUT`
before the handler's signed failure envelope runs (see C10).

**Files allowed:**
- `reports/runpod-test-runs/6dbec436/train-model/` (new receipt bundle)

**Files forbidden:**
- Same as A6.

**Commands to run:**
```powershell
# After A6 PASS; reuse the same endpoint scaled back up.
# Payload shape: minimal train_model task with a tiny dataset manifest.
# Exact payload TBD by the senior agent from handler.py's train_model branch.
uv run python scripts/runpod_smoke_probe.py --endpoint-id $endpoint --image-tag $image --interval 5 --timeout 1900 --payload-json $train_payload
```

**Acceptance criteria:**
- Job reaches `COMPLETED` with a model artifact reference (or a signed
  failure envelope if the handler fail-closed intentionally — record which).
- Worker remains `unhealthy=0` after completion.
- No secrets printed.
- Endpoint scaled down or deleted after the test.
- Receipt bundle written under `reports/runpod-test-runs/6dbec436/train-model/`.

**Rollback plan:**
- Scale down the endpoint on early exit. No code rollback.

**Evidence required:**
- `reports/runpod-test-runs/6dbec436/train-model/` receipt bundle.
- One-line interpretation: PASS or FAIL with the terminal status and whether
  an artifact was produced.

**Blocked by:** A6 (must PASS first) + operator awareness of spend.

---

## A8 — Consolidate A6/A7 receipts + update RECEIPT_INDEX (conditional)

**Status:** safe beginner
**Objective:** After A6 (and A7 if run), write the consolidated interpretation
and update `RECEIPT_INDEX.md` so the next agent reads a single current index
reflecting the live training-pipeline result.

**Context:**
Same shape as the (now-done) v2 A4. `RECEIPT_INDEX.md` currently lists the
canary as PASS but the training-pipeline row as NOT YET TESTED. This task
flips it based strictly on the raw A6/A7 evidence. Also corrects the stale
"Next Agent Instruction" item 1 (receipts already committed — see note above).

**Files allowed:**
- `docs/runpod-fix-plan/RECEIPT_INDEX.md`
- `reports/runpod-test-runs/6dbec436/gpu-healthcheck/interpretation.md` (new)
- `reports/runpod-test-runs/6dbec436/train-model/interpretation.md` (new, if A7 ran)

**Files forbidden:**
- Raw probe/health/cleanup JSON files (immutable evidence — do not edit).
- All source code, Dockerfiles, workflows.
- Other receipt dirs.

**Commands to run:**
```powershell
git status --short
Get-Content reports/runpod-test-runs/6dbec436/gpu-healthcheck/status-final-*.json
Get-Content reports/runpod-test-runs/6dbec436/gpu-healthcheck/probe-*.jsonl
uv run pytest runpod/tests/test_receipt_integrity.py -q
```

**Acceptance criteria:**
- `RECEIPT_INDEX.md` Evidence Map rows for the gpu-healthcheck / train-model
  results are updated with the endpoint id and PASS/FAIL.
- The stale "commit the receipts" instruction (item 1) is removed or marked
  done, since `a4cacc64`/`677c77ed` already committed them.
- No claims contradict the raw probe evidence.
- `runpod/tests/test_receipt_integrity.py` still passes.

**Rollback plan:**
```powershell
git restore docs/runpod-fix-plan/RECEIPT_INDEX.md reports/runpod-test-runs/6dbec436/gpu-healthcheck/interpretation.md reports/runpod-test-runs/6dbec436/train-model/interpretation.md
```

**Evidence required:**
- `git diff` of the index.
- `pytest runpod/tests/test_receipt_integrity.py -q` output (pass).

**Blocked by:** A6 (and A7 if run).

---

# Lane B — Repo Hygiene

## B1 — Classify dirty worktree items

**Status:** do not automate
**Objective:** With the operator, decide the disposition of every untracked
and unrelated-modified item in the worktree so the final commit set is
reviewable and safe.

**Context:**
`git status` (post-`677c77ed`) shows:
- `infra/docker/api.Dockerfile` — modified, adds `COPY experiments experiments`.
  This is a fix for the F2 `build-images` / `build (api)` failure (the
  `experiments/news-impact-model` workspace path dep can't resolve without the
  dir in build context). **Likely a real fix — see C9.** Keep separate from
  the RunPod fix.
- `SESSION_HANDOFF.md` — untracked, STALE. Predates the `parents[5]` fix;
  describes the superseded bisect-`handler()` approach and old commit
  `d15482ff`. Misleading if read as current.
- `handoffs/2026-07-03_01-51_fix-runpod-training-crash/` — untracked, STALE.
  Same superseded narrative as `SESSION_HANDOFF.md`.
- `kimiSuggestionFix.md` — untracked. 15-item config/deployment hygiene
  audit. Source material for Lane C tasks. Keep as a reference doc or move
  into `docs/`.
- `reports/ci-triage/` — untracked. Two CI triage receipts (20:05 + 21:30 UTC)
  documenting pre-existing CI/security debt. Durable evidence — candidate for
  committing (see D1).

Do NOT delete or commit anything without explicit operator approval per item.

**Files allowed:**
- Only after operator approval: `SESSION_HANDOFF.md`, `handoffs/`,
  `kimiSuggestionFix.md`, `reports/ci-triage/`.

**Files forbidden:**
- `infra/docker/api.Dockerfile` (handled by C9, not here).
- All tracked source code, Dockerfiles, workflows.
- Any file under `reports/runpod-test-runs/` (immutable evidence).

**Commands to run (triage only, no deletion/commit yet):**
```powershell
git status --short
Get-Content SESSION_HANDOFF.md -TotalCount 5
Get-Content kimiSuggestionFix.md -TotalCount 5
Get-Content reports/ci-triage/receipt-20260703T213000Z.md -TotalCount 5
ls handoffs/
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

## B2 — Add scratch patterns to `.gitignore`

**Status:** focused bugfix
**Objective:** Prevent future `.tmp_*` scratch files and similar ad-hoc
diagnostic artifacts from cluttering `git status` or being committed by
accident.

**Context:**
The `.tmp_*.py` files that prompted v1 T9 are no longer in the worktree, but
no `.gitignore` rule prevents them from returning. The `6dbec436` live run
also produced `.tmp_canary_payload{,2,3}.json` scratch files (per the stale
RECEIPT_INDEX instruction). A narrow rule keeps future diagnostic work out of
the tracked set without affecting any committed file.

**Files allowed:**
- `.gitignore`

**Files forbidden:**
- All source code, Dockerfiles, workflows.
- Do not add `SESSION_HANDOFF.md` or `handoffs/` to `.gitignore` here — those
  are intentional handoff artifacts whose disposition is B1's job.

**Commands to run:**
```powershell
git diff -- .gitignore
git check-ignore -v .tmp_check_health.py 2>$null  # may not exist; that's fine
git status --short
```

**Acceptance criteria:**
- `.gitignore` gains rules matching `.tmp_*` and `.tmp_*.json`.
- No existing tracked file becomes ignored (`git status` shows no surprises).
- The rule is narrow — does not blanket-ignore `reports/` or `handoffs/`.

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
The repo has `PROJECT_OVERVIEW.md` but no `AGENTS.md`. Operational knowledge
is scattered across commits, `RUNPOD_UNHEALTHY_ROOT_CAUSE.md`, the README, and
`RECEIPT_INDEX.md`. An `AGENTS.md` consolidates the "do not re-do" rules in
one place agents read first.

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
- `run_live_canary.py` (keep — reusable live canary tool committed in `677c77ed`)
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
debt, NOT a regression from this branch. Concentrated in
`services/quant_foundry/`. Rule families: S112, I001, B905, UP037, RUF046,
S301, F841, B017, UP035. This is large-scope and risks behavioral changes
from `--unsafe-fixes`; needs judgment and review. Do NOT commit alongside
the RunPod fix.

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
dir is absent from the build context) is fixed by this line. This is
unrelated to the RunPod training-worker fix and must ship as its own commit
so the RunPod fix review stays clean.

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

Source: `reports/ci-triage/receipt-20260703T200535Z.md` and
`receipt-20260703T213000Z.md`. These are pre-existing failures on `main`,
independent of the RunPod fix branch, but the security item has real-world
urgency. None block the RunPod fix path.

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

## D4 — Commit the CI triage receipts as durable evidence

**Status:** safe beginner
**Objective:** Commit the two CI triage receipts under `reports/ci-triage/`
so the CI/security debt record is durable and tracked, once B1 approves their
disposition.

**Context:**
`reports/ci-triage/receipt-20260703T200535Z.md` and
`receipt-20260703T213000Z.md` are untracked. They document the pre-existing
CI/security debt (F1–F4) and are the source for Lane D. They contain no
secrets (operator should verify before commit). This is the "commit them"
outcome of B1's disposition for these files.

**Files allowed:**
- `reports/ci-triage/receipt-20260703T200535Z.md`
- `reports/ci-triage/receipt-20260703T213000Z.md`

**Files forbidden:**
- All other files. Do not `git add -A`.
- Do not bundle with any RunPod-fix or code commit.

**Commands to run:**
```powershell
git status --short
# verify no secrets in the receipts:
Select-String -Path reports/ci-triage/*.md -Pattern "RUNPOD_API_KEY|QUANT_FOUNDRY_CALLBACK_SECRET|sk_live|rk_live"
git add reports/ci-triage/receipt-20260703T200535Z.md reports/ci-triage/receipt-20260703T213000Z.md
git commit -m "evidence(ci): commit triage receipts 20260703T200535Z + 20260703T213000Z (pre-existing CI/security debt)"
git status --short
```

**Acceptance criteria:**
- Commit contains exactly the two receipt files.
- Secret-scan of the receipts returns no hits.
- `git status` no longer lists `reports/ci-triage/` as untracked.

**Rollback plan:**
```powershell
git revert HEAD
```

**Evidence required:**
- `git show --stat HEAD` output.
- Secret-scan output (clean).

**Blocked by:** B1's disposition approval for `reports/ci-triage/`.

---

# Dependency Graph

```
A6 (gpu_healthcheck live) ─┬─> A7 (train_model live, conditional) ─> A8 (consolidate A6/A7 receipts)
                           └─> (failure → new task card, do not guess)

B1 (classify dirty work) ─┼─> C9 (commit api.Dockerfile fix, if operator approves)
                          └─> D4 (commit ci-triage receipts, if operator approves)
B2 (gitignore scratch)    # independent
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
D4 (ci-triage receipts)   # blocked by B1 disposition
```

---

# Blocked Tasks

- **A6** — blocked on operator awareness of live GPU spend (do not automate
  the spend decision). Not blocked by any code task.
- **A7** — blocked by A6 (must PASS first) + operator spend awareness.
- **A8** — blocked by A6 (and A7 if run).
- **B1** — blocked on operator decision (do not automate).
- **C2** — blocked on operator entrypoint-strategy decision (do not automate
  the choice; implementation is senior agent).
- **C9** — blocked by B1's disposition of `api.Dockerfile` (operator must
  approve committing it).
- **D1** — blocked on operator awareness (security incident); the removal can
  proceed once the location is confirmed, but key rotation is operator-only.
- **D4** — blocked by B1's disposition of `reports/ci-triage/`.
- **C8** — not blocked, but must run on a separate branch off `main`, not
  this branch.

---

# Recommended Next Assignment

1. **D1** (needs senior agent, security-urgent) — locate and remove the
   leaked Stripe secret token, then ask the operator to rotate the key. This
   is the only item with real-world urgency and it is NOT in v2. Highest
   priority despite being senior-agent work.
2. **B3** (safe beginner, ~15 min) — create `AGENTS.md` so future agents
   stop re-running disproved experiments (including the new full-SHA-image-tag
   rule from the `677c77ed` lesson). Independent, high leverage, no risk.
3. **A6** (needs senior agent, ~15–30 min live) — dispatch the
   `gpu_healthcheck` job against `6dbec436` to verify GPU access inside the
   container. The canary path is proven; this is the next live unknown on the
   critical path. → A7 (train_model) if it passes.

Run B3 (and B2, C7, C10 — all safe-beginner/focused, independent, no live
cloud) in parallel as a swarm of small agents. Hand D1 and A6 to senior
agents with RunPod/security access. Lane C focused-bugfix tasks (C1, C3, C5,
C6, C9) can be dispatched in parallel once B1 lands the dispositions they
depend on. C8 and D3 run on separate branches off `main`.
