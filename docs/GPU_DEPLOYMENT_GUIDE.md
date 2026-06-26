# GPU Deployment & Testing Guide — RunPod Quant Foundry

**Date:** 2026-06-25
**Scope:** How to build, deploy, test, and operate the Quant Foundry RunPod GPU workers (training + shadow inference).
**Audience:** The single operator running Fincept Terminal.

---

## Deployed Endpoints (Live)

| Endpoint | ID | Template ID | Network Volume |
| --- | --- | --- | --- |
| Training | `h2blqodcicxqyy` | `me58r5vdrp` | `rrsd005i3g` (10GB, US-NC-1) |
| Inference | `t31u1z426jy1ub` | `wnasp3v5jn` | `rrsd005i3g` (10GB, US-NC-1) |

**Base image:** `runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04` (Python 3.11, CUDA 12.4)
**Network volume mount:** `/runpod-volume` (serverless) / `/workspace` (pods)
**Code location on volume:** `/runpod-volume/fincept-terminal/` (git clone of `codex/portfolio-optimizer-core` branch)
**Python libs on volume:** `/runpod-volume/python-libs/` (pydantic, httpx, runpod SDK)
**Start scripts:** `/runpod-volume/start-training.sh`, `/runpod-volume/start-inference.sh`

**Key deployment fix:** The `runpod/pytorch` base image has an ENTRYPOINT that starts nginx/ssh. For serverless workers, this must be overridden with `dockerEntrypoint: []` (empty array) in the template, otherwise the handler never starts and the worker goes `unhealthy`.

---

## Table of Contents

1. [Architecture overview](#1-architecture-overview)
2. [Prerequisites](#2-prerequisites)
3. [Environment variables (complete reference)](#3-environment-variables-complete-reference)
4. [Step 1 — Local testing (no GPU, no Docker)](#step-1--local-testing-no-gpu-no-docker)
5. [Step 2 — Docker build and local container test](#step-2--docker-build-and-local-container-test)
6. [Step 3 — RunPod account setup and endpoint creation](#step-3--runpod-account-setup-and-endpoint-creation)
7. [Step 4 — Deploy the training worker to RunPod](#step-4--deploy-the-training-worker-to-runpod)
8. [Step 5 — Deploy the inference worker to RunPod](#step-5--deploy-the-inference-worker-to-runpod)
9. [Step 6 — Configure Fincept for RunPod mode](#step-6--configure-fincept-for-runpod-mode)
10. [Step 7 — Dispatch a real training job](#step-7--dispatch-a-real-training-job)
11. [Step 8 — Receive the callback](#step-8--receive-the-callback)
12. [Step 9 — Dispatch a real shadow inference job](#step-9--dispatch-a-real-shadow-inference-job)
13. [Step 10 — Verify the full loop](#step-10--verify-the-full-loop)
14. [Testing commands (complete reference)](#testing-commands-complete-reference)
15. [API endpoints (complete reference)](#api-endpoints-complete-reference)
16. [Troubleshooting](#troubleshooting)
17. [Security invariants (non-negotiable)](#security-invariants-non-negotiable)
18. [Rollback procedure](#rollback-procedure)

---

## 1. Architecture overview

```
FINCEPT (trusted, your machine or AWS)
  Operator ──JWT──▶ FastAPI ──▶ QuantFoundryGateway
  Gateway ──HTTP──▶ HttpRunPodClient ──▶ RunPod API (/run)
                                          │
                                          │ RunPod assigns GPU, runs container
                                          ▼
RUNPOD (untrusted, GPU cloud)
  quant-foundry-training container
    handler.py ──▶ RunPodTrainingHandler ──▶ LocalTrainer
    Returns: signed RunPodCallbackEnvelope (HMAC)
                                          │
                                          │ RunPod calls back to Fincept
                                          ▼
  Fincept: POST /quant-foundry/callbacks/runpod
    Verifies HMAC signature ──▶ records in inbox ──▶ processes
```

**Two containers:**
- **Training worker** (`runpod/quant-foundry-training/`) — receives a
  `RunPodTrainingRequest`, trains a model, returns a signed callback with
  the `ArtifactManifest` + `ModelDossier`.
- **Inference worker** (`runpod/quant-foundry-inference/`) — receives a
  `RunPodInferenceRequest` + `FeatureSnapshot`, runs shadow inference,
  returns a signed callback with `ShadowPrediction` batch.

**The only crossing point** between RunPod and Fincept is the HMAC-signed
callback to `POST /quant-foundry/callbacks/runpod`. RunPod cannot write to
anything on the Fincept side except this endpoint.

---

## 2. Prerequisites

### Software

| Tool | Version | Purpose | Install |
|------|---------|---------|---------|
| Python | >= 3.12 | Run Fincept + tests | `python --version` |
| uv | latest | Python package manager | `pip install uv` |
| Docker | latest | Build RunPod containers | [docker.com](https://docker.com) |
| Docker Desktop | running | Container builds on Windows | Start Docker Desktop |
| pnpm | latest | Dashboard workspace | `npm install -g pnpm` |
| Git | latest | Repo + build args | `git --version` |

### Accounts

| Account | Purpose | URL |
|---------|---------|-----|
| RunPod | GPU compute | [runpod.io](https://runpod.io) |
| (optional) AWS S3 | Artifact storage | [aws.amazon.com](https://aws.amazon.com) |

### Verify your environment

```powershell
# Check Python
python --version          # must be >= 3.12

# Check uv
uv --version

# Check Docker
docker --version
docker info               # must show "Server Version: ..."

# Check git
git --version

# Check you're in the right repo
git rev-parse --show-toplevel    # should be the fincept-terminal path
git branch --show-current        # current branch
```

### Sync dependencies

```powershell
# From the repo root:
uv sync

# Verify quant_foundry is installed:
uv run --package quant-foundry python -c "import quant_foundry; print('OK')"
```

---

## 3. Environment variables (complete reference)

### Fincept-side (gateway config)

| Variable | Default | Required? | Description |
|----------|---------|-----------|-------------|
| `QUANT_FOUNDRY_ENABLED` | `false` | **Yes** (set to `true`) | Master switch. When `false`, all QF endpoints return disabled state. |
| `QUANT_FOUNDRY_MODE` | `local_mock` | **Yes** (set to `runpod`) | `local_mock` = mock dispatcher (no GPU). `runpod` = real HTTP dispatch to RunPod. |
| `QUANT_FOUNDRY_SHADOW_ONLY` | `true` | Recommended | When `true`, gateway never writes to `sig.predict` or any trading stream. |
| `QUANT_FOUNDRY_CALLBACK_SECRET` | `""` | **Yes** | HMAC secret for signing/verifying callbacks. Must match the RunPod container's secret. Generate with: `python -c "import secrets; print(secrets.token_hex(32))"` |
| `QUANT_FOUNDRY_BASE_DIR` | `reports/quant-foundry` | No | Base directory for outbox, inbox, budget, dossier registry, shadow ledger (JSONL files). |
| `QUANT_FOUNDRY_ALLOW_PAPER_BRIDGE` | unset | **No** (leave unset) | Only set to `true` after a model is promoted and you want paper-bridge influence. Leave unset for shadow-only. |

### RunPod-side (HTTP client config)

| Variable | Default | Required in `runpod` mode? | Description |
|----------|---------|---------------------------|-------------|
| `RUNPOD_API_KEY` | `""` | **Yes** | Your RunPod API key. Get it from [runpod.io > Settings > API Keys](https://runpod.io/console/settings). |
| `RUNPOD_TRAINING_ENDPOINT_ID` | `""` | **Yes for training** | Serverless endpoint ID for the training worker. |
| `RUNPOD_INFERENCE_ENDPOINT_ID` | `""` | **Yes for inference** | Serverless endpoint ID for the shadow inference worker. |
| `RUNPOD_ENDPOINT_ID` | `""` | No | Legacy fallback used only when a job-type-specific endpoint ID is unset. |
| `RUNPOD_BASE_URL` | `https://api.runpod.ai/v2` | No | RunPod API base URL. Override only for testing. |
| `RUNPOD_TIMEOUT_SECONDS` | `30` | No | HTTP request timeout for dispatch calls. |
| `RUNPOD_COST_PER_DISPATCH_CENTS` | `0` | No | Estimated cost per dispatch (for budget guard prospective cost check). Set to your expected per-job cost in cents. |
| `QUANT_FOUNDRY_RUNPOD_POLL_INTERVAL_SECONDS` | `15` | No | API startup poll interval for completed RunPod job outputs. Set `0` to disable automatic polling. |

### Budget guard config

| Variable | Default | Required? | Description |
|----------|---------|-----------|-------------|
| `QUANT_FOUNDRY_MONTHLY_BUDGET_CENTS` | `0` | **Yes** (set > 0 for real GPU) | Monthly GPU spend ceiling in cents. `5000` = $50/mo. Default `0` blocks all paid jobs. |
| `QUANT_FOUNDRY_BUDGET_KILL_SWITCH` | `false` | No | Emergency stop. Set to `true` to block ALL paid jobs regardless of remaining budget. |

### RunPod container-side (injected at endpoint creation)

| Variable | Default | Required? | Description |
|----------|---------|-----------|-------------|
| `QUANT_FOUNDRY_CALLBACK_SECRET` | `""` | **Yes** | Must match the Fincept-side secret. Used to sign callbacks. |
| `QUANT_FOUNDRY_TRAINING_DEADLINE_SECONDS` | `600` | No | Max wall-clock seconds for a training run. |
| `QUANT_FOUNDRY_MODE` | — | Inference only: `runpod_shadow` | Enables the shadow inference engine. |
| `PYTHONPATH` | `/worker` or `/app` | Auto-set in Dockerfile | Path to the quant_foundry package. |

### AWS S3 (optional, for artifact storage)

| Variable | Default | Required? | Description |
|----------|---------|-----------|-------------|
| `AWS_ACCESS_KEY_ID` | — | Only for S3 artifacts | AWS access key for S3 reads. |
| `AWS_SECRET_ACCESS_KEY` | — | Only for S3 artifacts | AWS secret key. |
| `AWS_S3_BUCKET` | — | Only for S3 artifacts | Default S3 bucket for artifact storage. |
| `AWS_REGION` | `us-east-1` | No | AWS region. |

---

## Step 1 — Local testing (no GPU, no Docker)

Before touching Docker or RunPod, verify the contract works locally.

### 1.1 Run the full Quant Foundry test suite

```powershell
# From the repo root:

# Run all quant_foundry tests (562 tests, ~7 seconds):
uv run --package quant-foundry pytest services/quant_foundry/tests/ -q --no-header `
    --ignore=services/quant_foundry/tests/test_baseline_family.py

# Run only the RunPod client tests (21 tests, ~3 seconds):
uv run --package quant-foundry pytest services/quant_foundry/tests/test_runpod_client.py -q

# Run only the RunPod training handler tests:
uv run --package quant-foundry pytest services/quant_foundry/tests/test_runpod_training.py -q

# Run only the shadow inference tests:
uv run --package quant-foundry pytest services/quant_foundry/tests/test_shadow_inference.py -q

# Run only the mock flow tests (full local_mock loop):
uv run --package quant-foundry pytest services/quant_foundry/tests/test_mock_flow.py -q

# Run only the budget guard tests:
uv run --package quant-foundry pytest services/quant_foundry/tests/test_budget.py -q

# Run only the gateway budget integration tests:
uv run --package quant-foundry pytest services/quant_foundry/tests/test_gateway_budget.py -q
```

### 1.2 Test the training handler locally (no Docker)

```powershell
# Test the training handler via stdin (simulates RunPod's event protocol):
uv run --package quant-foundry python -c "
import json, sys
from quant_foundry.runpod_training import RunPodTrainingHandler
from quant_foundry.schemas import RunPodTrainingRequest

req = RunPodTrainingRequest(
    job_id='qf:train:local:1',
    dataset_manifest_ref='ds-test-1',
    model_family='gbm',
    search_space={'n_estimators': [100]},
    random_seed=42,
    hardware_class='local-cpu',
)
handler = RunPodTrainingHandler(callback_secret='test-secret')
result = handler.handle(req)
print(json.dumps({
    'job_id': 'qf:train:local:1',
    'artifact_id': result.artifact_id,
    'dossier_id': result.dossier_id,
    'callback_ts': result.callback_ts,
    'signature_length': len(result.callback_signature),
}, indent=2))
"
```

**Expected output:**
```json
{
  "job_id": "qf:train:local:1",
  "artifact_id": "artifact:abc123def4567890",
  "dossier_id": "model:qf:train:local:1",
  "callback_ts": 1719XXXXXXXX,
  "signature_length": 64
}
```

### 1.3 Test the inference handler locally (no Docker)

```powershell
# Test the shadow inference engine (must set QUANT_FOUNDRY_MODE=runpod_shadow):
$env:QUANT_FOUNDRY_MODE = "runpod_shadow"

uv run --package quant-foundry python -c "
import json
from quant_foundry.shadow_inference import ShadowInferenceEngine, FeatureSnapshot
from quant_foundry.schemas import RunPodInferenceRequest

req = RunPodInferenceRequest(
    job_id='qf:infer:local:1',
    artifact_ref='file:///model.pkl',
    symbols=['AAPL', 'MSFT'],
    horizons_ns=[3600000000000],
)
snapshot = FeatureSnapshot(
    symbols=['AAPL', 'MSFT'],
    features={'AAPL': [0.1, 0.2, 0.3], 'MSFT': [0.4, 0.5, 0.6]},
    availability={'AAPL': True, 'MSFT': True},
    ts_event=1000,
    freshness_ns=500,
)
engine = ShadowInferenceEngine(enabled=True)
result = engine.run(request=req, snapshot=snapshot, model_id='test-model-1')
print(json.dumps({
    'predictions': len(result.predictions),
    'latency_ms': result.latency_ms,
    'callback_job_id': result.callback.job_id,
}, indent=2))
"
```

### 1.4 Test the full mock loop via the API

```powershell
# Start the API in local_mock mode:
$env:QUANT_FOUNDRY_ENABLED = "true"
$env:QUANT_FOUNDRY_MODE = "local_mock"
$env:QUANT_FOUNDRY_SHADOW_ONLY = "true"
$env:QUANT_FOUNDRY_CALLBACK_SECRET = "test-secret"
$env:QUANT_FOUNDRY_MONTHLY_BUDGET_CENTS = "0"

# Start the API server (in a separate terminal):
uv run --package api uvicorn api.main:app --host 127.0.0.1 --port 8000 --reload

# In another terminal, test the health endpoint:
curl http://127.0.0.1:8000/quant-foundry/health -H "Authorization: Bearer <your-jwt>"

# Create a mock training job:
curl -X POST http://127.0.0.1:8000/quant-foundry/jobs `
  -H "Authorization: Bearer <your-jwt>" `
  -H "Content-Type: application/json" `
  -d '{
    "job_id": "qf:train:mock:1",
    "job_type": "training",
    "idempotency_key": "idem-mock-1",
    "request_payload": {
      "job_id": "qf:train:mock:1",
      "dataset_manifest_ref": "ds-1",
      "model_family": "gbm",
      "search_space": {"n_estimators": [100]},
      "random_seed": 42,
      "hardware_class": "mock-gpu"
    },
    "budget_cents": 0
  }'

# List jobs:
curl http://127.0.0.1:8000/quant-foundry/jobs -H "Authorization: Bearer <your-jwt>"

# Get job detail:
curl http://127.0.0.1:8000/quant-foundry/jobs/qf:train:mock:1 -H "Authorization: Bearer <your-jwt>"
```

---

## Step 2 — Docker build and local container test

### 2.1 Build the training container

```powershell
# From the repo root (NOT from inside the runpod/ directory):

# Build with the current git SHA pinned:
docker build -t fincept-qf-training:latest `
  --build-arg GIT_SHA=$(git rev-parse --short HEAD) `
  -f runpod/quant-foundry-training/Dockerfile .
```

**Verify the build:**
```powershell
docker images fincept-qf-training:latest
# Should show: fincept-qf-training   latest   <image-id>   <size>   ...
```

### 2.2 Test the training container locally

```powershell
# Run the container with a test job via stdin:
echo '{"input": {"job_id": "qf:train:docker:1", "dataset_manifest_ref": "ds-1", "model_family": "gbm", "search_space": {"n_estimators": [100]}, "random_seed": 42, "hardware_class": "docker-cpu"}}' | docker run --rm -i -e QUANT_FOUNDRY_CALLBACK_SECRET=test-secret fincept-qf-training:latest
```

**Expected output:**
```json
{
  "job_id": "qf:train:docker:1",
  "callback_payload": "{... RunPodCallbackEnvelope JSON ...}",
  "callback_signature": "<64-char hex HMAC>",
  "callback_ts": 1719XXXXXXXX,
  "artifact_id": "artifact:abc123def4567890",
  "dossier_id": "model:qf:train:docker:1"
}
```

### 2.3 Build the inference container

```powershell
# From the repo root:
docker build -t fincept-qf-inference:latest `
  --build-arg GIT_SHA=$(git rev-parse --short HEAD) `
  -f runpod/quant-foundry-inference/Dockerfile .
```

### 2.4 Test the inference container locally

```powershell
# Run the inference container with a test request:
echo '{"input": {"request": {"job_id": "job-1", "artifact_ref": "file:///model.pkl", "symbols": ["AAPL"], "horizons_ns": [3600000000000]}, "snapshot": {"symbols": ["AAPL"], "features": {"AAPL": [0.1, 0.2]}, "availability": {"AAPL": true}, "ts_event": 1000, "freshness_ns": 500}, "model_id": "m1"}}' | docker run --rm -i -e QUANT_FOUNDRY_MODE=runpod_shadow fincept-qf-inference:latest
```

**Expected output:**
```json
{
  "callback": {"job_id": "job-1", ...},
  "predictions": [...],
  "latency_ms": 0.X
}
```

### 2.5 Verify no broker credentials in the containers

```powershell
# Inspect the training container for any broker/trading env vars:
docker run --rm fincept-qf-training:latest env | findstr /I "alpaca broker api_key redis secret jwt"
# Expected: ONLY QUANT_FOUNDRY_CALLBACK_SECRET (empty by default)

# Inspect the inference container:
docker run --rm fincept-qf-inference:latest env | findstr /I "alpaca broker api_key redis secret jwt"
# Expected: ONLY QUANT_FOUNDRY_CALLBACK_SECRET (empty by default)
```

---

## Step 3 — RunPod account setup and endpoint creation

### 3.1 Create a RunPod account

1. Go to [runpod.io](https://runpod.io) and sign up.
2. Add credits (Settings > Billing). $10 is enough for initial testing.
3. Generate an API key (Settings > API Keys > Create API Key).
4. Save the API key securely — it will be set as `RUNPOD_API_KEY`.

### 3.2 Push the Docker image to a registry

RunPod needs to pull your Docker image from a registry. You can use
Docker Hub, RunPod's registry, or a private registry.

**Option A: Docker Hub (simplest)**

```powershell
# Tag the image for Docker Hub (replace YOUR_USERNAME):
docker tag fincept-qf-training:latest YOUR_USERNAME/fincept-qf-training:latest
docker tag fincept-qf-inference:latest YOUR_USERNAME/fincept-qf-inference:latest

# Login to Docker Hub:
docker login

# Push:
docker push YOUR_USERNAME/fincept-qf-training:latest
docker push YOUR_USERNAME/fincept-qf-inference:latest
```

**Option B: RunPod template (no registry needed)**

RunPod can build from a GitHub repo or a template. See the RunPod
console for template creation.

### 3.3 Create a serverless endpoint for training

1. Go to [RunPod Console > Serverless](https://runpod.io/console/serverless).
2. Click **New Endpoint**.
3. Configure:
   - **Name:** `fincept-qf-training`
   - **Container image:** `YOUR_USERNAME/fincept-qf-training:latest`
   - **GPU type:** `NVIDIA RTX A4000` (or cheaper — the baseline trainer
     is CPU-only, so any GPU works for the contract proof).
   - **Workers:** min 0, max 1 (scale to zero when idle).
   - **Environment variables:**
     - `QUANT_FOUNDRY_CALLBACK_SECRET` = `<your-secret>` (must match Fincept-side)
     - `QUANT_FOUNDRY_TRAINING_DEADLINE_SECONDS` = `600`
4. Click **Create**.
5. Copy the **Endpoint ID** — this is your `RUNPOD_TRAINING_ENDPOINT_ID`.

### 3.4 Create a serverless endpoint for inference

1. Repeat the above with:
   - **Name:** `fincept-qf-inference`
   - **Container image:** `YOUR_USERNAME/fincept-qf-inference:latest`
   - **Environment variables:**
     - `QUANT_FOUNDRY_CALLBACK_SECRET` = `<your-secret>` (same as training)
     - `QUANT_FOUNDRY_MODE` = `runpod_shadow`
2. Copy the **Endpoint ID** — this is your `RUNPOD_INFERENCE_ENDPOINT_ID`.

### 3.5 Verify endpoint health

```powershell
# Test the training endpoint health via curl:
curl -X GET "https://api.runpod.ai/v2/<TRAINING_ENDPOINT_ID>/health" `
  -H "Authorization: Bearer <RUNPOD_API_KEY>"

# Test the inference endpoint health:
curl -X GET "https://api.runpod.ai/v2/<INFERENCE_ENDPOINT_ID>/health" `
  -H "Authorization: Bearer <RUNPOD_API_KEY>"
```

**Expected:** `{"health": "ok", ...}` or a 200 response.

---

## Step 4 — Deploy the training worker to RunPod

This was done in Step 3.3. The training worker is deployed as a RunPod
serverless endpoint. RunPod will scale it to zero when idle and spin up
a GPU worker when a job arrives.

### 4.1 Test the training endpoint with a manual request

```powershell
# Submit a test training job directly to RunPod (bypassing Fincept):
curl -X POST "https://api.runpod.ai/v2/<TRAINING_ENDPOINT_ID>/run" `
  -H "Authorization: Bearer <RUNPOD_API_KEY>" `
  -H "Content-Type: application/json" `
  -d '{
    "input": {
      "job_id": "qf:train:runpod:manual:1",
      "dataset_manifest_ref": "ds-1",
      "model_family": "gbm",
      "search_space": {"n_estimators": [100]},
      "random_seed": 42,
      "hardware_class": "rtx-a4000"
    }
  }'
```

**Expected response:**
```json
{
  "id": "rp-job-XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX",
  "status": "IN_QUEUE"
}
```

### 4.2 Poll for the job status

```powershell
# Check the job status (replace the job ID from the previous response):
curl -X GET "https://api.runpod.ai/v2/<TRAINING_ENDPOINT_ID>/status/<JOB_ID>" `
  -H "Authorization: Bearer <RUNPOD_API_KEY>"
```

**Expected (after completion):**
```json
{
  "status": "COMPLETED",
  "output": {
    "job_id": "qf:train:runpod:manual:1",
    "callback_payload": "...",
    "callback_signature": "...",
    "callback_ts": 1719XXXXXXXX,
    "artifact_id": "artifact:...",
    "dossier_id": "model:qf:train:runpod:manual:1"
  }
}
```

---

## Step 5 — Deploy the inference worker to RunPod

This was done in Step 3.4. The inference worker is deployed as a separate
RunPod serverless endpoint.

### 5.1 Test the inference endpoint with a manual request

```powershell
# Submit a test inference job directly to RunPod:
curl -X POST "https://api.runpod.ai/v2/<INFERENCE_ENDPOINT_ID>/run" `
  -H "Authorization: Bearer <RUNPOD_API_KEY>" `
  -H "Content-Type: application/json" `
  -d '{
    "input": {
      "request": {
        "job_id": "qf:infer:runpod:manual:1",
        "artifact_ref": "file:///model.pkl",
        "symbols": ["AAPL"],
        "horizons_ns": [3600000000000]
      },
      "snapshot": {
        "symbols": ["AAPL"],
        "features": {"AAPL": [0.1, 0.2, 0.3]},
        "availability": {"AAPL": true},
        "ts_event": 1000,
        "freshness_ns": 500
      },
      "model_id": "test-model-1"
    }
  }'
```

---

## Step 6 — Configure Fincept for RunPod mode

### 6.1 Generate a callback secret (if you haven't already)

```powershell
python -c "import secrets; print(secrets.token_hex(32))"
# Save this — it must be set on BOTH the Fincept side and the RunPod endpoint.
```

### 6.2 Set environment variables

```powershell
# --- Master switch ---
$env:QUANT_FOUNDRY_ENABLED = "true"

# --- Mode: switch from mock to real RunPod ---
$env:QUANT_FOUNDRY_MODE = "runpod"

# --- Shadow only (no sig.predict writes) ---
$env:QUANT_FOUNDRY_SHADOW_ONLY = "true"

# --- HMAC callback secret (must match RunPod endpoint) ---
$env:QUANT_FOUNDRY_CALLBACK_SECRET = "<your-secret-from-step-6.1>"

# --- RunPod API config ---
$env:RUNPOD_API_KEY = "<your-runpod-api-key>"
$env:RUNPOD_TRAINING_ENDPOINT_ID = "<training-endpoint-id>"
$env:RUNPOD_INFERENCE_ENDPOINT_ID = "<inference-endpoint-id>"
$env:RUNPOD_BASE_URL = "https://api.runpod.ai/v2"
$env:RUNPOD_TIMEOUT_SECONDS = "30"
$env:RUNPOD_COST_PER_DISPATCH_CENTS = "50"
$env:QUANT_FOUNDRY_RUNPOD_POLL_INTERVAL_SECONDS = "15"

# --- Budget guard (fail-closed on GPU spend) ---
$env:QUANT_FOUNDRY_MONTHLY_BUDGET_CENTS = "5000"
$env:QUANT_FOUNDRY_BUDGET_KILL_SWITCH = "false"

# --- DO NOT set these (leave unset for shadow-only): ---
# $env:QUANT_FOUNDRY_ALLOW_PAPER_BRIDGE  ← leave UNSET
```

### 6.3 Verify the gateway is wired correctly

```powershell
# Start the API server:
uv run --package api uvicorn api.main:app --host 127.0.0.1 --port 8000 --reload

# In another terminal, check health (should show runpod_wired: true):
curl http://127.0.0.1:8000/quant-foundry/health -H "Authorization: Bearer <your-jwt>"
```

**Expected:**
```json
{
  "enabled": true,
  "mode": "runpod",
  "shadow_only": true,
  "job_count": 0,
  "runpod_wired": true,
  "runpod_routes": {
    "training": "<training-endpoint-id>",
    "inference": "<inference-endpoint-id>"
  }
}
```

### 6.4 Test RunPod endpoint health from Fincept

```powershell
# Using the HttpRunPodClient directly:
uv run --package quant-foundry python -c "
from quant_foundry.runpod_client import HttpRunPodClient
import os
client = HttpRunPodClient(
    api_key=os.environ['RUNPOD_API_KEY'],
    endpoint_id=os.environ['RUNPOD_TRAINING_ENDPOINT_ID'],
)
try:
    health = client.check_health()
    print('RunPod health:', health)
except Exception as e:
    print('RunPod health check failed:', e)
"
```

---

## Step 7 — Dispatch a real training job

### 7.1 Via the API

```powershell
# Create a training job (Fincept dispatches to RunPod via HTTP):
curl -X POST http://127.0.0.1:8000/quant-foundry/jobs `
  -H "Authorization: Bearer <your-jwt>" `
  -H "Content-Type: application/json" `
  -d '{
    "job_id": "qf:train:runpod:1",
    "job_type": "training",
    "idempotency_key": "idem-runpod-1",
    "request_payload": {
      "job_id": "qf:train:runpod:1",
      "dataset_manifest_ref": "ds-manifest-1",
      "model_family": "gbm",
      "search_space": {"n_estimators": [100, 200]},
      "random_seed": 42,
      "hardware_class": "rtx-a4000"
    },
    "priority": 0,
    "budget_cents": 50
  }'
```

**Expected response:**
```json
{
  "enabled": true,
  "job_id": "qf:train:runpod:1",
  "status": "dispatched",
  "mode": "runpod"
}
```

### 7.2 Check the job status

```powershell
# Get the job detail (should show runpod_job_id):
curl http://127.0.0.1:8000/quant-foundry/jobs/qf:train:runpod:1 `
  -H "Authorization: Bearer <your-jwt>"
```

**Expected:**
```json
{
  "job_id": "qf:train:runpod:1",
  "status": "dispatched",
  "runpod_job_id": "rp-job-XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX",
  "history": [...]
}
```

### 7.3 Poll RunPod for the job result (optional)

```powershell
# If you want to poll RunPod directly (the callback will arrive automatically):
uv run --package quant-foundry python -c "
from quant_foundry.runpod_client import HttpRunPodClient
import os, json
client = HttpRunPodClient(
    api_key=os.environ['RUNPOD_API_KEY'],
    endpoint_id=os.environ['RUNPOD_TRAINING_ENDPOINT_ID'],
)
# Replace with the runpod_job_id from the job detail:
status = client.check_status('rp-job-XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX')
print(json.dumps(status, indent=2))
"
```

---

## Step 8 — Receive the callback

When the RunPod worker finishes, it returns a signed callback as the RunPod
job output. The API startup now runs a polling loop when
`QUANT_FOUNDRY_MODE` is `runpod`, `runpod_research`, or `runpod_shadow` and
`QUANT_FOUNDRY_RUNPOD_POLL_INTERVAL_SECONDS` is greater than zero. The poller
checks `RUNNING` outbox records, extracts `callback_payload`,
`callback_signature`, and `callback_ts`, and submits them to
`/quant-foundry/callbacks/runpod`.

### 8.1 Automated callback polling

```powershell
# The API process polls automatically. To trigger one poll manually:
uv run --package quant-foundry python -c "
from quant_foundry.gateway import QuantFoundryGateway
gateway = QuantFoundryGateway.from_env()
print(gateway.poll_runpod_results())
"
```

### 8.2 Manual callback submission fallback

```powershell
# Send the callback to the Fincept callback endpoint (HMAC auth, NOT bearer):
# The callback_payload, signature, and ts come from the RunPod job output.

curl -X POST http://127.0.0.1:8000/quant-foundry/callbacks/runpod `
  -H "Content-Type: application/json" `
  -H "X-QF-Job-Id: qf:train:runpod:1" `
  -H "X-QF-Timestamp: <callback_ts>" `
  -H "X-QF-Signature: <callback_signature>" `
  -d '<callback_payload>'
```

**Expected response:**
```json
{
  "enabled": true,
  "ok": true,
  "job_id": "qf:train:runpod:1",
  "status": "processed",
  "signature_verified": true
}
```

### 8.3 Verify the dossier was registered

```powershell
# List dossiers:
curl http://127.0.0.1:8000/quant-foundry/dossiers `
  -H "Authorization: Bearer <your-jwt>"

# Get a specific dossier:
curl http://127.0.0.1:8000/quant-foundry/dossiers/model:qf:train:runpod:1 `
  -H "Authorization: Bearer <your-jwt>"
```

---

## Step 9 — Dispatch a real shadow inference job

### 9.1 Create an inference job

```powershell
curl -X POST http://127.0.0.1:8000/quant-foundry/jobs `
  -H "Authorization: Bearer <your-jwt>" `
  -H "Content-Type: application/json" `
  -d '{
    "job_id": "qf:infer:runpod:1",
    "job_type": "inference",
    "idempotency_key": "idem-infer-1",
    "request_payload": {
      "job_id": "qf:infer:runpod:1",
      "artifact_ref": "file:///model.pkl",
      "symbols": ["AAPL", "MSFT"],
      "horizons_ns": [3600000000000],
      "feature_snapshot_ref": "snap:live:example",
      "model_id": "model:qf:train:runpod:1",
      "decision_time": 1000,
      "expected_features": ["momentum", "volatility"],
      "feature_rows": [
        {
          "symbol": "AAPL",
          "event_ts": 900,
          "decision_time": 1000,
          "features": [
            {"name": "momentum", "value": 0.25, "observed_at": 990},
            {"name": "volatility", "value": 0.05, "observed_at": 990}
          ]
        },
        {
          "symbol": "MSFT",
          "event_ts": 900,
          "decision_time": 1000,
          "features": [
            {"name": "momentum", "value": 0.18, "observed_at": 990},
            {"name": "volatility", "value": 0.07, "observed_at": 990}
          ]
        }
      ]
    },
    "priority": 0,
    "budget_cents": 10
  }'
```

---

## Step 10 — Verify the full loop

### 10.1 Check the outbox

```powershell
# List all jobs:
curl http://127.0.0.1:8000/quant-foundry/jobs `
  -H "Authorization: Bearer <your-jwt>"

# Filter by status:
curl "http://127.0.0.1:8000/quant-foundry/jobs?status=dispatched" `
  -H "Authorization: Bearer <your-jwt>"
```

### 10.2 Check the shadow health

```powershell
curl http://127.0.0.1:8000/quant-foundry/shadow/health `
  -H "Authorization: Bearer <your-jwt>"
```

### 10.3 Check the tournament leaderboard

```powershell
curl http://127.0.0.1:8000/quant-foundry/tournament/leaderboard `
  -H "Authorization: Bearer <your-jwt>"
```

### 10.4 Check the promotion queue

```powershell
curl http://127.0.0.1:8000/quant-foundry/promotion/queue `
  -H "Authorization: Bearer <your-jwt>"
```

### 10.5 Check the budget

```powershell
# Check the spend ledger directly:
type reports\quant-foundry\budget\spend_2026-06.jsonl

# Or via Python:
uv run --package quant-foundry python -c "
from quant_foundry.budget import BudgetGuard
import pathlib
guard = BudgetGuard.from_env(pathlib.Path('reports/quant-foundry/budget'))
print(guard.get_summary())
"
```

---

## Testing commands (complete reference)

### Unit tests

```powershell
# All quant_foundry tests (562 tests):
uv run --package quant-foundry pytest services/quant_foundry/tests/ -q --no-header `
    --ignore=services/quant_foundry/tests/test_baseline_family.py

# RunPod client tests (21 tests — includes HttpRunPodClient):
uv run --package quant-foundry pytest services/quant_foundry/tests/test_runpod_client.py -v

# RunPod training handler tests:
uv run --package quant-foundry pytest services/quant_foundry/tests/test_runpod_training.py -v

# Shadow inference tests:
uv run --package quant-foundry pytest services/quant_foundry/tests/test_shadow_inference.py -v

# Mock flow tests (full local_mock loop):
uv run --package quant-foundry pytest services/quant_foundry/tests/test_mock_flow.py -v

# Budget guard tests:
uv run --package quant-foundry pytest services/quant_foundry/tests/test_budget.py -v

# Gateway budget integration tests:
uv run --package quant-foundry pytest services/quant_foundry/tests/test_gateway_budget.py -v

# Schema tests (extra="forbid", order-field rejection):
uv run --package quant-foundry pytest services/quant_foundry/tests/test_schemas.py -v

# Signature tests (HMAC):
uv run --package quant-foundry pytest services/quant_foundry/tests/test_signatures.py -v

# Outbox/inbox durability tests:
uv run --package quant-foundry pytest services/quant_foundry/tests/test_outbox.py services/quant_foundry/tests/test_inbox.py -v

# Settlement tests:
uv run --package quant-foundry pytest services/quant_foundry/tests/test_settlement.py -v

# Dossier registry tests:
uv run --package quant-foundry pytest services/quant_foundry/tests/test_dossier.py -v

# Tournament scoring tests:
uv run --package quant-foundry pytest services/quant_foundry/tests/test_tournament.py -v

# Leakage/overfit sentinel tests:
uv run --package quant-foundry pytest services/quant_foundry/tests/test_sentinel.py -v

# Promotion gate tests:
uv run --package quant-foundry pytest services/quant_foundry/tests/test_promotion.py -v

# Paper bridge tests:
uv run --package quant-foundry pytest services/quant_foundry/tests/test_paper_bridge.py -v

# MoE router tests:
uv run --package quant-foundry pytest services/quant_foundry/tests/test_moe_router.py -v

# Conformal gate tests:
uv run --package quant-foundry pytest services/quant_foundry/tests/test_conformal_gate.py -v

# Drift sentinel tests:
uv run --package quant-foundry pytest services/quant_foundry/tests/test_drift_sentinel.py -v

# Causal graph tests:
uv run --package quant-foundry pytest services/quant_foundry/tests/test_causal_graph.py -v

# Shadow ledger tests:
uv run --package quant-foundry pytest services/quant_foundry/tests/test_shadow_ledger.py -v

# Shadow settlement tests:
uv run --package quant-foundry pytest services/quant_foundry/tests/test_shadow_settlement.py -v

# Feature lake tests:
uv run --package quant-foundry pytest services/quant_foundry/tests/test_feature_lake.py -v

# Feature snapshot tests:
uv run --package quant-foundry pytest services/quant_foundry/tests/test_feature_snapshots.py -v

# Artifacts tests:
uv run --package quant-foundry pytest services/quant_foundry/tests/test_artifacts.py -v

# Leaderboard tests:
uv run --package quant-foundry pytest services/quant_foundry/tests/test_leaderboard_expanded.py -v

# Retirement tests:
uv run --package quant-foundry pytest services/quant_foundry/tests/test_retirement.py -v
```

### Integration tests

```powershell
# Run the verification receipt runner:
.\scripts\verification-receipt.ps1

# Run the preflight (lint, typecheck, tests, JS checks):
.\scripts\preflight.ps1

# Run route smoke tests:
uv run --package api python scripts/route_smoke.py
```

### Docker tests

```powershell
# Build training container:
docker build -t fincept-qf-training:latest `
  --build-arg GIT_SHA=$(git rev-parse --short HEAD) `
  -f runpod/quant-foundry-training/Dockerfile .

# Test training container:
echo '{"input": {"job_id": "qf:train:docker:1", "dataset_manifest_ref": "ds-1", "model_family": "gbm", "search_space": {"n_estimators": [100]}, "random_seed": 42, "hardware_class": "docker-cpu"}}' | docker run --rm -i -e QUANT_FOUNDRY_CALLBACK_SECRET=test-secret fincept-qf-training:latest

# Build inference container:
docker build -t fincept-qf-inference:latest `
  --build-arg GIT_SHA=$(git rev-parse --short HEAD) `
  -f runpod/quant-foundry-inference/Dockerfile .

# Test inference container:
echo '{"input": {"request": {"job_id": "job-1", "artifact_ref": "file:///model.pkl", "symbols": ["AAPL"], "horizons_ns": [3600000000000]}, "snapshot": {"symbols": ["AAPL"], "features": {"AAPL": [0.1, 0.2]}, "availability": {"AAPL": true}, "ts_event": 1000, "freshness_ns": 500}, "model_id": "m1"}}' | docker run --rm -i -e QUANT_FOUNDRY_MODE=runpod_shadow fincept-qf-inference:latest

# Verify no broker credentials leaked:
docker run --rm fincept-qf-training:latest env | findstr /I "alpaca broker api_key redis secret jwt"
docker run --rm fincept-qf-inference:latest env | findstr /I "alpaca broker api_key redis secret jwt"
```

### RunPod API tests

```powershell
# Check endpoint health:
curl -X GET "https://api.runpod.ai/v2/<ENDPOINT_ID>/health" `
  -H "Authorization: Bearer <RUNPOD_API_KEY>"

# Submit a test job:
curl -X POST "https://api.runpod.ai/v2/<ENDPOINT_ID>/run" `
  -H "Authorization: Bearer <RUNPOD_API_KEY>" `
  -H "Content-Type: application/json" `
  -d '{"input": {"job_id": "test:1", "dataset_manifest_ref": "ds-1", "model_family": "gbm", "search_space": {"n_estimators": [100]}, "random_seed": 42, "hardware_class": "rtx-a4000"}}'

# Check job status:
curl -X GET "https://api.runpod.ai/v2/<ENDPOINT_ID>/status/<JOB_ID>" `
  -H "Authorization: Bearer <RUNPOD_API_KEY>"
```

---

## API endpoints (complete reference)

### Operator endpoints (require Bearer JWT)

| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/quant-foundry/jobs` | Create a job (dispatches to RunPod in `runpod` mode) |
| `GET` | `/quant-foundry/jobs` | List all jobs (optional `?status=` filter) |
| `GET` | `/quant-foundry/jobs/{job_id}` | Get job detail |
| `GET` | `/quant-foundry/dossiers` | List registered dossiers (optional `?status=` filter) |
| `GET` | `/quant-foundry/dossiers/{model_id}` | Get dossier detail |
| `GET` | `/quant-foundry/tournament/leaderboard` | Get ranked tournament leaderboard |
| `GET` | `/quant-foundry/promotion/queue` | Get pending promotion requests |
| `GET` | `/quant-foundry/promotion/completed` | Get completed promotion receipts |
| `GET` | `/quant-foundry/shadow/health` | Get shadow inference health metrics |
| `GET` | `/quant-foundry/health` | Get gateway health state |
| `GET` | `/quant-foundry/heartbeats` | Get worker heartbeats |

### Callback endpoint (HMAC auth, NOT bearer)

| Method | Path | Headers | Purpose |
|--------|------|---------|---------|
| `POST` | `/quant-foundry/callbacks/runpod` | `X-QF-Job-Id`, `X-QF-Timestamp`, `X-QF-Signature` | Receive a signed callback from RunPod |

### HMAC signature format

```
HMAC_SHA256(callback_secret, timestamp + "." + job_id + "." + payload_hash)
```

- `timestamp` = unix seconds (integer)
- `job_id` = the job ID string
- `payload_hash` = SHA-256 hash of the callback payload bytes
- Maximum timestamp skew: 300 seconds (5 minutes)
- Verification uses `hmac.compare_digest` (constant-time)

---

## Troubleshooting

### "ModuleNotFoundError: No module named 'quant_foundry'"

```powershell
# Reinstall the package:
uv sync
uv run --package quant-foundry python -c "import quant_foundry; print('OK')"
```

### "HttpRunPodClient.dispatch is not yet implemented"

This error means you're running an older version of the code. The
`HttpRunPodClient.dispatch()` method is now fully implemented. Make
sure you're on the latest code:

```powershell
git pull
uv sync
```

### "budget_exceeded" or "budget_kill_switch" error

```powershell
# Check your budget:
uv run --package quant-foundry python -c "
from quant_foundry.budget import BudgetGuard
import pathlib, os
guard = BudgetGuard.from_env(pathlib.Path(os.environ.get('QUANT_FOUNDRY_BASE_DIR', 'reports/quant-foundry') + '/budget'))
print(guard.get_summary())
"

# Increase the monthly budget:
$env:QUANT_FOUNDRY_MONTHLY_BUDGET_CENTS = "10000"  # $100/mo

# Or disable the kill switch:
$env:QUANT_FOUNDRY_BUDGET_KILL_SWITCH = "false"
```

### "bad_signature" callback error

The callback secret doesn't match between Fincept and the RunPod
container. Verify:

```powershell
# Check the Fincept-side secret:
echo $env:QUANT_FOUNDRY_CALLBACK_SECRET

# Check the RunPod endpoint env vars (in the RunPod console):
# QUANT_FOUNDRY_CALLBACK_SECRET must match exactly.
```

### Docker build fails with "COPY failed"

You must build from the **repo root**, not from inside the `runpod/`
directory:

```powershell
# CORRECT (from repo root):
docker build -t fincept-qf-training:latest -f runpod/quant-foundry-training/Dockerfile .

# WRONG (from inside runpod/):
cd runpod
docker build -t fincept-qf-training:latest -f quant-foundry-training/Dockerfile .  # WILL FAIL
```

### RunPod endpoint returns 503

The endpoint may have no workers running (scaled to zero). Wait a few
seconds for RunPod to spin up a worker, then retry. Check the RunPod
console for worker status.

### "lightgbm" import error in tests

This is a pre-existing issue with `test_baseline_family.py` (the
`lightgbm` package is not declared as a dependency of `quant_foundry`).
Skip that test file:

```powershell
uv run --package quant-foundry pytest services/quant_foundry/tests/ -q `
    --ignore=services/quant_foundry/tests/test_baseline_family.py
```

---

## Security invariants (non-negotiable)

1. **No broker credentials on RunPod.** The RunPod containers have no
   `ALPACA_API_KEY`, no `BINANCE_API_KEY`, no Redis URL, no JWT secret.
   The only secret is `QUANT_FOUNDRY_CALLBACK_SECRET` (HMAC signing).

2. **No trading stream writes from RunPod.** The RunPod handlers cannot
   write to `sig.predict`, `ord.orders`, or any trading stream. Their
   output is a signed callback that Fincept verifies before processing.

3. **Shadow-only authority.** All predictions from RunPod carry
   `authority=shadow_only`. The `ShadowPrediction` schema enforces
   `extra="forbid"` and rejects order-like fields (`quantity`, `order
   side`, `broker account`, `order type`, `time in force`, `notional
   size`).

4. **HMAC-signed callbacks.** Every callback is signed with
   `QUANT_FOUNDRY_CALLBACK_SECRET`. Fincept verifies the signature
   (constant-time `hmac.compare_digest`) before processing. Bad
   signatures are rejected fail-closed.

5. **Budget guard fail-closed.** GPU spend is blocked before any job is
   dispatched if the monthly ceiling would be exceeded or the kill
   switch is active. Zero-cost jobs (mock, tests) are always allowed.

6. **OMS/risk isolation.** `quant_foundry` has zero imports of `oms` or
   `risk`. Order execution and risk evaluation remain authoritative in
   their own services. Quant Foundry cannot bypass risk.

7. **API key never exposed.** The `RUNPOD_API_KEY` is stored in the
   `HttpRunPodClient` as a private attribute and is never returned in
   `DispatchResult`, logs, or outbox records.

8. **Replay protection.** HMAC signatures include a timestamp with a
   5-minute skew window. Old signatures are rejected.

---

## Rollback procedure

To disable RunPod mode and revert to local mock (no code change, no
restart needed if env vars are reloaded):

```powershell
# Option 1: Switch back to mock mode:
$env:QUANT_FOUNDRY_MODE = "local_mock"

# Option 2: Disable Quant Foundry entirely:
$env:QUANT_FOUNDRY_ENABLED = "false"

# Option 3: Emergency stop all GPU spend:
$env:QUANT_FOUNDRY_BUDGET_KILL_SWITCH = "true"

# Option 4: Disable paper bridge (should already be unset):
Remove-Item Env:\QUANT_FOUNDRY_ALLOW_PAPER_BRIDGE
```

To verify the rollback:

```powershell
curl http://127.0.0.1:8000/quant-foundry/health -H "Authorization: Bearer <your-jwt>"
# Should show: {"enabled": false, ...} or {"mode": "local_mock", ...}
```

---

## File reference

| File | Purpose |
|------|---------|
| `services/quant_foundry/src/quant_foundry/runpod_client.py` | `HttpRunPodClient`, `RunPodDispatcher`, `BudgetGuard`, `MockRunPodClient` |
| `services/quant_foundry/src/quant_foundry/runpod_training.py` | `RunPodTrainingHandler`, `LocalTrainer`, `TrainingResult` |
| `services/quant_foundry/src/quant_foundry/shadow_inference.py` | `ShadowInferenceEngine`, `FeatureSnapshot` |
| `services/quant_foundry/src/quant_foundry/gateway.py` | `QuantFoundryGateway` (wires RunPod client + dispatcher) |
| `services/quant_foundry/src/quant_foundry/schemas.py` | Cross-boundary Pydantic schemas (`extra="forbid"`) |
| `services/quant_foundry/src/quant_foundry/signatures.py` | HMAC signing + verification |
| `services/quant_foundry/src/quant_foundry/budget.py` | `BudgetGuard` (fail-closed GPU spend) |
| `runpod/quant-foundry-training/handler.py` | RunPod training handler entrypoint |
| `runpod/quant-foundry-training/Dockerfile` | Training container build |
| `runpod/quant-foundry-inference/handler.py` | RunPod inference handler entrypoint |
| `runpod/quant-foundry-inference/Dockerfile` | Inference container build |
| `services/api/src/api/routes/quant_foundry.py` | FastAPI routes for QF gateway |
| `services/quant_foundry/tests/test_runpod_client.py` | RunPod client tests (21 tests) |
| `services/quant_foundry/tests/test_runpod_training.py` | Training handler tests |
| `services/quant_foundry/tests/test_shadow_inference.py` | Shadow inference tests |
