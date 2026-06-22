# AWS Production Control Plane — Design Document

**Task:** TASK-0903
**Status:** Design (not yet implemented)
**Date:** 2026-06-22
**Owner:** Builder 1 (GLM-5.2)
**Dependencies:** Phase 1 (safety guards + CI hardening) ✅, Phase 3 (Quant Foundry contracts + mock connectivity) ✅

---

## Purpose

This document designs the serious AWS production deployment path for Fincept
Terminal. It is a **design document only** — no infrastructure is created here.
The actual migration to AWS happens only after the evidence loop (Phase 4),
RunPod training MVP (Phase 5), shadow inference (Phase 6), and the promotion
workflow (Phase 7) are proven in local/staging.

The design principle is: **keep RunPod for GPUs, keep OMS/risk boundaries
inside the trusted Fincept deployment, and never let an external worker own
trading authority.**

---

## Architecture Overview

```
                    ┌─────────────────────────────────┐
                    │           Internet               │
                    └──────────────┬──────────────────┘
                                   │
                    ┌──────────────▼──────────────────┐
                    │   ALB + WAF (TLS termination,    │
                    │   rate limiting, IP allowlist)   │
                    └──────────────┬──────────────────┘
                                   │
              ┌────────────────────┼────────────────────┐
              │                     │                     │
     ┌────────▼───────┐  ┌─────────▼────────┐  ┌────────▼────────┐
     │  Dashboard     │  │   API Service    │  │  Orchestrator   │
     │  (Next.js)     │  │   (FastAPI)      │  │  Service        │
     │  ECS Fargate   │  │   ECS Fargate    │  │  ECS Fargate    │
     └────────────────┘  └────────┬─────────┘  └────────┬────────┘
                                  │                     │
              ┌───────────────────┼─────────────────────┤
              │                   │                     │
     ┌────────▼───────┐  ┌───────▼────────┐  ┌────────▼────────┐
     │  ElastiCache   │  │  Managed PG    │  │  OMS / Risk     │
     │  (Redis/Valkey)│  │  (Timescale)   │  │  Services       │
     └────────────────┘  └────────────────┘  └─────────────────┘
                                  │
                    ┌─────────────▼───────────────────┐
                    │         S3 Buckets               │
                    │  (receipts, dossiers, artifacts) │
                    └──────────────────────────────────┘
```

---

## Component Selection

### 1. Compute: ECS Fargate (or App Runner)

**Choice:** ECS Fargate for API, dashboard, orchestrator, OMS, risk, and
core ingestion services.

**Why not EC2/EKS:**
- Fargate is serverless containers — no instance management, no patching.
- Right-sized for a one-operator shop: pay per task, scale to zero for
  on-demand services.
- EKS is overkill for this scale (control plane cost + complexity).

**Why not Lambda:**
- Fincept services are long-running (WebSocket connections, stream consumers,
  scheduled ticks). Lambda's 15-minute timeout and cold-start latency are
  disqualifying for the orchestrator, OMS, and risk services.
- Lambda is appropriate only for short-lived batch jobs (e.g. a receipt
  generator), not for the control plane.

**Service mapping:**

| Service           | ECS Fargate Task  | Always-on? | Notes                          |
|-------------------|-------------------|------------|--------------------------------|
| Dashboard         | Next.js container | Yes        | Served via ALB                 |
| API               | FastAPI container | Yes        | Served via ALB                 |
| Orchestrator      | Python container  | Yes        | Redis stream consumer          |
| OMS               | Python container  | Yes        | Broker-adjacent, never on RunPod |
| Risk              | Python container  | Yes        | Pre-trade checks               |
| Core ingestion    | Python container  | Yes        | Bar/feature ingestion          |
| Features          | Python container  | On-demand  | Start/stop via module control  |
| Backtester        | Python container  | On-demand  | Start/stop via module control  |
| News analysis     | Python container  | On-demand  | Start/stop via module control  |
| OpenBB            | External container| On-demand  | Start/stop via module control  |

### 2. Storage: S3 for receipts, dossiers, artifacts

**Buckets:**
- `fincept-receipts-prod` — verification receipts (JSONL, immutable, versioned)
- `fincept-dossiers-prod` — model dossiers (JSONL, immutable, versioned)
- `fincept-artifacts-prod` — model artifacts (binary, hash-verified)
- `fincept-settlements-prod` — settlement records (JSONL, immutable)

**Invariants:**
- All buckets have **versioning enabled** and **object lock** (WORM) for
  audit-integrity-critical data (receipts, dossiers, settlements).
- Bucket policies deny non-SSL requests.
- Server-side encryption (SSE-KMS) with a customer-managed key.
- Lifecycle rules transition artifacts to Glacier after 90 days (cost
  optimization); receipts/dossiers/settlements never transition (audit trail).

### 3. Container Registry: ECR

- `fincept-api`, `fincept-dashboard`, `fincept-orchestrator`, etc.
- Image scanning enabled (CVE detection on push).
- Immutable tags enforced (a tag cannot be overwritten once pushed).
- Lifecycle policy: keep last 10 tagged images, untagged images expire after
  7 days.

### 4. Secrets: AWS Secrets Manager

**Secrets stored:**
- Broker API credentials (Alpaca, etc.)
- Redis auth token
- Postgres credentials
- JWT signing key
- HMAC callback secrets (Quant Foundry)
- OpenAI / Anthropic API keys (portfolio reports)

**Invariants:**
- No secret is ever stored in source code, environment files committed to git,
  or container image layers.
- Secrets are injected at task startup via ECS task execution role + Secrets
  Manager ARN in the task definition.
- Rotation is enabled for database credentials (automatic rotation via Lambda
  rotation function).
- Access is logged via CloudTrail.

### 5. Monitoring: CloudWatch

**Log groups:**
- `/fincept/api`, `/fincept/dashboard`, `/fincept/orchestrator`, etc.
- Retention: 30 days (hot) + 90 days (warm, moved to S3 via export task).
- Structured JSON logging (already in place via fincept-core).

**Metrics + alarms:**
- API latency p50/p95/p99 > threshold → alarm
- Error rate > 1% → alarm
- Redis evictions > threshold → alarm
- Postgres connections > 80% of max → alarm
- OMS/risk service down → CRITICAL alarm (PagerDuty)
- Kill switch tripped → CRITICAL alarm

**Dashboards:**
- Operator dashboard (CloudWatch dashboard mirroring the Next.js system
  readiness page)
- Cost dashboard (daily/monthly spend by service)

### 6. Networking: VPC + Private Subnets

**VPC design:**
- 2 AZ minimum (3 for production SLA)
- Public subnets: ALB only
- Private subnets: ECS tasks (no public IP)
- Database subnets: RDS/ElastiCache (isolated, no internet route)

**Security groups:**
- ALB SG: inbound 443 from internet, outbound to ECS SG
- ECS SG: inbound from ALB SG only, outbound to Redis/PG/SG
- Redis SG: inbound from ECS SG only
- PG SG: inbound from ECS SG only

**NAT Gateway:**
- Required for private subnet egress (package installs, API calls to
  providers, RunPod API).
- Single NAT Gateway for cost (one-operator shop); add a second per-AZ NAT
  only if availability requirements demand it.

### 7. Cache: ElastiCache (Redis/Valkey)

**Choice:** ElastiCache for Redis (or Valkey, the OSS Redis fork).

**Why:**
- Fincept uses Redis streams for the event bus (`libs/fincept-bus`).
- ElastiCache provides managed Redis with automatic failover, backups, and
  patching.
- Valkey is the OSS alternative if Redis licensing is a concern.

**Configuration:**
- Multi-AZ with automatic failover (replica in a second AZ).
- In-transit encryption (TLS) enabled.
- At-rest encryption enabled.
- `maxmemory-policy`: `noeviction` (stream data must not be silently dropped;
  the orchestrator must handle OOM explicitly, not lose events).

### 8. Database: Managed Postgres (Timescale-compatible)

**Choice:** Amazon RDS for PostgreSQL with the TimescaleDB extension, or
Amazon Aurora PostgreSQL-Compatible.

**Why not self-managed:**
- Patching, backups, and replication are handled by AWS.
- A one-operator shop cannot run a 24/7 on-call rotation for a self-managed
  database.

**Why TimescaleDB:**
- Fincept stores time-series bar/feature data. TimescaleDB's hypertables are
  purpose-built for this.
- RDS supports the TimescaleDB extension via `shared_preload_libraries`.

**Configuration:**
- Multi-AZ with synchronous standby (RDS) or Aurora reader replicas.
- Automated backups (35-day retention for PITR).
- Encryption at rest (KMS).
- Connection pooling via RDS Proxy or PgBouncer (prevent connection exhaustion
  from ECS task scaling).

### 9. Edge: ALB + WAF

**ALB (Application Load Balancer):**
- TLS termination (ACM certificate, auto-renewing).
- HTTP/2 support.
- Path-based routing: `/api/*` → API service, everything else → dashboard.
- Health checks per target group.

**WAF (Web Application Firewall):**
- AWS Managed Rules:
  - Core rule set (OWASP Top 10 protection)
  - Known bad inputs
  - IP reputation list
- Custom rules:
  - Rate limiting per IP (e.g. 100 req/5 min for API endpoints)
  - IP allowlist for operator-only endpoints (if the dashboard is not
    public-facing)
  - Block requests from non-allowlisted countries (if applicable)

---

## OMS/Risk Boundary

**Non-negotiable invariant:** OMS and risk services run inside the trusted
Fincept AWS deployment. They are NEVER deployed to RunPod or any external
compute provider.

**Why:**
- The OMS holds broker credentials and issues real orders.
- The risk service enforces pre-trade checks (position limits, kill switch,
  drawdown gates).
- Moving these to an external provider breaks the trust boundary and exposes
  broker credentials to a third-party environment.

**Implementation:**
- OMS and risk are ECS Fargate tasks in private subnets.
- Broker credentials are in Secrets Manager, accessible only to the OMS task
  execution role.
- The OMS has no inbound route from the internet (only from the orchestrator
  via the internal ALB or service connect).

---

## RunPod Integration

**RunPod is used ONLY for GPU workloads** (model training, shadow inference).
The AWS control plane dispatches jobs to RunPod and receives signed callbacks.

**Flow:**
1. API service receives a training job request.
2. Quant Foundry gateway (ECS Fargate) enqueues the job in the outbox.
3. RunPod dispatch client sends the job to RunPod with the HMAC callback URL.
4. RunPod worker trains the model on GPU, writes the artifact to S3 (via
   pre-signed URL or direct push), and sends a signed callback to the API.
5. API verifies the HMAC signature, records the callback in the inbox, and
   processes it (artifact pull + hash verification + dossier registration).

**Security:**
- RunPod workers have NO broker credentials, NO Redis access, NO direct
  database access.
- RunPod workers write artifacts to S3 via pre-signed URLs (time-limited,
  scoped to a single object key).
- Callbacks are HMAC-signed (TASK-0303) and verified before processing.
- No RunPod worker ever writes to `sig.predict`, `ord.orders`, or any trading
  stream.

---

## Cost Estimate (One-Operator Shop)

| Component                | Monthly Cost (USD) | Notes                          |
|--------------------------|---------------------|--------------------------------|
| ECS Fargate (always-on)  | $50-80              | 6 tasks, 0.5 vCPU / 1 GB each  |
| ECS Fargate (on-demand)  | $10-20              | 2h/day average                 |
| ALB                      | $20                 | Fixed + LCU                    |
| WAF                      | $10                 | 5 rules + request count        |
| ElastiCache (t3.small)   | $15                 | 2 AZ, 1.5 GB                   |
| RDS (db.t4g.medium)      | $50                 | 2 AZ, 40 GB storage            |
| S3 (receipts + artifacts)| $5-10               | < 50 GB                        |
| ECR                      | $5                  | < 10 GB                        |
| Secrets Manager          | $6                  | 3-6 secrets                    |
| CloudWatch               | $10                 | Logs + metrics + dashboards    |
| NAT Gateway              | $32                 | 1 NAT + data transfer          |
| **Total (always-on)**    | **~$210-260/mo**    | Without GPU workloads          |
| RunPod GPU (on-demand)   | $0.5-2/hour         | Only when training/inference   |

**Cost governance:**
- GPU spend is the variable cost. The budget guard (TASK-0901) enforces a
  monthly ceiling with a hard kill switch.
- On-demand services (features, backtester, news, OpenBB) are stopped when
  idle via the module control system (TASK-0203).
- S3 lifecycle rules transition old artifacts to Glacier.
- Reserved Instances / Savings Plans for the always-on Fargate tasks after
  steady state is reached (30-50% discount for 1-year commitment).

---

## Migration Path

**Phase A (local dev → Railway staging):**
- Already covered by TASK-0902 (Railway for test/staging only).
- Validate the dashboard, API, and mock Quant Foundry gateway on Railway.

**Phase B (Railway staging → AWS production):**
- Provision the VPC, subnets, security groups, ALB, WAF.
- Provision ElastiCache, RDS, S3 buckets, ECR, Secrets Manager.
- Build and push container images to ECR.
- Deploy ECS Fargate tasks with Secrets Manager injection.
- Migrate data from Railway Postgres to RDS (pg_dump/restore).
- Cut over DNS to the ALB.

**Phase C (AWS production + RunPod):**
- Wire the RunPod dispatch client (TASK-0502) to the AWS API.
- Configure HMAC callback secrets in Secrets Manager.
- Start with shadow-only training jobs; no live trading.

---

## Non-Goals

This document does NOT cover:
- **GPU infrastructure on AWS** — RunPod is used for GPUs, not AWS. AWS GPU
  (p3/g4 instances) is 3-5x more expensive than RunPod for spot training.
- **Multi-region deployment** — a one-operator shop does not need multi-region.
  Single-region (us-east-1), multi-AZ is sufficient.
- **Kubernetes** — ECS Fargate is simpler and cheaper at this scale. EKS is
  considered only if the service count grows beyond ~20 or if advanced
  orchestration (custom schedulers, service mesh) is needed.
- **Live trading deployment** — this document covers the control plane only.
  Live trading requires the full evidence loop (Phase 4-7) to be proven first,
  plus the limited live readiness review (TASK-1101).

---

## Open Questions

1. **TimescaleDB on RDS vs. Aurora:** RDS supports the TimescaleDB extension
   but with some limitations (no compression on hypertables in some versions).
   Aurora does not support TimescaleDB natively. If TimescaleDB compression is
   critical for cost, self-managed EC2 Postgres may be needed (higher
   operational burden). **Recommendation:** start with RDS + TimescaleDB
   extension; evaluate compression needs after 6 months of production data.

2. **Valkey vs. Redis on ElastiCache:** Valkey is the OSS fork of Redis 7.2+
   after Redis's license change. ElastiCache supports both. **Recommendation:**
   use Valkey to avoid licensing concerns; the API is compatible.

3. **ECS Service Connect vs. internal ALB:** Service Connect is simpler
   (built-in service discovery) but newer. Internal ALB is more mature and
   supports path-based routing. **Recommendation:** use internal ALB for the
   OMS/risk boundary (where path-based routing matters); use Service Connect
   for simpler service-to-service calls.

---

## References

- `AAAAAAAAA_BIG_PLAN.md` — master implementation plan
- `docs/NEXT_STEPS_PLAN.md` — TASK-0903 spec
- `docs/AAA_GLM_SUPERTEAM_LOGS/BUILDER2.md` — TASK-0306 (Quant Foundry API
  route, provides the gateway surface this deployment hosts)
- `docs/AAA_GLM_SUPERTEAM_LOGS/BUILDER1_GLM.md` — TASK-0801 (Quant Foundry
  overview page, the operator-facing surface this deployment serves)
- `libs/fincept-core/src/fincept_core/config.py` — runtime safety guard
  (must pass in the ECS task environment)
- `libs/fincept-bus/src/fincept_bus/streams.py` — Redis stream names
  (ElastiCache must support all streams)
