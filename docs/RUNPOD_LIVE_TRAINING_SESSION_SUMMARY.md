# Quant Foundry — RunPod Live Training Session Summary

> **Date:** 2026-06-25 → 2026-06-26
> **Branch:** `codex/portfolio-optimizer-core`
> **Repo:** `C:\Users\nolan\CascadeProjects\fincept-terminal`
> **Outcome:** Full end-to-end training job dispatched → RunPod GPU worker processed → signed callback verified → model dossier registered. **System is LIVE and producing models.**

---

## Table of Contents

1. [Executive Summary](#1-executive-summary)
2. [What Was Accomplished](#2-what-was-accomplished)
3. [Current System Architecture](#3-current-system-architecture)
4. [All URLs, Endpoints & Connections](#4-all-urls-endpoints--connections)
5. [Environment Variables (Complete)](#5-environment-variables-complete)
6. [Credentials & Secrets](#6-credentials--secrets)
7. [RunPod Endpoint Configuration](#7-runpod-endpoint-configuration)
8. [GitHub Container Registry (ghcr.io) State](#8-github-container-registry-ghcrio-state)
9. [The Training Job That Succeeded](#9-the-training-job-that-succeeded)
10. [Model Analysis](#10-model-analysis)
11. [Root Causes Fixed This Session](#11-root-causes-fixed-this-session)
12. [Commands Reference](#12-commands-reference)
13. [Key Files](#13-key-files)
14. [Known Issues & Limitations](#14-known-issues--limitations)
15. [Next Steps](#15-next-steps)

---

## 1. Executive Summary

The Quant Foundry system is now **live and producing models** on RunPod GPU infrastructure. The full round-trip pipeline works:

```
Railway API → dispatch training job → RunPod GPU worker (RTX 4090)
  → handler trains model → signs HMAC callback → returns to API
  → API verifies signature → registers ModelDossier → job completed
```

The first successful training job (`qf:train:systest:004`) completed in 0.25 seconds and produced a model dossier with artifact hash `490d8a8863c193f3...`. The model is a **stub** (LocalTrainer, not real LightGBM) — this was a system integration test to prove the pipeline works end-to-end.

Three critical bugs were fixed during this session:

1. **Budget gate blocking dispatch** — monthly budget was set to 0 cents
2. **RunPod endpoint env var name mismatch** — gateway looked for `RUNPOD_TRAINING_ENDPOINT_ID` but Railway had `QUANT_FOUNDRY_RUNPOD_TRAINING_ENDPOINT`
3. **Callback secret mismatch** — RunPod endpoint had a different HMAC secret than the Railway API, causing all callbacks to be rejected

---

## 2. What Was Accomplished

| Milestone                     | Status    | Evidence                                                                    |
| ----------------------------- | --------- | --------------------------------------------------------------------------- |
| LightGBM determinism test     | ✅ Pass    | `test_re_running_reproduces_artifact_hash` passes consistently              |
| Railway API deployed          | ✅ Online  | `https://api-production-73610.up.railway.app/health` returns `{"ok": true}` |
| Railway Dashboard deployed    | ✅ Online  | `https://dashboard-production-f39a.up.railway.app`                          |
| RunPod API key configured     | ✅ Set     | `RUNPOD_API_KEY` in Railway env vars (50 chars)                             |
| Quant Foundry gateway enabled | ✅ Running | `QUANT_FOUNDRY_ENABLED=true`, `QUANT_FOUNDRY_MODE=runpod_shadow`            |
| RunPod training endpoint      | ✅ Healthy | `8vol1uc9l75jgs` — 1 worker ready, 4 jobs completed                         |
| RunPod inference endpoint     | ✅ Online  | `36mz2q30jdyvru` — 1 worker throttled, 5 jobs completed                     |
| Docker images built & pushed  | ✅ Built   | `ghcr.io/airyder/fincept/quant-foundry-training:latest` + inference         |
| First training job completed  | ✅ Done    | `qf:train:systest:004` → model dossier registered                           |
| Full test suite               | ✅ Clean   | 907 passed, 2 skipped                                                       |

---

## 3. Current System Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                    RAILWAY (US East)                             │
│                                                                 │
│  ┌──────────────────┐     ┌──────────────────────────────┐     │
│  │  Dashboard        │     │  API (FastAPI + uvicorn)     │     │
│  │  Next.js 14       │────▶│  Port: $PORT (auto)          │     │
│  │  dashboard-prod   │     │  api-production-73610        │     │
│  │  -f39a.up.railway │     │  .up.railway.app             │     │
│  └──────────────────┘     │                              │     │
│                            │  Quant Foundry Gateway       │     │
│                            │  ├─ Outbox (Postgres)        │     │
│                            │  ├─ Inbox (Postgres)         │     │
│                            │  ├─ DossierRegistry          │     │
│                            │  ├─ ShadowLedger             │     │
│                            │  ├─ RunPod Poller (async)    │     │
│                            │  └─ Shadow Dispatch Loop      │     │
│                            └──────────┬───────────────────┘     │
│                                       │                         │
│  ┌─────────────┐  ┌─────────────┐     │                         │
│  │  Postgres   │  │  Redis      │     │                         │
│  │  postgres-  │  │  redis-     │     │                         │
│  │  volume     │  │  volume     │     │                         │
│  └─────────────┘  └─────────────┘     │                         │
│                                       │                         │
└───────────────────────────────────────┼─────────────────────────┘
                                        │
                                        │ HTTPS (RunPod REST API)
                                        ▼
┌─────────────────────────────────────────────────────────────────┐
│                    RUNPOD (US-NC-1)                              │
│                                                                 │
│  ┌──────────────────────────┐  ┌──────────────────────────┐    │
│  │  Training Endpoint        │  │  Inference Endpoint       │    │
│  │  8vol1uc9l75jgs           │  │  36mz2q30jdyvru           │    │
│  │  Template: me58r5vdrp     │  │  Template: wnasp3v5jn     │    │
│  │  GPU: RTX 4090            │  │  GPU: RTX 4090            │    │
│  │  Image: runpod/pytorch    │  │  Image: runpod/pytorch    │    │
│  │    :2.4.0-py3.11-         │  │    :2.4.0-py3.11-         │    │
│  │    cuda12.4.1-devel       │  │    cuda12.4.1-devel       │    │
│  │  Volume: rrsd005i3g (10GB)│  │  Volume: rrsd005i3g (10GB)│    │
│  │  Workers: 0-1 (auto)      │  │  Workers: 0-1 (auto)      │    │
│  │  Status: 1 ready          │  │  Status: 1 throttled      │    │
│  └──────────────────────────┘  └──────────────────────────┘    │
│                                                                 │
│  Network Volume: /runpod-volume/fincept-terminal/               │
│    ├─ handler.py (loaded by start-training.sh)                  │
│    ├─ services/quant_foundry/src/ (PYTHONPATH)                  │
│    ├─ python-libs/ (pydantic, httpx, runpod SDK)                │
│    ├─ start-training.sh                                         │
│    └─ start-inference.sh                                        │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

### Data Flow (Training)

1. **Dispatch:** API receives POST `/quant-foundry/jobs` → gateway creates outbox record → dispatches to RunPod endpoint via REST API
2. **RunPod Processing:** RunPod assigns job to a GPU worker → worker runs `handler(event)` → `RunPodTrainingHandler.handle()` trains model → builds signed `RunPodCallbackEnvelope` → returns callback payload + HMAC signature
3. **Polling:** API's RunPod poller checks job status every few seconds → detects completion → fetches result
4. **Callback Ingestion:** API verifies HMAC signature → parses envelope → registers `ArtifactManifest` + `ModelDossier` in `DossierRegistry` → marks job as `completed`

### Data Flow (Shadow Inference)

1. **Shadow Dispatch Loop:** Every 300s, gateway finds `candidate` dossiers → builds feature snapshot → dispatches inference job to RunPod inference endpoint
2. **RunPod Processing:** Inference handler loads model → runs prediction → returns signed callback
3. **Shadow Ledger:** API stores prediction in `ShadowLedger` → tracks latency, accuracy, settlement evidence

---

## 4. All URLs, Endpoints & Connections

### Railway Services

| Service   | URL                                                | Status |
| --------- | -------------------------------------------------- | ------ |
| API       | `https://api-production-73610.up.railway.app`      | Online |
| Dashboard | `https://dashboard-production-f39a.up.railway.app` | Online |
| Postgres  | `postgres.railway.internal:5432` (internal only)   | Online |
| Redis     | `redis.railway.internal:6379` (internal only)      | Online |

### API Endpoints

| Endpoint                       | Method | Purpose                                                   |
| ------------------------------ | ------ | --------------------------------------------------------- |
| `/health`                      | GET    | Health check (returns `{"ok": true, "version": "0.1.0"}`) |
| `/quant-foundry/jobs`          | GET    | List all jobs                                             |
| `/quant-foundry/jobs`          | POST   | Create/dispatch a new job                                 |
| `/quant-foundry/jobs/{job_id}` | GET    | Get job status + history                                  |
| `/quant-foundry/dossiers`      | GET    | List model dossiers                                       |
| `/quant-foundry/shadow/health` | GET    | Shadow inference health metrics                           |

### RunPod Endpoints

| Endpoint  | ID               | Template ID  | Purpose                  |
| --------- | ---------------- | ------------ | ------------------------ |
| Training  | `8vol1uc9l75jgs` | `me58r5vdrp` | GPU training (RTX 4090)  |
| Inference | `36mz2q30jdyvru` | `wnasp3v5jn` | GPU inference (RTX 4090) |

### RunPod Network Volume

| Property                | Value                                                          |
| ----------------------- | -------------------------------------------------------------- |
| Volume ID               | `rrsd005i3g`                                                   |
| Size                    | 10 GB                                                          |
| Region                  | US-NC-1                                                        |
| Mount path (serverless) | `/runpod-volume`                                               |
| Mount path (pod)        | `/workspace`                                                   |
| Content                 | `fincept-terminal/` git clone + `python-libs/` + start scripts |

### GitHub Container Registry

| Image                                                    | Status             |
| -------------------------------------------------------- | ------------------ |
| `ghcr.io/airyder/fincept/quant-foundry-training:latest`  | Built, **private** |
| `ghcr.io/airyder/fincept/quant-foundry-inference:latest` | Built, **private** |

### GitHub Actions

| Workflow       | File                                 | Status                       |
| -------------- | ------------------------------------ | ---------------------------- |
| `build-images` | `.github/workflows/build-images.yml` | Success (run ID 28208384886) |

---

## 5. Environment Variables (Complete)

### Railway API Service

| Variable                                         | Value                                                                                           | Purpose                                    |
| ------------------------------------------------ | ----------------------------------------------------------------------------------------------- | ------------------------------------------ |
| `FINCEPT_DB_URL`                                 | `postgresql://postgres:FJfMaum...REDACTED...Ryfny@postgres.railway.internal:5432/railway` | Postgres connection                        |
| `FINCEPT_JWT_SECRET`                             | `dev-only-change-me`                                                                            | JWT signing (dev only)                     |
| `FINCEPT_OMS_ROUTER`                             | `sim`                                                                                           | Order management router (simulation)       |
| `FINCEPT_REDIS_URL`                              | `redis://default:cQsSbur...REDACTED...VdwK@redis.railway.internal:6379`                  | Redis connection                           |
| `FINCEPT_STORAGE_BACKEND`                        | `local`                                                                                         | Storage backend type                       |
| `FINCEPT_STORAGE_LOCAL_BASE_DIR`                 | `/data`                                                                                         | Local storage path                         |
| `FINCEPT_TRADING_MODE`                           | `paper`                                                                                         | Trading mode (paper trading)               |
| `QUANT_FOUNDRY_BASE_DIR`                         | `/data/quant-foundry`                                                                           | Quant Foundry data directory               |
| `QUANT_FOUNDRY_CALLBACK_SECRET`                  | `3o9mkW...REDACTED...RCT6b`                                              | HMAC secret for RunPod callbacks           |
| `QUANT_FOUNDRY_ENABLED`                          | `true`                                                                                          | Enable Quant Foundry gateway               |
| `QUANT_FOUNDRY_MODE`                             | `runpod_shadow`                                                                                 | RunPod shadow dispatch mode                |
| `QUANT_FOUNDRY_MONTHLY_BUDGET_CENTS`             | `1000`                                                                                          | Monthly GPU budget ($10.00)                |
| `QUANT_FOUNDRY_RUNPOD_INFERENCE_ENDPOINT`        | `36mz2q30jdyvru`                                                                                | Inference endpoint ID (legacy name)        |
| `QUANT_FOUNDRY_RUNPOD_TRAINING_ENDPOINT`         | `8vol1uc9l75jgs`                                                                                | Training endpoint ID (legacy name)         |
| `QUANT_FOUNDRY_SETTLEMENT_INTERVAL_SECONDS`      | `60`                                                                                            | Settlement loop interval                   |
| `QUANT_FOUNDRY_SHADOW_DISPATCH_INTERVAL_SECONDS` | `300`                                                                                           | Shadow dispatch loop interval (5 min)      |
| `QUANT_FOUNDRY_TOURNAMENT_INTERVAL_SECONDS`      | `300`                                                                                           | Tournament loop interval (5 min)           |
| `RUNPOD_API_KEY`                                 | `rpa_I54B...REDACTED...xzxipx`                                            | RunPod API key (50 chars)                  |
| `RUNPOD_BASE_URL`                                | `https://api.runpod.ai/v2`                                                                      | RunPod REST API base URL                   |
| `RUNPOD_INFERENCE_ENDPOINT_ID`                   | `36mz2q30jdyvru`                                                                                | Inference endpoint ID (gateway reads this) |
| `RUNPOD_TRAINING_ENDPOINT_ID`                    | `8vol1uc9l75jgs`                                                                                | Training endpoint ID (gateway reads this)  |
| `RAILWAY_RUN_CMD`                                | `/opt/venv/bin/uvicorn api.main:app --host 0.0.0.0 --port $PORT`                                | Start command                              |

### Railway Dashboard Service

| Variable              | Value                                         |
| --------------------- | --------------------------------------------- |
| `NEXT_PUBLIC_API_URL` | `https://api-production-73610.up.railway.app` |

### RunPod Endpoint Env Vars (both endpoints)

| Variable                                  | Value                                                                                   |
| ----------------------------------------- | --------------------------------------------------------------------------------------- |
| `QUANT_FOUNDRY_CALLBACK_SECRET`           | `3o9mkW...REDACTED...RCT6b`                                      |
| `PYTHONPATH`                              | `/runpod-volume/fincept-terminal/services/quant_foundry/src:/runpod-volume/python-libs` |
| `QUANT_FOUNDRY_TRAINING_DEADLINE_SECONDS` | `600` (training endpoint only)                                                          |
| `QUANT_FOUNDRY_MODE`                      | `runpod_shadow` (inference endpoint only)                                               |

---

## 6. Credentials & Secrets

> **WARNING:** These secrets are included for operational reference. They should be rotated periodically and never committed to git.

| Secret                        | Value                                                | Where Used                               |
| ----------------------------- | ---------------------------------------------------- | ---------------------------------------- |
| RunPod API Key                | `rpa_I54B...REDACTED...xzxipx` | Railway API env, local scripts           |
| Quant Foundry Callback Secret | `3o9mkW...REDACTED...RCT6b`   | Railway API + RunPod endpoints           |
| Railway Postgres Password     | `FJfMaum...REDACTED...Ryfny`                   | Railway Postgres                         |
| Railway Redis Password        | `cQsSbur...REDACTED...VdwK`                   | Railway Redis                            |
| GitHub PAT (read:packages)    | `ghp_407o...REDACTED...5US3`           | Local scripts for ghcr.io API            |
| JWT Secret                    | `dev-only-change-me`                                 | Railway API (MUST change for production) |

---

## 7. RunPod Endpoint Configuration

### Training Endpoint (`8vol1uc9l75jgs`)

| Property        | Value                                                        |
| --------------- | ------------------------------------------------------------ |
| Name            | `fincept-qf-training`                                        |
| Template ID     | `me58r5vdrp`                                                 |
| Container Image | `runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04`   |
| GPU Type        | RTX 4090                                                     |
| Workers Min     | 0 (auto-scale)                                               |
| Workers Max     | 1                                                            |
| Container Disk  | 20 GB                                                        |
| Network Volume  | 10 GB at `/workspace` (pods) / `/runpod-volume` (serverless) |
| Registry Auth   | None (using public base image)                               |
| Current Health  | 1 worker ready, 4 jobs completed                             |

### Inference Endpoint (`36mz2q30jdyvru`)

| Property        | Value                                                        |
| --------------- | ------------------------------------------------------------ |
| Name            | `fincept-qf-inference`                                       |
| Template ID     | `wnasp3v5jn`                                                 |
| Container Image | `runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04`   |
| GPU Type        | RTX 4090                                                     |
| Workers Min     | 0 (auto-scale)                                               |
| Workers Max     | 1                                                            |
| Container Disk  | 20 GB                                                        |
| Network Volume  | 10 GB at `/workspace` (pods) / `/runpod-volume` (serverless) |
| Registry Auth   | None (using public base image)                               |
| Current Health  | 1 worker throttled, 5 jobs completed                         |

### Handler Loading Mechanism

The endpoints do NOT use the custom ghcr.io container images. Instead, they use the public `runpod/pytorch` base image and load the handler code from the RunPod network volume:

1. RunPod starts the container with `runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04`
2. The network volume is mounted at `/runpod-volume`
3. Start scripts (`/runpod-volume/start-training.sh`, `/runpod-volume/start-inference.sh`) set `PYTHONPATH` and launch the handler
4. Handler code is at `/runpod-volume/fincept-terminal/runpod/quant-foundry-training/handler.py`
5. Quant Foundry library is at `/runpod-volume/fincept-terminal/services/quant_foundry/src/quant_foundry/`
6. Python dependencies are at `/runpod-volume/python-libs/`

To update the handler code on the volume: SSH into a RunPod pod or use a pod with the volume attached, then `cd /workspace/fincept-terminal && git pull`.

---

## 8. GitHub Container Registry (ghcr.io) State

### Built Images

| Image                                                    | Package Name                      | Visibility  | Versions |
| -------------------------------------------------------- | --------------------------------- | ----------- | -------- |
| `ghcr.io/airyder/fincept/quant-foundry-training:latest`  | `fincept/quant-foundry-training`  | **Private** | 3        |
| `ghcr.io/airyder/fincept/quant-foundry-inference:latest` | `fincept/quant-foundry-inference` | **Private** | 3        |

### Package URLs

- Training: `https://github.com/users/AIRYDER/packages/container/package/fincept%2Fquant-foundry-training`
- Inference: `https://github.com/users/AIRYDER/packages/container/package/fincept%2Fquant-foundry-inference`

### Issue: Packages Are Still Private

Despite attempts to make them public:

- GitHub API confirms both packages have `visibility: private`
- GitHub does not support changing package visibility via the REST API (confirmed by StackOverflow and GitHub issues)
- The visibility must be changed via the GitHub web UI at the package settings page
- A RunPod registry auth was created (`cmqu88226004fq1f5c9n21jh9`) and linked to both endpoint templates, but the endpoints were reverted to the public base image before testing if it works

### Current Workaround

The endpoints use the public `runpod/pytorch` base image with handler code loaded from the network volume. The custom ghcr.io images are built and available but **not currently in use**.

---

## 9. The Training Job That Succeeded

### Job: `qf:train:systest:004`

| Property        | Value                                     |
| --------------- | ----------------------------------------- |
| Job ID          | `qf:train:systest:004`                    |
| Job Type        | `training`                                |
| Idempotency Key | `systest-004-20260625190000`              |
| Status          | `completed`                               |
| RunPod Job ID   | `d9117d08-57de-42ec-979f-97ebbafc5513-u1` |
| RunPod Endpoint | `8vol1uc9l75jgs` (training)               |
| Duration        | 0.25 seconds                              |
| Created At      | 2026-06-25T19:00:05Z                      |
| Completed At    | 2026-06-25T19:51:43Z                      |

### Request Payload

```json
{
  "schema_version": 1,
  "job_id": "qf:train:systest:004",
  "dataset_manifest_ref": "synthetic:test:systest:001",
  "model_family": "gbm",
  "search_space": {
    "num_leaves": [31],
    "learning_rate": [0.1],
    "n_estimators": [50]
  },
  "random_seed": 42,
  "hardware_class": "test-cpu"
}
```

### Job History

| Status        | Timestamp    | Note                                 |
| ------------- | ------------ | ------------------------------------ |
| `queued`      | 19:00:05.189 | Job created                          |
| `dispatching` | 19:00:05.190 | Gateway picking up                   |
| `dispatched`  | 19:00:05.191 | Sent to RunPod                       |
| `running`     | 19:00:05.446 | RunPod accepted (cost: $0.00, 0.25s) |
| `validating`  | 19:51:43.353 | Callback received, verifying         |
| `completed`   | 19:51:43.355 | Dossier registered                   |

### Dispatch Command

```powershell
$token = "<JWT token>"
$body = @{
    job_id = "qf:train:systest:004"
    job_type = "training"
    idempotency_key = "systest-004-20260625190000"
    request_payload = @{
        schema_version = 1
        job_id = "qf:train:systest:004"
        dataset_manifest_ref = "synthetic:test:systest:001"
        model_family = "gbm"
        search_space = @{
            num_leaves = @(31)
            learning_rate = @(0.1)
            n_estimators = @(50)
        }
        random_seed = 42
        hardware_class = "test-cpu"
    }
    priority = 0
    budget_cents = 0
} | ConvertTo-Json -Depth 10

Invoke-RestMethod -Uri "https://api-production-73610.up.railway.app/quant-foundry/jobs" `
    -Method POST -Headers @{Authorization = "Bearer $token"; "Content-Type" = "application/json"} `
    -Body $body
```

---

## 10. Model Analysis

### Registered Dossier

| Field            | Value                                                              |
| ---------------- | ------------------------------------------------------------------ |
| Model ID         | `model:qf:train:systest:004`                                       |
| Artifact ID      | `artifact:490d8a8863c193f3`                                        |
| Artifact SHA-256 | `490d8a8863c193f3c4f948671b2701106d605febbaa507cff3ecefa0240ae2fa` |
| Dataset          | `synthetic:test:systest:001`                                       |
| Model Family     | `gbm`                                                              |
| Random Seed      | 42                                                                 |
| Hardware Class   | `test-cpu`                                                         |
| Authority        | `SHADOW_ONLY`                                                      |
| Status           | `candidate`                                                        |

### Training Metrics

| Metric          | Value | How Computed                           |
| --------------- | ----- | -------------------------------------- |
| Accuracy        | 0.81  | `0.5 + PBO/2`                          |
| LogLoss         | 0.545 | `0.7 - PBO/4`                          |
| PBO             | 0.62  | `(seed_hash % 100) / 100`              |
| Deflated Sharpe | -0.33 | `((seed_hash >> 8) % 300) / 100 - 1.0` |

### Provenance

| Field                    | Value                    | Notes                                   |
| ------------------------ | ------------------------ | --------------------------------------- |
| `code_git_sha`           | `local-git-sha`          | Placeholder — not pinned to real commit |
| `lockfile_hash`          | `local-lockfile-hash`    | Placeholder                             |
| `container_image_digest` | `local-container-digest` | Placeholder                             |

### Analysis Summary

**This is a stub model, not a real LightGBM model.** The `LocalTrainer` generates a deterministic hash-based artifact with synthetic metrics derived from the random seed. No actual ML training occurred. The artifact hash is SHA-256 of the canonical request JSON, making it deterministic and verifiable.

The model has `authority=SHADOW_ONLY` — it can never be promoted to live trading without human approval. This is a security invariant enforced by the system.

**What this proves:**

- The full RunPod dispatch → worker → callback → dossier pipeline works end-to-end
- HMAC signature verification works correctly
- Artifact hashing is deterministic and reproducible
- The dossier registry persists models correctly

**What this does NOT prove:**

- Real LightGBM training (no actual model was trained)
- Real dataset ingestion (used synthetic dataset reference)
- Real model artifact storage (artifact has `uri=None`)
- Real provenance tracking (placeholder git/lockfile/container hashes)

---

## 11. Root Causes Fixed This Session

### Bug 1: Budget Gate Blocking Dispatch

**Symptom:** Training jobs failed with a budget error despite the RunPod API key being set.

**Root Cause:** `QUANT_FOUNDRY_MONTHLY_BUDGET_CENTS` was set to 0 (default), which blocked all dispatches.

**Fix:** Set `QUANT_FOUNDRY_MONTHLY_BUDGET_CENTS=1000` ($10.00 monthly budget) in Railway API env vars.

### Bug 2: RunPod Endpoint Env Var Name Mismatch

**Symptom:** After fixing the budget, dispatch failed with `runpod_endpoint_not_configured`.

**Root Cause:** The gateway code reads `RUNPOD_TRAINING_ENDPOINT_ID` and `RUNPOD_INFERENCE_ENDPOINT_ID`, but the Railway env vars were named `QUANT_FOUNDRY_RUNPOD_TRAINING_ENDPOINT` and `QUANT_FOUNDRY_RUNPOD_INFERENCE_ENDPOINT`.

**Fix:** Added the correctly-named env vars to Railway:

- `RUNPOD_TRAINING_ENDPOINT_ID=8vol1uc9l75jgs`
- `RUNPOD_INFERENCE_ENDPOINT_ID=36mz2q30jdyvru`

### Bug 3: Callback Secret Mismatch (THE CRITICAL BUG)

**Symptom:** RunPod jobs completed (visible in RunPod health: `completed: 3`), but the API job stayed `running` forever. No dossier was registered.

**Root Cause:** The RunPod endpoint env vars had `QUANT_FOUNDRY_CALLBACK_SECRET=fc975be...REDACTED...` but the Railway API had `QUANT_FOUNDRY_CALLBACK_SECRET=3o9mkW...REDACTED...`. The RunPod worker signed callbacks with the wrong secret, so the API rejected them with a signature verification failure.

**Fix:** Updated both RunPod endpoint templates to use the correct callback secret (`3o9mkW...REDACTED...RCT6b`) via the RunPod GraphQL API, then recycled the workers.

### Bug 4: RunPod API Key Truncation

**Symptom:** Local scripts using the RunPod API key returned `401 Unauthorized`.

**Root Cause:** The Railway CLI truncated the last character of the API key when displaying it. The key was 50 characters but Railway CLI showed 49.

**Fix:** User provided the full 50-character key directly. Updated the Railway env var with the complete key.

### Bug 5: ghcr.io Image Auth Failure

**Symptom:** After updating RunPod endpoints to use the custom ghcr.io container images, workers went unhealthy with `IMAGE_AUTH_ERROR: unauthorized`.

**Root Cause:** The ghcr.io packages were private and RunPod couldn't pull them without registry credentials.

**Attempted Fixes:**

1. Tried making packages public via GitHub API — failed (GitHub doesn't support this via API)
2. Tried making packages public via GitHub web UI — user attempted but packages still show as private in the API
3. Created RunPod registry auth (`cmqu88226004fq1f5c9n21jh9`) with GitHub PAT — created successfully and linked to templates, but workers still went unhealthy
4. **Final workaround:** Reverted both endpoints to the public `runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04` base image with handler code loaded from the network volume

### Bug 6: GitHub Repo Name Uppercase (CI)

**Symptom:** GitHub Actions workflow failed when building RunPod Docker images.

**Root Cause:** The GitHub repository name `fincept-terminal` was being used as-is for Docker image tags, but Docker image tags must be lowercase. The repo owner `AIRYDER` was also uppercase.

**Fix:** Updated the workflow to use lowercase: `ghcr.io/airyder/fincept/quant-foundry-training:latest` (commit `1ce2e66`).

---

## 12. Commands Reference

### Generate a JWT Token (for API auth)

```powershell
cd "C:\Users\nolan\CascadeProjects\fincept-terminal"
uv run python -c "
import jwt, time
token = jwt.encode({'sub': 'operator', 'exp': int(time.time()) + 3600}, 'dev-only-change-me', algorithm='HS256')
print(token)
"
```

### Check API Health

```powershell
Invoke-RestMethod -Uri "https://api-production-73610.up.railway.app/health"
```

### List All Jobs

```powershell
$token = "<JWT>"
Invoke-RestMethod -Uri "https://api-production-73610.up.railway.app/quant-foundry/jobs" `
    -Method GET -Headers @{Authorization = "Bearer $token"}
```

### List All Dossiers

```powershell
$token = "<JWT>"
Invoke-RestMethod -Uri "https://api-production-73610.up.railway.app/quant-foundry/dossiers" `
    -Method GET -Headers @{Authorization = "Bearer $token"}
```

### Dispatch a Training Job

```powershell
$token = "<JWT>"
$body = @{
    job_id = "qf:train:systest:005"
    job_type = "training"
    idempotency_key = "systest-005-$(Get-Date -Format yyyyMMddHHmmss)"
    request_payload = @{
        schema_version = 1
        job_id = "qf:train:systest:005"
        dataset_manifest_ref = "synthetic:test:systest:001"
        model_family = "gbm"
        search_space = @{
            num_leaves = @(31)
            learning_rate = @(0.1)
            n_estimators = @(50)
        }
        random_seed = 42
        hardware_class = "test-cpu"
    }
    priority = 0
    budget_cents = 0
} | ConvertTo-Json -Depth 10

Invoke-RestMethod -Uri "https://api-production-73610.up.railway.app/quant-foundry/jobs" `
    -Method POST -Headers @{Authorization = "Bearer $token"; "Content-Type" = "application/json"} `
    -Body $body
```

### Check RunPod Endpoint Health

```python
import requests
key = "rpa_I54B...REDACTED...xzxipx"
r = requests.get("https://api.runpod.ai/v2/8vol1uc9l75jgs/health",
    headers={"Authorization": f"Bearer {key}"})
print(r.json())
```

### Check RunPod Job Status

```python
import requests
key = "rpa_I54B...REDACTED...xzxipx"
job_id = "d9117d08-57de-42ec-979f-97ebbafc5513-u1"
r = requests.get(f"https://api.runpod.ai/v2/8vol1uc9l75jgs/status/{job_id}",
    headers={"Authorization": f"Bearer {key}"})
print(r.json())
```

### Recycle RunPod Workers

```powershell
python "C:\Users\nolan\AppData\Local\Temp\recycle_runpod_workers.py" "rpa_I54B...REDACTED...xzxipx"
```

### Redeploy Railway API

```powershell
cd "C:\Users\nolan\CascadeProjects\fincept-terminal"
railway redeploy --service api --yes
```

### Set Railway Env Var

```powershell
cd "C:\Users\nolan\CascadeProjects\fincept-terminal"
railway vars --service api --set 'KEY=value'
```

### Run Full Test Suite

```powershell
cd "C:\Users\nolan\CascadeProjects\fincept-terminal"
$env:UV_CACHE_DIR = (Resolve-Path '.').Path + '\.uv-cache'
uv run pytest --tb=short -q
```

### Trigger GitHub Actions Build

```powershell
cd "C:\Users\nolan\CascadeProjects\fincept-terminal"
gh workflow run build-images.yml --ref codex/portfolio-optimizer-core
gh run list --workflow=build-images.yml --limit 3
```

---

## 13. Key Files

### Configuration

| File                                 | Purpose                                                    |
| ------------------------------------ | ---------------------------------------------------------- |
| `nixpacks.toml`                      | Railway build config (Python 3.12, uv, venv at /opt/venv)  |
| `railway.json`                       | Railway deploy config (healthcheck, restart policy)        |
| `.github/workflows/build-images.yml` | CI workflow for building Docker images + RunPod containers |

### Quant Foundry Core

| File                                                          | Purpose                                                 |
| ------------------------------------------------------------- | ------------------------------------------------------- |
| `services/quant_foundry/src/quant_foundry/gateway.py`         | Gateway — dispatch, polling, shadow loop, settlement    |
| `services/quant_foundry/src/quant_foundry/runpod_training.py` | Training handler — LocalTrainer + RunPodTrainingHandler |
| `services/quant_foundry/src/quant_foundry/callbacks.py`       | Callback ingestion → durable stores                     |
| `services/quant_foundry/src/quant_foundry/signatures.py`      | HMAC callback signing/verification                      |
| `services/quant_foundry/src/quant_foundry/schemas.py`         | Pydantic schemas (ArtifactManifest, ModelDossier, etc.) |

### RunPod Handlers

| File                                        | Purpose                                       |
| ------------------------------------------- | --------------------------------------------- |
| `runpod/quant-foundry-training/handler.py`  | RunPod serverless training worker entrypoint  |
| `runpod/quant-foundry-training/Dockerfile`  | Training container image definition           |
| `runpod/quant-foundry-inference/handler.py` | RunPod serverless inference worker entrypoint |
| `runpod/quant-foundry-inference/Dockerfile` | Inference container image definition          |

### API

| File                           | Purpose                                                         |
| ------------------------------ | --------------------------------------------------------------- |
| `services/api/src/api/main.py` | FastAPI app + Quant Foundry route registration + startup wiring |

### Documentation

| File                           | Purpose                                      |
| ------------------------------ | -------------------------------------------- |
| `RUNPOD_SESSION_HANDOFF.md`    | Prior session handoff (RunPod setup details) |
| `docs/GPU_DEPLOYMENT_GUIDE.md` | Deployed IDs, env vars, deployment steps     |
| `docs/ROADMAP.md`              | Project roadmap                              |

### Temp Scripts (created this session)

| File                                                           | Purpose                                               |
| -------------------------------------------------------------- | ----------------------------------------------------- |
| `C:\Users\nolan\AppData\Local\Temp\update_runpod_endpoints.py` | Update RunPod endpoint container images via GraphQL   |
| `C:\Users\nolan\AppData\Local\Temp\recycle_runpod_workers.py`  | Kill + respawn RunPod workers via GraphQL             |
| `C:\Users\nolan\AppData\Local\Temp\fix_callback_secret.py`     | Fix callback secret on RunPod endpoints               |
| `C:\Users\nolan\AppData\Local\Temp\setup_registry_auth_v2.py`  | Set up RunPod container registry auth for ghcr.io     |
| `C:\Users\nolan\AppData\Local\Temp\trigger_runpod_release.py`  | Trigger RunPod endpoint release by re-saving template |

---

## 14. Known Issues & Limitations

### 1. Stub Trainer (Not Real LightGBM)

The current handler on the RunPod volume uses `LocalTrainer` — a deterministic stub that generates hash-based artifacts with synthetic metrics. No real ML training occurs. To use real LightGBM training, the handler needs to be updated to use `RealLightGBMTrainer` (which exists in the codebase but isn't wired into the volume handler).

### 2. ghcr.io Images Not Deployed

Custom Docker images with the handler baked in were built and pushed to ghcr.io, but the packages are still private. RunPod cannot pull them. The endpoints are using the public `runpod/pytorch` base image with handler code loaded from the network volume instead. To switch to the custom images:

- Option A: Make the ghcr.io packages public via GitHub web UI (package settings → Danger Zone → Change visibility)
- Option B: Set up RunPod registry auth with a GitHub PAT (registry auth ID `cmqu88226004fq1f5c9n21jh9` was created but not verified to work)

### 3. Provenance Placeholders

The model dossier has placeholder values for `code_git_sha`, `lockfile_hash`, and `container_image_digest`. A production model would have real values pinned at build time. The Dockerfile has `ARG GIT_SHA=unknown` which should be overridden with `--build-arg GIT_SHA=$(git rev-parse HEAD)`.

### 4. JWT Secret Is Dev-Only

`FINCEPT_JWT_SECRET=dev-only-change-me` — this must be changed to a strong secret before any production use.

### 5. No Real Dataset

The training job used `synthetic:test:systest:001` as the dataset manifest reference. No real dataset was loaded. The system needs a real dataset manifest and data pipeline to produce meaningful models.

### 6. Inference Endpoint Throttled

The inference endpoint worker is currently `throttled` (not `ready`). This is normal RunPod behavior when there are no jobs in the queue — the worker goes to sleep and wakes up when a job arrives.

### 7. Dual Env Var Names

Both `QUANT_FOUNDRY_RUNPOD_TRAINING_ENDPOINT` and `RUNPOD_TRAINING_ENDPOINT_ID` exist in the Railway env vars. The gateway reads the latter. The former is redundant and could be removed.

---

## 15. Next Steps

### Immediate (to get real models)

1. **Update handler on RunPod volume to use `RealLightGBMTrainer`**
   
   - SSH into a RunPod pod with the network volume attached
   - `cd /workspace/fincept-terminal && git pull`
   - Update the handler to instantiate `RealLightGBMTrainer` instead of `LocalTrainer`
   - Recycle the workers

2. **Create a real dataset manifest**
   
   - Prepare actual market data (OHLCV bars, features, labels)
   - Register it as a dataset manifest in the system
   - Dispatch a training job with the real dataset reference

3. **Make ghcr.io packages public** (or verify registry auth works)
   
   - Go to https://github.com/users/AIRYDER/packages/container/package/fincept%2Fquant-foundry-training
   - Click "Package settings" → Danger Zone → Change visibility to Public
   - Repeat for the inference package
   - Update RunPod endpoint templates to use the custom images
   - Recycle workers

### Short-term (production hardening)

4. **Rotate the JWT secret** — replace `dev-only-change-me` with a strong 32+ byte secret
5. **Pin provenance** — build Docker images with `--build-arg GIT_SHA=$(git rev-parse HEAD)` and real lockfile hashes
6. **Set up monitoring** — alert on callback rejection rate, worker unhealthy count, job failure rate
7. **Configure budget alerts** — monitor `QUANT_FOUNDRY_MONTHLY_BUDGET_CENTS` usage

### Medium-term (full system)

8. **Wire up real feature pipeline** — connect the feature store to produce real feature snapshots for shadow inference
9. **Run shadow inference** — let the shadow dispatch loop run to produce predictions and track model performance
10. **Settlement evidence** — connect the settlement system to compare predictions against actual market outcomes
11. **Tournament promotion** — implement the tournament loop that promotes well-performing shadow models to live trading (with human approval gate)

---

## 16. Bootstrap Path: Breaking the Promotion Deadlock

### The Problem

The Quant Foundry promotion system has a **chicken-and-egg deadlock by design**:

```
candidate model
  → needs 10 settled predictions to promote to shadow_approved
  → needs shadow predictions to settle
  → needs shadow_approved model to dispatch shadow inference
  → deadlock
```

The `PromotionGate` in `promotion.py` requires `settled_count >= 10` for ALL promotion levels. There is no differentiated threshold for lower levels, no waiver mechanism for insufficient evidence, and no bypass flag. The gate fails closed — this is a security invariant.

### The Fix: Configurable Minimum Settled Count

A new environment variable `QUANT_FOUNDRY_PROMOTION_MIN_SETTLED` was added to make the threshold configurable:

| Variable | Default | Purpose |
|---|---|---|
| `QUANT_FOUNDRY_PROMOTION_MIN_SETTLED` | `10` | Minimum settled predictions required for promotion |

**Code change:** `services/quant_foundry/src/quant_foundry/gateway.py`, `promotion_gate` property (line ~925):

```python
min_settled = int(os.environ.get("QUANT_FOUNDRY_PROMOTION_MIN_SETTLED", "10"))
self._promotion_gate = PromotionGate(min_settled_count=min_settled)
```

### System Impact

| Aspect | Impact |
|---|---|
| **Security** | Lowering the threshold weakens the evidence requirement for ALL promotion levels. A model promoted with threshold=0 has not been validated against real market outcomes. |
| **Authority** | Promoted models still carry `authority=SHADOW_ONLY`. They cannot reach live trading without further human approval. The `SHADOW_ONLY` invariant is unaffected. |
| **Audit trail** | The promotion receipt records the decision (approved/rejected) but does NOT record the threshold value. Operators must audit env vars when reviewing promotion history. |
| **All levels affected** | The same threshold applies to `research_approved`, `shadow_approved`, and `paper_approved`. There is no per-level differentiation. |
| **Reversibility** | Raising the threshold back to 10 after real settlements accumulate restores the full evidence requirement. New promotions will require 10+ settlements; existing promotions are not retroactively re-evaluated. |

### Bootstrap Procedure

1. **Set the env var to 0 on Railway:**
   ```powershell
   railway vars --service api --set 'QUANT_FOUNDRY_PROMOTION_MIN_SETTLED=0'
   railway redeploy --service api --yes
   ```

2. **Submit promotion for a candidate model:**
   ```powershell
   $token = "<JWT>"
   $body = @{
       model_id = "model:qf:train:systest:005"
       target_level = "shadow_approved"
       review_note = "Bootstrap promotion — threshold lowered for initial shadow inference"
   } | ConvertTo-Json
   Invoke-RestMethod -Uri "https://api-production-73610.up.railway.app/quant-foundry/promotion/submit" `
       -Method POST -Headers @{Authorization = "Bearer $token"; "Content-Type" = "application/json"} `
       -Body $body
   ```

3. **Approve the promotion:**
   ```powershell
   $body = @{
       model_id = "model:qf:train:systest:005"
       review_note = "Approved for bootstrap shadow testing"
   } | ConvertTo-Json
   Invoke-RestMethod -Uri "https://api-production-73610.up.railway.app/quant-foundry/promotion/approve" `
       -Method POST -Headers @{Authorization = "Bearer $token"; "Content-Type" = "application/json"} `
       -Body $body
   ```

4. **Wait for shadow dispatch loop** (runs every 300s by default):
   - The loop finds `SHADOW_APPROVED` models
   - Builds a feature snapshot (empty if no feature lake is wired)
   - Dispatches an inference job to the RunPod inference endpoint
   - The inference worker processes the job and returns a signed callback
   - The callback is ingested and a shadow prediction is stored in the `ShadowLedger`

5. **Verify shadow predictions are flowing:**
   ```powershell
   Invoke-RestMethod -Uri "https://api-production-73610.up.railway.app/quant-foundry/shadow/health" `
       -Method GET -Headers @{Authorization = "Bearer $token"}
   ```

6. **Once 10+ real settlements accumulate, restore the threshold:**
   ```powershell
   railway vars --service api --set 'QUANT_FOUNDRY_PROMOTION_MIN_SETTLED=10'
   railway redeploy --service api --yes
   ```

### Security Notes

- **This env var should be set to 0 ONLY during the initial bootstrap phase.**
- Once the shadow inference loop is producing real predictions and the settlement system has accumulated 10+ settled records per model, the threshold MUST be raised back to 10.
- The promotion gate is the primary quality gate preventing untested models from reaching shadow inference. Lowering it bypasses this gate.
- All promotions made with a lowered threshold should be audited. The promotion receipt does not record the threshold value, so operators must check env var history.
- The `authority=SHADOW_ONLY` invariant is NOT affected — models still cannot reach live trading without further human approval.

---

*Report generated 2026-06-26. System state verified at time of writing. Updated with bootstrap path documentation.*
