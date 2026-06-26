# Fincept Terminal Overview

## Executive Summary

Fincept Terminal is an internal AI-assisted research and paper-trading platform
for stock and crypto workflows. The repository is a mixed Python and Next.js
monorepo with implemented shared libraries, async Python services, a FastAPI
gateway, a Next.js operator dashboard, local automation scripts, planning docs,
and deterministic proof receipts.

The project has moved beyond a pure scaffold: package-level services, API
routes, dashboard pages, tests, and a deterministic paper-spine replay exist.
It is not production-ready. The strongest current evidence supports an MVP or
late prototype posture for paper trading and research operations, with remaining
blockers around live service proof, route smoke stability, auth hardening, path
boundaries, and generated artifact hygiene.

## Project Name

Fincept Terminal

Evidence:

- `README.md` describes "An internal, AI-agentic stock & crypto trading platform."
- `pyproject.toml` names the Python workspace `fincept-terminal`.
- `apps/dashboard/package.json` names the dashboard package `@fincept/dashboard`.

## Short Description

An internal operator console and service stack for market-data ingestion,
feature generation, predictive agents, paper-trading decisions, risk checks,
paper OMS fills, portfolio state, research tooling, and model lifecycle
operations.

## Primary Purpose

The primary purpose is to let internal operators and quants research, test,
paper-trade, and monitor AI-assisted strategies before any live-capital path is
considered.

Confirmed evidence:

- `docs/ROADMAP.md` recommends the "Research + Execution Terminal (MVP)" track.
- `docker-compose.yml` provides local Postgres/Timescale, Redis, and MinIO.
- `services/api/src/api/main.py` exposes FastAPI routes for data, orders,
  positions, strategies, models, research, news impact, and control.
- `apps/dashboard/src/app/` contains dashboard routes for markets, models,
  orders, positions, predictions, research, risk, system readiness, and more.
- `reports/paper-spine/latest.json` records a deterministic paper-spine replay
  proving data, feature, model signal, decision, risk, order, fill, portfolio,
  and audit-trail stages.

## Main Users

Confirmed:

- Internal operator using the Next.js dashboard.
- Developer or coding agent implementing contract-first tasks.
- Quant or researcher running backtests, model training, walk-forward reports,
  and research tooling.

Inferred:

- Future compliance or risk reviewer, because docs and code reference audit
  trails, kill switch state, WORM-like retention goals, and Phase H hardening.

## Main Workflows

Confirmed workflows:

- Local bootstrap and validation through `scripts/dev-setup.ps1`,
  `scripts/preflight.ps1`, `scripts/task-check.ps1`, `Makefile`, `uv`, and
  `pnpm`.
- Market-data and feature pipeline through `services/ingestor`,
  `services/features`, `fincept-bus`, and `fincept-db`.
- Predictive agent lifecycle through `services/agents`, `models/`, and
  `services/api/src/api/routes/models.py`.
- Paper decision flow through `services/orchestrator`, `services/risk`,
  `services/oms`, and `services/portfolio`.
- Operator UI through `apps/dashboard/src/app` and the typed client in
  `apps/dashboard/src/lib/api.ts`.
- Research and datasource workflows through `services/api/src/api/routes/research.py`,
  `services/api/src/api/routes/data.py`, and `libs/fincept-tools`.
- Deterministic proof workflows through `scripts/paper_spine_replay.py`,
  `scripts/route_smoke.py`, `scripts/openbb_live_proof.py`, and reports under
  `reports/`.

## Current Maturity Level

Assessment: MVP / late prototype, not production.

Why:

- More than a scaffold: many packages, routes, tests, dashboard pages, and
  receipts exist.
- Not production candidate: auth is single-token bearer JWT with a known dev
  default, dashboard tokens are stored in `localStorage`, direct live capital is
  intentionally out of scope, route-smoke has a known `/data/coverage` timeout
  receipt, and the paper-spine proof is deterministic/fakeredis rather than
  service-backed.

Confidence: high.
