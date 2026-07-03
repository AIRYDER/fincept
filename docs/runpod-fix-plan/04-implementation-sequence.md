# RunPod Fix Plan: Implementation Sequence

Last updated: 2026-07-03

This sequence is intentionally narrow. It preserves the current product design and isolates the first runtime failure.

## Step 1: Confirm Clean Repo And Exact Current SHA

Commands:

```powershell
git status --short --branch
$sha = git rev-parse HEAD
$short = $sha.Substring(0, 8)
$sha
```

Acceptance:

- Branch and SHA are recorded.
- Dirty files are understood.
- Unrelated user files are left untouched.

Stop condition:

- If the worktree has conflicting edits to `runpod/quant-foundry-training/Dockerfile`, stop and ask the operator which branch/SHA is the target.

## Step 2: Confirm Local Layer Tests Still Pass

Commands:

```powershell
foreach ($layer in 0..5) {
  $payload = @{
    input = @{
      task = "callback_secret_canary"
      job_id = "local-layer$layer"
      nonce = "n"
      diag_layer = $layer
    }
  } | ConvertTo-Json -Compress

  uv run python scripts/runpod_training_handler_local_test.py `
    --handler runpod/quant-foundry-training/handler_layered.py `
    --payload-json $payload
  if ($LASTEXITCODE -ne 0) { throw "local layer $layer failed" }
}
```

Acceptance:

- Layers 0 through 5 return locally.
- Results are JSON-serializable.

Stop condition:

- If a local layer fails, fix the local layer/harness issue before any live endpoint test.

## Step 3: Apply No-Healthcheck-Only Change

Apply only if the target Dockerfile still contains the import-based Docker healthcheck.

Allowed change:

```dockerfile
HEALTHCHECK NONE
```

or complete removal of the Docker `HEALTHCHECK` stanza.

Not allowed in this step:

- base image change
- SDK version change
- entrypoint change
- handler logic change
- endpoint template change
- README rewrite

Commands:

```powershell
Select-String -Path runpod/quant-foundry-training/Dockerfile `
  -Pattern "HEALTHCHECK|handler_layered|handler_full|ENTRYPOINT" `
  -Context 3
git diff -- runpod/quant-foundry-training/Dockerfile
uv run ruff check runpod/quant-foundry-training scripts
```

Acceptance:

- Diff is Dockerfile-only.
- Docker healthcheck is gone.
- Layered handler mapping remains unchanged.
- Base image, SDK, and entrypoint remain unchanged.

Current-branch caveat:

- If the current branch already has no healthcheck and no layered mapping, do not manufacture a code change. Record the branch drift and decide whether to test the current image or branch from the failed layered commit.

## Step 4: Commit With Precise Message

Only commit after Step 3 produced the intended narrow diff.

Suggested commit message:

```text
fix(runpod): remove training worker docker healthcheck

Keep the layered diagnostic handler, base image, SDK version, and entrypoint
unchanged so the next RunPod test isolates the healthcheck as the only
image-level variable.
```

Commands:

```powershell
git diff --check
git status --short
git add runpod/quant-foundry-training/Dockerfile
git commit -m "fix(runpod): remove training worker docker healthcheck"
```

Acceptance:

- Commit contains only the intended Dockerfile change.

Stop condition:

- If hooks fail, fix the underlying issue. Do not bypass hooks.

## Step 5: Push And Wait For Image Build

Commands:

```powershell
git push
gh run list --workflow build-runpod-training.yml --limit 5
$runId = "<workflow-run-id>"
gh run watch $runId --exit-status
gh run view $runId
```

Expected image:

```powershell
$sha = git rev-parse HEAD
$image = "ghcr.io/airyder/fincept/quant-foundry-training:$sha"
$image
```

Acceptance:

- Workflow succeeds.
- Exact SHA image is published.

Stop condition:

- If the image build fails, inspect `gh run view $runId --log-failed` and fix only the build failure.

## Step 6: Create Fresh Endpoint With Correct Shape

Commands:

```powershell
$sha = git rev-parse HEAD
$short = $sha.Substring(0, 8)
$image = "ghcr.io/airyder/fincept/quant-foundry-training:$sha"

uv run python scripts/runpod_create_smoke_endpoint.py `
  --image-tag $image `
  --name "qf-training-layered-$short" `
  --template-name "qf-training-layered-$short-template" `
  --copy-registry-auth-from-endpoint-id $env:RUNPOD_REGISTRY_AUTH_SOURCE_ENDPOINT_ID `
  --workers-min 1 `
  --workers-max 1 `
  --container-disk-gb 20 `
  --docker-args "" `
  --idle-timeout 300 `
  --scaler-type QUEUE_DELAY `
  --scaler-value 4 `
  --gpu-ids ADA_24 `
  --wait-health `
  --wait-timeout 600 `
  --wait-interval 10 `
  --env "QUANT_FOUNDRY_CALLBACK_SECRET=$env:QUANT_FOUNDRY_CALLBACK_SECRET"
```

Acceptance:

- Fresh endpoint id is recorded.
- Endpoint references exact SHA image.
- Endpoint becomes ready/idle.

Stop condition:

- If endpoint never becomes healthy, capture health output and do not run Layer 0.

## Step 7: Warm One Worker

Command:

```powershell
$endpoint = "<endpoint-id>"
Invoke-RestMethod `
  -Uri "https://api.runpod.ai/v2/$endpoint/health" `
  -Headers @{ Authorization = "Bearer $env:RUNPOD_API_KEY" } |
  ConvertTo-Json -Depth 8
```

Acceptance:

- `workers.ready` or `workers.idle` is at least 1.
- `workers.unhealthy` is 0.
- No unexpected queue backlog exists.

## Step 8: Run Layer 0 Only

Commands:

```powershell
$endpoint = "<endpoint-id>"
$sha = git rev-parse HEAD
$short = $sha.Substring(0, 8)
$image = "ghcr.io/airyder/fincept/quant-foundry-training:$sha"
$payload = @{
  input = @{
    task = "callback_secret_canary"
    job_id = "qf:diag-layer0:$short:001"
    nonce = "layer0-nonce"
    diag_layer = 0
  }
} | ConvertTo-Json -Compress

uv run python scripts/runpod_smoke_probe.py `
  --endpoint-id $endpoint `
  --image-tag $image `
  --interval 5 `
  --timeout 180 `
  --payload-json $payload
```

Acceptance:

- `/run` returns a job id.
- `/status/{job_id}` reaches `COMPLETED`.
- Result identifies `diag_layer=0`.
- Worker remains healthy.

Stop condition:

- If Layer 0 fails, do not run layers 1 through 5.

## Step 9: Interpret Result

If Layer 0 passes:

- The healthcheck was the likely root cause or dominant trigger.
- Keep the no-healthcheck path.
- Proceed to layers 1 through 5 on the same image.

If Layer 0 fails:

- Stop blaming preflight/canary/training logic.
- Pivot to SDK job loop, process exit behavior, entrypoint/serverless loader behavior, or RunPod scheduling.
- Preserve receipts before any new experiment.

## Step 10: If Layer 0 Passes, Proceed To Layers 1 Through 5

Commands:

```powershell
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
  if ($LASTEXITCODE -ne 0) { throw "layer $layer failed; stop and interpret" }
}
```

Acceptance:

- All layers complete, or the first failing layer is isolated with receipts.

## Step 11: If Layer 0 Fails, Pivot

First pivot candidates:

1. Force `QF_DIAG_LAYER=0` from endpoint env to bypass payload layer selection.
2. Replace only handler mapping with a tiny sentinel inside the same training image shape.
3. Compare process logs, startup logs, and RunPod SDK startup behavior against the smoke worker.

Do not pivot to SDK/base/entrypoint churn until a receipt shows Layer 0 fails with no Docker healthcheck.

## Step 12: Scale Down Debug Endpoints And Archive Receipts

Commands:

```powershell
$endpoint = "<endpoint-id>"
Invoke-RestMethod `
  -Method Post `
  -Uri "https://rest.runpod.io/v1/endpoints/$endpoint/update" `
  -Headers @{
    Authorization = "Bearer $env:RUNPOD_API_KEY"
    "Content-Type" = "application/json"
  } `
  -Body (@{ workersMin = 0; workersMax = 0 } | ConvertTo-Json -Compress) |
  ConvertTo-Json -Depth 8

Invoke-RestMethod `
  -Uri "https://api.runpod.ai/v2/$endpoint/health" `
  -Headers @{ Authorization = "Bearer $env:RUNPOD_API_KEY" } |
  ConvertTo-Json -Depth 8
```

Acceptance:

- Endpoint is scaled down.
- Receipts are stored.
- Final interpretation names the exact next task or acceptance result.

