# RunPod Fix Plan: Single-Variable Tests

Last updated: 2026-07-03

The goal is not to try random fixes. The goal is to isolate the first failing boundary with exact-SHA evidence.

## Test Rules

- One variable changes at a time.
- Record branch, commit SHA, image tag, endpoint id, and payload.
- Use a fresh endpoint for each image-level test.
- Set `workersMin=1` only while actively testing.
- Scale debug endpoints down after each run.
- Do not print secrets.
- Do not touch inference worker files.
- Do not change UI, product flow, or app design.
- Do not change SDK/base/entrypoint while testing the healthcheck hypothesis.

## Known Failed Control

Control image:

```text
ghcr.io/airyder/fincept/quant-foundry-training:412080c61a38cd138a92d43df8242503d27f71d2
```

Control endpoint:

```text
zbpy7m8s8dps7k
```

Control payload:

```json
{
  "input": {
    "task": "callback_secret_canary",
    "job_id": "qf:diag-layer0:412080c6:001",
    "nonce": "layer0-nonce",
    "diag_layer": 0
  }
}
```

Control interpretation:

- Layer 0 live failure means the first failure is before meaningful handler body work.
- It does not implicate `SecurityPreflight`, canary signing, training validation, or model training as the first failure.

## Test A: Layered Handler, No Docker Healthcheck, Layer 0 Only

Hypothesis:

Removing the Docker healthcheck from the layered training image is sufficient for Layer 0 to complete live. If true, the healthcheck/container lifecycle overlap was the root cause or the dominant trigger.

Exact variable changed:

- Remove the Docker `HEALTHCHECK` or set `HEALTHCHECK NONE`.

Variables that must not change:

- layered handler remains the live worker handler
- production handler remains available as `handler_full.py`
- Docker base image remains the same as the failed layered baseline
- RunPod SDK version remains the same as the failed layered baseline
- entrypoint remains the same as the failed layered baseline
- endpoint template shape remains the known working shape
- payload is Layer 0 only

Files touched:

- Allowed: `runpod/quant-foundry-training/Dockerfile`
- Not allowed: `runpod/quant-foundry-training/handler.py`
- Not allowed: `runpod/quant-foundry-training/handler_layered.py`
- Not allowed: SDK version changes in the Dockerfile
- Not allowed: base image changes
- Not allowed: entrypoint changes

Important reconciliation:

The current local branch at the time this scaffold was written already had no Docker healthcheck and copied the production handler directly. If applying Test A to that branch, first decide whether the test target is:

1. a branch derived from failed SHA `412080c6`, where the only edit is removing the healthcheck; or
2. the current branch, where Test A is no longer an edit and becomes a live validation of the exact current image.

Do not mix those two paths without recording the reason.

Expected image tag:

```text
ghcr.io/airyder/fincept/quant-foundry-training:<new_exact_sha>
```

Endpoint settings:

```text
dockerArgs=""
containerDiskInGb=20
containerRegistryAuthId=<copy from known working endpoint; redact in receipts>
workersMin=1
workersMax=1
idleTimeout=300
scalerType=QUEUE_DELAY
scalerValue=4
gpuIds=ADA_24
```

Payload:

```json
{
  "input": {
    "task": "callback_secret_canary",
    "job_id": "qf:diag-layer0:<short_sha>:001",
    "nonce": "layer0-nonce",
    "diag_layer": 0
  }
}
```

Commands:

```powershell
git status --short --branch
$sha = git rev-parse HEAD
$short = $sha.Substring(0, 8)
$image = "ghcr.io/airyder/fincept/quant-foundry-training:$sha"

uv run ruff check runpod/quant-foundry-training scripts
gh run list --workflow build-runpod-training.yml --limit 5
```

Create endpoint and probe using the commands in `01-validation-baseline.md`.

Success criteria:

- endpoint reaches `ready=1` or `idle=1`
- `workers.unhealthy=0` before dispatch
- `/run` returns a job id
- `/status/{job_id}` reaches `COMPLETED`
- result contains `diag_layer=0`
- job does not remain `IN_QUEUE`
- worker remains healthy after completion

Failure interpretation:

- If Layer 0 still stays `IN_QUEUE` or pod exits, stop blaming preflight, canary signing, model validation, and training logic.
- Pivot to SDK job loop, process exit behavior, entrypoint/serverless loader behavior, or RunPod platform scheduling.
- Do not jump to SDK/base image churn until the receipt proves Layer 0 still fails with healthcheck removed.

Next action:

- If pass: run Test B on the same endpoint/image.
- If fail: run Test D or Test E, depending on whether the pod exits before any handler log appears.

Rollback plan:

- Revert only the Dockerfile healthcheck removal commit if it proves unrelated and the product owner wants the old healthcheck restored.
- Scale down the debug endpoint.
- Keep receipts before reverting.

## Test B: Same Image, Layers 1 Through 5

Hypothesis:

If Layer 0 passes without the healthcheck, the next failing layer identifies the first meaningful handler boundary.

Exact variable changed:

- Only the payload `diag_layer` changes from 1 to 5.

Files touched:

- None.

Expected image tag:

```text
ghcr.io/airyder/fincept/quant-foundry-training:<same_sha_as_Test_A>
```

Endpoint settings:

- Same endpoint/image as Test A if the worker remains healthy.
- Create a fresh endpoint only if the Test A endpoint health changes unexpectedly.

Payload template:

```json
{
  "input": {
    "task": "callback_secret_canary",
    "job_id": "qf:diag-layer<layer>:<short_sha>:001",
    "nonce": "layer<layer>-nonce",
    "diag_layer": <layer>
  }
}
```

Commands:

```powershell
$endpoint = "<endpoint-id>"
$sha = git rev-parse HEAD
$short = $sha.Substring(0, 8)
$image = "ghcr.io/airyder/fincept/quant-foundry-training:$sha"

foreach ($layer in 1..5) {
  $payload = @{
    input = @{
      task = "callback_secret_canary"
      job_id = "qf:diag-layer$layer:$short:001"
      nonce = "layer$layer-nonce"
      diag_layer = $layer
    }
  } | ConvertTo-Json -Compress

  uv run python scripts/runpod_smoke_probe.py `
    --endpoint-id $endpoint `
    --image-tag $image `
    --interval 5 `
    --timeout 240 `
    --payload-json $payload
  if ($LASTEXITCODE -ne 0) { throw "live layer $layer failed; stop and interpret receipts" }
}
```

Success criteria:

- Every layer reaches `COMPLETED`.
- Worker remains healthy after each layer.
- First meaningful failure, if any, is isolated to one layer with receipts.

Failure interpretation:

- Layer 1 failure: event/input parsing or SDK result serialization boundary.
- Layer 2 failure: canary signing path without preflight.
- Layer 3 failure: `SecurityPreflight`.
- Layer 4 failure: preflight plus canary interaction.
- Layer 5 failure: full production handler path.

Next action:

- If all pass: run Test C.
- If a layer fails: open a narrow bug task for that layer only.

Rollback plan:

- No code rollback; this is a payload-only probe.
- Scale down endpoint after collecting receipts.

## Test C: Full Canary Path On Accepted Image

Hypothesis:

The no-healthcheck image that passes layers 0 through 5 can complete the normal callback-secret canary path.

Exact variable changed:

- Payload no longer sets `diag_layer`, or uses the production handler image if the sequence has moved back from diagnostic to production.

Files touched:

- None.

Expected image tag:

```text
ghcr.io/airyder/fincept/quant-foundry-training:<accepted_sha>
```

Payload:

```json
{
  "input": {
    "task": "callback_secret_canary",
    "job_id": "qf:canary:<short_sha>:001",
    "nonce": "canary-nonce"
  }
}
```

Success criteria:

- `/status/{job_id}` reaches `COMPLETED`.
- Output has expected canary fields.
- Callback payload verifies locally without printing the secret.
- Worker remains healthy after completion.

Failure interpretation:

- If layers passed but full canary fails, the failure is in full handler composition, response size/serialization, callback signing output, or production preflight mode.

Next action:

- Open a narrow handler/canary task with exact failing output.

Rollback plan:

- No code rollback from this test.
- Scale endpoint down.

## Test D: Force Layer 0 From Env

Run only if Test A fails and it is unclear whether payload parsing/dispatch reaches the handler.

Hypothesis:

If `QF_DIAG_LAYER=0` is set in the endpoint environment, the layered handler can return before consulting payload `diag_layer`. This helps distinguish payload parse issues from earlier SDK/job-loop/container issues.

Exact variable changed:

- Endpoint env adds `QF_DIAG_LAYER=0`.

Files touched:

- None.

Expected image tag:

```text
ghcr.io/airyder/fincept/quant-foundry-training:<same_sha_as_Test_A>
```

Payload:

```json
{
  "input": {
    "task": "callback_secret_canary",
    "job_id": "qf:diag-env-layer0:<short_sha>:001",
    "nonce": "env-layer0-nonce"
  }
}
```

Success criteria:

- Job reaches `COMPLETED`.
- Result shows `diag_layer=0` and `diag_layer_source=env`.

Failure interpretation:

- If this still fails, the failure is before handler layer resolution.
- Focus on RunPod SDK start loop, process lifetime, endpoint lifecycle, entrypoint, or platform scheduling.

Next action:

- Run Test E.

Rollback plan:

- Delete or scale down the env-forced endpoint.
- Do not keep `QF_DIAG_LAYER=0` on any reusable endpoint.

## Test E: SDK Job Loop Sentinel Inside Training Image

Run only if Test A and Test D fail.

Hypothesis:

The failure is caused by the RunPod SDK job loop, entrypoint, or process lifetime in the training image, not by the layered handler body.

Exact variable changed:

- Replace only the handler copied to `/worker/handler.py` with a tiny sentinel handler inside the same training image shape.

Files touched:

- Allowed: `runpod/quant-foundry-training/Dockerfile`
- Allowed: `runpod/quant-foundry-training/handler_minimal.py` if it already exists and is suitable
- Not allowed: base image changes
- Not allowed: SDK version changes
- Not allowed: endpoint shape changes

Expected image tag:

```text
ghcr.io/airyder/fincept/quant-foundry-training:<sentinel_sha>
```

Payload:

```json
{
  "input": {
    "task": "sentinel",
    "job_id": "qf:sentinel:<short_sha>:001"
  }
}
```

Success criteria:

- Sentinel job reaches `COMPLETED`.
- Worker remains healthy.

Failure interpretation:

- If sentinel fails in the training image but the smoke worker succeeds, focus on image/runtime/entrypoint/container lifecycle.
- If sentinel succeeds, compare sentinel startup to layered startup and isolate the next import/process boundary.

Next action:

- Open a narrow SDK/entrypoint task with receipts.

Rollback plan:

- Revert the sentinel Dockerfile commit after receipts are captured.
- Scale endpoint down.

## Optional Test F: Healthcheck Confirmation

Run only after Test A passes and the team wants a proof-positive root-cause receipt.

Hypothesis:

Reintroducing the import-based Docker healthcheck to the otherwise passing image makes Layer 0 fail again.

Exact variable changed:

- Reintroduce only the old import-based Docker healthcheck.

Files touched:

- Allowed: `runpod/quant-foundry-training/Dockerfile`
- Not allowed: handler/base/SDK/entrypoint changes

Success criteria:

- Layer 0 fails in the same way as the original failed control.

Failure interpretation:

- If reintroduced healthcheck does not fail, the original fix may have been caused by another hidden variable. Re-open the evidence chain.

Next action:

- Usually skip this test unless the team needs a strong postmortem receipt.

Rollback plan:

- Revert the temporary healthcheck reintroduction immediately.
- Scale endpoint down.

