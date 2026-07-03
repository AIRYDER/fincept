# RunPod Training Worker Remaining Work

Last updated: 2026-07-03 (consolidation pass #3)
Branch observed: `fix/test-harness-optional-deps-guards`
Newest committed evidence reviewed: `6dbec436`

This is the active remaining-work checklist for the Quant Foundry RunPod
training-worker dispatch investigation. It supersedes stale parts of
`06-swarm-task-queue.md` where later receipt consolidation corrected the Test F
interpretation.

Read this with:

- `docs/runpod-fix-plan/RECEIPT_INDEX.md`
- `reports/runpod-test-runs/c0f15fa7/import-bisection/interpretation.md`
- `docs/runpod-fix-plan/05-acceptance-criteria.md`

## Current State

**Items 1, 2, 9, 10, 11 are DONE (commit `6dbec436` + `61dca0a4`).** The
`parents[5]` IndexError is fixed in `equities.py`/`news.py` (guarded index +
`ModuleNotFoundError` fallback), the production handler is restored as the
direct RunPod entrypoint in the Dockerfile, the bisection probe false-negative
logic is fixed, the receipt-integrity guard test is added, and the Test F
receipt corrections are committed. Local gates passed (ruff clean, pytest 7+4,
local canary COMPLETED).

**The Dockerfile is now production-shaped** (copies `handler.py` to
`/worker/handler.py`; `handler_import_bisect.py` is no longer copied in).

**Items 1, 2, 3, 5, 6, 9, 10, 11 are DONE.** The live production-handler
canary PASSED at `6dbec436` (3/3 COMPLETED, worker remained healthy). The
`parents[5]` IndexError is confirmed as the root cause of the dispatch-time
crash. See `reports/runpod-test-runs/6dbec436/interpretation.md`.

**Remaining: items 4 (build — already green), 7 (conditional — not needed),
8 (receipt consolidation — in progress), 12 (repo hygiene).** The next
milestone is a live `gpu_healthcheck` / `train_model` job to verify the full
training pipeline (not just the canary path).

## Do Not Re-Do

- Do not pursue the "lightgbm poisons the worker" hypothesis. The Test F
  lightgbm result was a probe false negative: the worker was `running=1` and
  `unhealthy=0`.
- Do not re-run individual import bisection profiles unless a new post-fix
  failure requires it. All 12 Test F profiles already ran.
- Do not reintroduce a Docker `HEALTHCHECK`.
- Do not switch the base image, RunPod SDK version, entrypoint model, endpoint
  shape, or product flow as part of the next fix.
- Do not touch the inference endpoint or any trading/broker credential surface.

## Remaining Checklist

### 1. Fix the `parents[5]` container path crash

Status: **DONE (commit `6dbec436`)**
Type: focused code fix

Files:

- `services/quant_foundry/src/quant_foundry/data_ingestion/equities.py`
- `services/quant_foundry/src/quant_foundry/data_ingestion/news.py`

Work:

- Replace the unguarded `parents[5]` access with safe path resolution.
- Skip `sys.path` insertion when the repo-only `scripts/` or `experiments/`
  directory is unavailable in the worker image.
- Preserve local ingestion behavior when the repo root is available.
- Avoid catching broad exceptions around imports just to hide the bug.

Suggested validation:

```powershell
uv run ruff check services/quant_foundry/src/quant_foundry/data_ingestion/equities.py services/quant_foundry/src/quant_foundry/data_ingestion/news.py
uv run python -c "import quant_foundry.data_ingestion.quality_report; print('quality_report import ok')"
uv run python -c "import quant_foundry.data_ingestion.equities; import quant_foundry.data_ingestion.news; print('ingestion imports ok')"
```

Acceptance:

- Importing `quant_foundry.data_ingestion.quality_report` no longer depends on
  a path depth that only exists in the repo checkout.
- Importing `equities.py` and `news.py` cannot raise `IndexError` from path
  parent indexing.
- No ingestion API contract changes.

### 2. Restore the production handler as the direct RunPod entrypoint

Status: **DONE (commit `6dbec436`)**
Type: Dockerfile production-shape restoration

File:

- `runpod/quant-foundry-training/Dockerfile`

Work:

- Replace the bisection handler mapping with the production handler mapping:

```dockerfile
COPY runpod/quant-foundry-training/handler.py /worker/handler.py
```

- Keep `handler_import_bisect.py` in the repo for future diagnostics, but do
  not copy it as `/worker/handler.py`.
- Keep base image, dependency pins, `ENTRYPOINT`, and no-healthcheck stance
  unchanged.

Acceptance:

- `/worker/handler.py` is the production handler.
- `handler_import_bisect.py` is no longer the active RunPod handler.
- The Dockerfile diff is limited to restoring the handler mapping.

### 3. Run local gates before any live endpoint

Status: **DONE (per `6dbec436` commit message)** — ruff clean, pytest 7+4, local canary COMPLETED, `git diff --check` clean.
Type: local verification

Commands:

```powershell
uv run ruff check runpod/quant-foundry-training services/quant_foundry/src/quant_foundry/data_ingestion
uv run pytest runpod/tests/test_dockerfile_no_healthcheck.py -q
uv run python scripts/runpod_training_handler_local_test.py `
  --handler runpod/quant-foundry-training/handler.py `
  --payload-json '{"input":{"task":"callback_secret_canary","job_id":"local-parents5-fix","nonce":"n"}}'
git diff --check
```

Acceptance:

- Ruff passes for touched code.
- No-healthcheck guard still passes.
- Local callback-secret canary still returns.
- Whitespace check passes.

### 4. Build and publish an exact SHA training image

Status: **DONE** — build run `28683991294` green (13m28s). Image:
`ghcr.io/airyder/fincept/quant-foundry-training:6dbec436c92b57a788b84622338baacc3df8665d`
(full 40-char SHA tag — the workflow tags with `github.sha`, NOT a short SHA).
Type: CI/image build

Work:

- Commit the focused code and Dockerfile fix.
- Push the branch.
- Wait for the RunPod training image workflow.
- Record the exact SHA image:

```text
ghcr.io/airyder/fincept/quant-foundry-training:<accepted_sha>
```

Acceptance:

- Build workflow succeeds.
- Image tag matches the commit being tested.
- Commit contains only intentional production-fix files and required docs.

### 5. Run a fresh live production-handler canary

Status: **DONE (commit pending — receipt at `reports/runpod-test-runs/6dbec436/`)**
Type: senior/operator live RunPod validation

Endpoint shape:

- GPU: `ADA_24`
- scaler: `QUEUE_DELAY`
- scaler value: `4`
- workers: `workersMin=1`, `workersMax=1`
- idle timeout: `300`
- container disk: `20 GB`
- docker args: empty string
- env: `QUANT_FOUNDRY_CALLBACK_SECRET` only from the operator environment
- registry auth: copied from the known working source endpoint/template

Work:

- Create a fresh endpoint for the exact SHA image.
- Capture redacted endpoint-create output.
- Poll `/health` before dispatch.
- Dispatch a `callback_secret_canary` job.
- Poll `/status` and `/health` every 5 seconds until terminal status or timeout.
- Capture `/health` after completion.
- Scale the endpoint down and record cleanup.

Acceptance:

- Job reaches `COMPLETED`.
- Job does not stay `IN_QUEUE`.
- Worker remains `unhealthy=0` after completion.
- Callback signature is present but secrets are not printed.
- Debug endpoint is scaled down or deleted after the test.

### 6. Repeat canary for stability if the first production canary passes

Status: **DONE** — 3/3 canaries COMPLETED (44ms, 43ms, 50ms executionTime),
same worker ID `goi504hgln2q6x`, `unhealthy=0` throughout.
Type: live stability confirmation

Work:

- Run 2 to 3 additional canary jobs against the same exact SHA and endpoint
  shape.
- Keep the same redaction and cleanup discipline.

Acceptance:

- All repeated canaries reach `COMPLETED`.
- No worker transitions to `unhealthy=1`.
- Receipts show stable job pickup and terminal status.

### 7. If the production canary fails, isolate only the new failing boundary

Status: conditional
Type: failure branch

Trigger:

- The post-`parents[5]` fix production handler still goes `unhealthy=1`, stays
  `IN_QUEUE`, or exits before terminal status.

Work:

- Stop broad experimentation.
- Compare the failed direct-entrypoint receipt against the passing
  `full_handler_call` receipt from Test F.
- Inspect the production handler `__main__` path, preflight behavior, and the
  RunPod SDK `serverless.start({"handler": handler})` boundary.
- Re-run import bisection only if the failure evidence points to a different
  import after the path fix.

Acceptance:

- New hypothesis is tied to the fresh receipt, not to stale Test F assumptions.
- Any new bisection run uses the fixed probe logic from item 9 below.

### 8. Write and consolidate the new receipt bundle

Status: **DONE (uncommitted — receipt at `reports/runpod-test-runs/6dbec436/`,
index updated pass #4)** — bundle contains `interpretation.md`,
`canary-probe.jsonl`, `health-before.json`, `health-after.json`,
`cleanup.json`. Index updated in pass #4. Needs committing.
Type: evidence/documentation

Files:

- `reports/runpod-test-runs/<accepted_sha>/...`
- `docs/runpod-fix-plan/RECEIPT_INDEX.md`
- `docs/runpod-fix-plan/06-swarm-task-queue.md` or this file, if the queue
  needs a status note

Receipt bundle must include:

- accepted branch and commit SHA
- exact image tag
- workflow run id
- endpoint id and redacted endpoint settings
- health before dispatch
- canary `/run` response
- status probe JSONL
- final status JSON
- health after completion
- cleanup receipt
- short interpretation

Acceptance:

- `RECEIPT_INDEX.md` names the new result and updates the proven/disproved
  hypothesis tables.
- Stale queue items are marked done, obsolete, or conditional.
- No raw evidence is edited to match interpretation. Interpretation must follow
  raw evidence.

### 9. Fix the bisection probe false-negative logic before future bisection

Status: **DONE (commit `6dbec436`)**
Type: test tooling fix

File:

- `runpod/quant-foundry-training/run_import_bisection.py`

Known bug:

```python
if job_status == "IN_QUEUE" and workers.get("ready", 0) == 0:
    failure_reason = "worker_died_while_job_in_queue"
```

Work:

- Do not treat `ready=0` alone as worker death.
- Consider a worker dead only with evidence such as `unhealthy > 0`, or
  `ready=0`, `running=0`, and no terminal progress after an appropriate wait.
- Preserve `running=1, unhealthy=0` as an active processing state.

Acceptance:

- The lightgbm Test F shape would no longer be mislabeled as a failure while
  the worker is `running=1`.
- Ruff passes for the script.
- Future bisection summaries derive profile result from final probe evidence.

### 10. Add a receipt-integrity guard

Status: **DONE (commit `6dbec436`)**
Type: regression test

Suggested file:

- `runpod/tests/test_receipt_integrity.py`

Work:

- Add a test that scans receipt bundles with both summary and raw probe/status
  evidence.
- Fail when `summary.json` or `interpretation.md` contradicts terminal raw
  status evidence.
- Do not hardcode the c0f15fa7 values.

Acceptance:

- `uv run pytest runpod/tests/test_receipt_integrity.py -q` passes on the
  corrected receipts.
- The test would have caught the pre-correction Test F false-negative summary.

### 11. Commit or classify the receipt corrections already in the worktree

Status: **DONE (commit `61dca0a4`)**
Type: repo hygiene

The previously uncommitted receipt corrections
(`reports/runpod-test-runs/c0f15fa7/import-bisection/summary.json`,
`interpretation.md`, `reports/runpod-test-runs/d7ba5a2d/test-e-sentinel.md`)
were committed in `61dca0a4` along with the raw bisection evidence for all 12
profiles. The worktree no longer has ambiguous receipt corrections for these
files.

Work (historical, kept for reference):

- Review each diff against raw evidence.
- Commit the valid corrections, or explicitly document why any correction is
  being left uncommitted.
- Keep raw probe/status/health receipts immutable.

Acceptance:

- The worktree no longer has ambiguous receipt corrections.
- Commit message states these are evidence corrections, not product fixes.

### 12. Classify unrelated dirty work before final ship

Status: open
Type: repo hygiene

Currently observed worktree items at consolidation pass #3 (from `git status`):

- `infra/docker/api.Dockerfile` — modified (uncommitted). Unrelated to the
  RunPod fix. Keep separate.
- `SESSION_HANDOFF.md` — untracked.
- `handoffs/2026-07-03_01-51_fix-runpod-training-crash/` — untracked session
  handoff docs (predate the fix; describe the earlier bisect-`handler()`
  approach — now superseded by the `parents[5]` root cause).
- `kimiSuggestionFix.md` — untracked.
- `reports/ci-triage/receipt-20260703T200535Z.md` — untracked CI triage
  receipt (covers `c0f15fa7`; documents pre-existing CI debt, not the RunPod
  fix path).

Note: the previously uncommitted receipt corrections and the
`docs/runpod-fix-plan/` plan docs are now committed (`61dca0a4` / `6dbec436`).
The `.tmp_*.py` scratch scripts are no longer present in the worktree.

Work:

- Do not delete untracked scratch files without operator approval.
- Decide which receipts are durable evidence and which are local scratch.
- Consider adding a narrow `.gitignore` rule for `.tmp_*` only after confirming
  none of those scripts should become durable tools.
- Keep the production RunPod fix commit separate from unrelated API Dockerfile
  changes unless the operator intentionally wants them bundled.

Acceptance:

- Final commit set is understandable and safe to review.
- No secrets or local-only scratch artifacts are staged accidentally.
- `git status --short` noise is either resolved or explicitly documented.

## Dependency Order

```text
1 parents[5] fix
  -> 2 restore production handler mapping
  -> 3 local gates
  -> 4 build/publish exact SHA image
  -> 5 live production canary
  -> 6 repeat canaries if pass
  -> 8 receipt consolidation
  -> final commit/push readiness

9 bisection probe fix
  -> required before any future bisection, not before the immediate retest

10 receipt-integrity guard
  -> independent, useful before final consolidation

11 receipt corrections
12 dirty-work classification
  -> should happen before final ship
```

## Final Done Definition

This lane is done only when:

- `parents[5]` path indexing is fixed without breaking ingestion imports.
- The RunPod training Dockerfile runs the production handler directly.
- A fresh exact-SHA production image completes live canary validation.
- Worker health remains acceptable after the job.
- Debug endpoints are cleaned up.
- Receipts are written and redacted.
- `RECEIPT_INDEX.md` reflects the accepted result.
- The final commit set excludes secrets, scratch files, and unrelated changes.
