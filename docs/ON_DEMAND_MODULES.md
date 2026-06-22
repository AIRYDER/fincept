# On-Demand Modules (TASK-0203)

> Operator workflow for starting and stopping optional Fincept modules only
> when needed, with idle timeouts, one-instance controls, and receipts.

## Why this exists

Fincept runs a light control plane (dashboard + API + Redis + Timescale) all
the time. Optional modules — OpenBB, market-data ingestion, news learning,
GBM predictor, sentiment agents, regime agent, the jobs worker — cost
resources (CPU, API quota, LLM tokens) and should run only when the operator
actually needs them. This task makes that workflow explicit, safe, and
auditable.

## Where to find it

- **API route:** `services/api/src/api/routes/modules.py`
- **Dashboard panel:** `apps/dashboard/src/components/modules/module-control-panel.tsx` (mounted on `/system`)
- **Typed client:** `apps/dashboard/src/lib/api.ts` (`api.modules`, `api.startModule`, …)
- **Tests:** `services/api/tests/test_modules.py`
- **Allowlisted launch scripts (reused):** `scripts/start_feature.ps1`, `scripts/stop_feature.ps1`

## Module registry

The registry is predeclared in `MODULE_REGISTRY` inside `modules.py`. Each
entry maps a module ID to the existing allowlisted feature control surface
(`api.routes.control`) plus operator metadata:

| module_id            | display name              | cost   | idle timeout | services                                  |
|----------------------|---------------------------|--------|--------------|-------------------------------------------|
| `openbb`             | OpenBB Research Terminal  | medium | 30 min       | —                                         |
| `market_data`        | Market Data Ingestion     | medium | 60 min       | ingestor, features                        |
| `news_learning`      | News Learning Loop        | medium | 45 min       | information_enricher, news_outcome_labeler|
| `jobs`               | Background Jobs Worker    | low    | 20 min       | jobs                                      |
| `gbm_predictor`      | GBM Predictor Agent       | low    | 30 min       | gbm_predictor                             |
| `news_alpha_predictor`| News Alpha Predictor     | low    | 30 min       | news_alpha_predictor                      |
| `sentiment`          | Sentiment Agents          | high   | 15 min       | sentiment_agent, sentiment_features       |
| `regime`             | Regime Agent              | low    | 60 min       | regime_agent                              |

To add a new module, add a `ModuleSpec` entry to `MODULE_REGISTRY`. The
`feature_id` must already exist in `control._FEATURE_SERVICES` and have a
case in `scripts/start_feature.ps1` / `scripts/stop_feature.ps1`.

## API endpoints

All endpoints require a valid Bearer JWT (`require_user`). Launch endpoints
are **local-only** (host must be in the local allowlist).

| Method | Path                          | Purpose                                  |
|--------|-------------------------------|------------------------------------------|
| GET    | `/modules`                    | List all modules with live status + idle |
| GET    | `/modules/{id}`               | Single module detail                     |
| POST   | `/modules/{id}/start`         | Start an allowlisted module              |
| POST   | `/modules/{id}/stop`          | Stop a module                            |
| POST   | `/modules/{id}/restart`       | Restart a module                         |
| POST   | `/modules/stop-all`           | Stop every running optional module       |
| POST   | `/modules/sweep-idle`         | Stop modules past their idle timeout     |
| GET    | `/modules/receipts`           | Recent start/stop/auto_stop receipts     |

## Security invariants (non-negotiable)

- **No arbitrary shell command execution from user input.** Module IDs are
  allowlisted against `MODULE_REGISTRY`. Start/stop dispatch reuses
  `control._run_feature_script`, which invokes the predeclared
  `start_feature.ps1` / `stop_feature.ps1` scripts keyed only by the
  allowlisted module ID. The user never supplies a command string.
- **Auth required** for every operator endpoint.
- **Local-only** launches (`_assert_local`).
- **Secrets are never echoed.** Receipt output is run through `_redact_output`
  before persistence and before any response (strips `sk-`, `Bearer `, token
  patterns, private keys, etc.).
- **Duplicate starts do not spawn unbounded processes.** If a module's
  declared services are already heartbeating fresh, `start` returns
  `already_running` without spawning a subprocess.

## Idle timeout enforcement

`POST /modules/sweep-idle` is the canonical idle-timeout enforcement. It
stops any module that is both marked `running` AND past its
`idle_timeout_sec` with no fresh service heartbeats, recording an
`auto_stop` receipt. The dashboard polls it via the "Sweep idle" button; a
future enhancement can call it on a timer. Modules with fresh heartbeats are
considered actively in use and are not stopped (their `last_activity_unix`
is refreshed instead).

## Receipts

Every `start`, `stop`, `restart`, and `auto_stop` action records a receipt in
the Redis list `module:receipts` (trimmed to 500 entries, 7-day TTL). Each
receipt carries: `module_id`, `action`, `status`, `actor` (JWT `sub`),
redacted `output`, and `ts_unix`. The dashboard shows the 10 most recent
receipts under the module panel.

## Operator workflow

1. Open the dashboard `/system` page.
2. The "On-demand modules" panel shows every optional module with status,
   idle countdown, and cost class.
3. Click the power button to start a module; the API launches the
   allowlisted script locally.
4. Click the stop button to stop a module, or "Stop all" to stop every
   running optional module.
5. Click "Sweep idle" to enforce idle timeouts.
6. Recent receipts appear at the bottom of the panel for audit.
