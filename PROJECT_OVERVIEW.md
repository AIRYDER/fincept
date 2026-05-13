# Fincept Terminal Project Overview

Last updated: 2026-05-10 after local Codex handoff reconciliation.

## Purpose

Fincept Terminal is an internal AI-agentic stock and crypto research plus paper-trading terminal. It is designed around a typed event spine that moves from market data ingestion through feature generation, agent/model prediction, orchestration, risk gating, paper OMS execution, portfolio state updates, and dashboard/operator review.

The current product boundary remains paper/research-first. Live capital execution, HFT latency targets, FIX/SIP buildout, and Bloomberg-parity scope are deliberately out of MVP scope.

## Architecture at a glance

Primary flow:

```text
ingestor -> features -> agents -> orchestrator -> risk -> OMS -> portfolio -> API/dashboard
```

Supporting stores and infrastructure:

- Postgres/Timescale for bars, ticks, audit, training metadata, and stateful read models.
- Redis Streams for event transport, service heartbeat, leadership locks, and online feature/state paths.
- MinIO/S3-compatible storage for model artifacts and larger offline assets.
- Next.js dashboard for operator surfaces.
- uv Python workspace for libraries and services.
- pnpm workspace for dashboard UI.

## Repository shape

- `libs/fincept-core` — Pydantic schemas, config, events, IDs, clocks, heartbeat/leadership, strategy config, prediction log, and shared portfolio math.
- `libs/fincept-bus` — Redis Streams producer/consumer and canonical stream names.
- `libs/fincept-db` — SQLAlchemy/Alembic helpers and universe data support.
- `libs/fincept-tools` — typed tool registry for LLM/data/research/paper-exec tools, including OpenBB/Exa research surfaces.
- `services/ingestor` — exchange/EOD ingestion and Timescale writing.
- `services/features` — point-in-time feature transforms and online/offline feature store paths.
- `services/agents` — model and information agents including GBM predictor, regime, sentiment, sentiment features, information enricher, news alpha predictor, and news outcome labeler.
- `services/orchestrator` — prediction consensus, target state, decisions, and order intents.
- `services/risk` — pre-trade checks, kill switch, and portfolio/risk snapshots.
- `services/oms` — paper OMS, Alpaca adapter, paper fills, order state transitions, and news sync support.
- `services/portfolio` — fill-driven positions and P&L.
- `services/api` — FastAPI REST/WebSocket gateway for the dashboard and operator routes.
- `services/backtester` — historical replay, strategy tests, walk-forward evaluation, and GBM strategy support.
- `services/jobs` — scheduled EOD/news/model candidate jobs.
- `services/strategy_host` — filesystem-backed strategy instance supervisor.
- `apps/dashboard` — Next.js 14 App Router operator console.
- `experiments/news-impact-model` — out-of-tree news impact research/workbench.
- `scripts` — Windows/local automation plus smoke/proof scripts.
- `docs` and `spec` — architecture, roadmap, ADRs, task specs, and product/design references.

## Dashboard surfaces

The dashboard currently includes:

- Overview
- Positions
- Orders
- Strategies and strategy detail pages
- Predictions
- Models and model detail pages
- Markets
- Risk
- Research
- News
- News lab
- News impact lab
- Reconciliation
- Signal cockpit demo
- AI portfolio builder at `/portfolio-builder`
- `/optimizer` compatibility redirect to `/portfolio-builder`

The dashboard uses `NEXT_PUBLIC_API_URL=http://localhost:8010` by default for local API calls.

## Portfolio builder and optimizer

The portfolio builder is currently an operator-facing planning surface, not an execution tool. It builds deterministic allocations and then asks an AI provider to write an investment committee-style packet.

Key properties:

- Deterministic allocation math is local and auditable.
- AI explains the allocation but does not set prices, share counts, or final weights.
- Providers include Auto/Best, GPT-5.5, and Claude Opus 4.7.
- It supports risk level, horizon, sectors/themes, cash reserve, target holdings, concentration caps, fractional shares, preferred/excluded tickers, dividend preference, volatility tolerance, and rebalance cadence.
- It emits candidate audits, constraints used, assumptions, risk analysis, charts, exportable JSON/CSV/PDF, and scenario war-room surfaces.

## OpenBB and research

OpenBB integration is read-only and uses local OpenBB API by default:

```text
OPENBB_API_URL=http://127.0.0.1:6900
```

Relevant surfaces:

- `POST /research/openbb/quote`
- `POST /research/openbb`
- `GET /research/openbb/health`
- `GET /research/openbb/health/history`
- `GET /research/openbb/readiness?symbol=NVDA&provider=yfinance`

The readiness route separates local API reachability from provider-backed quote/fundamental failures. The live proof script is:

```pwsh
uv run python scripts/openbb_live_proof.py --symbol NVDA
```

## Proof and verification receipts

Implemented local proof scripts:

- `scripts/paper_spine_replay.py` — deterministic local replay proving data -> feature -> signal -> decision -> risk approved/rejected -> order -> fill -> portfolio update -> audit trail.
- `scripts/openbb_live_proof.py` — probes Fincept OpenBB routes and writes live OpenBB receipts.
- `scripts/route_smoke.py` — probes operator-facing API routes and writes a route-smoke receipt.

Generated receipts under `reports/openbb-live`, `reports/paper-spine`, and `reports/route-smoke` are local generated artifacts and are ignored by Git.

## Local verification status

Focused verification passed on 2026-05-10 after Codex reconciliation:

```pwsh
uv run ruff check scripts/paper_spine_replay.py scripts/openbb_live_proof.py scripts/route_smoke.py services/api/src/api/routes/research.py services/api/src/api/openbb_health_store.py services/api/src/api/rate_limit.py libs/fincept-tools/src/fincept_tools/research/openbb.py
```

Result: passed.

```pwsh
uv run python scripts/paper_spine_replay.py
```

Result: passed.

```pwsh
uv run pytest services/api/tests libs/fincept-tools/tests/test_research_openbb.py libs/fincept-core/tests/test_strategy_config.py -q
```

Result: `398 passed`.

```pwsh
pnpm --dir apps/dashboard exec tsc --noEmit --pretty false
```

Result: passed.

## Current local Git/reconciliation state

After ignore cleanup, the visible local tree had:

- 157 changed paths
- 95 tracked modified paths
- 62 untracked source/docs paths
- Largest changed areas: `services`, `apps`, `docs`, `libs`, `experiments`, and `scripts`

The `.gitignore` was updated to hide local/session/generated artifacts such as `.bridgecode`, `.codex`, generated `tmp`, `reviewdocs`, local PDFs, prediction/training outputs, local model outputs, and generated proof receipt directories.

## Recommended review and commit grouping

Use separate review/staging groups:

1. Repo hygiene and script lint fixes.
2. OpenBB/research readiness and rate-limit surfaces.
3. Paper-spine, OpenBB, and route-smoke proof scripts.
4. Dashboard/operator UI expansion.
5. API/control/strategy-host work.
6. News impact, agents, jobs, and experiments.
7. Docs, roadmap, and task/spec synchronization.

Prefer `git add -p` or path-based staging. Do not commit generated `data`, `models`, `reports`, `tmp`, local PDFs, or IDE/session metadata.

## Next priorities

- Run full `scripts/preflight.ps1` once the local tree is staged or reviewable.
- Run live port-8010 route smoke against a running API.
- Run OpenBB live proof with the OpenBB API backend started.
- Repeat paper-spine proof against live Redis/Timescale service wiring, not just local pure/fakeredis components.
- Continue portfolio optimizer improvements after the current Codex handoff is reviewed and grouped.
