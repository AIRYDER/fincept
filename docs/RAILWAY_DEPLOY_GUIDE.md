# Railway Production Deployment Guide

**Status:** Implemented (this guide + `railway-production.json` service template)
**Date:** 2026-06-25
**Owner:** Agent K (Railway Production Config)
**Companion files:** `railway-production.json`, `reports/verification/railway-deployment-template.md`

> Railway is the **primary production target** for the Fincept Terminal control
> plane. AWS Terraform (`infra/aws/`) remains a fallback / upgrade path for when
> Railway's limits are hit (multi-region, WAF, compliance, >4GB RAM).

---

## 1. Architecture Overview

```
┌──────────────────────────── Railway project ────────────────────────────┐
│                                                                          │
│  ┌──────────────┐   ┌─────────────┐   ┌──────────────────────────────┐   │
│  │ Managed PG   │   │ Managed     │   │ Managed Object Storage       │   │
│  │ (DATABASE_URL)│   │ Redis       │   │ (S3-compatible, fincept-     │   │
│  │              │   │ (REDIS_URL) │   │  artifacts bucket)           │   │
│  └──────┬───────┘   └──────┬──────┘   └──────────┬───────────────────┘   │
│         │                  │                     │                       │
│         │  FINCEPT_DB_URL  │  FINCEPT_REDIS_URL  │  FINCEPT_STORAGE_*    │
│         ▼                  ▼                     ▼                       │
│  ┌──────────────────────────────────────────────────────────────────┐    │
│  │  API service (container, Nixpacks)                               │    │
│  │  uvicorn api.main:app --host 0.0.0.0 --port $PORT                │    │
│  │  ┌─ /health (public liveness)                                   │    │
│  │  ├─ lifespan background tasks:                                  │    │
│  │  │   • RunPod result poll        (15s)                          │    │
│  │  │   • Settlement sweep          (60s)                          │    │
│  │  │   • Tournament sweep          (300s)                         │    │
│  │  │   • Shadow dispatch batch     (300s)                         │    │
│  │  └─ Persistent volume mounted at /data                          │    │
│  │      (quant-foundry durable stores: outbox, inbox,              │    │
│  │       shadow ledger, dossier registry)                          │    │
│  └──────────────────────────┬───────────────────────────────────────┘    │
│                             │ RAILWAY_PUBLIC_DOMAIN                       │
│                             ▼                                            │
│  ┌──────────────────────────────────────────────────────────────────┐    │
│  │  Dashboard service (container, Nixpacks)                         │    │
│  │  cd apps/dashboard && pnpm start --port $PORT                     │    │
│  │  NEXT_PUBLIC_API_URL → API public domain                          │    │
│  └──────────────────────────────────────────────────────────────────┘    │
│                                                                          │
└──────────────────────────────────────────────────────────────────────────┘
                  │ QUANT_FOUNDRY_RUNPOD_*  (HTTPS, external)
                  ▼
┌──────────────────────────── RunPod (external) ──────────────────────────┐
│  Training endpoint  (GPU, on-demand)                                     │
│  Inference endpoint (GPU, on-demand)                                     │
│  → callbacks HMAC-signed with CALLBACK_SECRET → API /quant-foundry       │
└──────────────────────────────────────────────────────────────────────────┘
```

**Key properties:**

- **Paper-only trading.** `FINCEPT_TRADING_MODE=paper`, `FINCEPT_OMS_ROUTER=sim`.
  No broker credentials live on Railway. The OMS uses the in-process
  PaperFiller.
- **RunPod is external.** Railway hosts the control plane (API + Dashboard);
  GPU training/inference runs on RunPod and calls back into the API over HTTPS.
- **Persistent volume** at `/data` holds the quant-foundry durable stores so
  outbox/inbox/shadow-ledger/dossier state survives container restarts.
- **Managed everything.** Postgres, Redis, and Object Storage are Railway
  managed services — no DBA or SRE required.

---

## 2. Prerequisites

| # | Requirement | Notes |
|---|---|---|
| 1 | Railway account | https://railway.app — Hobby plan ($5/mo) or Pro. |
| 2 | RunPod account | For GPU training + inference endpoints. https://runpod.io |
| 3 | GitHub repo connected to Railway | Railway builds from the repo via Nixpacks. |
| 4 | Domain name (optional) | Railway provides `*.up.railway.app` domains by default. A custom domain can be attached to the API and Dashboard services. |
| 5 | `openssl` (local) | To generate the `CALLBACK_SECRET`. |
| 6 | Repo cloned locally | For running the verification checklist. |

---

## 3. Step-by-Step Deployment

> The `railway-production.json` file in the repo root is the service topology
> reference. Railway does not natively parse it — use it as the checklist of
> services and env vars to create in the dashboard.

### a. Create a new Railway project

1. Sign in to https://railway.app.
2. **New Project** → name it `fincept-production`.
3. Leave it empty for now; we'll add services one at a time.

### b. Provision managed Postgres

1. In the project → **New → Database → PostgreSQL**.
2. Name the service `postgres`.
3. Once provisioned, open the service → **Variables** tab. Note the
   `DATABASE_URL` value (Railway exposes it automatically).
4. This becomes `FINCEPT_DB_URL` for the API service.

### c. Provision managed Redis

1. **New → Database → Redis**.
2. Name the service `redis`.
3. Note the `REDIS_URL` variable — this becomes `FINCEPT_REDIS_URL`.

### d. Provision Object Storage

1. **New → Object Storage**.
2. Name the service `object-storage`.
3. Note the exposed variables: `ENDPOINT`, `ACCESS_KEY`, `SECRET_KEY`.
4. Create a bucket named `fincept-artifacts` (via the Railway dashboard or an
   S3-compatible CLI using the endpoint + keys).

### e. Create the API service

1. **New → GitHub Repo** → select the fincept-terminal repo.
2. Name the service `api`.
3. Set the **root directory** to `services/api` (if Railway doesn't auto-detect).
4. Railway will use the repo-root `railway.json` (Nixpacks builder,
   `uvicorn api.main:app --host 0.0.0.0 --port $PORT`, `/health` healthcheck).
5. Go to the service **Variables** tab and add every env var from the
   `railway-production.json` → `services.api.env` block. Use Railway's
   **reference variables** syntax (`${{postgres.DATABASE_URL}}`,
   `${{redis.REDIS_URL}}`, `${{object-storage.ENDPOINT}}`, etc.) so the API
   auto-binds to the managed services.
6. Add the two secrets (see step i) — mark them as **Secret** in the dashboard
   so they are masked in logs.

### f. Create the Dashboard service

1. **New → GitHub Repo** → select the same repo.
2. Name the service `dashboard`.
3. Set the **root directory** to `apps/dashboard` (or adjust the start command
   to `cd apps/dashboard && pnpm start --port $PORT`).
4. Add `NEXT_PUBLIC_API_URL` = `${{api.RAILWAY_PUBLIC_DOMAIN}}` so the dashboard
   points at the API's public Railway domain.
5. Healthcheck path: `/api/health`.

### g. Create the persistent volume

1. In the `api` service → **Settings → Volumes**.
2. Add a volume mounted at `/data`.
3. Size: 5GB is sufficient for v1 durable stores. Scale up if the shadow ledger
   or dossier registry grows.
4. This must exist **before** the first deploy so the lifespan-created
   `QuantFoundryGateway` has a writable `QUANT_FOUNDRY_BASE_DIR=/data/quant-foundry`.

### h. Configure RunPod endpoints

Set these on the `api` service. Endpoint IDs are **not secrets** but should
not be committed to the repo — set them as Railway variables (not secrets)
so they are visible in the dashboard but not in git history.

| Variable | Value | Source |
|---|---|---|
| `RUNPOD_TRAINING_ENDPOINT_ID` | *(from RunPod dashboard)* | RunPod serverless endpoint ID for the training worker |
| `RUNPOD_INFERENCE_ENDPOINT_ID` | *(from RunPod dashboard)* | RunPod serverless endpoint ID for the inference worker |
| `QUANT_FOUNDRY_MODE` | `runpod_shadow` | Shadow dispatch (no sig.predict writes) |

> **Deprecated names:** `QUANT_FOUNDRY_RUNPOD_TRAINING_ENDPOINT` and
> `QUANT_FOUNDRY_RUNPOD_INFERENCE_ENDPOINT` are read as fallbacks with a
> DeprecationWarning, but the canonical `RUNPOD_TRAINING_ENDPOINT_ID` /
> `RUNPOD_INFERENCE_ENDPOINT_ID` names are preferred. Migrate existing
> dashboard setups to the canonical names.

### i. Set secrets

Generate and set these as **Secret** variables in the `api` service:

```bash
# Local terminal — generate the callback secret
openssl rand -hex 32
```

| Secret name | Purpose | How to set |
|---|---|---|
| `QUANT_FOUNDRY_CALLBACK_SECRET` | HMAC-validates RunPod → API callbacks. **Must be identical** on Railway and RunPod endpoint templates. | Railway dashboard → api → Variables → New Variable → paste value → mark **Secret**. Also set in RunPod endpoint template env. |
| `RUNPOD_API_KEY` | Authenticates API → RunPod job dispatch | Railway dashboard → api → Variables → New Variable → paste RunPod API key → mark **Secret** |

> **Never** commit these to the repo. They exist only as `${{secrets.NAME}}`
> placeholders in `railway-production.json`.
>
> **Callback secret parity:** after deploy, verify that Railway and RunPod
> share the same callback secret by calling
> `GET /quant-foundry/health/runpod-canary` (bearer-auth). A 200 with
> `verified: true` proves both sides can sign/verify with the same secret.

### j. Deploy and verify health checks

1. Trigger a deploy of the `api` service (Railway auto-deploys on push, or
   click **Deploy**).
2. Wait for the build + healthcheck to pass (`/health` returns 200).
3. Deploy the `dashboard` service.
4. Open the API's public domain → `/health` → expect `{"ok": true, ...}`.
5. Open the dashboard's public domain → expect the UI to load.
6. Run through the verification checklist in
   `reports/verification/railway-deployment-template.md`.

---

## 4. Environment Variables Reference

### Core (`FINCEPT_*`)

| Variable | Value | Notes |
|---|---|---|
| `FINCEPT_DB_URL` | `${{postgres.DATABASE_URL}}` | Asyncpg Postgres connection string |
| `FINCEPT_REDIS_URL` | `${{redis.REDIS_URL}}` | Shared Redis client (lifespan) |
| `FINCEPT_TRADING_MODE` | `paper` | **Safety invariant** — no live orders |
| `FINCEPT_OMS_ROUTER` | `sim` | **Safety invariant** — in-process PaperFiller, no broker |
| `FINCEPT_STORAGE_BACKEND` | `s3` | Object storage for artifacts |
| `FINCEPT_STORAGE_S3_ENDPOINT` | `${{object-storage.ENDPOINT}}` | Railway Object Storage endpoint |
| `FINCEPT_STORAGE_S3_ACCESS_KEY` | `${{object-storage.ACCESS_KEY}}` | |
| `FINCEPT_STORAGE_S3_SECRET_KEY` | `${{object-storage.SECRET_KEY}}` | |
| `FINCEPT_STORAGE_S3_BUCKET` | `fincept-artifacts` | Bucket for model weights, receipts, dossiers |

### Quant Foundry (`QUANT_FOUNDRY_*`)

| Variable | Value | Notes |
|---|---|---|
| `QUANT_FOUNDRY_ENABLED` | `true` | Enables the gateway + background poll tasks |
| `QUANT_FOUNDRY_MODE` | `runpod_shadow` | Shadow dispatch — no `sig.predict` writes |
| `QUANT_FOUNDRY_BASE_DIR` | `/data/quant-foundry` | On the persistent volume |
| `QUANT_FOUNDRY_SHADOW_DISPATCH_INTERVAL_SECONDS` | `300` | Shadow inference batch interval |
| `QUANT_FOUNDRY_SETTLEMENT_INTERVAL_SECONDS` | `60` | Settlement sweep interval |
| `QUANT_FOUNDRY_TOURNAMENT_INTERVAL_SECONDS` | `300` | Tournament sweep interval |
| `QUANT_FOUNDRY_RUNPOD_POLL_INTERVAL_SECONDS` | `15` | RunPod result poll interval |
| `RUNPOD_TRAINING_ENDPOINT_ID` | *(from RunPod dashboard)* | RunPod serverless endpoint ID (training) |
| `RUNPOD_INFERENCE_ENDPOINT_ID` | *(from RunPod dashboard)* | RunPod serverless endpoint ID (inference) |
| `RUNPOD_BASE_URL` | `https://api.runpod.ai/v2` | RunPod API base URL |
| `RUNPOD_TIMEOUT_SECONDS` | `60` | HTTP request timeout for dispatch calls |
| `RUNPOD_COST_PER_DISPATCH_CENTS` | `0` | Estimated cost per dispatch (budget guard) |

### RunPod + Secrets (SECRET — set in dashboard, never in repo)

| Variable | Classification | Notes |
|---|---|---|
| `QUANT_FOUNDRY_CALLBACK_SECRET` | **SECRET** | HMAC for RunPod → API callbacks. Must be identical on Railway and RunPod. `openssl rand -hex 32` |
| `RUNPOD_API_KEY` | **SECRET** | RunPod API key |

### Dashboard

| Variable | Value | Notes |
|---|---|---|
| `NEXT_PUBLIC_API_URL` | `${{api.RAILWAY_PUBLIC_DOMAIN}}` | Browser-facing API URL |

### Explicitly NOT set on Railway (paper-only)

| Variable | Why |
|---|---|
| `FINCEPT_ALPACA_API_KEY` | No broker credentials — `OMS_ROUTER=sim` |
| `FINCEPT_ALPACA_API_SECRET` | No broker credentials |
| `FINCEPT_ALPACA_BASE_URL` | Not needed in sim mode |
| `FINCEPT_BINANCE_API_SECRET` | No exchange secrets on Railway |

---

## 5. Cost Estimate

| Component | Railway plan | Est. cost |
|---|---|---|
| Managed Postgres | Hobby (1GB) | ~$5/mo |
| Managed Redis | Hobby (256MB) | ~$5/mo |
| Managed Object Storage | 10GB | ~$5/mo |
| API container | 512MB–1GB RAM | ~$5–10/mo |
| Dashboard container | 512MB RAM | ~$5–10/mo |
| Persistent volume | 5GB | ~$1–5/mo |
| **Railway subtotal (always-on)** | | **~$25–40/mo** |
| RunPod GPU | On-demand | ~$0.5–2/hour (only when jobs run) |

**Total: ~$25–40/mo always-on + RunPod GPU on-demand.**

RunPod cost depends on job frequency. At ~10 GPU-hours/month, that's ~$5–20/mo
on top of the Railway base.

---

## 6. Comparison with AWS

| Component | Railway | AWS |
|---|---|---|
| Postgres | Managed (~$5) | RDS (~$20) |
| Redis | Managed (~$5) | ElastiCache (~$15) |
| Object Storage | S3-compatible (~$5) | S3 (~$2) |
| Container | Nixpacks (~$5–10) | ECS Fargate (~$15) |
| Load Balancer | Built-in | ALB (~$18) |
| WAF | Not available | WAF (~$6) |
| Secrets | Env vars (free) | Secrets Manager (~$0.40/secret) |
| Monitoring | Built-in metrics | CloudWatch (~$5) |
| **Total** | **~$25–40/mo** | **~$210–260/mo** |

**Takeaways:**

- Railway is **6–8x cheaper** for the same control-plane workload.
- AWS adds WAF (rate limiting, SQL injection protection), Secrets Manager
  (broker credentials for Phase 12), and CloudWatch alarms (BudgetGuard,
  settlement lag).
- Railway is simpler to set up and maintain — no Terraform, no ALB, no IAM.
- AWS is better for multi-region, compliance certifications, and horizontal
  scale beyond Railway's single-region containers.

---

## 7. When to Upgrade to AWS

Migrate from Railway to AWS (see `infra/aws/` Terraform +
`docs/AWS_DEPLOY_RUNBOOK.md`) when **any** of the following become true:

- **WAF required** — rate limiting, SQL injection / XSS protection, geo-blocking.
- **Multi-AZ or multi-region** — Railway is single-region; AWS gives multi-AZ
  RDS, multi-region read replicas.
- **Compliance certifications** — SOC2, HIPAA, PCI require AWS's audit trail.
- **>4GB RAM for the API** — Railway containers cap out; Fargate scales higher.
- **CloudWatch alarms** — BudgetGuard spend alarms, settlement-lag alarms,
  RunPod-failure alarms wired to PagerDuty.
- **Secrets Manager for broker credentials** — Phase 12 (limited-live trading)
  requires broker keys in Secrets Manager with rotation, not Railway env vars.
- **Dedicated VPC / peering** — private network to a broker or data vendor.

Until then, Railway is the recommended production home.

---

## 8. Rollback

### Railway rollback

- **Delete the project** in the Railway dashboard → all services, volumes, and
  managed databases are removed. Billing stops immediately.
- **Per-service rollback**: Railway keeps deploy history — click a previous
  deploy → **Redeploy** to roll back a single service.
- **Volume data**: deleting the project deletes volumes. Back up `/data` to
  Object Storage or local disk before deleting if durable-store state must be
  preserved.

### Local development fallback

If Railway is unavailable, the full stack runs locally:

```bash
docker-compose up
```

This brings up Postgres, Redis, and the API + Dashboard per
`docker-compose.yml`. Paper-only trading and `OMS_ROUTER=sim` apply locally
too. See `.env.example` for the local env var set.

### AWS fallback

The AWS Terraform in `infra/aws/` is the infrastructure-as-code fallback /
upgrade path. See `docs/AWS_DEPLOY_RUNBOOK.md` (Agent E) for the AWS
deployment procedure.

---

## 9. Security Notes

- **Paper-only enforced.** `FINCEPT_TRADING_MODE=paper` and
  `FINCEPT_OMS_ROUTER=sim` are set in the template and must not be changed on
  Railway. The runtime safety guard (`assert_safe_for_runtime()` in
  `fincept_core/config.py`) validates this at startup.
- **No broker credentials on Railway.** `FINCEPT_ALPACA_API_KEY`,
  `FINCEPT_ALPACA_API_SECRET`, and `FINCEPT_BINANCE_API_SECRET` are explicitly
  NOT set. They belong in AWS Secrets Manager (Phase 12).
- **Secrets are masked.** `CALLBACK_SECRET` and `RUNPOD_API_KEY` are marked as
  Secret in the Railway dashboard and never appear in logs or the repo.
- **Shadow-only mode.** `QUANT_FOUNDRY_MODE=runpod_shadow` ensures the gateway
  never writes to `sig.predict` or any trading stream.
- **Healthcheck is public.** `/health` returns only `{"ok": true, "version"}`
  — no secrets, no PII.

---

## 10. References

- `railway-production.json` — production service topology (repo root)
- `railway.json` — staging config (repo root, READ ONLY)
- `docs/RAILWAY_STAGING_GUIDE.md` — staging guide (predecessor to this doc)
- `reports/verification/railway-deployment-template.md` — post-deploy checklist
- `.env.example` — canonical env var documentation
- `services/api/src/api/main.py` — FastAPI lifespan (background poll tasks)
- `docs/AWS_PRODUCTION_CONTROL_PLANE.md` — AWS architecture (upgrade path)
- `docs/AWS_DEPLOY_RUNBOOK.md` — AWS deployment runbook (Agent E)
- `infra/aws/` — AWS Terraform (fallback / upgrade)
