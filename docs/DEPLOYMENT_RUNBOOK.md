# Deployment Runbook — Quant Foundry Limited Live Pilot

**Status:** Operational runbook
**Date:** 2026-06-25
**Scope:** Step-by-step deployment of the Quant Foundry limited paper-to-live pilot
  (Railway control plane + RunPod GPU workers + shadow inference + promotion +
  paper bridge).
**Posture:** This runbook is the operator-facing companion to
  `docs/LIMITED_LIVE_READINESS_REVIEW.md` (go/no-go synthesis),
  `docs/RAILWAY_DEPLOY_GUIDE.md` (Railway production config),
  `docs/RAILWAY_STAGING_GUIDE.md` (staging-only),
  `docs/AWS_PRODUCTION_CONTROL_PLANE.md` (AWS upgrade path), and
  `docs/RUNPOD_TRAINING_ARCHITECTURE.md` (GPU training loop).

> **Read before executing:** The readiness review
> (`docs/LIMITED_LIVE_READINESS_REVIEW.md`) marks the system **NOT READY** as of
> its authoring date. Every step in this runbook is structured to resolve a
> specific blocker (B1–B8) from that review. Do not skip the verification
> commands at the end of each section — they are the evidence that the blocker
> is cleared.

---

## Table of Contents

1. [Prerequisites](#1-prerequisites)
2. [Environment Variables (complete table)](#2-environment-variables-complete-table)
3. [Railway Staging Setup](#3-railway-staging-setup)
4. [RunPod Container Rebuild](#4-runpod-container-rebuild)
5. [Network Volume Mount](#5-network-volume-mount)
6. [First Real Training Job](#6-first-real-training-job)
7. [Shadow Inference Dispatch](#7-shadow-inference-dispatch)
8. [Promotion](#8-promotion)
9. [Paper Bridge Enablement](#9-paper-bridge-enablement)
10. [Rollback / Disable](#10-rollback--disable)
11. [Monitoring Checklist](#11-monitoring-checklist)

---

## 1. Prerequisites

### 1.1 Accounts needed

| # | Account | Purpose | URL |
|---|---------|---------|-----|
| 1 | **Railway** | Hosts the control plane: API (FastAPI), Dashboard (Next.js), managed Postgres, managed Redis, Object Storage. | https://railway.app |
| 2 | **RunPod** | GPU training + shadow inference serverless endpoints. | https://runpod.io |
| 3 | **Alpaca** | Paper-trading broker sandbox for the paper bridge (Phase 9). Use the **paper** API only (`https://paper-api.alpaca.markets`). | https://alpaca.markets |
| 4 | **FRED** (St. Louis Fed) | Macro regime features (optional but recommended for the regime agent). | https://fred.stlouisfed.org |
| 5 | **NewsAPI** | News sentiment features (optional; the news scheduler skips gracefully if unset). | https://newsapi.org |
| 6 | **Redis provider** | Railway managed Redis is the default. An external provider (Upstash, ElastiCache) is only needed if migrating to AWS. | — |
| 7 | **GitHub** | Railway builds from the repo via Nixpacks; the repo must be connected to Railway. | https://github.com |

### 1.2 API keys / secrets to provision

Generate these **before** the first deploy. Store them in a password manager or
secrets vault — never in the repo.

| Secret | How to generate | Where it goes |
|--------|-----------------|---------------|
| `QUANT_FOUNDRY_CALLBACK_SECRET` | `openssl rand -hex 32` | Railway API service (Secret) **and** RunPod training + inference endpoint templates. **Must be identical on both sides.** |
| `FINCEPT_JWT_SECRET` | `openssl rand -hex 32` | Railway API service (Secret). The runtime safety guard (`assert_safe_for_runtime()` in `libs/fincept-core/src/fincept_core/config.py:128`) refuses to start in staging/production if this is the dev default `dev-only-change-me`. |
| `RUNPOD_API_KEY` | RunPod dashboard → Account Settings → API Keys | Railway API service (Secret). |
| `FINCEPT_ALPACA_API_KEY` | Alpaca dashboard → Paper Trading → API Keys | Railway API service (Secret). **Paper keys only.** Only needed for Phase 9 (paper bridge). Do NOT set during Phases 3–8. |
| `FINCEPT_ALPACA_API_SECRET` | Alpaca dashboard (same screen as the key) | Railway API service (Secret). Paper secret only. |
| `FINCEPT_FRED_API_KEY` | FRED account → API Keys | Railway API service (variable). Optional. |
| `FINCEPT_NEWSAPI_API_KEY` | NewsAPI account → API Keys | Railway API service (variable). Optional. |

> **Naming note:** `fincept_core.Settings` uses the env prefix `FINCEPT_`
> (`libs/fincept-core/src/fincept_core/config.py:19`). So the Pydantic field
> `ALPACA_API_KEY` is read from the env var `FINCEPT_ALPACA_API_KEY`, the field
> `JWT_SECRET` from `FINCEPT_JWT_SECRET`, etc. The task brief mentions
> `ALPACA_API_KEY` / `ALPACA_API_SECRET` / `NEWSAPI_KEY` / `FRED_API_KEY` —
> those are the Pydantic field names; the **actual env var names** carry the
> `FINCEPT_` prefix. The staging guide (`docs/RAILWAY_STAGING_GUIDE.md`)
> references `ALPACA_SECRET_KEY` in its "NOT set" table — that is a stale alias;
> the canonical name is `FINCEPT_ALPACA_API_SECRET`.

### 1.3 Local tooling

| Tool | Min version | Install | Why |
|------|-------------|---------|-----|
| `docker` | 24.x | https://docs.docker.com/get-docker/ | Build RunPod training + inference container images. |
| `docker buildx` | bundled | — | Multi-platform builds if pushing to RunPod registry. |
| `railway` CLI | latest | `npm i -g @railway/cli` | Optional: deploy + tail logs from the terminal. The dashboard works too. |
| `runpod` CLI | latest (if available) | `pip install runpod` | Optional: the RunPod serverless SDK is already in the container images; the CLI is only for ad-hoc endpoint management. The dashboard is the primary surface. |
| `openssl` | any | bundled on macOS/Linux; `Git Bash` on Windows | Generate secrets. |
| `curl` | any | bundled | Run the verification `curl` examples in this runbook. |
| `python` | 3.12+ | https://python.org | Mint JWTs for `curl` examples (see §6.1). |
| `git` | any | bundled | Pin `GIT_SHA` at container build time. |

Verify local tooling:

```bash
docker --version
railway version 2>/dev/null || echo "railway CLI not installed (dashboard is fine)"
openssl version
curl --version
python --version
```

---

## 2. Environment Variables (complete table)

Every env var below is read by code in this repo. Defaults and service
assignments are taken from the actual source — `gateway.py:from_env()` (lines
283–394), `budget.py:from_env()` (lines 297–316), `main.py` (lines 231–311),
`fincept_core/config.py` (lines 25–78), the RunPod Dockerfiles, and the
handlers. **Do not guess; if a var is not in this table, the code does not read
it.**

### 2.1 Quant Foundry gateway (`QUANT_FOUNDRY_*`)

Read by `QuantFoundryGateway.from_env()` in
`services/quant_foundry/src/quant_foundry/gateway.py:283-394`.

| Variable | Required? | Default | Description | Used by |
|----------|-----------|---------|-------------|---------|
| `QUANT_FOUNDRY_ENABLED` | yes | `false` | Master switch. `true` enables the gateway + all background poll tasks. Default off — no jobs created or processed when `false`. | API gateway |
| `QUANT_FOUNDRY_MODE` | yes | `local_mock` | Dispatch mode. One of `local_mock`, `runpod`, `runpod_research`, `runpod_shadow`. RunPod modes wire `HttpRunPodClient` for training + inference; `local_mock` runs the full loop synchronously in-process. | API gateway |
| `QUANT_FOUNDRY_SHADOW_ONLY` | no | `true` | When `true`, the gateway never writes to `sig.predict` or any trading stream. Structural shadow isolation. | API gateway |
| `QUANT_FOUNDRY_CALLBACK_SECRET` | conditional (required in any RunPod mode) | `""` | HMAC-SHA256 secret used to sign/verify RunPod → API callbacks. `from_env()` raises `RunPodConfigError` if missing in RunPod mode. **Must be identical** on Railway and RunPod. | API gateway, training handler, inference handler |
| `QUANT_FOUNDRY_BASE_DIR` | no | `reports/quant-foundry` | Filesystem root for durable stores (outbox, inbox, shadow ledger, dossier registry, budget ledger). On Railway set to `/data/quant-foundry` (persistent volume). | API gateway |
| `QUANT_FOUNDRY_ALLOW_PAPER_BRIDGE` | conditional (required for Phase 9) | `""` (disabled) | `true` constructs `PaperBridge` and allows `publish()` to proceed. Unset/`false` → bridge refuses every publish with `"bridge is disabled (QUANT_FOUNDRY_ALLOW_PAPER_BRIDGE != true)"` (`paper_bridge.py:247`). | API gateway |
| `QUANT_FOUNDRY_WORKER_STATUS_DIR` | no | `""` (disabled) | Directory the gateway scans for worker heartbeat JSON files. Set to the RunPod network volume status path (e.g. `/runpod-volume/status`) so the gateway can detect stale/crashed workers. | API gateway |
| `QUANT_FOUNDRY_STALE_THRESHOLD_SECONDS` | no | `60` | Heartbeat staleness threshold. Workers whose `heartbeat_at` is older than this are flagged stale by `detect_stale_workers()`. | API gateway |

### 2.2 Budget guard

Read by `budget_from_env()` in
`services/quant_foundry/src/quant_foundry/budget.py:297-316`.

| Variable | Required? | Default | Description | Used by |
|----------|-----------|---------|-------------|---------|
| `QUANT_FOUNDRY_MONTHLY_BUDGET_CENTS` | no | `0` | Monthly GPU spend ceiling in cents. `0` = no paid jobs allowed until a budget is set. Enforced before any dispatch. | API gateway (BudgetGuard) |
| `QUANT_FOUNDRY_BUDGET_KILL_SWITCH` | no | `false` | `true` blocks ALL non-zero spend globally (`budget.py:137`). Defense-in-depth; set `true` on staging. | API gateway (BudgetGuard) |

### 2.3 RunPod dispatch (read by gateway in RunPod modes)

Constants defined at `gateway.py:129-137`. Canonical names first; legacy
fallbacks are read with a `DeprecationWarning`.

| Variable | Required? | Default | Description | Used by |
|----------|-----------|---------|-------------|---------|
| `RUNPOD_API_KEY` | conditional (required in RunPod mode) | — | RunPod API key for `POST /v2/{endpoint_id}/run`. Legacy fallback: `QUANT_FOUNDRY_RUNPOD_API_KEY`. | API gateway |
| `RUNPOD_TRAINING_ENDPOINT_ID` | conditional (required in RunPod mode) | — | RunPod serverless endpoint ID for the training worker. Legacy fallback: `QUANT_FOUNDRY_RUNPOD_TRAINING_ENDPOINT`, then `RUNPOD_ENDPOINT_ID`. | API gateway |
| `RUNPOD_INFERENCE_ENDPOINT_ID` | conditional (required in RunPod mode) | — | RunPod serverless endpoint ID for the inference worker. Legacy fallback: `QUANT_FOUNDRY_RUNPOD_INFERENCE_ENDPOINT`, then `RUNPOD_ENDPOINT_ID`. | API gateway |
| `RUNPOD_BASE_URL` | no | `https://api.runpod.ai/v2` | RunPod API base URL. | API gateway |
| `RUNPOD_TIMEOUT_SECONDS` | no | `30` | HTTP request timeout for dispatch/status calls. | API gateway |
| `RUNPOD_COST_PER_DISPATCH_CENTS` | no | `0` | Estimated cost per dispatch, fed to BudgetGuard. Set to the real GPU-seconds cost for accurate budget enforcement. | API gateway |

> **Task-brief alias note:** The brief lists `RUNPOD_TRAINING_ENDPOINT` and
> `RUNPOD_INFERENCE_ENDPOINT`. The canonical env var names in code are
> `RUNPOD_TRAINING_ENDPOINT_ID` and `RUNPOD_INFERENCE_ENDPOINT_ID`
> (`gateway.py:131,133`). The brief also lists `RUNPOD_API_KEY` — that one
> matches the code exactly.

### 2.4 API background poll intervals

Read by `services/api/src/api/main.py:231-311`. Each interval defaults to a
positive value; set to `0` to disable the corresponding background task.

| Variable | Required? | Default | Description | Used by |
|----------|-----------|---------|-------------|---------|
| `QUANT_FOUNDRY_RUNPOD_POLL_INTERVAL_SECONDS` | no | `15` | How often the API polls RunPod for completed job results (`gateway.poll_runpod_results`). Only runs in RunPod modes. | API gateway (lifespan) |
| `QUANT_FOUNDRY_TOURNAMENT_INTERVAL_SECONDS` | no | `300` | Tournament sweep interval (`gateway.run_tournament_sweep`). Runs when gateway enabled. | API gateway (lifespan) |
| `QUANT_FOUNDRY_SETTLEMENT_INTERVAL_SECONDS` | no | `60` | Settlement sweep interval (`gateway.run_settlement_sweep`). Runs when gateway enabled. | API gateway (lifespan) |
| `QUANT_FOUNDRY_SHADOW_DISPATCH_INTERVAL_SECONDS` | no | `300` | Shadow inference batch dispatch interval (`gateway.dispatch_shadow_inference_batch`). Set `>0` to enable the scheduled loop; `0` disables it (manual dispatch only via `POST /quant-foundry/shadow/dispatch`). | API gateway (lifespan) |
| `SETTLEMENTS_WORKER_POLL_S` | no | (see `settlements_poller.py`) | Interval for the generic settlements worker (fincept_core.datasets spine). Runs regardless of gateway mode; set `0` to disable. | API gateway (lifespan) |

### 2.5 RunPod training container

Read by `runpod/quant-foundry-training/handler.py` and baked into
`runpod/quant-foundry-training/Dockerfile`.

| Variable | Required? | Default | Description | Used by |
|----------|-----------|---------|-------------|---------|
| `QUANT_FOUNDRY_CALLBACK_SECRET` | yes (prod) | `""` (Dockerfile sets empty; handler raises `RuntimeError` if unset) | HMAC secret for signing callbacks. Must match the API side. | Training handler |
| `QUANT_FOUNDRY_TRAINING_DEADLINE_SECONDS` | no | `600` | Max wall-clock seconds for a training job. Handler returns `error_code="timeout"` if breached. | Training handler |
| `QUANT_FOUNDRY_USE_REAL_TRAINER` | no | `false` | `true` → `RealLightGBMTrainer` (real LightGBM + walk-forward validation). `false` → `LocalTrainer` (deterministic stub). **Set `true` for the pilot.** | Training handler |
| `QUANT_FOUNDRY_GIT_SHA` | no | `unknown` | Pinned at container build time via `ARG GIT_SHA`. Recorded in the `ArtifactManifest` for reproducibility. | Training container (build-time) |

### 2.6 RunPod inference container

Read by `runpod/quant-foundry-inference/handler.py` and baked into
`runpod/quant-foundry-inference/Dockerfile`.

| Variable | Required? | Default | Description | Used by |
|----------|-----------|---------|-------------|---------|
| `QUANT_FOUNDRY_CALLBACK_SECRET` | yes (prod) | `""` (Dockerfile sets empty; handler raises `RuntimeError` if unset) | HMAC secret for signing callbacks. Must match the API side. | Inference handler |
| `QUANT_FOUNDRY_MODE` | no | `runpod_shadow` (set in Dockerfile) | Enables inference. The handler checks `== "runpod_shadow"` to enable the engine. | Inference handler |
| `QUANT_FOUNDRY_USE_REAL_INFERENCE` | no | `false` | `true` → `RealInferenceEngine` (loads ONNX/LightGBM artifacts and runs real predictions). `false` → `ShadowInferenceEngine` (stub). **Set `true` for the pilot.** | Inference handler |
| `QUANT_FOUNDRY_GIT_SHA` | no | `unknown` | Pinned at container build time. | Inference container (build-time) |

### 2.7 Fincept core (`FINCEPT_*` prefix)

Read by `fincept_core.Settings` (`libs/fincept-core/src/fincept_core/config.py`).
The `env_prefix="FINCEPT_"` means the field `REDIS_URL` is read from
`FINCEPT_REDIS_URL`, etc.

| Variable | Required? | Default | Description | Used by |
|----------|-----------|---------|-------------|---------|
| `FINCEPT_ENV` | yes (non-dev) | `dev` | `dev` / `staging` / `production`. The runtime safety guard fails closed on the dev JWT secret in non-dev envs (`config.py:128-149`). | API gateway (startup) |
| `FINCEPT_TRADING_MODE` | no | `paper` | `paper` / `live`. **Keep `paper` for the pilot.** | API gateway |
| `FINCEPT_OMS_ROUTER` | no | `sim` | `sim` (in-process PaperFiller) / `alpaca` (REST to Alpaca). **Keep `sim` on Railway**; `alpaca` only when broker credentials are wired (AWS Secrets Manager, Phase 12). | API gateway (OMS) |
| `FINCEPT_DB_URL` | no | `""` | Asyncpg Postgres connection string. Railway: `${{postgres.DATABASE_URL}}`. | API gateway |
| `FINCEPT_REDIS_URL` | yes | `redis://127.0.0.1:6379/0` | Shared Redis client (lifespan). Railway: `${{redis.REDIS_URL}}`. **The task brief lists `REDIS_URL`; the actual env var is `FINCEPT_REDIS_URL`.** | API gateway (lifespan) |
| `FINCEPT_JWT_SECRET` | yes (non-dev) | `dev-only-change-me` | HS256 JWT signing key. Guard refuses to start in staging/production if this is the default. | API gateway (auth) |
| `FINCEPT_ALPACA_API_KEY` | conditional (Phase 9) | `None` | Alpaca paper API key. Only set for the paper bridge. **Not set during Phases 3–8.** | API gateway (AlpacaScheduler, data routes) |
| `FINCEPT_ALPACA_API_SECRET` | conditional (Phase 9) | `None` | Alpaca paper API secret. | API gateway |
| `FINCEPT_ALPACA_BASE_URL` | no | `https://paper-api.alpaca.markets` | Alpaca base URL. Default is the **paper** sandbox. | API gateway |
| `FINCEPT_FRED_API_KEY` | no | `None` | FRED macro data API key. Optional; regime agent skips gracefully if unset. | API gateway (regime route) |
| `FINCEPT_NEWSAPI_API_KEY` | no | `None` | NewsAPI key. Optional; news scheduler skips gracefully if unset. **The task brief lists `NEWSAPI_KEY`; the actual env var is `FINCEPT_NEWSAPI_API_KEY`.** | API gateway (NewsScheduler) |
| `FINCEPT_BINANCE_API_KEY` | no | `None` | Binance key. Not used in the pilot. | API gateway |
| `FINCEPT_BINANCE_API_SECRET` | no | `None` | Binance secret. Not used in the pilot. | API gateway |
| `FINCEPT_OPENAI_API_KEY` | no | `None` | OpenAI key for portfolio reports / LLM agents. Optional. | API gateway |
| `FINCEPT_ANTHROPIC_API_KEY` | no | `None` | Anthropic key. Optional. | API gateway |
| `FINCEPT_POLYGON_API_KEY` | no | `None` | Polygon data key. Optional. | API gateway |
| `FINCEPT_STORAGE_BACKEND` | no | (local) | `s3` for object storage. Railway: `s3`. | API gateway |
| `FINCEPT_STORAGE_S3_ENDPOINT` | conditional (if `s3`) | — | Railway Object Storage endpoint. | API gateway |
| `FINCEPT_STORAGE_S3_ACCESS_KEY` | conditional (if `s3`) | — | Railway Object Storage access key. | API gateway |
| `FINCEPT_STORAGE_S3_SECRET_KEY` | conditional (if `s3`) | — | Railway Object Storage secret key. | API gateway |
| `FINCEPT_STORAGE_S3_BUCKET` | conditional (if `s3`) | — | Bucket name, e.g. `fincept-artifacts`. | API gateway |

### 2.8 Dashboard

| Variable | Required? | Default | Description | Used by |
|----------|-----------|---------|-------------|---------|
| `NEXT_PUBLIC_API_URL` | yes | — | Browser-facing API URL. Railway: `${{api.RAILWAY_PUBLIC_DOMAIN}}`. | Dashboard (Next.js) |

### 2.9 Explicitly NOT set on Railway (paper-only invariant)

These must remain unset on Railway for the pilot. They belong in AWS Secrets
Manager (Phase 12, live trading) — see `docs/AWS_PRODUCTION_CONTROL_PLANE.md`.

| Variable | Why |
|----------|-----|
| `FINCEPT_ALPACA_API_KEY` | No broker credentials on Railway during Phases 3–8. Only set in Phase 9 (paper bridge) with **paper** keys. |
| `FINCEPT_ALPACA_API_SECRET` | Same. |
| `FINCEPT_BINANCE_API_SECRET` | No exchange secrets on Railway. |

---

## 3. Railway Staging Setup

Railway is the **primary production target** for the control plane (per
`docs/RAILWAY_DEPLOY_GUIDE.md`). RunPod is external and handles GPU workloads.
This section stands up the API, Dashboard, Redis, Postgres, and Object Storage
on Railway.

> **Staging vs production:** For the limited live pilot, a single Railway
> project in `runpod_shadow` mode is sufficient. If you want a separate
> staging environment for route smoke tests first, follow
> `docs/RAILWAY_STAGING_GUIDE.md` (which uses `local_mock` mode, budget kill
> switch on, and no broker credentials) and then create a second project for
> the pilot using the steps below.

### 3.1 Create the Railway project

1. Sign in to https://railway.app.
2. **New Project** → name it `fincept-production` (or `fincept-pilot`).
3. Leave it empty; add services one at a time.

### 3.2 Provision managed Postgres

1. In the project → **New → Database → PostgreSQL**.
2. Name the service `postgres`.
3. Open the service → **Variables** tab. Note the auto-exposed `DATABASE_URL`.
   This becomes `FINCEPT_DB_URL` for the API.

### 3.3 Provision managed Redis

1. **New → Database → Redis**.
2. Name the service `redis`.
3. Note the auto-exposed `REDIS_URL`. This becomes `FINCEPT_REDIS_URL`.

### 3.4 Provision Object Storage

1. **New → Object Storage**.
2. Name the service `object-storage`.
3. Note the exposed variables: `ENDPOINT`, `ACCESS_KEY`, `SECRET_KEY`.
4. Create a bucket named `fincept-artifacts` (via the dashboard or an
   S3-compatible CLI using the endpoint + keys).

### 3.5 Create the API service

1. **New → GitHub Repo** → select the `fincept-terminal` repo.
2. Name the service `api`.
3. Set the **root directory** to `services/api` if Railway doesn't auto-detect.
4. Railway uses the repo-root `railway.json` (Nixpacks builder,
   `uvicorn api.main:app --host 0.0.0.0 --port $PORT`, `/health` healthcheck).
5. Go to the service **Variables** tab and add every variable from §2. Use
   Railway's **reference variables** syntax so the API auto-binds to the
   managed services:

   ```
   FINCEPT_DB_URL=${{postgres.DATABASE_URL}}
   FINCEPT_REDIS_URL=${{redis.REDIS_URL}}
   FINCEPT_STORAGE_BACKEND=s3
   FINCEPT_STORAGE_S3_ENDPOINT=${{object-storage.ENDPOINT}}
   FINCEPT_STORAGE_S3_ACCESS_KEY=${{object-storage.ACCESS_KEY}}
   FINCEPT_STORAGE_S3_SECRET_KEY=${{object-storage.SECRET_KEY}}
   FINCEPT_STORAGE_S3_BUCKET=fincept-artifacts
   FINCEPT_ENV=production
   FINCEPT_TRADING_MODE=paper
   FINCEPT_OMS_ROUTER=sim
   QUANT_FOUNDRY_ENABLED=true
   QUANT_FOUNDRY_MODE=runpod_shadow
   QUANT_FOUNDRY_SHADOW_ONLY=true
   QUANT_FOUNDRY_BASE_DIR=/data/quant-foundry
   QUANT_FOUNDRY_SHADOW_DISPATCH_INTERVAL_SECONDS=300
   QUANT_FOUNDRY_SETTLEMENT_INTERVAL_SECONDS=60
   QUANT_FOUNDRY_TOURNAMENT_INTERVAL_SECONDS=300
   QUANT_FOUNDRY_RUNPOD_POLL_INTERVAL_SECONDS=15
   QUANT_FOUNDRY_MONTHLY_BUDGET_CENTS=2000
   QUANT_FOUNDRY_BUDGET_KILL_SWITCH=false
   RUNPOD_BASE_URL=https://api.runpod.ai/v2
   RUNPOD_TIMEOUT_SECONDS=60
   RUNPOD_COST_PER_DISPATCH_CENTS=10
   ```

   > Set `RUNPOD_TRAINING_ENDPOINT_ID` and `RUNPOD_INFERENCE_ENDPOINT_ID`
   > after you create the RunPod endpoints in §4. Leave them empty for now and
   > come back — the API will start in a "RunPod config invalid" state until
   > they are set, but `/health` will still return 200.

6. Add the two secrets (mark as **Secret** in the dashboard so they are masked
   in logs):

   ```
   FINCEPT_JWT_SECRET=<openssl rand -hex 32>
   QUANT_FOUNDRY_CALLBACK_SECRET=<openssl rand -hex 32>
   RUNPOD_API_KEY=<from RunPod dashboard>
   ```

   > **Callback secret parity:** `QUANT_FOUNDRY_CALLBACK_SECRET` must be
   > identical on Railway and on both RunPod endpoint templates. After deploy,
   > verify parity with the canary endpoint (§6.4).

### 3.6 Add the persistent volume

1. In the `api` service → **Settings → Volumes**.
2. Add a volume mounted at `/data`.
3. Size: 5GB for v1 (holds outbox, inbox, shadow ledger, dossier registry).
   Scale up if the shadow ledger grows.
4. This must exist **before** the first deploy so the lifespan-created
   `QuantFoundryGateway` has a writable `QUANT_FOUNDRY_BASE_DIR=/data/quant-foundry`.

### 3.7 Deploy the API service

1. Trigger a deploy (Railway auto-deploys on push, or click **Deploy**).
2. Wait for the build + healthcheck to pass (`/health` returns 200).
3. Open the API's public domain → `/health` → expect:

   ```json
   {"ok": true, "version": "0.1.0"}
   ```

4. If the deploy fails on startup with
   `FINCEPT_JWT_SECRET is the dev default (or empty) in environment
   'production'`, you forgot to set `FINCEPT_JWT_SECRET` — the runtime safety
   guard (`config.py:128`) caught it. Set the secret and redeploy.

### 3.8 Deploy the Dashboard (Next.js)

1. **New → GitHub Repo** → select the same repo.
2. Name the service `dashboard`.
3. Set the **root directory** to `apps/dashboard` (or set the start command to
   `cd apps/dashboard && pnpm start --port $PORT`).
4. Add `NEXT_PUBLIC_API_URL=${{api.RAILWAY_PUBLIC_DOMAIN}}`.
5. Healthcheck path: `/api/health`.
6. Deploy. Open the dashboard's public domain → expect the UI to load,
   including the Quant Foundry overview page.

### 3.9 Verify the staging environment

Run the receipt runner locally against the Railway API:

```bash
./scripts/verification-receipt.ps1
```

Or smoke-test the Quant Foundry routes (see §6.1 for how to mint a JWT):

```bash
TOKEN=$(python -c "import jwt; print(jwt.encode({'sub':'operator'}, '$FINCEPT_JWT_SECRET', algorithm='HS256'))")
curl -s -H "Authorization: Bearer $TOKEN" \
  https://<api-public-domain>/quant-foundry/health | jq .
```

Expect `enabled: true`, `mode: "runpod_shadow"`, `runpod_config_valid: false`
(until you set the endpoint IDs in §4).

---

## 4. RunPod Container Rebuild

The shipped Dockerfiles (`runpod/quant-foundry-training/Dockerfile`,
`runpod/quant-foundry-inference/Dockerfile`) already install the real ML deps
(`lightgbm`, `pyarrow`, `onnxruntime`, `numpy`). The real trainers/engines are
gated behind `QUANT_FOUNDRY_USE_REAL_TRAINER=true` /
`QUANT_FOUNDRY_USE_REAL_INFERENCE=true`, which you set on the RunPod endpoint
template. This section builds the images, pushes them, and creates the
endpoints.

> **Blocker resolved:** B6 (Real RunPod GPU has never run). After this section,
> a real `runpod.io` job can be dispatched from the API.

### 4.1 Build the training container image

From the repo root:

```bash
GIT_SHA=$(git rev-parse HEAD)
docker build \
  --build-arg GIT_SHA=$GIT_SHA \
  -t fincept-qf-training:latest \
  -f runpod/quant-foundry-training/Dockerfile \
  .
```

Verify the image loads the handler:

```bash
docker run --rm fincept-qf-training:latest \
  python -c "import sys; sys.path.insert(0, '/worker'); import handler; print('ok')"
```

Expect `ok`.

### 4.2 Build the inference container image

```bash
GIT_SHA=$(git rev-parse HEAD)
docker build \
  --build-arg GIT_SHA=$GIT_SHA \
  -t fincept-qf-inference:latest \
  -f runpod/quant-foundry-inference/Dockerfile \
  .
```

Verify:

```bash
docker run --rm fincept-qf-inference:latest \
  python -c "import sys; sys.path.insert(0, '/app'); import handler; print('ok')"
```

Expect `ok`.

### 4.3 Local smoke test (stub trainer, no GPU)

Test the training handler end-to-end with the deterministic `LocalTrainer`
stub (no GPU needed):

```bash
echo '{"input": {"job_id": "qf:train:test:1", "dataset_manifest_ref": "ds-1", "model_family": "gbm", "search_space": {"n_estimators": [100]}, "random_seed": 42, "hardware_class": "mock-gpu"}}' \
  | docker run --rm -i -e QUANT_FOUNDRY_CALLBACK_SECRET=dev-secret \
    fincept-qf-training:latest
```

Expect a JSON object with `callback_payload`, `callback_signature`,
`callback_ts`, `artifact_id`, `dossier_id`.

Test the real trainer locally (CPU, small dataset via `inline_dataset_csv`):

```bash
echo '{"input": {"job_id": "qf:train:real:1", "dataset_manifest_ref": "ds-1", "model_family": "gbm", "search_space": {"n_estimators": [50], "num_leaves": [15], "learning_rate": [0.1]}, "random_seed": 42, "hardware_class": "cpu", "inline_dataset_csv": "ts,f1,label\n1,0.1,0\n2,0.2,1\n3,0.3,0\n4,0.4,1\n5,0.5,0\n6,0.6,1\n7,0.7,0\n8,0.8,1\n9,0.9,0\n10,1.0,1\n11,0.1,1\n12,0.2,0\n13,0.3,1\n14,0.4,0\n15,0.5,1\n16,0.6,0\n17,0.7,1\n18,0.8,0\n19,0.9,1\n20,1.0,0"}}' \
  | docker run --rm -i \
    -e QUANT_FOUNDRY_CALLBACK_SECRET=dev-secret \
    -e QUANT_FOUNDRY_USE_REAL_TRAINER=true \
    fincept-qf-training:latest
```

Expect a real `artifact_id` derived from the LightGBM model SHA256.

### 4.4 Push images to RunPod registry

RunPod serverless endpoints pull from a container registry. Use RunPod's
registry or Docker Hub:

```bash
# Tag for your registry
REGISTRY=<your-registry>  # e.g. registry.runpod.io/<account> or docker.io/<user>
docker tag fincept-qf-training:latest $REGISTRY/fincept-qf-training:latest
docker tag fincept-qf-inference:latest $REGISTRY/fincept-qf-inference:latest

docker push $REGISTRY/fincept-qf-training:latest
docker push $REGISTRY/fincept-qf-inference:latest
```

> **Reproducibility:** The `ArtifactManifest` pins `container_image_digest`.
> Use immutable tags (e.g. `fincept-qf-training:$GIT_SHA`) in addition to
> `latest` so a deploy can be rolled back to an exact image.

### 4.5 Create the RunPod serverless endpoints

1. RunPod dashboard → **Serverless → New Endpoint**.
2. **Training endpoint:**
   - Name: `qf-training`
   - Image: `$REGISTRY/fincept-qf-training:latest`
   - GPU: choose a cost-effective GPU (e.g. RTX 4090 / A4500 for LightGBM;
     LightGBM is CPU-bound so a low-tier GPU is fine).
   - Workers: min 0, max 1 (scale to zero when idle).
   - Network volume: attach the volume created in §5.
3. **Inference endpoint:**
   - Name: `qf-inference`
   - Image: `$REGISTRY/fincept-qf-inference:latest`
   - GPU: same tier.
   - Workers: min 0, max 1.
   - Network volume: attach the same volume.
4. Note both **endpoint IDs** from the dashboard.

### 4.6 Configure env vars on the RunPod endpoint templates

On **both** endpoint templates, set:

| Variable | Value |
|----------|-------|
| `QUANT_FOUNDRY_CALLBACK_SECRET` | `<the same value as Railway>` |
| `QUANT_FOUNDRY_USE_REAL_TRAINER` | `true` (training endpoint only) |
| `QUANT_FOUNDRY_USE_REAL_INFERENCE` | `true` (inference endpoint only) |
| `QUANT_FOUNDRY_MODE` | `runpod_shadow` (inference endpoint; the Dockerfile already sets this) |
| `QUANT_FOUNDRY_TRAINING_DEADLINE_SECONDS` | `600` (training endpoint; optional override) |

> **Never** set `ALPACA_API_KEY`, `FINCEPT_JWT_SECRET`, `REDIS_URL`, or any
> broker/trading var on RunPod. The RunPod container is untrusted and must
> remain a pure function over its inputs (see
> `docs/RUNPOD_TRAINING_ARCHITECTURE.md` §1.3).

### 4.7 Wire the endpoint IDs back to Railway

In the Railway `api` service **Variables** tab, set:

```
RUNPOD_TRAINING_ENDPOINT_ID=<from RunPod dashboard>
RUNPOD_INFERENCE_ENDPOINT_ID=<from RunPod dashboard>
```

Redeploy the API. Verify `runpod_config_valid: true`:

```bash
curl -s -H "Authorization: Bearer $TOKEN" \
  https://<api-public-domain>/quant-foundry/health | jq .runpod_config_valid
```

Expect `true`.

### 4.8 Verify callback-secret parity (canary)

```bash
curl -s -H "Authorization: Bearer $TOKEN" \
  https://<api-public-domain>/quant-foundry/health/runpod-canary | jq .
```

Expect `{"ok": true, "verified": true, ...}`. If `verified: false`, the
`QUANT_FOUNDRY_CALLBACK_SECRET` on Railway and RunPod differ — fix and retry.

---

## 5. Network Volume Mount

The RunPod network volume is shared between the workers (which write heartbeat
status files) and the gateway (which scans them to detect stale/crashed
workers). This resolves the worker-status observability gap.

### 5.1 Create a RunPod network volume

1. RunPod dashboard → **Volumes → New Volume**.
2. Name: `qf-shared`.
3. Size: 50GB is plenty (status files are tiny JSON; model artifacts may also
   live here during the pilot).
4. Note the volume ID.

### 5.2 Mount it on the workers (write side)

`runpod/shared/worker_status.py:37-51` writes status files to
`/runpod-volume/status/{job_id}.json` (falling back to `/workspace/status/`).
RunPod mounts network volumes at `/runpod-volume` by default, so attaching the
volume to the endpoint template (done in §4.5) is sufficient.

Verify the mount inside a worker by checking the handler debug log path
(`handler.py:276` writes to `/runpod-volume/handler-debug.log`):

```bash
# After dispatching a job, check the volume in the RunPod dashboard file browser
# or via a one-off pod with the volume attached:
ls /runpod-volume/status/
# Expect: <job_id>.json files
cat /runpod-volume/status/<job_id>.json | jq .
```

Expect a JSON object with `job_id`, `status`, `updated_at`, `heartbeat_at`.

### 5.3 Mount it so the gateway can read it (read side)

The gateway reads `QUANT_FOUNDRY_WORKER_STATUS_DIR`
(`gateway.py:374`). On Railway the gateway cannot directly mount a RunPod
volume, so for the pilot you have two options:

**Option A (recommended for pilot) — sync the status dir to the API volume.**
Run a small sidecar (or a cron on a RunPod pod with the volume attached) that
copies `/runpod-volume/status/*.json` to the Railway persistent volume at
`/data/quant-foundry/worker-status/` via the API or S3. Then set on Railway:

```
QUANT_FOUNDRY_WORKER_STATUS_DIR=/data/quant-foundry/worker-status
QUANT_FOUNDRY_STALE_THRESHOLD_SECONDS=60
```

**Option B (AWS upgrade path) — mount the volume on an ECS task.** When you
migrate to AWS (`docs/AWS_PRODUCTION_CONTROL_PLANE.md`), mount the RunPod
volume (or an EFS equivalent) on the API ECS task so the gateway reads it
directly. Set `QUANT_FOUNDRY_WORKER_STATUS_DIR=/runpod-volume/status`.

### 5.4 Verify stale-worker detection

```bash
curl -s -H "Authorization: Bearer $TOKEN" \
  https://<api-public-domain>/quant-foundry/worker-health | jq .
```

Expect:

```json
{
  "status_dir": "/data/quant-foundry/worker-status",
  "stale_threshold_seconds": 60,
  "heartbeats": [...],
  "stale_workers": [],
  "stale_count": 0
}
```

If a worker crashes, its `heartbeat_at` stops updating and it appears in
`stale_workers` after `QUANT_FOUNDRY_STALE_THRESHOLD_SECONDS`.

---

## 6. First Real Training Job

> **Blockers resolved:** B6 (real RunPod GPU run), B7 (dossier registry
> reliable — a real dossier is registered).

### 6.1 Mint a JWT for curl examples

There is no login endpoint; the API uses a single operator JWT minted with the
`FINCEPT_JWT_SECRET` (`api/auth.py:24`). Mint one locally:

```bash
JWT_SECRET=<your FINCEPT_JWT_SECRET>
TOKEN=$(python -c "import jwt; print(jwt.encode({'sub':'operator'}, '$JWT_SECRET', algorithm='HS256'))")
echo $TOKEN
```

### 6.2 Dispatch a real training job

```bash
JOB_ID="qf:train:gbm:h1:$(date +%s)"
curl -s -X POST \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d "{
    \"job_id\": \"$JOB_ID\",
    \"job_type\": \"training\",
    \"idempotency_key\": \"$JOB_ID\",
    \"request_payload\": {
      \"schema_version\": 1,
      \"job_id\": \"$JOB_ID\",
      \"dataset_manifest_ref\": \"s3://fincept-artifacts/datasets/features-h1.parquet\",
      \"model_family\": \"gbm\",
      \"search_space\": {
        \"n_estimators\": [200],
        \"num_leaves\": [31],
        \"learning_rate\": [0.05]
      },
      \"random_seed\": 42,
      \"hardware_class\": \"rtx4090\",
      \"extra_constraints\": {}
    },
    \"priority\": 0,
    \"budget_cents\": 50
  }" \
  https://<api-public-domain>/quant-foundry/jobs | jq .
```

> **Dataset:** For the first run, you can use `inline_dataset_csv` (see §4.3)
> to avoid needing a real parquet file in S3. For production, upload a feature
> parquet to the `fincept-artifacts` bucket and reference it via
> `s3://fincept-artifacts/datasets/...`.

Expect a receipt with the job enqueued. The API's background poll task
(`QUANT_FOUNDRY_RUNPOD_POLL_INTERVAL_SECONDS=15`) polls RunPod for the result
and processes the signed callback.

### 6.3 Track the job

```bash
curl -s -H "Authorization: Bearer $TOKEN" \
  https://<api-public-domain>/quant-foundry/jobs/$JOB_ID | jq .
```

Watch `status` move from `QUEUED` → `DISPATCHED` → `VALIDATING` →
`COMPLETED` (or `FAILED`).

List all jobs:

```bash
curl -s -H "Authorization: Bearer $TOKEN" \
  "https://<api-public-domain>/quant-foundry/jobs?status=COMPLETED" | jq .
```

### 6.4 Verify the dossier was created

Once the job is `COMPLETED`, the callback processor stores a `ModelDossier`:

```bash
curl -s -H "Authorization: Bearer $TOKEN" \
  https://<api-public-domain>/quant-foundry/dossiers/$JOB_ID | jq .
```

Expect a dossier with:
- `authority: "SHADOW_ONLY"` (never auto-promoted)
- `training_metrics` (accuracy, logloss, brier_score, sharpe_ratio,
  max_drawdown, win_rate)
- `pbo` (probability of backtest overfitting)
- `deflated_sharpe`
- `artifact_manifest` with `sha256`, `feature_schema_hash`, `code_git_sha`,
  `container_image_digest`, `random_seed`, `hardware_class`

List all dossiers:

```bash
curl -s -H "Authorization: Bearer $TOKEN" \
  https://<api-public-domain>/quant-foundry/dossiers | jq .
```

### 6.5 Verify the artifact manifest

The dossier's `artifact_manifest` is the reproducibility set. Verify the
`sha256` matches by re-running the same job (same seed + data + image) and
confirming the `artifact_id` is identical:

```bash
# Re-dispatch the same job_id (idempotent)
curl -s -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d "{ \"job_id\": \"$JOB_ID\", \"job_type\": \"training\", \"idempotency_key\": \"$JOB_ID-retry\", \"request_payload\": { ... same as above ... } }" \
  https://<api-public-domain>/quant-foundry/jobs | jq .artifact_id
```

> **Cross-container caveat:** Pickle output depends on Python + LightGBM
> versions, so cross-container reproducibility is NOT guaranteed. The
> `container_image_digest` pin records the exact scope. Same image + same
> seed + same data → same `artifact_id`.

---

## 7. Shadow Inference Dispatch

> **Blockers resolved:** B2 (shadow inference stub-only → real), B8 (settled
> history is empty → starts filling).

Shadow inference runs candidate model predictions and settles them against
realized outcomes, but **never** writes to `sig.predict`. This is enforced by
`QUANT_FOUNDRY_SHADOW_ONLY=true` and the structural isolation in
`callbacks.py`.

### 7.1 Enable the scheduled dispatch loop

The background task is wired in `main.py:176-183`. It runs when:
- `QUANT_FOUNDRY_ENABLED=true`
- `QUANT_FOUNDRY_SHADOW_DISPATCH_INTERVAL_SECONDS > 0`

On Railway, set:

```
QUANT_FOUNDRY_SHADOW_DISPATCH_INTERVAL_SECONDS=300
```

Redeploy the API. The loop calls `gateway.dispatch_shadow_inference_batch()`
every 300s, dispatching inference jobs for `SHADOW_APPROVED` models to the
RunPod inference endpoint.

> **Manual dispatch:** You can also trigger a batch on demand:

```bash
curl -s -X POST -H "Authorization: Bearer $TOKEN" \
  https://<api-public-domain>/quant-foundry/shadow/dispatch | jq .
```

### 7.2 Verify predictions are landing

Check the dispatch loop status:

```bash
curl -s -H "Authorization: Bearer $TOKEN" \
  https://<api-public-domain>/quant-foundry/shadow/dispatch-status | jq .
```

Expect `enabled: true`, `dispatch_count > 0`, `last_dispatch_ts` recent.

Check shadow health:

```bash
curl -s -H "Authorization: Bearer $TOKEN" \
  https://<api-public-domain>/quant-foundry/shadow/health | jq .
```

Check settlement status (predictions settled against realized outcomes):

```bash
curl -s -H "Authorization: Bearer $TOKEN" \
  https://<api-public-domain>/quant-foundry/settlement/status | jq .
```

Expect `settled` and `pending` counts to grow over time.

### 7.3 Monitor settlement

The settlement sweep runs every `QUANT_FOUNDRY_SETTLEMENT_INTERVAL_SECONDS`
(60s). It matches past shadow predictions against realized outcomes and
records net-of-cost returns. These settled records feed the tournament
leaderboard.

```bash
curl -s -H "Authorization: Bearer $TOKEN" \
  https://<api-public-domain>/quant-foundry/tournament/leaderboard | jq .
```

Expect entries to appear as settlement history accumulates. The leaderboard
ranks models by `total_score` (weighted: `net_edge` 0.40, `deflated_sharpe`
0.35, ...).

> **Minimum history:** The promotion gate requires sufficient settled count
> (see `promotion.py:228`). Let the shadow loop run for days/weeks until the
> leaderboard has meaningful entries. There is no shortcut — settled history
> is real-time only.

---

## 8. Promotion

> **Blockers resolved:** B1 (no promoted model family), B7 (sentinel runnable
> on a real dossier).

Promotion is human-gated. No code path auto-promotes. The gate
(`promotion.py:198`) enforces four fail-closed checks: (1) dossier present,
(2) tournament evidence sufficient, (3) settlement evidence sufficient,
(4) sentinel receipt passes.

### 8.1 Submit a model for promotion

```bash
MODEL_ID="<job_id from §6>"
curl -s -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d "{
    \"model_id\": \"$MODEL_ID\",
    \"target_level\": \"paper_approved\",
    \"review_note\": \"Sufficient settled history; deflated Sharpe > threshold; sentinel green.\"
  }" \
  https://<api-public-domain>/quant-foundry/promotion/submit | jq .
```

> **Advisory-only:** `submit` builds the evidence packet (dossier + tournament
> result + sentinel receipt) and adds the request to the pending queue. It
> does **not** promote. If the evidence is insufficient, submit returns an
> error code (`no_dossier`, `insufficient_evidence`, etc.).

### 8.2 Review the evidence packet

List the pending queue:

```bash
curl -s -H "Authorization: Bearer $TOKEN" \
  https://<api-public-domain>/quant-foundry/promotion/queue | jq .
```

For each pending entry, review:
- **Dossier:** `GET /quant-foundry/dossiers/$MODEL_ID` — training metrics,
  PBO, deflated Sharpe, artifact manifest.
- **Tournament result:** `GET /quant-foundry/tournament/leaderboard` —
  rank, `total_score`, decomposition.
- **Settlement evidence:** `GET /quant-foundry/settlement/status` — settled
  count, net-of-cost returns.
- **Sentinel receipt:** included in the evidence packet; check
  `sentinel_receipt.passed == true` and `blocking_issues` is empty.

> **Do not approve** if any of: PBO > 0.5, deflated Sharpe < threshold,
  settled count < minimum, sentinel receipt failed, or blocking issues
  present.

### 8.3 Approve or reject

**Approve:**

```bash
curl -s -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d "{
    \"model_id\": \"$MODEL_ID\",
    \"review_note\": \"Approved for paper bridge. Evidence packet reviewed on <date>.\"
  }" \
  https://<api-public-domain>/quant-foundry/promotion/approve | jq .
```

The gate runs `PromotionGate.evaluate()`. If all checks pass, the model's
authority becomes `PROMOTED` (or `paper_approved`). If any check fails, the
gate returns `REJECTED` — **approval does not bypass the gate**.

**Reject:**

```bash
curl -s -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d "{
    \"model_id\": \"$MODEL_ID\",
    \"review_note\": \"PBO too high; needs more settled history.\",
    \"rejection_reason\": \"insufficient_evidence\"
  }" \
  https://<api-public-domain>/quant-foundry/promotion/reject | jq .
```

Review completed promotions:

```bash
curl -s -H "Authorization: Bearer $TOKEN" \
  https://<api-public-domain>/quant-foundry/promotion/completed | jq .
```

---

## 9. Paper Bridge Enablement

> **Blocker resolved:** B3 (paper bridge never enabled with a real model), B5
> (broker credentials configured — paper sandbox only).

The paper bridge publishes `paper_approved` predictions to the `sig.predict`
Redis stream so the OMS (in `sim` or `alpaca` paper mode) can act on them.
It is disabled by default and guarded by three independent config gates.

### 9.1 Configure the paper broker sandbox

1. Create an Alpaca **paper trading** account at https://alpaca.markets.
2. Generate paper API keys (dashboard → Paper Trading → API Keys).
3. Confirm the base URL is `https://paper-api.alpaca.markets` (the default for
   `FINCEPT_ALPACA_BASE_URL`).

> **Paper only.** Do not use live keys. The runtime safety guard and the
> pilot scope require paper.

### 9.2 Enable the paper bridge (env vars)

On the Railway `api` service, set:

```
QUANT_FOUNDRY_ALLOW_PAPER_BRIDGE=true
FINCEPT_OMS_ROUTER=alpaca
FINCEPT_ALPACA_API_KEY=<paper key>
FINCEPT_ALPACA_API_SECRET=<paper secret>
FINCEPT_ALPACA_BASE_URL=https://paper-api.alpaca.markets
```

> **Keep** `FINCEPT_TRADING_MODE=paper`. The OMS router `alpaca` submits to
> Alpaca's paper endpoint; combined with `TRADING_MODE=paper` no live orders
> are placed.

Redeploy the API. The lifespan (`main.py:203-228`) constructs `PaperBridge`
and injects the `RedisPredictionPublisher` so `paper_approved` predictions
flow to `sig.predict`.

### 9.3 Verify the rollback pointer

`PaperBridge.publish()` (`paper_bridge.py:297-316`) creates a `RollbackPointer`
recording the prior model pointer **before** publishing the new one. Verify
the bridge is configured:

```bash
curl -s -H "Authorization: Bearer $TOKEN" \
  https://<api-public-domain>/quant-foundry/health | jq .paper_bridge
```

Expect:

```json
{
  "configured": true,
  "status": "ready",
  "publisher_wired": true
}
```

After a publish, inspect the bridge receipt (returned by the callback
processor when a `paper_approved` model's prediction is published). The
receipt includes `rollback_pointer` with the prior model pointer. Save this —
it is your revert path.

### 9.4 Monitor

- **`sig.predict` stream:** Confirm predictions are landing:

  ```bash
  # From a pod/task with Redis access
  redis-cli -u "$FINCEPT_REDIS_URL" XLEN sig.predict
  redis-cli -u "$FINCEPT_REDIS_URL" XRANGE sig.predict - + COUNT 5
  ```

- **Paper orders:** Check the Alpaca paper dashboard for orders matching the
  predictions.
- **Bridge receipts:** Each publish writes a receipt with the rollback
  pointer. Monitor for `BridgeStatus.PUBLISHED` vs `REFUSED`.

> **If anything looks wrong:** unset `QUANT_FOUNDRY_ALLOW_PAPER_BRIDGE` and
  redeploy. The bridge refuses every publish immediately (§10).

---

## 10. Rollback / Disable

Three independent config layers make disabling a config flip, not a code
change (proven in `docs/LIMITED_LIVE_READINESS_REVIEW.md` §5).

### 10.1 Disable everything (full shutdown)

On the Railway `api` service, unset (or set to `false`/`""`):

```
QUANT_FOUNDRY_ENABLED=false
QUANT_FOUNDRY_ALLOW_PAPER_BRIDGE=
QUANT_FOUNDRY_MODE=local_mock
QUANT_FOUNDRY_SHADOW_DISPATCH_INTERVAL_SECONDS=0
```

Redeploy. Effects:
- Gateway disabled — no jobs created or processed (`gateway.py:98`).
- Paper bridge refuses every publish (`paper_bridge.py:247`).
- Shadow dispatch loop stops (`main.py:177`).
- No `sig.predict` writes.

> **No code change, no restart of RunPod workers.** RunPod workers keep
> running but the API stops dispatching and stops processing callbacks.

### 10.2 Disable only the paper bridge (keep shadow inference running)

```
QUANT_FOUNDRY_ALLOW_PAPER_BRIDGE=
```

Redeploy. Shadow inference + tournament + settlement continue; predictions
stop reaching `sig.predict`.

### 10.3 Roll back a promotion

The `RollbackPointer` (`paper_bridge.py:104`) records the prior model pointer
before each publish. To revert:

1. Find the bridge receipt for the promotion you want to roll back.
2. Extract the `rollback_pointer.prior_model_id`.
3. Submit a new promotion for the prior model (§8.1) and approve it (§8.3).
   The bridge will publish the prior model's pointer, effectively reverting.

> **There is no "demote" endpoint.** Reversion is a new promotion of the
> prior model. The rollback pointer ensures the prior state is known and
> recoverable.

### 10.4 Kill switch (budget)

To block all paid GPU jobs immediately without a redeploy, set:

```
QUANT_FOUNDRY_BUDGET_KILL_SWITCH=true
```

`BudgetGuard.check_and_reserve()` (`budget.py:137`) blocks any non-zero spend.
Redeploy. Existing in-flight jobs finish; no new paid jobs dispatch.

### 10.5 Railway-level rollback

- **Per-service:** Railway keeps deploy history. Click a previous deploy →
  **Redeploy** to roll back the API or Dashboard image.
- **Full project:** Delete the project in the dashboard. Back up `/data` to
  Object Storage first if durable-store state must be preserved.

### 10.6 Local fallback

If Railway is unavailable, the full stack runs locally:

```bash
docker-compose up
```

Paper-only trading and `OMS_ROUTER=sim` apply locally too. See `.env.example`
for the local env var set.

---

## 11. Monitoring Checklist

### 11.1 Daily checks

| Check | How | Healthy | Action if unhealthy |
|-------|-----|---------|---------------------|
| API liveness | `curl https://<api>/health` | `{"ok": true}` | Check Railway deploy logs; redeploy. |
| Gateway health | `GET /quant-foundry/health` (bearer) | `enabled: true`, `runpod_config_valid: true`, `paper_bridge.configured: <expected>` | Check env vars; fix missing `RUNPOD_*` or `QUANT_FOUNDRY_CALLBACK_SECRET`. |
| RunPod canary | `GET /quant-foundry/health/runpod-canary` | `verified: true` | Callback secret mismatch — re-sync Railway and RunPod. |
| Job queue | `GET /quant-foundry/jobs?status=QUEUED` | Empty or draining | If stuck in `QUEUED`, RunPod endpoint may be down; check RunPod dashboard. |
| Stale workers | `GET /quant-foundry/worker-health` | `stale_count: 0` | If `stale_count > 0`, a worker crashed; investigate RunPod logs, mark job `FAILED`. |
| Shadow dispatch | `GET /quant-foundry/shadow/dispatch-status` | `last_dispatch_ts` within `2 * QUANT_FOUNDRY_SHADOW_DISPATCH_INTERVAL_SECONDS` | If stale, check API logs for `shadow_dispatch_poll_failed`. |
| Settlement | `GET /quant-foundry/settlement/status` | `pending` not growing unboundedly | If `pending` grows, settlement sweep is failing; check API logs. |
| Budget | `GET /quant-foundry/health` → budget ledger | Monthly spend < `QUANT_FOUNDRY_MONTHLY_BUDGET_CENTS` | If approaching ceiling, raise budget or trip kill switch (§10.4). |

### 11.2 Weekly checks

| Check | How | Healthy | Action if unhealthy |
|-------|-----|---------|---------------------|
| Tournament leaderboard | `GET /quant-foundry/tournament/leaderboard` | Entries growing; `total_score` decomposition sensible | If empty, shadow dispatch or settlement is broken (see daily checks). |
| Dossier registry | `GET /quant-foundry/dossiers` | New dossiers appearing as training jobs complete | If stale, training jobs are failing; check RunPod endpoint health. |
| Promotion queue | `GET /quant-foundry/promotion/queue` | Reviewed and cleared weekly | Approve or reject pending entries (§8). |
| Promotion completed | `GET /quant-foundry/promotion/completed` | Audit trail complete | Review each promotion's evidence packet for sentinel pass. |
| Paper bridge receipts | Inspect bridge receipts for `PUBLISHED` vs `REFUSED` | `PUBLISHED` for approved models | If `REFUSED`, check `QUANT_FOUNDRY_ALLOW_PAPER_BRIDGE` and publisher wiring. |
| `sig.predict` stream | `XLEN sig.predict` (Redis) | Growing if paper bridge enabled | If empty, paper bridge disabled or publisher not wired. |
| RunPod GPU spend | RunPod dashboard → usage | Within monthly budget | Adjust `QUANT_FOUNDRY_MONTHLY_BUDGET_CENTS` or trip kill switch. |
| Railway volume | Dashboard → `api` service → volume usage | < 80% | Scale up volume or archive old ledger entries. |

### 11.3 Alert thresholds

Configure these as Railway deploy notifications or (on AWS) CloudWatch alarms.
See `docs/AWS_PRODUCTION_CONTROL_PLANE.md` §5 for the AWS alarm mapping.

| Metric | Threshold | Severity | Action |
|--------|-----------|----------|--------|
| API `/health` non-200 | 3 consecutive failures | CRITICAL | Check Railway; redeploy. |
| `runpod_config_valid` | `false` | HIGH | A `RUNPOD_*` env var is missing; re-set and redeploy. |
| RunPod canary `verified` | `false` | HIGH | Callback secret mismatch; re-sync. |
| `stale_count` | `> 0` for > 5 min | HIGH | Worker crashed; investigate RunPod logs. |
| Budget monthly spend | `> 80%` of `QUANT_FOUNDRY_MONTHLY_BUDGET_CENTS` | WARN | Raise budget or reduce dispatch frequency. |
| Budget monthly spend | `>= 100%` | CRITICAL | BudgetGuard blocks new jobs automatically; trip kill switch if needed. |
| `QUANT_FOUNDRY_BUDGET_KILL_SWITCH` | `true` | CRITICAL | Kill switch tripped; no paid jobs dispatch. |
| Settlement `pending` | growing for > 1 hour | WARN | Settlement sweep failing; check API logs for `settlement_poll_failed`. |
| Shadow dispatch `last_dispatch_ts` | older than `2 * interval` | WARN | Dispatch loop stuck; check API logs. |
| OMS/risk service down | (AWS only) | CRITICAL | PagerDuty; see AWS runbook. |
| Paper bridge `REFUSED` | any `PUBLISHED` model | WARN | Bridge disabled or publisher not wired; check env vars. |

---

## References

- `docs/LIMITED_LIVE_READINESS_REVIEW.md` — go/no-go synthesis, blockers B1–B8,
  rollback proof, risk caps proof.
- `docs/RAILWAY_DEPLOY_GUIDE.md` — Railway production config (primary
  companion to this runbook).
- `docs/RAILWAY_STAGING_GUIDE.md` — staging-only guide (`local_mock` mode).
- `docs/AWS_PRODUCTION_CONTROL_PLANE.md` — AWS upgrade path (ECS Fargate,
  Secrets Manager, CloudWatch).
- `docs/RUNPOD_TRAINING_ARCHITECTURE.md` — GPU training loop, security
  boundary, HMAC signature contract.
- `railway-production.json` — Railway service topology reference (repo root).
- `railway.json` — Railway build/deploy config (repo root).
- `services/quant_foundry/src/quant_foundry/gateway.py` —
  `from_env()` (lines 283–394), env var constants (lines 129–137).
- `services/quant_foundry/src/quant_foundry/budget.py` —
  `from_env()` (lines 297–316).
- `services/api/src/api/main.py` — lifespan, background poll tasks (lines
  126–326).
- `services/api/src/api/routes/quant_foundry.py` — all operator + callback
  endpoints.
- `libs/fincept-core/src/fincept_core/config.py` — `Settings` (FINCEPT_ prefix),
  `assert_safe_for_runtime()`.
- `runpod/quant-foundry-training/Dockerfile`, `runpod/quant-foundry-inference/Dockerfile`
  — container build instructions.
- `runpod/quant-foundry-training/handler.py`,
  `runpod/quant-foundry-inference/handler.py` — RunPod handler env vars.
- `runpod/shared/worker_status.py` — worker heartbeat status files.
- `services/quant_foundry/src/quant_foundry/paper_bridge.py` — paper bridge,
  rollback pointer.
- `services/quant_foundry/src/quant_foundry/promotion.py` — promotion gate.
- `reports/verification/railway-deployment-template.md` — post-deploy
  checklist.
