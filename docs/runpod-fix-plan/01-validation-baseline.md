# RunPod Fix Plan: Validation Baseline

Last updated: 2026-07-03

This baseline is the repeatable loop every agent should run before changing variables. Use Windows PowerShell from the repo root unless a command explicitly says otherwise.

## Preconditions

Required local tools:

- `git`
- `uv`
- `gh`
- PowerShell 7 or Windows PowerShell

Required environment variables for live RunPod work:

- `RUNPOD_API_KEY`
- `QUANT_FOUNDRY_CALLBACK_SECRET`
- `RUNPOD_REGISTRY_AUTH_SOURCE_ENDPOINT_ID` or an explicit source endpoint id

Never echo secret values. Confirm presence only:

```powershell
if (-not $env:RUNPOD_API_KEY) { throw "RUNPOD_API_KEY is not set" }
if (-not $env:QUANT_FOUNDRY_CALLBACK_SECRET) { throw "QUANT_FOUNDRY_CALLBACK_SECRET is not set" }
if (-not $env:RUNPOD_REGISTRY_AUTH_SOURCE_ENDPOINT_ID) {
  Write-Warning "Set RUNPOD_REGISTRY_AUTH_SOURCE_ENDPOINT_ID or pass --copy-registry-auth-from-endpoint-id explicitly"
}
```

## 1. Record Repo State

```powershell
git status --short --branch
$sha = git rev-parse HEAD
$short = $sha.Substring(0, 8)
$sha
```

Receipt:

- branch line from `git status --short --branch`
- exact SHA
- note any unrelated dirty or untracked files

Do not modify unrelated scratch files.

## 2. Reconcile The Target Dockerfile

The known failed layered build was:

```text
412080c61a38cd138a92d43df8242503d27f71d2
```

Inspect the failed Dockerfile shape:

```powershell
git show 412080c61a38cd138a92d43df8242503d27f71d2:runpod/quant-foundry-training/Dockerfile |
  Select-String -Pattern "FROM|handler_layered|handler_full|runpod==|HEALTHCHECK|ENTRYPOINT" -Context 2
```

Inspect the current working tree:

```powershell
Select-String -Path runpod/quant-foundry-training/Dockerfile `
  -Pattern "FROM|handler_layered|handler_full|runpod==|HEALTHCHECK|ENTRYPOINT" `
  -Context 2
```

Expected for the first single-variable test:

- layered handler is still the worker handler
- production handler is still available as `handler_full.py`
- base image is unchanged from the failed layered baseline
- RunPod SDK version is unchanged from the failed layered baseline
- entrypoint is unchanged from the failed layered baseline
- only Docker healthcheck is removed or set to `HEALTHCHECK NONE`

If the current branch already has no Docker healthcheck, record that fact and move to exact-SHA validation instead of editing.

## 3. Run Local Handler Tests

Diagnostic handler default return-only mode:

```powershell
$payload = @{
  input = @{
    task = "callback_secret_canary"
    job_id = "local-diagnostic-return-only"
    nonce = "n"
  }
} | ConvertTo-Json -Compress

uv run python scripts/runpod_training_handler_local_test.py `
  --handler runpod/quant-foundry-training/handler_diagnostic.py `
  --payload-json $payload
if ($LASTEXITCODE -ne 0) { throw "diagnostic return-only local test failed" }
```

Diagnostic handler full mode:

```powershell
$oldMode = $env:QUANT_FOUNDRY_DIAGNOSTIC_HANDLER_MODE
$env:QUANT_FOUNDRY_DIAGNOSTIC_HANDLER_MODE = "full"
try {
  $payload = @{
    input = @{
      task = "callback_secret_canary"
      job_id = "local-diagnostic-full"
      nonce = "n"
    }
  } | ConvertTo-Json -Compress

  uv run python scripts/runpod_training_handler_local_test.py `
    --handler runpod/quant-foundry-training/handler_diagnostic.py `
    --payload-json $payload
  if ($LASTEXITCODE -ne 0) { throw "diagnostic full local test failed" }
}
finally {
  if ($null -eq $oldMode) { Remove-Item Env:QUANT_FOUNDRY_DIAGNOSTIC_HANDLER_MODE -ErrorAction SilentlyContinue }
  else { $env:QUANT_FOUNDRY_DIAGNOSTIC_HANDLER_MODE = $oldMode }
}
```

Production handler canary:

```powershell
$payload = @{
  input = @{
    task = "callback_secret_canary"
    job_id = "local-production-canary"
    nonce = "n"
  }
} | ConvertTo-Json -Compress

uv run python scripts/runpod_training_handler_local_test.py `
  --handler runpod/quant-foundry-training/handler.py `
  --payload-json $payload
if ($LASTEXITCODE -ne 0) { throw "production canary local test failed" }
```

Layered handler layers 0 through 5:

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
  if ($LASTEXITCODE -ne 0) { throw "layer $layer local test failed" }
}
```

Receipt:

- command transcript
- exit code for each run
- JSON result redacted if it includes callback signatures

## 4. Run Static Checks

```powershell
uv run ruff check runpod/quant-foundry-training scripts
if ($LASTEXITCODE -ne 0) { throw "ruff failed" }
```

If a later task edits Markdown only, this command is still useful as a baseline receipt but not proof of live RunPod behavior.

## 5. Check Image Build Workflow Status

List recent training image workflow runs:

```powershell
gh run list --workflow build-runpod-training.yml --limit 5
```

View a specific run:

```powershell
$runId = "<workflow-run-id>"
gh run view $runId
gh run view $runId --log-failed
```

Watch a build after pushing:

```powershell
gh run watch $runId --exit-status
```

Expected image tag:

```powershell
$sha = git rev-parse HEAD
$trainingImage = "ghcr.io/airyder/fincept/quant-foundry-training:$sha"
$trainingImage
```

Receipt:

- workflow run id
- commit SHA
- image tag
- success or failure
- failed log excerpt if not successful

## 6. Create A Fresh Debug Endpoint

Use the known working template shape:

- `dockerArgs=""`
- `containerDiskInGb=20`
- valid `containerRegistryAuthId` copied from an existing working endpoint
- `workersMin=1` only while testing
- `workersMax=1` while testing
- `idleTimeout=300`
- `scalerType=QUEUE_DELAY`
- `scalerValue=4`
- `gpuIds=ADA_24`

The helper is named `runpod_create_smoke_endpoint.py`, but it creates an endpoint from any supplied image tag.

Dry-run first:

```powershell
$sha = git rev-parse HEAD
$short = $sha.Substring(0, 8)
$trainingImage = "ghcr.io/airyder/fincept/quant-foundry-training:$sha"

uv run python scripts/runpod_create_smoke_endpoint.py `
  --dry-run `
  --image-tag $trainingImage `
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
  --env "QUANT_FOUNDRY_CALLBACK_SECRET=$env:QUANT_FOUNDRY_CALLBACK_SECRET"
```

Create live endpoint:

```powershell
uv run python scripts/runpod_create_smoke_endpoint.py `
  --image-tag $trainingImage `
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

Receipt:

- endpoint id
- endpoint name
- redacted template input
- first healthy `/health` response

## 7. Probe Endpoint Health

```powershell
$endpoint = "<endpoint-id>"
Invoke-RestMethod `
  -Uri "https://api.runpod.ai/v2/$endpoint/health" `
  -Headers @{ Authorization = "Bearer $env:RUNPOD_API_KEY" } |
  ConvertTo-Json -Depth 8
```

Expected before dispatch:

- `workers.ready >= 1` or `workers.idle >= 1`
- `workers.unhealthy = 0`
- no unexpected queued jobs

## 8. Run Layer Probe

Layer 0 payload:

```powershell
$endpoint = "<endpoint-id>"
$sha = git rev-parse HEAD
$short = $sha.Substring(0, 8)
$trainingImage = "ghcr.io/airyder/fincept/quant-foundry-training:$sha"
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
  --image-tag $trainingImage `
  --interval 5 `
  --timeout 180 `
  --payload-json $payload
```

Receipt:

- `probe_start`
- `health_before`
- `run_response` with job id
- every `status`
- every `health`
- final `probe_end`, `probe_timeout`, or `probe_error`

## 9. Check A Job Status Manually

```powershell
$endpoint = "<endpoint-id>"
$jobId = "<job-id>"
Invoke-RestMethod `
  -Uri "https://api.runpod.ai/v2/$endpoint/status/$jobId" `
  -Headers @{ Authorization = "Bearer $env:RUNPOD_API_KEY" } |
  ConvertTo-Json -Depth 12
```

Terminal statuses count as terminal evidence:

- `COMPLETED`
- `FAILED`
- `CANCELLED`
- `TIMED_OUT`

`IN_QUEUE` after timeout is failure evidence.

## 10. Cancel A Stuck Job

Use this if a job remains queued or running after the probe timeout:

```powershell
$endpoint = "<endpoint-id>"
$jobId = "<job-id>"
Invoke-RestMethod `
  -Method Post `
  -Uri "https://api.runpod.ai/v2/$endpoint/cancel/$jobId" `
  -Headers @{ Authorization = "Bearer $env:RUNPOD_API_KEY" } |
  ConvertTo-Json -Depth 8
```

Then verify:

```powershell
Invoke-RestMethod `
  -Uri "https://api.runpod.ai/v2/$endpoint/status/$jobId" `
  -Headers @{ Authorization = "Bearer $env:RUNPOD_API_KEY" } |
  ConvertTo-Json -Depth 12
```

If the cancel endpoint behavior differs, capture the HTTP response and update this plan before retrying.

## 11. Scale Debug Endpoints Down

Never leave debug endpoints warm after a test.

Scale to zero:

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
```

Verify:

```powershell
Invoke-RestMethod `
  -Uri "https://api.runpod.ai/v2/$endpoint/health" `
  -Headers @{ Authorization = "Bearer $env:RUNPOD_API_KEY" } |
  ConvertTo-Json -Depth 8
```

Expected:

- `workersMin=0` in endpoint settings, if available
- health shows no warm debug worker after scale-down settles
- no new queued jobs

## 12. Receipt Directory

Store receipts outside source code changes unless explicitly asked to commit them:

```powershell
$sha = git rev-parse HEAD
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$receiptDir = "reports/runpod-test-runs/$sha/$stamp"
New-Item -ItemType Directory -Force $receiptDir | Out-Null
$receiptDir
```

Suggested files:

- `repo-state.txt`
- `local-tests.txt`
- `ruff.txt`
- `workflow.txt`
- `endpoint-create-redacted.json`
- `health-before.json`
- `layer0-probe.jsonl`
- `cleanup.json`
- `interpretation.md`

