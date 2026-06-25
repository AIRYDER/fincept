# Railway Production Deployment — Verification Checklist

> Copy this file to `railway-deployment-YYYY-MM-DD.md`, fill in the fields, and
> check every box. This is the binding proof-of-deploy artifact for the Railway
> production control plane. Pair with `docs/RAILWAY_DEPLOY_GUIDE.md` and
> `railway-production.json`.

**Receipt status:** PENDING

| Field | Value |
|---|---|
| Deployment date | YYYY-MM-DD |
| Operator | (name / agent) |
| Railway project name | fincept-production |
| Git SHA | (commit sha deployed) |
| API service URL | https://api-<project>.up.railway.app |
| Dashboard service URL | https://dashboard-<project>.up.railway.app |
| Postgres service | (service name) |
| Redis service | (service name) |
| Object Storage service | (service name) |
| Volume mount | /data (size: __ GB) |

---

## 1. Service URLs

- [ ] API public domain reachable: `https://api-<project>.up.railway.app`
- [ ] Dashboard public domain reachable: `https://dashboard-<project>.up.railway.app`
- [ ] Custom domain configured (if applicable): ___________

## 2. Database (Postgres)

- [ ] `FINCEPT_DB_URL` references `${{postgres.DATABASE_URL}}`
- [ ] API startup log shows successful DB connection (no connection errors)
- [ ] Schema migrations / table creation succeeded
- [ ] A test read query returns rows (or empty set) without error

## 3. Redis

- [ ] `FINCEPT_REDIS_URL` references `${{redis.REDIS_URL}}`
- [ ] API startup log shows `api.start` with the Redis URL
- [ ] Heartbeat task running (check Redis for `heartbeat:api` key)
- [ ] AlpacaScheduler + NewsScheduler started without errors

## 4. Object Storage

- [ ] `FINCEPT_STORAGE_BACKEND=s3`
- [ ] `FINCEPT_STORAGE_S3_ENDPOINT` references `${{object-storage.ENDPOINT}}`
- [ ] `FINCEPT_STORAGE_S3_BUCKET=fincept-artifacts`
- [ ] Write test: uploaded a test object to the bucket successfully
- [ ] Read test: downloaded the test object back successfully
- [ ] Delete test: removed the test object successfully

## 5. Health Endpoints

- [ ] `GET /health` → 200 `{"ok": true, "version": "..."}`
- [ ] `GET /api/health` (dashboard) → 200
- [ ] Railway healthcheck passing for both API and Dashboard services
- [ ] No restart loops (restartPolicyMaxRetries not exhausted)

## 6. Quant Foundry Gateway

- [ ] `QUANT_FOUNDRY_ENABLED=true`
- [ ] `QUANT_FOUNDRY_MODE=runpod_shadow`
- [ ] `QUANT_FOUNDRY_BASE_DIR=/data/quant-foundry` exists and is writable
- [ ] Gateway initialized at startup (check API logs for gateway init)
- [ ] `/data` persistent volume mounted and survived a container restart

## 7. Background Poll Tasks (lifespan)

- [ ] **Shadow dispatch loop** running (interval: 300s)
  - Log evidence: no `quant_foundry.shadow_dispatch_poll_failed` errors
- [ ] **Settlement sweep** running (interval: 60s)
  - Log evidence: no `quant_foundry.settlement_poll_failed` errors
- [ ] **Tournament sweep** running (interval: 300s)
  - Log evidence: no `quant_foundry.tournament_poll_failed` errors
- [ ] **RunPod result poll** running (interval: 15s)
  - Log evidence: no `quant_foundry.runpod_poll_failed` errors

## 8. RunPod Connectivity

- [ ] `QUANT_FOUNDRY_RUNPOD_TRAINING_ENDPOINT` set: `8vol1uc9l75jgs`
- [ ] `QUANT_FOUNDRY_RUNPOD_INFERENCE_ENDPOINT` set: `36mz2q30jdyvru`
- [ ] `QUANT_FOUNDRY_RUNPOD_API_KEY` set (SECRET — not in logs)
- [ ] Test dispatch: a shadow inference job was accepted by RunPod
- [ ] Callback endpoint reachable from RunPod (HMAC validated with
      `QUANT_FOUNDRY_CALLBACK_SECRET`)

## 9. Secrets Hygiene

- [ ] `QUANT_FOUNDRY_CALLBACK_SECRET` set as Secret in Railway dashboard
- [ ] `QUANT_FOUNDRY_RUNPOD_API_KEY` set as Secret in Railway dashboard
- [ ] No secret value appears in any API response body
- [ ] No secret value appears in any log line
- [ ] No secret value appears in the repo or `railway-production.json`
- [ ] `FINCEPT_ALPACA_API_KEY` is **NOT** set (paper-only)
- [ ] `FINCEPT_ALPACA_API_SECRET` is **NOT** set (paper-only)
- [ ] `FINCEPT_BINANCE_API_SECRET` is **NOT** set

## 10. Trading Safety

- [ ] `FINCEPT_TRADING_MODE=paper` (verified in env vars)
- [ ] `FINCEPT_OMS_ROUTER=sim` (verified in env vars)
- [ ] Runtime safety guard `assert_safe_for_runtime()` passed at startup
- [ ] No live order was submitted (check OMS logs — PaperFiller only)
- [ ] No `sig.predict` writes occurred (shadow-only mode enforced)

## 11. Dashboard

- [ ] Dashboard loads in browser without console errors
- [ ] `NEXT_PUBLIC_API_URL` points at the API public domain
- [ ] Dashboard can fetch from the API (CORS / proxy working)
- [ ] System readiness center renders
- [ ] Quant Foundry overview page renders

## 12. Cost & Billing

- [ ] Railway plan confirmed (Hobby / Pro)
- [ ] Estimated monthly cost recorded: $___/mo
- [ ] RunPod spending monitored (on-demand GPU hours)
- [ ] BudgetGuard / spend limits reviewed

---

## Sign-off

- Operator: ___________
- Date: YYYY-MM-DD
- Git SHA: ___________
- Receipt status: **PENDING** → (change to **VERIFIED** when all boxes checked)

---

## Post-deployment follow-ups

- [ ] Set up Railway deploy notifications (Slack / email)
- [ ] Schedule periodic `/data` volume backups to Object Storage
- [ ] Monitor RunPod GPU spend weekly
- [ ] Review log volume and rotate if needed
- [ ] Plan AWS upgrade trigger criteria (see deploy guide §7)
