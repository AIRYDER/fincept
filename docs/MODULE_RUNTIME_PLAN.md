# Local/Staging Module Runtime Plan

**Task:** TASK-0901
**Status:** Implemented (budget guard + this plan document)
**Date:** 2026-06-22
**Owner:** Builder 1 (GLM-5.2)
**Dependencies:** TASK-0203 (On-Demand Module Control) ✅ by Builder 5

---

## Purpose

This document defines the module list, start/stop scripts, health checks,
idle timeouts, max instances, and estimated monthly costs for the Fincept
Terminal local/staging runtime. It also documents the budget guard that
prevents heavy jobs from exceeding the monthly GPU spend ceiling.

The operator runs Fincept as a one-person shop. The goal is to minimize
always-on cost while keeping the system ready for on-demand research and
trading work. The principle is: **always-on thin shell + on-demand workers.**

---

## Always-On vs On-Demand

### Always-On (the thin shell)

These services run continuously and must be available for the system to
function:

| Service           | Description                              | Est. Cost (local) | Est. Cost (AWS)   |
|-------------------|------------------------------------------|--------------------|--------------------|
| Dashboard         | Next.js operator console                 | $0 (local)         | $10-15/mo (Fargate)|
| API               | FastAPI control plane                    | $0 (local)         | $10-15/mo (Fargate)|
| Redis             | Event bus (streams) + cache              | $0 (local)         | $15/mo (ElastiCache)|
| Postgres          | TimescaleDB for bars/features/positions  | $0 (local)         | $50/mo (RDS)       |
| Orchestrator      | Stream consumer + agent dispatch         | $0 (local)         | $10-15/mo (Fargate)|
| OMS               | Order management (broker-adjacent)       | $0 (local)         | $10-15/mo (Fargate)|
| Risk              | Pre-trade risk checks                    | $0 (local)         | $10-15/mo (Fargate)|
| Core ingestion    | Bar/feature ingestion (minimal)          | $0 (local)         | $10-15/mo (Fargate)|
| **Total**         |                                          | **$0 (local)**     | **~$125-155/mo**   |

### On-Demand (optional modules)

These modules are started only when needed and stopped when idle. The module
control system (TASK-0203) manages start/stop with idle timeout sweep.

| Module ID             | Display Name              | Cost Class | Idle Timeout | Always-On? | Est. Cost (2h/day) |
|-----------------------|---------------------------|------------|--------------|------------|---------------------|
| `openbb`              | OpenBB Research Terminal  | medium     | 30 min       | No         | $5-10/mo            |
| `market_data`         | Market Data Ingestion     | medium     | 60 min       | No         | $10-15/mo           |
| `news_learning`       | News Learning Loop        | medium     | 45 min       | No         | $10-15/mo           |
| `jobs`                | Background Jobs Worker    | low        | 20 min       | No         | $3-5/mo             |
| `gbm_predictor`       | GBM Predictor Agent       | low        | 30 min       | No         | $3-5/mo             |
| `news_alpha_predictor`| News Alpha Predictor      | low        | 30 min       | No         | $3-5/mo             |
| `sentiment`           | Sentiment Agents          | high       | 15 min       | No         | $20-40/mo (LLM API) |
| `regime`              | Regime Agent              | low        | 60 min       | No         | $3-5/mo             |
| **Total (on-demand)** |                           |            |              |            | **~$57-115/mo**     |

### Heavy Jobs (budget-guarded)

These are NOT modules — they are one-off jobs that incur GPU or LLM API cost.
The budget guard (see below) checks the monthly ceiling before allowing them
to start.

| Job Type             | Description                    | Est. Cost per Job   | Guard         |
|----------------------|--------------------------------|----------------------|---------------|
| RunPod training      | GPU model training             | $0.50-5.00          | Budget guard  |
| RunPod inference     | GPU shadow inference           | $0.10-1.00          | Budget guard  |
| Heavy backtest       | Large-scale backtest           | $0 (local CPU)      | Module control|
| LLM portfolio report | OpenAI/Anthropic report        | $0.01-0.10          | API key check |

---

## Start/Stop Scripts

The module control system (TASK-0203) provides HTTP endpoints for module
lifecycle:

```
POST /modules/{module_id}/start    — start a module
POST /modules/{module_id}/stop     — stop a module
POST /modules/{module_id}/restart  — restart a module
POST /modules/stop-all             — stop ALL optional modules
POST /modules/sweep-idle           — stop modules past their idle timeout
GET  /modules                      — list all modules with status
GET  /modules/{module_id}          — module detail
GET  /modules/receipts             — recent start/stop receipts
```

### Local start/stop (CLI)

For local development, the operator can also use the existing PowerShell
scripts:

```powershell
# Start the always-on thin shell
.\scripts\start.ps1

# Start a specific optional module
.\scripts\start.ps1 -Modules openbb,market_data

# Stop all optional modules
.\scripts\stop-optional.ps1

# Stop everything
.\scripts\stop.ps1
```

### Health checks

Each module reports health via the existing `/services` endpoint (heartbeat
with age). The module control system checks:
- Process is running (PID exists)
- Service heartbeat is fresh (age < threshold)
- Health endpoint responds 200 (if applicable)

A module that fails any check is reported as `degraded` or `down` in the
dashboard.

---

## Idle Timeout Sweep

The `POST /modules/sweep-idle` endpoint stops any module that has been running
longer than its `idle_timeout_sec` with no fresh service heartbeats. This
prevents the operator from accidentally leaving an expensive module running
overnight.

Sweep logic (implemented in `services/api/src/api/routes/modules.py`):
1. For each running module, check `idle_timeout_sec` against the time since
   the last heartbeat.
2. If idle time >= `idle_timeout_sec`, stop the module and record a receipt
   with `status="idle_timeout_elapsed"`.
3. Return the list of stopped modules.

The operator can trigger a sweep manually from the dashboard, or a cron job
can call the endpoint periodically (e.g. every 5 minutes).

---

## Max Instances

For local/staging, each module has a max instance count of **1**. The system
is designed for a single operator — running multiple instances of the same
module would consume resources without benefit and could cause port conflicts.

The module control system enforces this by checking the existing module
status before starting: if a module is already running, `start` returns the
existing instance's details rather than starting a new one.

For production (AWS), max instances can be increased for horizontal scaling,
but this is out of scope for the local/staging plan.

---

## Budget Guard

The budget guard (`services/quant_foundry/src/quant_foundry/budget.py`)
enforces a hard monthly spending ceiling for heavy jobs. This is the
cost-governance invariant from cross-cutting rigor §4: "GPU spend must fail
closed."

### How it works

1. **Monthly ceiling:** The operator sets
   `QUANT_FOUNDRY_MONTHLY_BUDGET_CENTS` (e.g. 5000 = $50/mo). Default is 0
   (no paid jobs allowed until a budget is explicitly set).

2. **Check before start:** Before a heavy job (RunPod training, RunPod
   inference) starts, the gateway calls `budget.check_and_reserve(amount_cents,
   job_type)`. If the reservation would exceed the monthly ceiling, the job
   is rejected with a clear reason: "job cost Xc exceeds monthly budget:
   spent Yc + requested Xc > ceiling Zc."

3. **Durable tracking:** Spend is tracked in a JSONL file
   (`<base_dir>/spend_<YYYY-MM>.jsonl`) so a process restart does not reset
   the counter. Each line records `ts_unix`, `job_type`, `amount_cents`, and
   `kind` (reserve/record).

4. **Kill switch:** `QUANT_FOUNDRY_BUDGET_KILL_SWITCH=true` blocks ALL paid
   jobs regardless of remaining budget. This is the manual emergency stop —
   use it when you want to pause all GPU spend without changing the budget
   ceiling.

5. **Monthly reset:** Spend is tracked per calendar month (YYYY-MM). A new
   month starts with a fresh budget; previous months' spend is preserved in
   their own ledger files for audit.

6. **Zero-cost jobs:** Jobs with `amount_cents=0` (local mock, tests) are
   always allowed, even with a zero budget or active kill switch. This
   ensures local development is never blocked by the budget guard.

### Configuration

```bash
# Set a $50/month GPU budget
export QUANT_FOUNDRY_MONTHLY_BUDGET_CENTS=5000

# Emergency stop: block all paid jobs
export QUANT_FOUNDRY_BUDGET_KILL_SWITCH=true

# Normal operation: budget enforced, kill switch off
export QUANT_FOUNDRY_MONTHLY_BUDGET_CENTS=5000
export QUANT_FOUNDRY_BUDGET_KILL_SWITCH=false
```

### Integration

The budget guard is designed to be injected into the Quant Foundry gateway's
`create_job` method. When the gateway receives a job request with
`budget_cents > 0`, it calls `check_and_reserve` before enqueuing. If the
guard rejects the job, the gateway returns a 402 Payment Required (or 429
Too Many Requests) with the budget decision reason.

The gateway integration is NOT wired in this task (it would modify
`gateway.py`, which is owned by Builder 2). The budget guard module is
file-disjoint and ready for injection when the gateway is updated.

### Tests

`services/quant_foundry/tests/test_budget.py` — 20 tests covering:
- Basic guard behavior (allow within budget, reject over budget, cumulative
  tracking)
- Kill switch (blocks all paid jobs, can be toggled)
- Durability (spend survives restart, resets across months)
- Record spend (actual cost adjustment, per-month)
- Read API (get monthly spend, get summary)
- Edge cases (exact budget allowed, 1c over rejected, zero budget blocks
  paid but allows free, job type recorded)

---

## Cost Summary

| Category              | Local (dev)  | AWS (production)     |
|-----------------------|--------------|----------------------|
| Always-on shell       | $0           | ~$125-155/mo         |
| On-demand modules     | $0           | ~$57-115/mo (2h/day) |
| GPU (RunPod)          | $0           | $0.50-5.00/job       |
| LLM API (sentiment)   | $0 (no key)  | $20-40/mo            |
| **Total (no GPU)**    | **$0**       | **~$200-310/mo**     |
| **Total (with GPU)**  | **$0**       | **~$250-400/mo**     |

The budget guard ensures GPU spend stays within the operator's monthly
ceiling. With a $50/mo GPU budget, the operator can run 10-100 training jobs
per month depending on model size.

---

## References

- `services/api/src/api/routes/modules.py` — module control system (TASK-0203)
- `services/quant_foundry/src/quant_foundry/budget.py` — budget guard (TASK-0901)
- `services/quant_foundry/tests/test_budget.py` — budget guard tests
- `docs/ON_DEMAND_MODULES.md` — operator workflow for module control
- `docs/AWS_PRODUCTION_CONTROL_PLANE.md` — AWS production design (TASK-0903)
- `docs/NEXT_STEPS_PLAN.md` — TASK-0901 spec
