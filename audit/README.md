# Fincept Terminal — Codebase Audit

> **Date:** 2026-06-26
> **Method:** Multi-agent swarm audit — 5 parallel builders, each read every source file in their assigned area.
> **Scope:** Entire codebase — 5 shared libraries, 12 backend services, frontend dashboard, RunPod GPU containers, infrastructure, operational scripts, MCP integrations, experiments, research, top-level config, and cross-cutting concerns.

---

## Audit Documents

| # | Document | Area | Lines | KB |
|---|----------|------|-------|----|
| 1 | [01-libs.md](01-libs.md) | `libs/` — 5 shared libraries (fincept-core, fincept-bus, fincept-db, fincept-sdk, fincept-tools) | 1,044 | 58 |
| 2 | [02-services-core.md](02-services-core.md) | `services/` core — api, orchestrator, jobs, ingestor, features | 1,240 | 70 |
| 3 | [02-services-quant.md](02-services-quant.md) | `services/` quant — quant_foundry, backtester, risk, portfolio, oms, settlements, strategy_host, agents | 1,071 | 57 |
| 4 | [03-frontend.md](03-frontend.md) | `apps/dashboard` — Next.js 14 frontend | 535 | 30 |
| 5 | [04-infra-ops.md](04-infra-ops.md) | `runpod/`, `infra/`, `scripts/`, deployment config, CI/CD | 388 | 48 |
| 6 | [05-integrations-crosscutting.md](05-integrations-crosscutting.md) | `mcps/`, `experiments/`, `research/`, `strategies/`, top-level config, docs, cross-cutting concerns | 470 | 58 |
| | **Total** | | **~4,748** | **~322** |

---

## Architecture Overview

```
ingestor → features → agents → orchestrator → risk → OMS → portfolio → API/dashboard
                                    ↑
                              quant_foundry (ML pipeline)
                                    ↓
                              RunPod GPU (training + inference)
```

**Stack:** Python 3.12 (uv workspace, FastAPI, Pydantic 2, Redis Streams, TimescaleDB), Next.js 14 (React 18, TypeScript, TanStack Query, Zustand, Radix UI), RunPod serverless GPU containers (LightGBM), Railway deployment (Nixpacks), AWS production (Terraform, ECS Fargate, RDS, ElastiCache).

---

## Top Findings by Priority

### P0 — Correctness / Security

| Finding | Location | Document |
|---------|----------|----------|
| `evidence_redaction.py` has zero tests | `libs/fincept-db/` | [01-libs](01-libs.md) |
| `exec.tools` bypasses `fincept-bus` Event/serialize contract | `libs/fincept-tools/` | [01-libs](01-libs.md) |
| `clock.iso_to_ns` loses nanosecond precision via float multiplication | `libs/fincept-core/` | [01-libs](01-libs.md) |
| `assert_safe_for_runtime()` called at module import time (crashes test imports) | `services/api/main.py:63` | [02-services-core](02-services-core.md) |
| `sys.path` mutation at import time in news_impact route | `services/api/routes/news_impact.py:31` | [02-services-core](02-services-core.md) |
| Kill-switch state not persisted across restarts | `services/risk/` | [02-services-quant](02-services-quant.md) |
| `sys.path` manipulation in news_impact_agent | `services/agents/` | [02-services-quant](02-services-quant.md) |

### P1 — Design / Scaling

| Finding | Location | Document |
|---------|----------|----------|
| Per-call `Redis.from_url()` with no connection pooling | `libs/fincept-tools/` | [01-libs](01-libs.md) |
| No dead-letter queue in `fincept-bus` | `libs/fincept-bus/` | [01-libs](01-libs.md) |
| No backpressure on in-memory buffers in ingestor writer | `services/ingestor/writer.py` | [02-services-core](02-services-core.md) |
| `TargetState` and `ConsensusBuilder` cache in-memory only (lost on restart) | `services/orchestrator/` | [02-services-core](02-services-core.md) |
| `api/main.py` lifespan: 185 lines with 6+ manually-managed background tasks | `services/api/main.py` | [02-services-core](02-services-core.md) |
| Gateway complexity: `gateway.py` is 1,783 lines | `services/quant_foundry/gateway.py` | [02-services-quant](02-services-quant.md) |
| Missing `lightgbm` in `pyproject.toml` deps | `services/quant_foundry/` | [02-services-quant](02-services-quant.md) |
| `strategy_host.cancel()` is a stub, `get_feature` is a no-op | `services/strategy_host/` | [02-services-quant](02-services-quant.md) |
| Outstanding-order ledger doesn't survive restart in OMS | `services/oms/` | [02-services-quant](02-services-quant.md) |

### P2 — Cleanup / Consistency

| Finding | Location | Document |
|---------|----------|----------|
| 4-copy DRY violation for name validation (`_BAD_NAME_CHARS`) | `libs/fincept-core/` | [01-libs](01-libs.md) |
| Dead duplicate `return` statements | `libs/fincept-core/events.py:106`, `heartbeat.py:92` | [01-libs](01-libs.md) |
| Inconsistent `__init__.py` export surfaces across libs | `libs/` | [01-libs](01-libs.md) |
| No direct tests for any service's `main.py` entrypoint (6 files) | `services/*/src/*/main.py` | [02-services-core](02-services-core.md) |
| Hardcoded freq→bars/year map in backtester | `services/backtester/` | [02-services-quant](02-services-quant.md) |
| Empty `__init__.py` files in agents package | `services/agents/` | [02-services-quant](02-services-quant.md) |

---

## What's Optimally Implemented (Highlights)

- **Event-driven architecture** with Redis Streams, canonical stream names, and per-stream retention policies
- **RunPod integration** with HMAC-signed callbacks, shadow deployment before promotion, walk-forward CV
- **Security boundaries**: JWT auth with runtime safety guards, gitleaks pre-commit, no broker creds in GPU containers
- **AWS Terraform**: unusually well-documented, COMPLIANCE object lock on audit S3, TLS 1.3 + WAF on ALB
- **CI/CD**: SHA-pinned third-party actions, multi-workflow pipeline (ci, build-images, aws-iac-validate, nightly)
- **Frontend**: Bloomberg-terminal aesthetic, strong live-vs-mock data discipline, paper-trading safety rails
- **Testing**: pytest with markers (long/gpu/live), fakeredis fixtures, ASGI client pattern, TimescaleDB test setup
- **Observability**: OpenTelemetry tracing, structlog structured logging, heartbeat/leadership system

---

## How to Use This Audit

1. **Start with the Executive Summary** in each document for a high-level overview
2. **Check the Recommendations Summary** at the end of each document for prioritized action items
3. **Cross-reference findings** using the table above — many issues span multiple areas
4. **Use the file paths and line numbers** in each finding to locate the exact code
5. **The cross-cutting concerns** in [05-integrations-crosscutting.md](05-integrations-crosscutting.md) cover topics that affect the entire codebase

---

## Methodology

This audit was produced by a Devin swarm — 5 parallel AI agents (builders) coordinated through a shared filesystem workspace. Each builder was assigned a specific area of the codebase and instructed to:
1. Read every source file in its assigned area
2. Analyze and critique (not just describe) the implementation
3. Document findings with specific file paths, function names, and line numbers
4. Cover: optimal implementations, layout/design, how each section works, connections, what needs work, what might break, what isn't implemented yet, and better approaches

A Scout agent provided overall codebase intelligence (architecture, data flow, tech stack, patterns, risks) that informed the builders' work.
