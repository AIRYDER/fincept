# Railway Staging Guide — Test/Staging Only

**Task:** TASK-0902
**Status:** Implemented (this guide + railway.json config)
**Date:** 2026-06-22
**Owner:** Builder 1 (GLM-5.2)
**Dependencies:** TASK-0101 (receipt runner) ✅, TASK-0901 (module runtime plan) ✅

---

## Purpose

Railway is a cost-effective platform for hosting the Fincept Terminal staging
environment. This guide defines what to deploy on Railway, what NOT to deploy
on Railway, and how to configure the staging environment for route smoke
tests, operator demos, and mock Quant Foundry gateway testing.

**Key principle:** Railway is for test/staging ONLY. It is NOT for GPU
training, production broker-adjacent OMS, or any workload that requires
high availability or serious data persistence.

---

## What to Deploy on Railway

### Always-On Staging Services

| Service           | Railway Service         | Image/Source          | Est. Cost (Railway) |
|-------------------|-------------------------|-----------------------|----------------------|
| Dashboard         | `fincept-dashboard-stg` | `apps/dashboard`      | $5-10/mo             |
| API               | `fincept-api-stg`       | `services/api`        | $5-10/mo             |
| Redis             | Railway Redis plugin    | (managed)             | $5/mo                |
| Postgres          | Railway Postgres plugin | (managed, 1GB)        | $5/mo                |
| **Total**         |                         |                       | **~$20-30/mo**       |

### On-Demand Staging Services (start/stop via module control)

| Service           | Railway Service           | When to Start         |
|-------------------|---------------------------|-----------------------|
| OpenBB            | `fincept-openbb-stg`      | Research demos        |
| News analysis     | (part of API)             | On-demand             |
| Mock QF gateway   | (part of API)             | Always (local_mock)   |

### What the Staging Environment Proves

1. **Route smoke tests:** Every API route responds 200/401/404 as expected.
2. **Dashboard renders:** The Next.js dashboard loads and displays the system
   readiness center, module control panel, and Quant Foundry overview page.
3. **Mock Quant Foundry loop:** The full job loop (enqueue → dispatch →
   process) works over HTTP in `local_mock` mode (TASK-0306).
4. **Auth flow:** JWT login, token refresh, and protected endpoint access.
5. **Module control:** Start/stop/sweep-idle endpoints work against the
   Railway-hosted services.
6. **Operator demos:** The operator can show the system to stakeholders without
   running it locally.

---

## What NOT to Deploy on Railway

### GPU Workloads

**Never deploy RunPod training or inference workers on Railway.**

- Railway does not offer GPU instances.
- GPU training belongs on RunPod (cheaper, spot capacity, checkpoint/resume).
- GPU inference belongs on RunPod or AWS (when wired in Phase 6).

### Broker-Adjacent OMS

**Never deploy the OMS or risk service on Railway for production use.**

- Railway is a shared platform; broker credentials should not live there.
- The OMS must run inside the trusted AWS production deployment (see
  `docs/AWS_PRODUCTION_CONTROL_PLANE.md`).
- **Exception:** A mock OMS (no real broker credentials) can run on Railway
  for staging route smoke tests.

### Serious Artifact Storage

**Never use Railway Postgres for production artifact storage.**

- Railway Postgres is capped at 1GB on the hobby plan.
- Artifacts (model weights, training receipts, dossiers) belong on S3
  (production) or local disk (dev).
- Railway Postgres is for staging schema validation and route smoke only.

### Always-On Heavy Backtests

**Never run continuous backtests on Railway.**

- Backtests are CPU-intensive and will consume Railway compute credits.
- Run backtests locally or on AWS Fargate (on-demand, stop when done).

### High-Frequency Inference

**Never run high-frequency inference on Railway.**

- Railway's cold-start latency and shared infrastructure make it unsuitable
  for latency-sensitive inference.
- Inference belongs on RunPod (GPU) or AWS Fargate (CPU, when wired).

### Long-Running Data Ingestion

**Never run continuous data ingestion on Railway.**

- Ingestion requires stable connections to data providers and low-latency
  writes to TimescaleDB.
- Use the local dev environment or AWS production for ingestion.

---

## Railway Configuration

### `railway.json` (repo root)

```json
{
  "$schema": "https://railway.app/railway.schema.json",
  "build": {
    "builder": "NIXPACKS"
  },
  "deploy": {
    "startCommand": "uvicorn api.main:app --host 0.0.0.0 --port $PORT",
    "healthcheckPath": "/health",
    "healthcheckTimeout": 30,
    "restartPolicyType": "ON_FAILURE",
    "restartPolicyMaxRetries": 3
  }
}
```

### Environment Variables (Railway Dashboard)

**Required for staging:**

| Variable                            | Value (staging)          | Notes                              |
|-------------------------------------|--------------------------|------------------------------------|
| `FINCEPT_ENV`                       | `staging`                | Runtime safety guard check         |
| `FINCEPT_API_URL`                   | `https://api-stg.railway.app` | Public URL                    |
| `NEXT_PUBLIC_API_URL`               | `https://api-stg.railway.app` | Dashboard client             |
| `JWT_SECRET`                        | (generate random)        | Staging-only; rotate for prod      |
| `REDIS_URL`                         | (Railway Redis plugin)   | Auto-provisioned                   |
| `DATABASE_URL`                      | (Railway PG plugin)      | Auto-provisioned                   |
| `QUANT_FOUNDRY_ENABLED`             | `true`                   | Enable mock gateway                |
| `QUANT_FOUNDRY_MODE`                | `local_mock`             | No RunPod in staging               |
| `QUANT_FOUNDRY_SHADOW_ONLY`         | `true`                   | No sig.predict writes              |
| `QUANT_FOUNDRY_MONTHLY_BUDGET_CENTS`| `0`                      | No paid jobs in staging            |
| `QUANT_FOUNDRY_BUDGET_KILL_SWITCH`  | `true`                   | Extra safety: block all paid jobs  |

**NOT set in staging (must remain unset):**

| Variable              | Why                              |
|-----------------------|----------------------------------|
| `ALPACA_API_KEY`      | No broker credentials on Railway |
| `ALPACA_SECRET_KEY`   | No broker credentials on Railway |
| `BINANCE_API_KEY`     | No broker credentials on Railway |
| `OPENAI_API_KEY`      | Only if testing portfolio reports|
| `ANTHROPIC_API_KEY`   | Only if testing portfolio reports|

### Railway Service Setup

1. **Create a Railway project** named `fincept-staging`.
2. **Add a Postgres plugin** (1GB hobby plan is sufficient for staging).
3. **Add a Redis plugin** (hobby plan).
4. **Deploy the API** from `services/api/` with the `railway.json` config.
5. **Deploy the Dashboard** from `apps/dashboard/` with `NEXT_PUBLIC_API_URL`
   set to the API service's Railway URL.
6. **Set environment variables** per the table above.
7. **Run the receipt runner** to verify the staging environment:
   ```powershell
   ./scripts/verification-receipt.ps1
   ```

---

## Cost Estimate

| Component              | Railway Hobby Plan   | Notes                          |
|------------------------|----------------------|--------------------------------|
| API service            | $5-10/mo             | 512MB RAM, 1 vCPU              |
| Dashboard service      | $5-10/mo             | 512MB RAM, 1 vCPU              |
| Redis plugin           | $5/mo                | 10MB (hobby)                   |
| Postgres plugin        | $5/mo                | 1GB (hobby)                    |
| **Total**              | **~$20-30/mo**       | Sufficient for staging         |

**Cost comparison:**
- Railway staging: ~$20-30/mo
- AWS production (always-on): ~$200-310/mo (see AWS_PRODUCTION_CONTROL_PLANE.md)
- Local dev: $0

Railway is 10x cheaper than AWS for staging, which is why it's the recommended
staging platform. But it is NOT a production replacement.

---

## Migration Path

1. **Local dev** (current): Everything runs locally. No cost.
2. **Railway staging** (this task): Deploy API + dashboard + Redis + Postgres
   on Railway for route smoke and demos. ~$20-30/mo.
3. **AWS production** (TASK-0903): Migrate from Railway to AWS when the
   evidence loop (Phase 4-7) is proven. Keep Railway for staging only.

The migration from Railway to AWS is documented in
`docs/AWS_PRODUCTION_CONTROL_PLANE.md` (Phase B: Railway staging → AWS
production).

---

## Security Notes

- **No broker credentials on Railway.** The staging OMS runs in mock mode
  (no real orders). Broker credentials are only set in the AWS production
  environment via Secrets Manager.
- **JWT secret is staging-only.** Generate a random secret for Railway; do NOT
  reuse the production JWT secret.
- **Budget kill switch is ON.** `QUANT_FOUNDRY_BUDGET_KILL_SWITCH=true` ensures
  no paid GPU jobs can accidentally start on Railway (even though Railway
  doesn't have GPUs, this is defense-in-depth).
- **Shadow-only mode.** `QUANT_FOUNDRY_SHADOW_ONLY=true` ensures the Quant
  Foundry gateway never writes to `sig.predict` or any trading stream.
- **Runtime safety guard.** `FINCEPT_ENV=staging` triggers the
  `assert_safe_for_runtime()` check in `fincept_core/config.py`, which
  validates that no production-only config (real broker keys, live trading
  flags) is set in a staging environment.

---

## References

- `railway.json` — Railway deployment config (repo root)
- `docs/MODULE_RUNTIME_PLAN.md` — module list, cost estimates, budget guard
- `docs/AWS_PRODUCTION_CONTROL_PLANE.md` — AWS production design (TASK-0903)
- `docs/NEXT_STEPS_PLAN.md` — TASK-0902 spec
- `services/api/src/api/main.py` — API entrypoint (uvicorn target)
- `libs/fincept-core/src/fincept_core/config.py` — runtime safety guard
