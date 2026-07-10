# Fincept Swarm Task Queue v2

Last generated: 2026-07-03
Branch: `fix/test-harness-optional-deps-guards`
HEAD: `6dbec436` (fix(runpod): parents[5] IndexError + restore production handler + probe fix)
Source of truth for prior state: `docs/runpod-fix-plan/RECEIPT_INDEX.md`

This queue **supersedes** `docs/runpod-fix-plan/06-swarm-task-queue.md`. The
v1 queue (T1–T10) is now mostly DONE or OBSOLETE; see the v1 status rollup
below. This v2 queue reflects the post-`6dbec436` state: the code fix is
committed, the image is published, and the remaining work is **live validation
+ repo hygiene + configuration/deployment hygiene**.

Read `RECEIPT_INDEX.md` first for what is already proven. Do NOT re-run
experiments listed in its "What Failed" / "What Should NOT Be Retried" tables.

---

## One-paragraph state brief

The RunPod training-worker dispatch failure was root-caused to an unguarded
`parents[5]` index in `equities.py`/`news.py` (only 4 path parents exist in
the container). Commit `6dbec436` fixed it (guarded index +
`ModuleNotFoundError` fallback), restored the production handler as the
direct RunPod entrypoint, fixed the bisection probe false-negative logic, and
added a receipt-integrity guard test. Local gates passed (ruff clean, pytest
7+4, local canary COMPLETED). The `build-runpod-training` workflow SUCCEEDED
for `6dbec436` (run 28683991294); the image is published as
`ghcr.io/airyder/fincept/quant-foundry-training:6dbec436c92b57a788b84622338baacc3df8665d`
(and `:latest`). **No live RunPod receipt exists yet for `6dbec436`** — the
single critical open step is a fresh live production-handler canary.

---

## v1 Queue Status Rollup (T1–T10)

| v1 Task | v2 Status | Note |
|---------|-----------|------|
| T1 reconcile Test F receipt | **DONE** (`61dca0a4`) | summary/interpretation corrected |
| T2 commit Test E correction | **DONE** (`61dca0a4`) | committed |
| T3 receipt-integrity guard | **DONE** (`6dbec436`) | `runpod/tests/test_receipt_integrity.py` |
| T4 fix bisection script | **DONE** (`6dbec436`) | probe false-negative logic fixed |
| T5 run remaining bisection profiles | **OBSOLETE** | all 12 profiles already ran |
| T6 lazy-import fix | **OBSOLETE** | imports are NOT the problem |
| T7 restore prod handler | **DONE** (`6dbec436`) | Dockerfile restored; live retest portion → A2 |
| T8 update RECEIPT_INDEX | **PARTIAL** | updated through pass #3; final live receipt → A4 |
| T9 triage .tmp_ files | **MOSTLY MOOT** | `.tmp_*.py` no longer in worktree; broader dirty-work → B1 |
| T10 gitignore scratch | **OPEN** → B2 | still valuable |

---

## Task Status Legend

- `safe beginner` — read-only or doc-only, no live cloud, no secrets, no production code.
- `focused bugfix` — narrow code/config change with a clear root cause; no live cloud.
- `needs senior agent` — touches live RunPod (secrets, spend, endpoints), production handler logic, security boundaries, or large-scope refactors requiring judgment.
- `do not automate` — requires explicit operator decision; do not run without human approval.

---

## Active Automations (do not duplicate)

- **Receipt consolidation pass** — `RECEIPT_INDEX.md` + `07-remaining-work.md`
  were updated by "hourly pass #3" but those updates are **uncommitted** in the
  worktree. Do not run a competing consolidation; A1 commits the existing
  pass #3 updates. If a new pass #4 runs, it should build on A1's commit.
- **`build-runpod-training` workflow** — green for `6dbec436`. Do not re-trigger
  the build unless a new commit lands on this branch.
- **Receipt-integrity guard** (`runpod/tests/test_receipt_integrity.py`) and
  **no-healthcheck guard** (`runpod/tests/test_dockerfile_no_healthcheck.py`)
  are in place — do not re-add them.
- **Import bisection** (`run_import_bisection.py` / `handler_import_bisect.py`)
  is complete (all 12 profiles ran). Do not spawn a parallel bisection.

---

# Lane A — Live Validation (critical path)

The code fix is done and the image is published. The only thing standing
between the current state and "investigation closed" is a live canary.

## A1 — Commit consolidation pass #3 doc updates

**Status:** safe beginner
**Objective:** Commit the already-made pass #3 updates to
`07-remaining-work.md` and `RECEIPT_INDEX.md` so the worktree is clean and
the investigation record is current.

**Context:**
`git status` shows two modified (uncommitted) plan docs:
- `docs/runpod-fix-plan/07-remaining-work.md` — updated to mark items 1, 2, 9,
  10, 11 DONE and reflect the post-`6dbec436` state.
- `docs/runpod-fix-plan/RECEIPT_INDEX.md` — updated with pass #3 consolidation
  (newest commit `6dbec436`, build workflow status, corrected evidence map).

Both diffs are doc-only evidence/plan updates — no code, no Dockerfile, no
secrets. They were produced by the autonomous receipt consolidation pass #3.
Committing them unblocks a clean worktree for the live canary lane.

**Files allowed:**
- `docs/runpod-fix-plan/07-remaining-work.md`
- `docs/runpod-fix-plan/RECEIPT_INDEX.md`

**Files forbidden:**
- `infra/docker/api.Dockerfile` (unrelated — belongs to C9)
- `SESSION_HANDOFF.md`, `handoffs/`, `kimiSuggestionFix.md`,
  `reports/ci-triage/` (untracked — belongs to B1)
- All source code, Dockerfiles, workflows, other docs.

**Commands to run:**
```powershell
git status --short
git diff -- docs/runpod-fix-plan/07-remaining-work.md docs/runpod-fix-plan/RECEIPT_INDEX.md
git add docs/runpod-fix-plan/07-remaining-work.md docs/runpod-fix-plan/RECEIPT_INDEX.md
git commit -m "docs(runpod): consolidate pass #3 — mark parents[5] fix done, build green"
git status --short
```

**Acceptance criteria:**
- Commit contains exactly the two plan docs.
- `git status` no longer lists either file as modified.
- Commit message states these are doc/evidence updates, not product fixes.
- No raw evidence files under `reports/runpod-test-runs/` are touched.

**Rollback plan:**
```powershell
git revert HEAD
```
Only if a factual error is found in the pass #3 updates.

**Evidence required:**
- `git show --stat HEAD` output.
- `git status --short` showing the two files are no longer dirty.

---

## A2 — Run fresh live production-handler canary against `6dbec436`

**Status:** needs senior agent
**Objective:** Validate live that the `parents[5]` fix resolved the dispatch
failure by running a `callback_secret_canary` job against the exact-SHA
production image with the production handler as the direct entrypoint.

**Context:**
The image is published:
`ghcr.io/airyder/fincept/quant-foundry-training:6dbec436c92b57a788b84622338baacc3df8665d`
(build run 28683991294, conclusion success). Local canary COMPLETED. The
`full_handler_call` bisection profile PASSED live via the bisect wrapper
(`c0f15fa7`), which strongly suggests the fix works — but no live receipt
exists for `6dbec436` with the production handler as the **direct** entrypoint.

This spends RunPod GPU time and uses secrets (`RUNPOD_API_KEY`,
`QUANT_FOUNDRY_CALLBACK_SECRET`, registry auth id). Do NOT run without
operator awareness of spend. Follow the exact endpoint shape from
`07-remaining-work.md` item 5.

**Files allowed:**
- `reports/runpod-test-runs/6dbec436/` (new receipt bundle)

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

uv run python scripts/runpod_create_smoke_endpoint.py `
  --image-tag $image `
  --name "qf-prod-canary-6dbec436" `
  --template-name "qf-prod-canary-6dbec436-template" `
  --copy-registry-auth-from-endpoint-id $env:RUNPOD_REGISTRY_AUTH_SOURCE_ENDPOINT_ID `
  --workers-min 1 --workers-max 1 `
  --container-disk-gb 20 --docker-args "" `
  --idle-timeout 300 --scaler-type QUEUE_DELAY --scaler-value 4 `
  --gpu-ids ADA_24 --wait-health --wait-timeout 600 --wait-interval 10 `
  --env "QUANT_FOUNDRY_CALLBACK_SECRET=$env:QUANT_FOUNDRY_CALLBACK_SECRET"

$endpoint = "<endpoint-id-from-create>"
$payload = @{ input = @{ task = "callback_secret_canary"; job_id = "qf:prod-canary:6dbec436:001"; nonce = "n" } } | ConvertTo-Json -Compress

uv run python scripts/runpod_smoke_probe.py `
  --endpoint-id $endpoint --image-tag $image `
  --interval 5 --timeout 240 --payload-json $payload
```
Then capture `/health` after completion, scale the endpoint down, and record
cleanup. See `07-remaining-work.md` item 5 for the full receipt-bundle
checklist.

**Acceptance criteria:**
- Job reaches `COMPLETED`.
- Job does not stay `IN_QUEUE`.
- Worker remains `unhealthy=0` after completion.
- Callback signature is present but secrets are not printed.
- Debug endpoint is scaled down or deleted after the test.
- Receipt bundle written under `reports/runpod-test-runs/6dbec436/` with:
  endpoint id, redacted endpoint settings, health before dispatch, canary
  `/run` response, status probe JSONL, final status JSON, health after
  completion, cleanup receipt, short interpretation.

**Rollback plan:**
- No code rollback (probe-only on an already-built image).
- Scale down the endpoint even on early exit.
- If spend or rate limits are hit, stop and report; do not retry blindly.

**Evidence required:**
- `reports/runpod-test-runs/6dbec436/` receipt bundle (all items above).
- A one-line interpretation: PASS or FAIL with the terminal status.

**Blocked by:** A1 (clean worktree preferred before live work, not strictly
required — A2 may proceed if the operator accepts the dirty worktree).

---

## A3 — Repeat canary 2–3× for stability (conditional)

**Status:** needs senior agent
**Objective:** Confirm the `6dbec436` fix is stable, not a single-pass fluke,
by running 2–3 additional `callback_secret_canary` jobs against the same
endpoint/image.

**Context:**
Only runs if A2 PASSES. Same endpoint, same image, same redaction/cleanup
discipline. The goal is to rule out a coincidental single success.

**Files allowed:**
- `reports/runpod-test-runs/6dbec436/` (append stability receipts)

**Files forbidden:**
- Same as A2.

**Commands to run:**
```powershell
# Reuse the endpoint from A2; dispatch 2-3 more canary jobs with incrementing job_id
$payload = @{ input = @{ task = "callback_secret_canary"; job_id = "qf:prod-canary:6dbec436:002"; nonce = "n2" } } | ConvertTo-Json -Compress
uv run python scripts/runpod_smoke_probe.py --endpoint-id $endpoint --image-tag $image --interval 5 --timeout 240 --payload-json $payload
# repeat for :003
```

**Acceptance criteria:**
- All repeated canaries reach `COMPLETED`.
- No worker transitions to `unhealthy=1`.
- Receipts show stable job pickup and terminal status.
- Endpoint scaled down or deleted after the final run.

**Rollback plan:**
- Scale down the endpoint on early exit.

**Evidence required:**
- Per-run probe JSONL + final status + health after + cleanup.
- A stability summary line: "N/N canaries COMPLETED, 0 unhealthy transitions."

**Blocked by:** A2 (must PASS first).

---

## A4 — Consolidate `6dbec436` live receipt + update RECEIPT_INDEX

**Status:** safe beginner
**Objective:** Write the consolidated `6dbec436` receipt interpretation and
update `RECEIPT_INDEX.md` so the next agent reads a single current index
reflecting the live validation result.

**Context:**
After A2 (and A3 if run), `reports/runpod-test-runs/6dbec436/` contains raw
evidence. `RECEIPT_INDEX.md` currently lists `6dbec436` as PENDING. This task
flips it to PASS or FAIL based strictly on the raw evidence, updates the
proven/disproved tables, and marks the investigation closed (if PASS) or
escalates (if FAIL → A5).

**Files allowed:**
- `docs/runpod-fix-plan/RECEIPT_INDEX.md`
- `reports/runpod-test-runs/6dbec436/interpretation.md` (new, derived from raw evidence)
- `reports/runpod-test-runs/6dbec436/summary.json` (new, if not produced by the probe script)

**Files forbidden:**
- Raw probe/health/cleanup JSON files (immutable evidence — do not edit).
- All source code, Dockerfiles, workflows.
- Other receipt dirs.

**Commands to run:**
```powershell
git status --short
# Re-read the raw evidence to derive the interpretation:
Get-Content reports/runpod-test-runs/6dbec436/status-final-*.json
Get-Content reports/runpod-test-runs/6dbec436/probe-*.jsonl
git diff -- docs/runpod-fix-plan/RECEIPT_INDEX.md
```

**Acceptance criteria:**
- `RECEIPT_INDEX.md` Evidence Map row for `6dbec436` is updated from PENDING
  to PASS or FAIL with the endpoint id and result.
- "What Was Proven" table adds the live `6dbec436` result.
- "Next Agent Instruction" reflects the closed (or escalated) state.
- No claims contradict the raw probe evidence.
- `runpod/tests/test_receipt_integrity.py` still passes after the update.

**Rollback plan:**
```powershell
git restore docs/runpod-fix-plan/RECEIPT_INDEX.md reports/runpod-test-runs/6dbec436/interpretation.md
```

**Evidence required:**
- `git diff` of the index.
- `uv run pytest runpod/tests/test_receipt_integrity.py -q` output (pass).
- One-line confirmation that interpretation follows raw evidence.

**Blocked by:** A2 (and A3 if run).

---

## A5 — If canary fails, isolate the new failing boundary (conditional)

**Status:** needs senior agent
**Objective:** If A2 FAILS (worker `unhealthy=1`, job stuck `IN_QUEUE`, or
exit before terminal status), isolate only the new failing boundary — do NOT
re-run the disproved hypotheses.

**Context:**
The `parents[5]` IndexError was the leading root cause. If the fix does not
resolve the live failure, the new receipt must be compared against the
passing `full_handler_call` receipt from Test F (`c0f15fa7`). The only
structural difference is which `handler` function is passed to
`runpod.serverless.start()`. Inspect the production handler `__main__` path,
preflight behavior, and the SDK start boundary. Re-run import bisection ONLY
if the fresh failure evidence points to a different import after the path fix.

**Files allowed:**
- `reports/runpod-test-runs/6dbec436/` (failure receipt analysis)
- `runpod/quant-foundry-training/handler.py` (read-only inspection; any fix
  requires a new task card)

**Files forbidden:**
- `equities.py`, `news.py` (already fixed; do not re-touch without evidence)
- `Dockerfile` (production-shaped; do not change without a new hypothesis)
- inference worker files, UI, app, product files

**Commands to run:**
```powershell
# Compare the failed receipt against the passing full_handler_call receipt:
Get-Content reports/runpod-test-runs/c0f15fa7/import-bisection/probe-full_handler_call.jsonl
Get-Content reports/runpod-test-runs/6dbec436/probe-*.jsonl
# Inspect the production handler __main__ + preflight path:
Select-String -Path runpod/quant-foundry-training/handler.py -Pattern "__main__|SecurityPreflight|serverless.start|def handler"
```

**Acceptance criteria:**
- New hypothesis is tied to the fresh `6dbec436` receipt, not to stale Test F
  assumptions.
- Any new bisection run uses the fixed probe logic (already in `6dbec436`).
- A new task card is written for the identified fix before implementation.

**Rollback plan:**
- No code changes in this task (analysis only). Rollback = discard the
  analysis note if the hypothesis is rejected.

**Evidence required:**
- A written hypothesis tied to the `6dbec436` raw evidence.
- A diff/comparison against the `c0f15fa7` `full_handler_call` receipt.
- A new task card for the proposed fix (do not implement here).

**Blocked by:** A2 (must FAIL first).

---

# Lane B — Repo Hygiene

## B1 — Classify dirty worktree items

**Status:** do not automate
**Objective:** With the operator, decide the disposition of every untracked
and unrelated-modified item in the worktree so the final commit set is
reviewable and safe.

**Context:**
`git status` (post-A1) shows these unrelated items:
- `infra/docker/api.Dockerfile` — modified, adds `COPY experiments experiments`.
  This is a fix for the F2 `build-images` / `build (api)` failure (the
  `experiments/news-impact-model` workspace path dep can't resolve without
  the dir in build context). **Likely a real fix — see C9.** Keep separate
  from the RunPod fix.
- `SESSION_HANDOFF.md` — untracked, STALE. Predates the `parents[5]` fix;
  describes the superseded bisect-`handler()` approach and old commit
  `d15482ff`. Misleading if read as current.
- `handoffs/2026-07-03_01-51_fix-runpod-training-crash/` — untracked, STALE.
  Same superseded narrative as `SESSION_HANDOFF.md`.
- `kimiSuggestionFix.md` — untracked. 15-item config/deployment hygiene
  audit. Source material for Lane C tasks. Keep as a reference doc or move
  into `docs/`.
- `reports/ci-triage/receipt-20260703T200535Z.md` — untracked. CI triage
  receipt covering `c0f15fa7`; documents pre-existing CI debt. Durable
  evidence — candidate for committing.

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
Get-Content reports/ci-triage/receipt-20260703T200535Z.md -TotalCount 5
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
no `.gitignore` rule prevents them from returning. A narrow rule keeps future
diagnostic work out of the tracked set without affecting any committed file.

**Files allowed:**
- `.gitignore`

**Files forbidden:**
- All source code, Dockerfiles, workflows.
- Do not add `SESSION_HANDOFF.md` or `handoffs/` to `.gitignore` here — those
  are intentional handoff artifacts whose disposition is B1's job.

**Commands to run:**
```powershell
git diff -- .gitignore
# verify the pattern matches without affecting tracked files:
git check-ignore -v .tmp_check_health.py 2>$null  # may not exist; that's fine
git status --short
```

**Acceptance criteria:**
- `.gitignore` gains a rule matching `.tmp_*` (and, if not already present,
  `.tmp_*.py`).
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
selection, no Docker HEALTHCHECK, direct entrypoint, init timeout) so future
agents avoid re-running disproved experiments.

**Context:**
The repo has `PROJECT_OVERVIEW.md` but no `AGENTS.md`. Operational knowledge
is scattered across commits, `RUNPOD_UNHEALTHY_ROOT_CAUSE.md`, the README,
and `RECEIPT_INDEX.md`. An `AGENTS.md` consolidates the "do not re-do" rules
in one place agents read first.

**Files allowed:**
- `AGENTS.md` (new, repo root)

**Files forbidden:**
- All other files. Do not modify `PROJECT_OVERVIEW.md`, the README, or any
  existing doc here.

**Commands to run:**
```powershell
git status --short
# verify it does not duplicate existing rules:
Select-String -Path PROJECT_OVERVIEW.md -Pattern "HEALTHCHECK|base image|entrypoint" -SimpleMatch
```

**Acceptance criteria:**
- `AGENTS.md` exists at the repo root with concise rules:
  - Do not change the RunPod training base image to `pytorch/pytorch` or
    `runpod/base` without re-running the layer-0 probe (disproved).
  - Do not reintroduce a Docker `HEALTHCHECK` (disproved + regression-guarded).
  - Always set `RUNPOD_INIT_TIMEOUT >= 900` for CUDA training endpoints.
  - Keep `ENTRYPOINT ["python", "-u", "/worker/handler.py"]` (direct).
  - Never commit `RUNPOD_API_KEY`, `QUANT_FOUNDRY_CALLBACK_SECRET`, or
    endpoint IDs in scripts/docs.
  - Local handler test passes are NOT live proof (disproved by `c508103f`).
  - Do not pursue the "lightgbm/ML imports poison the worker" hypothesis
    (disproved by Test F `full_handler_import`).
- Rules are derived from `RECEIPT_INDEX.md` "What Should NOT Be Retried".

**Rollback plan:**
```powershell
git rm AGENTS.md
```

**Evidence required:**
- `git diff` / file content of `AGENTS.md`.
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
- A live canary (A2 shape) still passes after the change.

**Rollback plan:**
```powershell
git restore runpod/quant-foundry-training/Dockerfile runpod/quant-foundry-training/README.md
```

**Evidence required:**
- `git diff` of the Dockerfile + README.
- Operator's chosen option (A or B) recorded.
- Live canary receipt on the changed image (separate from A2).

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

**Coordinate with:** C2, C3 (all touch the README).

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
- `Dockerfile` (production-shaped; do not change)
- inference worker files

**Commands to run:**
```powershell
git status --short
# confirm none are referenced by committed code/workflows:
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
- `git diff` / new file content.
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
- Do not bundle with A1 or any RunPod-fix commit.

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
failure envelope runs.

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

# Dependency Graph

```
A1 (commit pass #3 docs) ─┬─> A2 (live canary) ─┬─> A3 (stability repeat) ─> A4 (consolidate receipt)
                          │                      └─> A5 (failure isolation, conditional)
                          │
B1 (classify dirty work) ─┼─> C9 (commit api.Dockerfile fix, if operator approves)
                          │
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
C10 (job timeout doc)     # coordinate with C4
```

---

# Blocked Tasks

- **A2** — preferred blocked by A1 (clean worktree); may proceed with operator
  acceptance of the dirty worktree. Spends live GPU budget.
- **A3** — blocked by A2 (must PASS).
- **A4** — blocked by A2 (and A3 if run).
- **A5** — blocked by A2 (must FAIL).
- **B1** — blocked on operator decision (do not automate).
- **C2** — blocked on operator entrypoint-strategy decision (do not automate
  the choice; implementation is senior agent).
- **C9** — blocked by B1's disposition of `api.Dockerfile` (operator must
  approve committing it).
- **C8** — not blocked, but must run on a separate branch off `main`, not
  this branch.

---

# Recommended Next Assignment

1. **A1** (safe beginner, ~3 min) — commit the pass #3 doc updates to clean
   the worktree. Unblocks a clean state for the live lane. No risk.
2. **A2** (needs senior agent, ~15–30 min live) — **the single most important
   open step.** The image is published and local gates passed; run the live
   production-handler canary against `6dbec436`. If it PASSES, the
   investigation is essentially closed (→ A3, A4).
3. **B3** (safe beginner, ~15 min) — create `AGENTS.md` so future agents
   stop re-running disproved experiments. Independent, high leverage, no risk.

Run A1 and B3 in parallel (independent files), then hand A2 to a senior
agent with RunPod access. Lane C tasks can be dispatched to focused-bugfix
agents in parallel once A1 lands, except C2/C9 which need operator decisions.
