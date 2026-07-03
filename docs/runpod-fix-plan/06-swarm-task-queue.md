# Fincept Swarm Task Queue

Last generated: 2026-07-03
Branch: `fix/test-harness-optional-deps-guards`
Newest commit reviewed: `c0f15fa7` (test(runpod): Test F import bisection handler)
Source of truth for prior state: `docs/runpod-fix-plan/RECEIPT_INDEX.md`

This queue is the active work list for the RunPod training-worker dispatch
investigation and surrounding repo hygiene. Each task is self-contained so a
smaller agent can execute it safely without re-deriving the investigation
history.

Read `RECEIPT_INDEX.md` first for what is already proven. Do NOT re-run
experiments listed in its "What Failed" table.

---

## Current Investigation State (one-paragraph brief)

The RunPod training worker goes `unhealthy=1` ~6s after a live job is
dispatched, leaving the job stuck in `IN_QUEUE`. Test E (sentinel-only handler)
PASSED live (`d7ba5a2d`); the production handler FAILED live (`c508103f`) in an
otherwise identical image. Test F (import bisection, `c0f15fa7`) ran ALL 12
profiles live — 11 PASSED, and the `lightgbm` "failure" was a FALSE NEGATIVE
caused by a probe script bug (worker was `running=1, unhealthy=0` when the
probe declared failure). Critically, `full_handler_call` PASSED — the
production handler's canary path COMPLETED live via the bisect wrapper.

**Root cause identified by commit `06646f1c`:** `equities.py` and `news.py`
use `parents[5]` to find the repo root, but the container path has only 4
parents, causing `IndexError`. The bisection handler's `try/except` catches
this; the production handler does not. The fix is to make the path resolution
safe for the container. The Dockerfile at HEAD copies
`handler_import_bisect.py` to `/worker/handler.py` (diagnostic shape), so
HEAD is NOT production-ready.

### Task Status Update (2026-07-03 hourly consolidation pass #2)

The receipt consolidation pass corrected the Test F receipt and updated
`RECEIPT_INDEX.md`. Several tasks in this queue are now done or obsolete:

- **T1 (reconcile Test F receipt): DONE.** The `summary.json` and
  `interpretation.md` have been corrected. The original T1 description
  referenced the old "sentinel poisons" contradiction; the actual
  contradiction was "lightgbm poisons" (also false — see `RECEIPT_INDEX.md`).
  T1's acceptance criteria are met: `summary.json` `first_failing_profile`
  is `null`, lightgbm `result` is `inconclusive_false_negative`, and
  `interpretation.md` no longer claims any import poisons the worker.
- **T2 (commit Test E correction): still open.** The working tree still has
  an uncommitted correction to `test-e-sentinel.md`.
- **T3 (receipt-integrity guard): still open.** Still valuable as a
  regression guard.
- **T4 (fix bisection script): still open but lower priority.** The probe
  bug (line 478) is documented in `RECEIPT_INDEX.md`. No further bisection
  runs are needed, so this is no longer blocking.
- **T5 (run remaining bisection profiles): OBSOLETE.** All 12 profiles
  already ran. `full_handler_call` proved the full import tree + handler
  call works. No further bisection is needed.
- **T6 (lazy-import fix): OBSOLETE.** Imports are NOT the problem.
  `full_handler_import` loaded all ML imports and passed.
- **T7 (restore prod handler): NOW THE PRIMARY NEXT STEP.** But with a
  different rationale — not "after lazy-import fix is proven" but "after
  the `parents[5]` IndexError fix is applied." The root cause is identified:
  `equities.py`/`news.py` use `parents[5]` which raises IndexError in the
  container (only 4 path parents). Fix the path resolution, restore the
  production handler, then retest live. See the updated Next Agent
  Instruction in `RECEIPT_INDEX.md`.
- **NEW: T11 — Fix `parents[5]` IndexError in equities.py and news.py.**
  This is the actual root cause fix. See `RECEIPT_INDEX.md` Next Agent
  Instruction step 2 for details. Status: `focused bugfix` (no live cloud
  needed for the code fix itself, but the live retest in T7 needs cloud).
- **T8 (update RECEIPT_INDEX): PARTIALLY DONE.** The index has been updated
  with Test F results. A final update will be needed after T7's live retest.
- **T9, T10: still open.** Unchanged.

---

## Task Status Legend

- `safe beginner` — read-only or doc-only, no live cloud, no secrets, no production code.
- `focused bugfix` — narrow code/doc change with a clear root cause; no live cloud.
- `needs senior agent` — touches live RunPod (secrets, spend, endpoints), production handler logic, or requires judgment across multiple hypotheses.
- `do not automate` — requires explicit operator decision; do not run without a human approval.

---

## T1 — Reconcile Test F receipt contradiction

**Status:** safe beginner
**Objective:** Fix the internally-contradictory Test F receipt so `summary.json`
and `interpretation.md` agree with the authoritative raw probe evidence.

**Context:**
`reports/runpod-test-runs/c0f15fa7/import-bisection/` contains three
conflicting signals:
- `probe-sentinel.jsonl` — job `6aa0bf6d-...-u1` went `IN_QUEUE` → `COMPLETED`,
  workers stayed healthy (`ready=1`, `unhealthy=0`) throughout.
- `status-final-sentinel.json` — `final_status: COMPLETED`.
- `summary.json` and `interpretation.md` — claim `result: fail` with
  `failure_reason: endpoint_create_error: ... flashBootType "NONE" does not
  exist in "FlashBootType" enum`, and conclude "the import group `sentinel`
  poisons the worker at dispatch time."

The endpoint-create initially failed with a `flashBootType` enum error, then a
retry succeeded and the probe completed. The summary/interpretation were
written from the initial failure and never updated. The "sentinel poisons the
worker" conclusion is FALSE — it directly contradicts the proven Test E PASS
(`d7ba5a2d`) and the probe JSONL in the same directory.

**Files allowed:**
- `reports/runpod-test-runs/c0f15fa7/import-bisection/summary.json`
- `reports/runpod-test-runs/c0f15fa7/import-bisection/interpretation.md`

**Files forbidden:**
- `probe-sentinel.jsonl`, `status-final-sentinel.json`, `health-*.json`,
  `cleanup-*.json`, `template-create-redacted.txt` (these are the authoritative
  raw evidence — do not edit).
- Any source code, Dockerfile, or other receipts.

**Commands to run:**
```powershell
git status --short --branch
git rev-parse HEAD
# Re-read the raw evidence to confirm the corrected values:
Get-Content reports/runpod-test-runs/c0f15fa7/import-bisection/probe-sentinel.jsonl
Get-Content reports/runpod-test-runs/c0f15fa7/import-bisection/status-final-sentinel.json
```

**Acceptance criteria:**
- `summary.json` `results[0].result` is `pass`, `final_status` is `COMPLETED`,
  `endpoint_id`/`job_id` are populated from the probe, and `failure_reason` is
  `null` or removed.
- `summary.json` `first_failing_profile` is `null` and
  `last_passing_profile` is `sentinel`.
- `interpretation.md` results table shows sentinel = `pass` / `COMPLETED`, and
  the "Next Steps" section says the sentinel profile PASSED and the remaining
  bisection profiles still need to be run (it must NOT claim sentinel poisons
  the worker).
- The `flashBootType` endpoint-create error is recorded as a transient
  pre-probe note, not as the profile result.
- No raw evidence files were edited.

**Rollback plan:**
```powershell
git restore reports/runpod-test-runs/c0f15fa7/import-bisection/summary.json reports/runpod-test-runs/c0f15fa7/import-bisection/interpretation.md
```

**Evidence required:**
- `git diff` of the two corrected files.
- Confirmation line that `probe-sentinel.jsonl` and `status-final-sentinel.json`
  were not modified (`git status` clean for those paths).

---

## T2 — Commit the Test E receipt correction (already staged in working tree)

**Status:** safe beginner
**Objective:** Commit the already-made correction to
`reports/runpod-test-runs/d7ba5a2d/test-e-sentinel.md` so the working tree is
clean and the receipt matches the raw probe evidence.

**Context:**
`git status` shows `reports/runpod-test-runs/d7ba5a2d/test-e-sentinel.md` is
modified but uncommitted. `RECEIPT_INDEX.md` documents exactly which fields were
corrected (endpoint id, job id, executionTime, delayTime, platform, started_at,
workerId, payload job_id) and why (the markdown contradicted the raw
`sentinel-probe.jsonl` / `endpoint-create-redacted.txt` / `health-*.json` in
the same directory). The correction is doc-only; no code changed.

**Files allowed:**
- `reports/runpod-test-runs/d7ba5a2d/test-e-sentinel.md`

**Files forbidden:**
- Everything else. Do not `git add -A`. Stage only this one file.

**Commands to run:**
```powershell
git status --short
git diff -- reports/runpod-test-runs/d7ba5a2d/test-e-sentinel.md
git add reports/runpod-test-runs/d7ba5a2d/test-e-sentinel.md
git commit -m "docs(runpod): correct Test E receipt to match raw probe evidence"
git status --short
```

**Acceptance criteria:**
- Commit contains exactly one file.
- `git status` no longer lists `test-e-sentinel.md` as modified.
- Commit message matches the correction (doc-only, no code).

**Rollback plan:**
```powershell
git revert HEAD
```
Only if the correction is found to be wrong. Do not revert just to tidy.

**Evidence required:**
- `git show --stat HEAD` output.
- `git status --short` showing the file is no longer dirty.

---

## T3 — Add a receipt-integrity guard test

**Status:** focused bugfix
**Objective:** Add a unit test that fails when a receipt bundle's
`summary.json` / `interpretation.md` contradicts its own raw probe JSONL, so
the T1 contradiction class cannot recur silently.

**Context:**
T1 exists because `run_import_bisection.py` wrote `summary.json` from an
initial endpoint-create failure and never reconciled it with the subsequent
successful probe. A guard that cross-checks `final_status` in
`status-final-*.json` against `results[].result` in `summary.json` for every
receipt dir under `reports/runpod-test-runs/` would have caught this.

**Files allowed:**
- `runpod/tests/test_receipt_integrity.py` (new)
- `runpod/tests/__init__.py` if missing and needed for collection

**Files forbidden:**
- Any receipt files under `reports/runpod-test-runs/` (the test reads them, it
  does not modify them).
- `run_import_bisection.py` (fix the script in T4, not here).
- Source code, Dockerfiles, workflows.

**Commands to run:**
```powershell
uv run ruff check runpod/tests/test_receipt_integrity.py
uv run pytest runpod/tests/test_receipt_integrity.py -q
```

**Acceptance criteria:**
- Test scans every `reports/runpod-test-runs/*/` directory that contains both a
  `summary.json` and a `status-final-*.json` (or `probe-*.jsonl`).
- For each, it asserts that the `final_status` from the raw evidence matches
  the `result`/`final_status` recorded in `summary.json`.
- The test FAILS on the current (pre-T1) state and PASSES after T1 is applied.
  (If T1 is already done, the test passes on HEAD.)
- Test does not hardcode the c0f15fa7 values; it derives them by scanning.
- Ruff passes on the new file.

**Rollback plan:**
```powershell
git rm runpod/tests/test_receipt_integrity.py
```

**Evidence required:**
- `pytest` output (pass on HEAD after T1).
- `ruff check` output (clean).
- A one-line note confirming the test would have failed on the pre-T1
  `summary.json`.

---

## T4 — Fix `run_import_bisection.py` to write summary from final probe state

**Status:** focused bugfix
**Objective:** Fix the orchestration script so a transient endpoint-create
error cannot poison the final `summary.json` / `interpretation.md`.

**Context:**
`runpod/quant-foundry-training/run_import_bisection.py` (currently untracked)
wrote `summary.json` reporting `result: fail` while the probe it drove reached
`COMPLETED`. The script must reconcile the summary with the actual probe
outcome, and must record a profile as `fail` only when the probe's final status
is not `COMPLETED` (or the endpoint genuinely never got created and no probe
ran). A transient create error followed by a successful retry must be recorded
as `pass` with the create error noted as a transient pre-probe event.

**Files allowed:**
- `runpod/quant-foundry-training/run_import_bisection.py`

**Files forbidden:**
- `handler_import_bisect.py`
- `Dockerfile`
- Any receipt files (T1 handles the existing one).
- `runpod/tests/**` (T3 owns the guard).

**Commands to run:**
```powershell
uv run ruff check runpod/quant-foundry-training/run_import_bisection.py
uv run python -c "import ast; ast.parse(open('runpod/quant-foundry-training/run_import_bisection.py').read())"
```

**Acceptance criteria:**
- `summary.json` `result` for a profile is derived from the probe's final
  status, not from the most recent error before the probe ran.
- If an endpoint-create error occurs and is then retried successfully, the
  profile is `pass` and the create error is stored in a `notes`/`transient_errors`
  field, not in `failure_reason`.
- `failure_reason` is populated only when the profile genuinely failed (no
  `COMPLETED` probe, or endpoint never created after all retries).
- Ruff passes.

**Rollback plan:**
```powershell
git restore runpod/quant-foundry-training/run_import_bisection.py
```
(If the file was never committed, simply discard the edit and re-evaluate.)

**Evidence required:**
- `git diff` of the script.
- `ruff check` output (clean).
- A short note describing the new summary-decision logic.

---

## T5 — Continue import bisection: run remaining profiles live

**Status:** needs senior agent
**Objective:** Run the remaining `handler_import_bisect.py` profiles live to
isolate the first module-level import that poisons the RunPod worker at
dispatch time.

**Context:**
Only the `sentinel` profile has been run (and per the raw probe it PASSED,
despite the broken summary). The remaining profiles in order are:
`pandas_numpy`, `xgboost`, `catboost`, `lightgbm`, `torch`,
`signatures_schemas`, `runpod_training`, `quality_report`,
`dataset_manifest`, `full_handler_import`, and (only if
`full_handler_import` passes) `full_handler_call`.

The image at HEAD (`c0f15fa7`) already has `handler_import_bisect.py` wired as
`/worker/handler.py` and reads `QF_IMPORT_PROFILE` from env. Each profile is a
single variable: only the endpoint env `QF_IMPORT_PROFILE=<profile>` and the
payload `profile` field change. Stop at the FIRST profile that makes the worker
`unhealthy=1` at dispatch — that import group is the culprit.

This is the live root-cause isolation step. It spends RunPod GPU time and uses
secrets (`RUNPOD_API_KEY`, `QUANT_FOUNDRY_CALLBACK_SECRET`, registry auth id).
Do NOT run this without operator awareness of spend.

**Files allowed:**
- `reports/runpod-test-runs/<new_sha>/` (new receipt bundle per profile or per run)
- `runpod/quant-foundry-training/run_import_bisection.py` only if T4 found a
  blocking bug (otherwise run it as-is)

**Files forbidden:**
- `Dockerfile` (already correct for bisection; do not change base/SDK/entrypoint)
- `handler_import_bisect.py`
- `handler.py` (production handler — do not touch during bisection)
- inference worker files, UI, app, product files
- `.github/workflows/**`

**Commands to run (per profile):**
```powershell
$sha = git rev-parse HEAD
$short = $sha.Substring(0, 8)
$image = "ghcr.io/airyder/fincept/quant-foundry-training:$sha"
$profile = "<profile_name>"   # e.g. pandas_numpy

uv run python scripts/runpod_create_smoke_endpoint.py `
  --image-tag $image `
  --name "qf-bisect-$profile-$short" `
  --template-name "qf-bisect-$profile-$short-template" `
  --copy-registry-auth-from-endpoint-id $env:RUNPOD_REGISTRY_AUTH_SOURCE_ENDPOINT_ID `
  --workers-min 1 --workers-max 1 `
  --container-disk-gb 20 --docker-args "" `
  --idle-timeout 300 --scaler-type QUEUE_DELAY --scaler-value 4 `
  --gpu-ids ADA_24 --wait-health --wait-timeout 600 --wait-interval 10 `
  --env "QUANT_FOUNDRY_CALLBACK_SECRET=$env:QUANT_FOUNDRY_CALLBACK_SECRET" `
  --env "QF_IMPORT_PROFILE=$profile"

$endpoint = "<endpoint-id>"
$payload = @{ input = @{ task = "import_bisect"; job_id = "qf:import-bisect:$profile`:$short`:001"; profile = $profile } } | ConvertTo-Json -Compress

uv run python scripts/runpod_smoke_probe.py `
  --endpoint-id $endpoint --image-tag $image `
  --interval 5 --timeout 240 --payload-json $payload
```
Then scale down the endpoint and write a receipt bundle matching the Test E
shape (probe JSONL, health before/after, status-final, cleanup,
interpretation).

**Acceptance criteria:**
- Each profile run has a receipt bundle with probe JSONL + final status +
  health before/after + cleanup.
- The FIRST failing profile is identified and named.
- All profiles up to the first failure are recorded as `pass` with `COMPLETED`.
- No profile is marked `fail` unless the probe's final status is not
  `COMPLETED` (per the T4 rule).
- The culprit import group is named in the interpretation with the exact
  failing endpoint id and job id.
- Every debug endpoint is scaled to `workersMin=0 workersMax=0` after its run.
- No secrets printed in receipts; registry auth id redacted.

**Rollback plan:**
- No code rollback (this is a probe-only sequence on an already-built image).
- Scale down every endpoint created during the run, even on early exit.
- If spend or rate limits are hit, stop and report; do not retry blindly.

**Evidence required:**
- Per-profile: probe JSONL, `status-final-*.json`, `health-before/after-*.json`,
  `cleanup-*.json`, redacted template-create.
- A run-level `summary.json` listing every profile tested, its result, and the
  first failing profile (or `null` if all passed).
- An `interpretation.md` naming the culprit import group and the exact next
  step (lazy-import fix target).

**Blocked by:** T1 (receipt must be honest before adding more profiles), T4
(script must not silently mislabel results).

---

## T6 — Implement lazy-import fix for the culprit module(s)

**Status:** needs senior agent
**Objective:** Convert the identified poisoning import(s) to lazy imports
(inside the handler function, loaded only when the task needs them) so the
production handler boots and completes a live job without going unhealthy.

**Context:**
This task cannot be scoped until T5 names the culprit import group. The
leading hypothesis is module-level ML imports (`torch`/`xgboost`/`catboost`/
`lightgbm`) causing memory pressure or a native crash ~6s after dispatch. The
fix must preserve the production handler's behavior and the callback-secret
canary workflow.

**Files allowed:**
- `runpod/quant-foundry-training/handler.py`
- `runpod/quant-foundry-training/handler_import_bisect.py` (only to remove it
  from the production path; do not delete the diagnostic)

**Files forbidden:**
- `Dockerfile` base image / SDK / entrypoint (only the `COPY` line may change
  to restore `handler.py` as `/worker/handler.py` after the fix is proven)
- `handler_sentinel.py`, `handler_minimal.py`, `handler_layered.py`,
  `handler_diagnostic.py`
- inference worker files, UI, app, product files
- `.github/workflows/**`

**Commands to run:**
```powershell
uv run ruff check runpod/quant-foundry-training
uv run python scripts/runpod_training_handler_local_test.py `
  --handler runpod/quant-foundry-training/handler.py `
  --payload-json '{"input":{"task":"callback_secret_canary","job_id":"local-lazy-fix","nonce":"n"}}'
```
Then a LIVE canary on a fresh endpoint (per `05-acceptance-criteria.md`).

**Acceptance criteria:**
- Local canary passes.
- LIVE callback-secret canary reaches `COMPLETED` on a fresh endpoint with the
  fixed image.
- Worker remains healthy after completion (`unhealthy=0`).
- Job does not stay `IN_QUEUE`.
- Callback signature verifies locally without printing the secret.
- No product behavior changed; canary workflow intact.

**Rollback plan:**
```powershell
git revert HEAD
```
Revert the lazy-import commit if the live canary still fails; re-open the
bisection with the new evidence. Do not stack additional hypotheses on a
failed fix.

**Evidence required:**
- `git diff` of `handler.py`.
- Local canary output.
- Live canary probe JSONL + final status + health before/after + cleanup.
- Redacted endpoint settings.

**Blocked by:** T5 (culprit import group must be named first).

---

## T7 — Restore production handler in Dockerfile after fix is proven live

**Status:** needs senior agent
**Objective:** Switch the Dockerfile `COPY` line back to the production
handler as `/worker/handler.py` once T6 is proven live, so HEAD is
production-ready again.

**Context:**
At HEAD the Dockerfile copies `handler_import_bisect.py` to
`/worker/handler.py` (diagnostic shape). This must not ship. After T6's live
canary passes, restore `handler.py` as the active handler and remove the
bisection handler from the production path (keep the file for future
diagnostics).

**Files allowed:**
- `runpod/quant-foundry-training/Dockerfile`

**Files forbidden:**
- `handler.py`, `handler_import_bisect.py` (do not delete either)
- base image / SDK / entrypoint changes
- inference worker files, UI, app, product files

**Commands to run:**
```powershell
Select-String -Path runpod/quant-foundry-training/Dockerfile -Pattern "COPY.*handler|FROM|ENTRYPOINT|runpod==" -Context 1
git diff -- runpod/quant-foundry-training/Dockerfile
uv run ruff check runpod/quant-foundry-training
```

**Acceptance criteria:**
- Dockerfile copies `handler.py` to `/worker/handler.py`.
- `handler_import_bisect.py` is no longer the active handler (may still be
  copied to `/worker/` for diagnostics, but not as `handler.py`).
- Base image, SDK version, entrypoint unchanged.
- A final live canary on the restored image reaches `COMPLETED` with a healthy
  worker.

**Rollback plan:**
```powershell
git restore --source=HEAD~1 -- runpod/quant-foundry-training/Dockerfile
```

**Evidence required:**
- `git diff` of the Dockerfile.
- Final live canary receipt on the restored image.

**Blocked by:** T6 (live canary must pass first).

---

## T8 — Update RECEIPT_INDEX with Test F + bisection results

**Status:** safe beginner
**Objective:** Add the Test F / import-bisection result rows to
`RECEIPT_INDEX.md` so the next agent reads a single current index.

**Context:**
`RECEIPT_INDEX.md` was last consolidated at `c990476a` and does not yet
include the `c0f15fa7` Test F run or any subsequent bisection profiles. After
T1 reconciles the Test F receipt and T5 runs the remaining profiles, the index
must be updated with the new evidence map rows and any newly-proven/disproved
hypotheses.

**Files allowed:**
- `docs/runpod-fix-plan/RECEIPT_INDEX.md`

**Files forbidden:**
- All source code, Dockerfiles, workflows, other docs.
- Other receipt files (they are evidence, not editable here).

**Commands to run:**
```powershell
git status --short --branch
git diff -- docs/runpod-fix-plan/RECEIPT_INDEX.md
```

**Acceptance criteria:**
- Evidence Map table has a new row for `c0f15fa7` (Test F, sentinel profile,
  PASS per reconciled receipt) and one row per bisection profile run in T5.
- "What Was Proven" and "What Failed" tables updated with the culprit import
  group once T5 names it.
- "Next Agent Instruction" section updated to point at T6 (lazy-import fix) or
  the next open bisection step.
- No claims contradict the raw probe evidence in the referenced receipt dirs.

**Rollback plan:**
```powershell
git restore docs/runpod-fix-plan/RECEIPT_INDEX.md
```

**Evidence required:**
- `git diff` of the index.
- A one-line confirmation that every new row maps to an existing receipt dir.

**Blocked by:** T1, T5.

---

## T9 — Triage and remove untracked `.tmp_*.py` scratch files

**Status:** do not automate
**Objective:** Decide, with the operator, whether the 17 untracked `.tmp_*.py`
files in the repo root are safe to delete, then remove the ones that are.

**Context:**
`git status` lists 17 untracked `.tmp_*.py` files (e.g. `.tmp_check_health.py`,
`.tmp_deploy_train.py`, `.tmp_pod_logs.py`, `.tmp_purge.py`,
`.tmp_test_cuda_job.py`, etc.). These are ad-hoc RunPod diagnostic scripts
from prior debugging sessions. They are not tracked, not in `.gitignore`, and
not referenced by any committed code. Deleting the wrong one could lose
in-progress diagnostic work; keeping them clutters the worktree and risks
accidental commits.

Do NOT delete anything without explicit operator confirmation. This task is
gated on a human decision.

**Files allowed:**
- `.tmp_*.py` files in the repo root, only after operator approval per file.

**Files forbidden:**
- Everything else. Especially `SESSION_HANDOFF.md`, `kimiSuggestionFix.md`,
  `handoffs/`, and any tracked file.

**Commands to run (triage only, no deletion yet):**
```powershell
git status --short | Select-String "\.tmp_"
Get-ChildItem .tmp_*.py | Select-Object Name, Length, LastWriteTime
# For each file, show its first lines so the operator can judge:
Get-Content .tmp_check_health.py -TotalCount 5
```

**Acceptance criteria:**
- Operator has reviewed the list and approved a per-file delete/keep decision.
- Only approved files are removed.
- No tracked file is touched.
- A note is left (in the commit message or a receipt) listing what was kept
  and why.

**Rollback plan:**
- Deleted untracked files are NOT recoverable via git. If unsure, move to a
  local backup dir outside the repo instead of deleting.

**Evidence required:**
- The operator's approved keep/delete list.
- `git status --short` after removal showing only intended files gone.

---

## T10 — Add `.tmp_*` and scratch patterns to `.gitignore`

**Status:** focused bugfix
**Objective:** Prevent future `.tmp_*.py` scratch files (and similar ad-hoc
diagnostic scripts) from cluttering `git status` or being committed by
accident.

**Context:**
The 17 `.tmp_*.py` files exist because there is no `.gitignore` rule for the
scratch pattern. Adding a narrow rule keeps future diagnostic work out of the
tracked set without affecting any committed file.

**Files allowed:**
- `.gitignore`

**Files forbidden:**
- All source code, Dockerfiles, workflows.
- Do not delete the existing `.tmp_*.py` files here (that is T9).

**Commands to run:**
```powershell
git diff -- .gitignore
git check-ignore .tmp_check_health.py
```

**Acceptance criteria:**
- `.gitignore` gains a rule matching `.tmp_*` (and, if not already present,
  `SESSION_HANDOFF.md` and `handoffs/` are NOT added — those are intentional
  handoff artifacts that may need committing).
- `git check-ignore .tmp_check_health.py` returns the file (it is now ignored).
- No existing tracked file becomes ignored (run `git status` to confirm no
  surprises).

**Rollback plan:**
```powershell
git restore .gitignore
```

**Evidence required:**
- `git diff` of `.gitignore`.
- `git check-ignore` output proving the pattern matches.
- `git status --short` showing the `.tmp_*` files no longer appear as untracked.

---

## Dependency Graph

```
T1 (reconcile Test F receipt) ─┬─> T5 (run remaining bisection profiles) ─> T6 (lazy-import fix) ─> T7 (restore prod handler)
T4 (fix bisection script) ─────┘                                          └─> T8 (update RECEIPT_INDEX)
T3 (receipt-integrity guard)  # independent; can run anytime after T1
T2 (commit Test E correction) # independent; safe to run immediately
T9 (triage .tmp_ files)       # independent; gated on operator decision
T10 (gitignore scratch)       # independent; safe to run anytime
```

---

## Active Automations (do not duplicate)

- The import-bisection live run (`run_import_bisection.py` driving
  `handler_import_bisect.py`) is the active investigation. T5 continues it; do
  not spawn a parallel bisection.
- `RECEIPT_INDEX.md` is the consolidated entry point. Do not create a competing
  index; update it via T8.
- The healthcheck regression guard
  (`runpod/tests/test_dockerfile_no_healthcheck.py`) is already in place — do
  not re-add it.

---

## Blocked Tasks

- **T5** blocked by T1 + T4 (receipt must be honest and script must not
  mislabel results before adding more live profiles).
- **T6** blocked by T5 (culprit import group must be named).
- **T7** blocked by T6 (live canary must pass).
- **T8** blocked by T1 + T5 (needs reconciled + completed bisection results).
- **T9** blocked on operator decision (do not automate).

---

## Recommended Next Assignment

1. **T2** (safe beginner, ~2 min) — commit the already-staged Test E receipt
   correction to clean the working tree. Unblocks nothing but removes noise.
2. **T1** (safe beginner, ~10 min) — reconcile the Test F receipt so the
   investigation record is truthful. Unblocks T5.
3. **T4** (focused bugfix, ~15 min) — fix `run_import_bisection.py` so the
   next bisection profiles cannot be silently mislabeled. Unblocks T5.

Run T2, T1, and T4 in parallel (independent files), then hand T5 to a senior
agent with RunPod access.
