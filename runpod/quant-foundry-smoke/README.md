# Quant Foundry RunPod Smoke Worker

This worker exists only to split RunPod platform/job-pickup failures from the
Quant Foundry training import tree.

It intentionally has:

- no `quant_foundry`, `fincept_core`, NumPy, pandas, sklearn, LightGBM,
  XGBoost, CatBoost, PyArrow, or Torch imports
- no Docker healthcheck
- no custom preflight, callback signing, or entrypoint wrapper
- one runtime dependency: `runpod==1.7.13`

If this worker cannot complete a single job on a brand-new endpoint, the
blocker is outside the training handler. If it completes reliably, add the
training imports back one layer at a time in a separate test image.

## Build and Push

Run from the repo root:

```pwsh
$sha = git rev-parse HEAD
docker build `
  -f runpod/quant-foundry-smoke/Dockerfile `
  -t "ghcr.io/airyder/fincept/quant-foundry-smoke:$sha" `
  --build-arg "GIT_SHA=$sha" .
docker push "ghcr.io/airyder/fincept/quant-foundry-smoke:$sha"
```

Use the exact pushed tag when creating the RunPod endpoint. Do not deploy
`latest` for this isolation pass.

## Create Endpoint

If the image is private in GHCR, copy registry auth from an existing endpoint
that can already pull Fincept images. This script only creates a new endpoint;
it does not update, delete, or recycle existing endpoints.

```pwsh
$env:RUNPOD_API_KEY = "<redacted>"
$sha = git rev-parse HEAD
$sourceEndpoint = "<existing-endpoint-with-ghcr-auth>"
python scripts/runpod_create_smoke_endpoint.py `
  --image-tag "ghcr.io/airyder/fincept/quant-foundry-smoke:$sha" `
  --copy-registry-auth-from-endpoint-id $sourceEndpoint `
  --wait-health
```

For the exact GitHub Actions image from a completed run, replace `$sha` with
the full commit SHA printed by `git rev-parse HEAD` for that build.

## RunPod Probe

Create a brand-new RunPod serverless endpoint with the pushed smoke image, then
run:

```pwsh
$env:RUNPOD_API_KEY = "<redacted>"
$endpoint = "<new-endpoint-id>"
$sha = git rev-parse HEAD
python scripts/runpod_smoke_probe.py `
  --endpoint-id $endpoint `
  --image-tag "ghcr.io/airyder/fincept/quant-foundry-smoke:$sha" `
  --interval 5 `
  --timeout 180
```

The probe prints JSONL receipts for:

- git SHA and expected image tag
- `/health` before dispatch
- `/run` response and RunPod job id
- `/status/{job_id}` every interval
- `/health` every interval
- final terminal status

Never put credentials or secrets inside the smoke payload. The handler echoes
the received event by design.

## Decision Rule

| Smoke result | Meaning |
| --- | --- |
| Completes on a new endpoint | The production crash is in the training handler, its imports, or its preflight path. |
| Crashes on a new endpoint | The blocker is image, SDK, account, endpoint config, RunPod platform, or job-pickup lifecycle. |
| Works on a new endpoint but not an old one | The old endpoint is polluted, throttled, or misconfigured. |
