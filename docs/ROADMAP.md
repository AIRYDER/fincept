# Fincept Terminal — Pragmatic Roadmap

> **Source:** Derived from `BLUEPRINT.md` with realistic scoping applied.
> **Audience:** Engineering leadership, product, founding team.
> **Last updated:** 2026-06-23
> **Current-state note:** this roadmap began as a pragmatic plan derived from `BLUEPRINT.md`. The local codebase now contains working slices from later phases, including paper trading services, API/dashboard surfaces, strategy configuration, agents, research/data-source tooling, and model workflows. Treat earlier phase tables as planning context, not a strict remaining-work checklist.

---

## 1. Reality Check on the Blueprint

The blueprint describes a platform that, if fully delivered, would rival Bloomberg Terminal + a top-tier HFT shop + a quant research platform — simultaneously. That is a **5–10 year, 50–100 engineer effort** at $50M–$200M fully-loaded cost. The blueprint claims 14 months with 12–15 engineers.

Before any work begins, leadership must pick one of the following **positioning bets**. These are mutually exclusive in the 12–24 month window.

| Bet | What you're actually building | Team | Realistic timeline | Cuts from blueprint |
|---|---|---|---|---|
| **A. Research + Execution Terminal (MVP)** | Python-based research & paper-trading platform for 1–5 internal quants, live crypto + EOD equities | 4–6 | 6–9 months to internal beta | Drop FPGA, kernel bypass, Qt6, sub-100μs, FIX certification, regulatory reporting |
| **B. Low-Latency Crypto Trading** | Rust/C++ crypto HFT engine, minimal UI, 1–3 exchanges, sub-millisecond (not sub-100μs) | 5–8 | 9–15 months to live trading | Drop equities, Bloomberg-style UI, multi-agent AI orchestration, regulatory modules |
| **C. AI-First Research Platform** | Multi-agent AI orchestration + backtesting + web dashboard, no live execution | 4–6 | 6–12 months to alpha discovery pipeline | Drop HFT, exchange connectivity, OMS/EMS, FPGA, compliance |
| **D. Full blueprint (aspirational)** | Everything in `BLUEPRINT.md` | 40+ | 4–7 years | Nothing — but requires $30M+ funding |

**Recommendation:** Start with **Bet A (MVP Track)** below. It derisks the team, produces demonstrable value in 6 months, and the code is not thrown away when expanding into B or C.

---

## 2. MVP Track (Bet A) — Recommended

### Guiding principles

1. **Python-first, optimize later.** No C++ or FPGA until a specific latency bottleneck is proven and profiled.
2. **Paper trading before capital.** Live execution is out of scope for MVP.
3. **Crypto before equities.** Crypto APIs are open; equity direct market access requires months of legal + FIX certification.
4. **Web UI, not native.** Qt6 is a 6–12 month investment; React/Next.js dashboard delivers 80% of the value in 2 months.
5. **Monorepo, single language per service.** Python for services, TypeScript for frontend, SQL for storage.

### Phase 0 — Foundation (Weeks 1–4)

| # | Deliverable | Owner | Exit criteria |
|---|---|---|---|
| 0.1 | Monorepo bootstrap (uv/poetry + pnpm workspace) | Platform eng | `make dev` spins up Postgres + Timescale + Redis locally |
| 0.2 | CI pipeline (lint, typecheck, test, build) | Platform eng | PR must pass before merge; <5 min pipeline |
| 0.3 | Secrets management (.env + Vault-compatible abstraction) | Platform eng | No secrets in repo; rotation documented |
| 0.4 | ADR process established (`docs/DECISIONS.md`) | Tech lead | First 5 ADRs written & approved |
| 0.5 | Observability baseline (OpenTelemetry + Grafana Cloud free tier) | Platform eng | Traces visible for every service request |

**Budget:** 2 engineers × 4 weeks. **Cost of skipping:** every future phase slows 2–3×.

### Phase 1 — Data Spine (Weeks 5–12)

Goal: reliable market data pipeline feeding a queryable store.

| # | Deliverable | Blueprint ref | Effort (pts) |
|---|---|---|---|
| 1.1 | Crypto WebSocket ingestor (Binance, Coinbase, Kraken) | 2.1.1.2 | 13 |
| 1.2 | Normalized event schema (trade, book update, bar) | 2.1.3.1 | 5 |
| 1.3 | TimescaleDB schema + hypertables for trades/books/bars | 2.1.3.2 | 8 |
| 1.4 | EOD equity loader (yfinance / polygon.io free tier) | 2.1.1.1 | 5 |
| 1.5 | Gap detection + resubscribe logic | 2.1.1.2 | 8 |
| 1.6 | Data quality dashboard (latency, gaps, cross-spread) | 2.1.2 | 5 |

**Exit criteria:** 7-day continuous uptime ingesting 10 crypto pairs; <100ms end-to-end ingestion latency (WS recv → DB commit); queryable via SQL.

**Original scope note:** FIX, SIP, Level 2 equity, and on-chain metrics remain out of scope. News sentiment is no longer fully deferred: a NewsAPI + LLM sentiment agent exists, but it remains optional/key-gated and requires calibration before it should affect sizing materially.

### Phase 2 — Research Environment (Weeks 13–20)

Goal: internal quants can iterate strategies against historical + live data.

| # | Deliverable | Blueprint ref | Effort (pts) |
|---|---|---|---|
| 2.1 | Python SDK (`fincept` package) for data access | 2.2, 3.3 | 8 |
| 2.2 | JupyterHub deployment with shared kernels | 4.3.3.2 | 5 |
| 2.3 | Event-driven backtester (vectorbt or custom) | 4.3.3.1 | 13 |
| 2.4 | Transaction cost model (spread + slippage + fees) | 4.3.3.1 | 8 |
| 2.5 | Strategy base class + registry | 2.3.1 | 5 |
| 2.6 | Reference strategies: MA crossover, pairs, mean reversion | 2.3.1.2 | 8 |
| 2.7 | Performance report generator (QuantStats integration) | 4.3.3.3 | 3 |

**Exit criteria:** one quant can go from idea → backtest → report in <1 day. Backtester throughput ≥50k events/sec on laptop.

### Phase 3 — Paper Trading + Dashboard (Weeks 21–30)

Goal: strategies run live against paper books; humans watch via web UI.

| # | Deliverable | Blueprint ref | Effort (pts) |
|---|---|---|---|
| 3.1 | Paper OMS (internal order book, fill simulator) | 2.3.3 | 13 |
| 3.2 | Strategy runner (async Python, one process per strategy) | 2.3.1 | 8 |
| 3.3 | Position + P&L service | 2.4 | 8 |
| 3.4 | Pre-trade risk checks (notional, concentration, max dd) | 2.4.2 | 8 |
| 3.5 | Next.js dashboard shell with auth | 4.2.3.1 | 8 |
| 3.6 | Real-time positions + P&L panel (WebSocket) | 4.2.3.3 | 8 |
| 3.7 | Live chart with TradingView Lightweight Charts | 4.4.1.1 | 5 |
| 3.8 | Strategy control panel (start/stop/params) | 2.3.1 | 5 |
| 3.9 | Alert service (Slack + email) | 3.5.2.2 | 5 |

**Exit criteria:** 3 strategies run paper-traded for 2 weeks; dashboard visualizes all positions; risk limits enforced; zero unplanned outages >5 min.

### Phase 4 — AI Agent Layer (Weeks 31–40)

Goal: introduce one predictive agent and one research agent — prove the pattern.

| # | Deliverable | Blueprint ref | Effort (pts) |
|---|---|---|---|
| 4.1 | Agent base class + message bus (Redis Streams or NATS) | 2.2.2 | 8 |
| 4.2 | Feature store (Feast or custom on Timescale) | 3.3.2.2 | 13 |
| 4.3 | Predictive agent: LightGBM short-horizon direction | 2.2.1.1 | 8 |
| 4.4 | Model registry + MLflow integration | 2.2.3.2 | 5 |
| 4.5 | Shadow deployment harness (compare live vs. model) | 2.2.3.2 | 8 |
| 4.6 | Research agent: nightly hyperparameter sweep | 2.2.1.4 | 8 |
| 4.7 | Consensus layer (weighted voting across 2+ models) | 2.2.2.2 | 5 |

**Exit criteria:** one model deployed in shadow for 4 weeks with calibration report; walk-forward backtest shows Sharpe >0.5 improvement over baseline.

**Explicitly deferred:** hierarchical meta-agents, dynamic agent spawning, reinforcement learning. These require a working single-agent pipeline first.

### Phase 5 — Hardening (Weeks 41–48)

Only enter this phase if leadership commits to limited-capital live trading.

- Reliability: chaos testing, hot standby DB, RTO <30 min
- Security: HSM for exchange API keys, mTLS between services, audit log
- Compliance prep: trade archival (7y retention), user access review
- Performance: profile and optimize hot paths; migrate ingestor to Rust only if measured latency >SLO

---

## 3. Where the Blueprint Is Wrong (Read Before Funding)

| Blueprint claim | Reality | Recommendation |
|---|---|---|
| Sub-100μs tick-to-trade in 14 months with 12 engineers | Achievable only with co-lo + FPGA + 3+ years experienced team; typical 3–5 years for greenfield | Drop latency target to sub-10ms for MVP; revisit only if alpha demands it |
| "Bloomberg replacement at zero recurring cost" | Bloomberg's moat is data licensing (SIP, NI news, MSG), not software. Data alone costs $500k–$5M/year for comparable coverage | Scope to crypto + free/cheap equity data; accept narrower asset coverage |
| Multi-agent AI with hierarchical coordination + consensus + dynamic spawning | Cutting-edge research; production deployments are rare and brittle | Start with 1 agent, add second only after first proves Sharpe improvement in shadow |
| Qt6 desktop terminal with command mnemonics + multi-monitor | 12–18 months of UI work alone; alienates non-traders on the team | Build Next.js web UI; add desktop shell via Tauri only if traders request it |
| FPGA acceleration as Phase 4 deliverable | FPGA engineers cost $300k+ and take 6 months to onboard; Vitis workflows are brutal | Remove entirely unless proving a specific arb that requires it |
| Continuous learning with online model adaptation | Concept drift + model governance is an unsolved problem in finance; most prod systems retrain offline weekly | Offline nightly retrain with holdout validation; no online SGD in production |
| "Democratization of institutional-grade tools" + "24,000–32,000/user Bloomberg" | These goals contradict. If you democratize, you compete with retail platforms (TradingView, QuantConnect) that are already excellent and free/cheap | Pick: internal tool for one firm, OR public platform. Not both. |

---

## 4. Decision Gates

Do not proceed to the next phase without meeting the gate.

- **Gate 0→1:** CI green, observability functional, 2 ADRs signed for language/DB/UI stack.
- **Gate 1→2:** 7-day data uptime, <100ms ingestion latency, data quality dashboard green.
- **Gate 2→3:** 3 backtested strategies with positive Sharpe on 2+ years history, peer-reviewed.
- **Gate 3→4:** 2 weeks paper trading without unplanned outage, P&L matches backtest within 20% after costs.
- **Gate 4→5:** Shadow model beats baseline at p<0.05 over ≥4 weeks; risk committee approval to deploy capital.
- **Gate 5→Live:** Internal SOC-2-equivalent audit; disaster recovery drill passed; 4 eyes on every production deployment.

---

## 5. Staffing Plan

| Phase | Backend Py | Platform/DevOps | Frontend | Quant/ML | Total |
|---|---|---|---|---|---|
| 0 | 1 | 1 | 0 | 0 | 2 |
| 1 | 2 | 1 | 0 | 1 | 4 |
| 2 | 2 | 1 | 0 | 2 | 5 |
| 3 | 2 | 1 | 2 | 2 | 7 |
| 4 | 2 | 1 | 1 | 3 | 7 |
| 5 | 3 | 2 | 1 | 3 | 9 |

Optional roles (introduce only when justified): Rust/C++ systems engineer (Phase 5+), security lead (Phase 5+), compliance officer (pre-live).

---

## 6. Budget Envelope (Order-of-Magnitude)

| Line item | Year 1 |
|---|---|
| Engineering (7 avg × $200k fully-loaded) | $1.4M |
| Cloud + data feeds (AWS + Polygon + CCXT Pro) | $60k |
| Tooling (GitHub, Grafana Cloud, MLflow, Sentry) | $20k |
| Market data (if equities expand) | $100k–$500k |
| **Total Year 1** | **~$1.6M–$2M** |

If budget is <$500k: execute Bet C (research platform) only, 3-person team, 12 months to alpha discovery dashboard.

If budget is >$10M: revisit Bet D (full blueprint) with proper org design.

---

## 7. Local Update — 2026-04-26

The local repo now contains the Phase 0/Phase F scaffold: uv workspace, pnpm dashboard workspace, Docker dev stack, Makefile commands, package/service directories, contract docs, and prompt/task specs. Treat this as "scaffold complete, runtime not implemented."

### What changed since the prior planning-only state

| Area | Current local state | Roadmap impact |
|---|---|---|
| Repo structure | `libs/*`, `services/*`, `apps/dashboard`, `docs/*`, and `spec/*` exist. | Stop debating repo shape; start filling contracts and tests. |
| Build order | `spec/BUILD_ORDER.md` marks Task 001 complete and Tasks 002-006 open. | Phase F should stay the immediate focus. |
| Runtime code | Python service packages are stubs with `__init__.py` only. | Do not start agents, UI, or advanced models yet. |
| Specs | Contracts, layout, task prompts, and build order are detailed. | Use spec-driven implementation one task at a time. |

### Roadmap adjustment

Before Phase 1 market-data ingestion, add two Foundation hardening tasks:

| # | Deliverable | Owner | Exit criteria |
|---|---|---|---|
| 0.6 | Contract replay harness | Platform eng | JSON fixtures validate all market, signal, decision, order, fill, and risk schemas in CI. |
| 0.7 | Latency budget ledger | Platform eng | Every bus envelope can record service timestamps and emit p50/p95/p99 latency summaries. |

### Recommended next local task

Implement `TASK-002-fincept-core` first, then immediately add fixture tests that import the exact models from `spec/CONTRACTS.md`. Only after that should `fincept-bus` or `fincept-db` receive implementation work.

See `featuresmenu.md` for the innovation backlog and skill progression map.

---

## 8. Next Actions (This Week)

1. Leadership picks a Bet (A/B/C/D). **This is the critical decision.**
2. If Bet A: approve Phase 0 kickoff, hire/assign 2 engineers.
3. Sign ADRs in `docs/DECISIONS.md` for: primary language (Python), primary DB (Timescale), UI framework (Next.js), message bus (Redis Streams vs NATS).
4. Stand up the monorepo per Phase 0.
5. For the current local scaffold: implement `fincept-core` contracts, then add replay fixtures and latency ledger before ingesting real venue data.

## 9. Automation Review Update — 2026-04-26 07:01 America/Chicago

### Changes observed locally since the prior analysis

| Area | Evidence | Roadmap response |
|---|---|---|
| Repo boundary | `git rev-parse --show-toplevel` resolves to `C:/Users/nolan`, so this folder is not an isolated Git repo yet. | Initialize or move into a dedicated repo before treating status/diffs as project signal. |
| Scaffold | `libs/*`, `services/*`, `apps/dashboard`, `docs/*`, and `spec/*` exist; service modules are still package stubs. | Keep implementation on Phase F; do not skip to venue adapters or agents. |
| Build sequencing | `spec/BUILD_ORDER.md` marks only Task 001 complete; Tasks 002-006 remain open. | The next coding task is still `TASK-002-fincept-core`. |
| Architecture | `spec/ARCHITECTURE.md` has hard service boundaries and Redis Streams split into `md.*`, `sig.*`, and `ord.*`. | Add replay and latency tooling at the bus/schema layer so later agents inherit it. |

### Roadmap refinement

Add these two tasks to Phase F before `fincept-bus` and `fincept-db` are considered complete:

| # | Deliverable | Exit criteria |
|---|---|---|
| 0.8 | Schema drift sentinel | CI fails when examples in `spec/CONTRACTS.md`, `fincept-core` models, and replay fixtures disagree. |
| 0.9 | Paper-only execution guardrail | Any code path that can create an order must carry `execution_mode=paper` until a second live-trading confirmation gate exists. |

### Recommended next local sequence

1. Make `fincept-terminal` an isolated repository or consciously keep it as a project folder under the broader `C:/Users/nolan` tree.
2. Implement `libs/fincept-core` models directly from `spec/CONTRACTS.md`.
3. Add replay fixtures for `md.*`, `sig.*`, and `ord.*`.
4. Add a paper-only guard to the order/decision schemas before OMS work begins.

## 10. Automation Review Update — 2026-04-27 America/Chicago

### Changes observed locally since the last analysis

| Area | Evidence | Roadmap response |
|---|---|---|
| Build sequence | `spec/BUILD_ORDER.md` now marks Tasks 001, 002, 003, and 004 complete. | Foundation is no longer scaffold-only; the next unchecked task is `TASK-005-fincept-tools`. |
| Core contracts | `libs/fincept-core` contains schemas, events, config, tracing/logging, clocks, IDs, errors, and leadership tests. | Treat `fincept-core` as the source library for future fixtures; stop copying examples from docs into service code. |
| Event bus | `libs/fincept-bus` now has Redis Streams producer/consumer code, stale pending-message claiming, acknowledgement behavior, and consumer tests. | Add real Redis latency and replay checks in CI before venue ingestors are trusted. |
| Database layer | `libs/fincept-db` now has async SQLAlchemy engine/session helpers, ORM models, Alembic migration, and ticks/bars/audit access tests. | Keep Postgres/Timescale service-container tests as a Phase F gate, because local DB tests were skipped without the service running. |
| CI surface | `.github/workflows/ci.yml` now runs Python lint/typecheck, Python tests with Redis and Timescale, JS workspace checks, coverage upload, and gitleaks. New `build-images.yml` and `nightly.yml` exist. | Finish `TASK-006` by proving these workflows against the current repo and adding a local preflight command that mirrors CI. |
| Local verification | `uv run pytest libs -q` passed with 29 passed and 11 skipped. | Good unit signal, but not enough to mark Foundation checkpoint complete until Redis/Postgres-backed tests run. |

### Local automation landed

- `scripts/dev-setup.ps1` now wraps the repeated Windows bootstrap path: copy `.env`, start Docker, sync `uv`, install `pnpm` deps, install hooks.
- `scripts/preflight.ps1` now provides the Phase F `0.11` local CI-parity command on Windows: Docker up, `uv sync`, ruff, format-check, mypy, Alembic upgrade, pytest coverage, JS workspace checks, and gitleaks.

### Roadmap refinement

Move these items ahead of market-data ingestion:

| # | Deliverable | Exit criteria |
|---|---|---|
| 0.10 | Tool protocol and audit-safe registry | `fincept-tools` exposes typed data, analytics, and paper-exec tools; every tool call records input hash, output hash, caller, and run ID. |
| 0.11 | CI parity preflight | One local command runs ruff, mypy, pytest, alembic upgrade against local services, and JS workspace checks with the same env defaults as CI. |
| 0.12 | Redis/Postgres replay proof | A checked-in fixture suite publishes representative `md.*`, `sig.*`, and `ord.*` events through Redis, persists to Timescale, and can replay from a recorded stream ID. |

### Recommended next local sequence

1. Implement `TASK-005-fincept-tools` with a paper-only execution tool and no live venue path.
2. Finish `TASK-006` by running the new GitHub Actions logic locally or in CI and recording the result.
3. Add replay fixtures that exercise `fincept-core`, `fincept-bus`, and `fincept-db` together.
4. Only then start `TASK-010` ingestor base class and normalizer.

## 11. Automation Review Update — 2026-04-28 America/Chicago

### Changes observed locally since the last analysis

| Area | Evidence | Roadmap response |
|---|---|---|
| Tools package | `libs/fincept-tools` exists and is listed in the root uv workspace, but it is still a stub package. | Start `TASK-005` directly in that package; do not spend another pass on folder scaffolding. |
| Build sequence | `spec/BUILD_ORDER.md` still marks Tasks 001-004 complete and 005-006 open. | Keep Phase F focused on tool protocol plus CI/service proof before any ingestor or agent work. |
| Local automation | `scripts/dev-setup.ps1` and `scripts/preflight.ps1` are present and documented. | Use `preflight.ps1` as the official Windows Phase F gate, but record skipped or missing service checks explicitly. |
| Verification boundary | The last recorded library test signal remains `uv run pytest libs -q` with 29 passed and 11 skipped. | Do not claim Foundation complete until Redis latency and Timescale tests pass under local Docker or CI services. |

### Roadmap refinement

Move these implementation details into `TASK-005`:

| Deliverable | First file to create | Exit criteria |
|---|---|---|
| Typed tool manifest | `libs/fincept-tools/src/fincept_tools/manifest.py` | Every tool declares name, input schema, output schema, side-effect class, required capability, and audit policy. |
| Audit wrapper | `libs/fincept-tools/src/fincept_tools/registry.py` | Test calls record caller ID, run ID, input hash, output hash, duration, and success/failure without leaking secrets. |
| Paper execution guard | `libs/fincept-tools/src/fincept_tools/paper_exec.py` | Paper order tools can emit an order proposal, while live execution imports fail closed. |
| Contract fixtures | `libs/fincept-tools/tests/test_registry.py` | Malformed arguments, unknown tools, blocked live tools, and replayable audit records are covered. |

### Recommended next local sequence

1. Implement the minimal `fincept-tools` manifest and registry before adding analytics helpers.
2. Add one paper-only order proposal tool that returns a typed decision/order payload and an audit receipt.
3. Run `powershell -ExecutionPolicy Bypass -File .\scripts\preflight.ps1`; if Docker or service-backed checks fail, record the exact missing gate in this roadmap.
4. Add the Redis-to-Timescale replay drill only after the tool audit wrapper has a stable run ID format.

## 12. Automation Review Update — 2026-04-30 America/Chicago

### Changes observed locally since the last analysis

| Area | Evidence | Roadmap response |
|---|---|---|
| Foundation tools | `libs/fincept-tools` now contains protocol, registry, data, analytics, and paper execution tool modules plus tests. | Treat `TASK-005-fincept-tools` as package-complete; next work should harden audit receipts and service integration rather than re-scaffold tools. |
| Build sequence | `spec/BUILD_ORDER.md` marks Tasks 005 and 006 complete, and many later data/backtest/agent/risk/OMS/API/UI tasks are checked. | Do not read checked boxes as production proof; require service-backed receipts for each checkpoint before advancing live-capital assumptions. |
| Task verification wrapper | `scripts/task-check.ps1` previously allowed a pytest import failure to be followed by "Task check passed"; it now runs pytest with the selected uv workspace package and fails on nonzero exits. | Keep the task-level wrapper as the narrow daily loop, and use `scripts/preflight.ps1` only for broader CI parity. |
| News impact experiment | `experiments/news-impact-model` has modified docs/source plus new workbench files and sample data. | Treat this as a separate experimental surface; route it through shadow-mode evaluation before connecting to orchestrator decisions. |

### Verification recorded this run

| Command | Result | Boundary |
|---|---|---|
| `powershell -ExecutionPolicy Bypass -File .\scripts\task-check.ps1 -PackagePath libs/fincept-tools -PytestPath libs/fincept-tools/tests` | Passed: 81 pytest tests, Ruff clean, Mypy clean. | Package-local only; no Redis/Timescale service proof. |

### Roadmap refinement

Move these items to the front of the local queue:

| Priority | Work | Actionable exit criteria |
|---|---|---|
| P0 | Phase checkpoint receipt | A short `VERIFICATION_REPORT.md` or docs section records `scripts/preflight.ps1` output with pass/fail/skip status for Redis, Timescale, Python, JS, Alembic, and secret scan. |
| P0 | End-to-end paper spine replay | One fixture goes through data event, feature row, prediction, decision, risk, paper order, fill, and portfolio update with a reconstructable audit trail. |
| P1 | Tool audit receipt table | Every `fincept-tools` call emits caller ID, run ID, input hash, output hash, side-effect class, duration, and error type when applicable. |
| P1 | News-impact shadow gate | The news impact workbench produces a replayable shadow signal report with no order path until calibration and drawdown impact are measured. |

### Recommended next local sequence

1. Run `scripts/preflight.ps1` and record the exact pass/fail/skip matrix.
2. Add the end-to-end paper spine replay before expanding Phase X agents.
3. Add tool audit receipt persistence so `fincept-tools` can be used by LLM agents without opaque behavior.
4. Keep the news-impact model in `experiments/` until it has a deterministic shadow-mode report and a clear feature contract.

## 13. Automation Review Update — 2026-05-02 America/Chicago

### Changes observed locally since the last analysis

| Area | Evidence | Roadmap response |
|---|---|---|
| Operator app surface | Dashboard and API changes now cover strategy config CRUD/lifecycle, manual orders, research pages, news-impact lab, symbol search, OpenBB quote/dispatcher/health, and richer strategy/order tests. | Treat this as an operator-workflow integration phase; the next proof should be route/API/dashboard contract evidence, not another isolated package test. |
| Strategy runtime | `libs/fincept-core/src/fincept_core/strategy_config.py` and `services/strategy_host/` add persistent strategy instance configs plus a live runner/supervisor. | Promote strategy-host to a first-class Phase F/G workstream and test disabled/enabled strategy behavior inside the paper-spine replay. |
| Research tooling | `fincept_tools.research` adds Exa and OpenBB tools, and `/research/*` routes add allowlists, local OpenBB URL handling, health history, and Redis-backed rate limiting. | Keep these tools read-only and add cost/usage receipts before connecting them to autonomous agents. |
| News-impact experiment | `/news-impact/*` exposes the experiment workbench through the API while keeping it in experimental demo mode. | Keep it behind shadow gates: no order emission until replayable calibration, drawdown-impact, and feature-contract reports exist. |
| Local run scripts | `start/status/stop` default the API to `8010` and detect non-Fincept processes occupying the API port. | Document port `8010` as the current local default and include it in preflight/status receipts. |

### Verification recorded this run

| Command | Result | Boundary |
|---|---|---|
| Local inspection and docs refresh | Completed. | No broad `scripts/preflight.ps1`, Docker service, API server, dashboard build, or route smoke test was run in this pass. |

### Roadmap refinement

Move these items to the front of the local queue:

| Priority | Work | Actionable exit criteria |
|---|---|---|
| P0 | API/dashboard contract receipt | A checked report records `GET /health`, `/data/symbols/search`, `/research/openbb/health`, `/strategies/configs`, `/orders`, and the dashboard API client against port `8010`. |
| P0 | Strategy-host paper replay | One replay starts an enabled strategy config, confirms a disabled config stays silent, emits an order intent, and traces it through OMS/fill/portfolio state. |
| P1 | Research tool governance | Exa/OpenBB calls emit caller, route/tool name, cost or provider latency, rate-limit state, input hash, output hash, and error type. |
| P1 | News-impact shadow report | The experiment produces a deterministic report with calibration buckets, top analogs, horizon error, drawdown impact, and an explicit "no order route" assertion. |
| P1 | Docs routing cleanup | Link `docs/datasources.md`, `docs/openbb-research-handoff.md`, `docs/portfoliooptimizer.md`, and `docs/uirecommendations.md` from the main README or a docs index. |

### Recommended next local sequence

1. Run `powershell -ExecutionPolicy Bypass -File .\scripts\preflight.ps1` and record pass/fail/skip status.
2. Add a route smoke script for the port-`8010` API plus dashboard client assumptions.
3. Build the strategy-host replay before expanding autonomous research/agent features.
4. Keep Exa/OpenBB and news-impact features read-only until usage receipts and shadow reports exist.

## 14. Automation Review Update — 2026-05-02 Evening America/Chicago

### Changes observed locally since the last analysis

| Area | Evidence | Roadmap response |
|---|---|---|
| Datasource hardening | Commit `2d506fd` adds datasource contract work across `/data`, symbol search, dashboard market types, Exa/OpenBB tools, and related tests. | Promote datasource registry and coverage freshness to the next operator-control primitive instead of treating provider calls as isolated utilities. |
| Review evidence | `docs/codebase-review-2026-05-02.md` records 31 targeted API tests passed, selected Ruff checks passed, and dashboard typecheck passed. | Treat this as targeted subsystem evidence only; broad preflight, live Timescale/OpenBB, route smoke, and browser checks remain open. |
| Contract drift | The review flags `venue_default` returned by universe reads while the dashboard type names `venue`. | Resolve the API/frontend contract before adding new market panels that consume venue fields. |
| Coverage performance and safety | Coverage reads were hardened, but the review still calls out batching, venue semantics, and raw exception exposure as risk areas. | Make `/data/coverage` the canonical data heartbeat only after batch reads, safe public errors, and freshness history are in place. |
| Placeholder docs | Several planned docs exist as zero-byte files, including datasource, portfolio, UI, and next-level feature notes. | Fill docs only when they route real implementation surfaces; do not count empty docs as roadmap evidence. |

### Verification recorded this run

| Command or source | Result | Boundary |
|---|---|---|
| `docs/codebase-review-2026-05-02.md` targeted checks | API tests, selected Ruff checks, and dashboard typecheck passed in that review. | Not rerun in this automation pass; no live Timescale/OpenBB, browser, full preflight, or Docker proof. |
| Local inspection and docs refresh | Completed. | Read-only code review plus markdown updates; no service startup. |

### Roadmap refinement

| Priority | Work | Actionable exit criteria |
|---|---|---|
| P0 | Datasource contract cleanup | `/data/universe` and dashboard types agree on `venue_default` or deliberately expose both `venue` and `venue_default`, with tests proving the chosen shape. |
| P0 | Data heartbeat receipt | `/data/coverage` uses batch reads, safe public errors, explicit venue semantics, and stores periodic snapshots for freshness trend display. |
| P0 | Port-8010 contract smoke | One local command probes `/health`, `/data/sources`, `/data/coverage`, symbol search, OpenBB health, strategy configs, and orders against `http://127.0.0.1:8010`. |
| P1 | Provider health center | Dashboard renders datasource registry rows with safety tier, health mode, last success, stale/error state, and required config names without secret values. |
| P1 | OpenBB preset registry | Move hardcoded OpenBB page presets into a typed registry with provider requirements, expected columns, latency expectation, and renderer hints. |

### Recommended next local sequence

1. Fix the `venue` / `venue_default` contract before adding new markets UI.
2. Add safe coverage errors and shorter OpenBB health timeout.
3. Add the port-`8010` smoke receipt around `/data/sources` and coverage.
4. Only then expand the datasource dashboard into a provider health center.

## 15. Automation Review Update — 2026-05-07 America/Chicago

### Changes observed locally since the last analysis

| Area | Evidence | Roadmap response |
|---|---|---|
| Agent layer expansion | Seven agents now have `main.py` entrypoints: `gbm_predictor`, `regime_agent`, `sentiment_agent`, `sentiment_features`, `information_enricher`, `news_alpha_predictor`, `news_outcome_labeler`. Only `pairs` (TASK-033) remains a stub. | Promote `regime_agent` to implemented in BUILD_ORDER; add the four new agents as Phase X/D extensions. Keep `pairs` gated until cointegration infrastructure is production-ready. |
| Dashboard surface growth | New pages: `/predictions`, `/signal-cockpit-demo`, `/reconciliation`, `/portfolio-builder`, `/news-lab`, `/news-impact-lab`, `/optimizer`. | These are operator workflow surfaces; the next proof should be route-smoke receipts covering each, not more scaffold. |
| ADR resolution | ADR-0006 (feature store) resolved as custom Redis+Parquet; ADR-0009 (datasource routing) resolved as registry in `data.py`. | Promote both from "open" to "accepted" in `docs/DECISIONS.md`. |
| ML lifecycle completeness | Train → walk-forward → promote → hot-reload → shadow → predict → log is now end-to-end. 190 API tests, 93 agent tests, dashboard typecheck clean. | Treat the ML vertical as shipped; next work is proving it against live services via paper-spine replay. |
| Open questions | `venue`/`venue_default` contract drift, coverage error safety, OpenBB health timeout, and no e2e replay receipt remain unresolved. | These are the P0 blockers before any Phase X+ or live-capital work. |

### Roadmap refinement

| Priority | Work | Actionable exit criteria |
|---|---|---|
| P0 | Paper-spine replay receipt | One deterministic fixture flows data → feature → prediction → decision → risk → order → fill → portfolio with a reconstructable audit trail. |
| P0 | Port-8010 route smoke receipt | One command probes `/health`, `/data/sources`, `/data/coverage`, symbol search, OpenBB health, strategy configs, orders, models, predictions, and regime against `http://127.0.0.1:8010`. |
| P0 | `venue`/`venue_default` contract fix | `/data/universe` and dashboard types agree on field naming with tests proving the chosen shape. |
| P1 | New-agent BUILD_ORDER entries | Add `regime_agent` (032) as `[x]`, add `sentiment_agent`, `sentiment_features`, `information_enricher`, `news_alpha_predictor`, `news_outcome_labeler` as Phase X/D tasks with dependencies. |
| P1 | Safe coverage error envelope | `/data/coverage` returns stable error codes with correlation IDs; raw exception text is server-side only. |
| P1 | Research tool governance receipts | Every Exa/OpenBB call emits caller, route/tool, latency, rate-limit state, input hash, output hash. |

### Recommended next local sequence

1. Fix the `venue` / `venue_default` contract before adding new markets UI.
2. Build the paper-spine replay fixture (the single highest-value proof artifact).
3. Add the port-8010 smoke receipt.
4. Promote ADR-0006 and ADR-0009 to accepted.
5. Update BUILD_ORDER.md with new agent entries.
6. Only then expand autonomous research/agent behavior or live-brokerage assumptions.

## 16. Automation Review Update — 2026-05-08 America/Chicago

### Changes observed locally since the last analysis

| Area | Evidence | Roadmap response |
|---|---|---|
| Route-smoke receipts | `scripts/route_smoke.py` exists and `reports/route-smoke/route-smoke-20260506-211742.json` records 8/9 probes passing against `http://127.0.0.1:8010`; `/data/coverage` timed out after about 5s. | Treat route smoke as landed, but not green. The next fix is coverage latency/error shaping, not another broad API surface. |
| Earlier green route receipt | `reports/route-smoke/route-smoke-20260505-151250.json` records 9/9 probes passing, with `/data/coverage` returning an expected 503 instead of timing out. | Preserve the receipt history because it shows regression shape: coverage moved from bounded degraded response to timeout. |
| OpenBB live proof | `reports/openbb-live/openbb-live-20260505-151250.json` records OpenBB health passing but quote and generic dispatcher probes returning 503. | Separate OpenBB API reachability from provider-readiness; quote/fundamental provider failures should be visible and bounded. |
| Venue contract drift | Current code adds a backward-compatible `venue` alias from `venue_default`, and dashboard types now document `venue_default` as preferred. | Treat the original drift as partially resolved; keep tests around both fields until old dashboard callers are removed. |
| Agent and UI expansion | The tree now contains new agent packages, model data, dashboard route folders, and operator docs beyond the May 2 baseline. | Shift roadmap emphasis from scaffolding to proof receipts: route inventory, paper-spine replay, and agent promotion evidence. |

### Verification reviewed this run

| Command or artifact | Result | Boundary |
|---|---|---|
| Latest route-smoke receipt | 8/9 passed; `/data/coverage` failed with `ReadTimeout`. | Existing receipt only; the API server was not restarted or reprobed in this automation pass. |
| Latest OpenBB live proof | 1/3 passed; health passed, quote and dispatcher returned 503. | Live dependency readiness remains degraded or unavailable; no new live proof was run. |
| Local inspection and docs refresh | Completed. | No broad `scripts/preflight.ps1`, Docker, dashboard build, browser check, or paper-spine replay was run. |

### Roadmap refinement

| Priority | Work | Actionable exit criteria |
|---|---|---|
| P0 | Coverage timeout fix | `/data/coverage` returns 200 or expected 503 within the smoke timeout with stable public error codes and server-side correlation logs. |
| P0 | Latest route-smoke green receipt | A new `reports/route-smoke/*.json` records all probes passing or intentionally degraded without timeouts. |
| P0 | Paper-spine replay receipt | One deterministic fixture links data, feature, prediction, decision, risk, order, fill, and portfolio state with reconstructable IDs. |
| P1 | OpenBB readiness split | OpenBB health, provider availability, quote readiness, and dispatcher allowlist failures are reported as separate operator states. |
| P1 | Dashboard route inventory | Every dashboard route added since May 2 has a smoke entry for load/redirect/API-contract status. |
| P1 | Agent promotion ledger | Each implemented agent has tests, calibration/data-window notes, side-effect policy, and promotion status in docs or a receipt file. |

### Recommended next local sequence

1. Fix or bound `/data/coverage` so the latest route smoke can pass without timeouts.
2. Rerun `uv run python scripts/route_smoke.py --base-url http://127.0.0.1:8010` and record the new receipt.
3. Split OpenBB health into platform-up versus provider-call-ready states.
4. Build the paper-spine replay receipt before adding more autonomous agents or live-brokerage assumptions.
5. Add a dashboard route inventory smoke for the newly added operator pages.

## 17. Automation Review Update — 2026-05-09 America/Chicago

### Changes observed locally since the last analysis

| Area | Evidence | Roadmap response |
|---|---|---|
| Paper spine replay | `reports/paper-spine/latest.json` records a passed deterministic replay generated at `2026-05-08T19:20:11Z`. It proves data, feature, model signal, decision, approved risk, rejected low-limit risk, order, fill, portfolio update, and audit-trail stages. | Treat the paper-spine proof as the strongest current integration receipt, but keep it labeled deterministic/fakeredis-only until Redis, Timescale, API, and dashboard routes are included. |
| Route smoke | The latest reviewed port-8010 smoke receipt still passes 8/9 probes and times out on `/data/coverage` after about 5 seconds. | Keep `/data/coverage` latency as the top API blocker before claiming route-smoke health. |
| OpenBB live proof | OpenBB health returns a structured unavailable response, while quote and dispatcher probes still return 503 when the OpenBB backend/package is missing. | Preserve the readiness split: API reachability, provider availability, and route policy should stay separate in docs and UI. |
| Dashboard/API scope | The local tree now includes many untracked dashboard route folders, model/news/portfolio pages, and API route expansions. | Add route inventory smoke before adding more pages; otherwise the UI surface will outgrow the proof harness. |
| Worktree size | `git diff --stat` shows about 9.9k inserted lines across 94 tracked files plus many untracked app, report, model, and service paths. | Plan the next commit/review in slices: receipts and docs, API contracts, dashboard pages, agents/models, and generated data artifacts should not be bundled blindly. |

### Verification reviewed this run

| Artifact | Result | Boundary |
|---|---|---|
| `reports/paper-spine/latest.json` | Passed with 11 assertions true and no live broker credentials required. | Uses fakeredis and a deterministic AAPL fixture; not a live-service or dashboard proof. |
| `reports/route-smoke/route-smoke-20260506-211742.json` | 8/9 probes passed; `/data/coverage` failed with `ReadTimeout`. | Existing API run only; not rerun during this automation pass. |
| `reports/openbb-live/openbb-live-20260505-151250.json` | Health probe passed as structured unavailable; quote and generic dispatcher returned 503. | OpenBB backend/package not available for provider-call proof. |

### Roadmap refinement

| Priority | Work | Actionable exit criteria |
|---|---|---|
| P0 | Bound `/data/coverage` latency | `/data/coverage` returns 200 or expected 503 inside the smoke timeout, with timing spans for universe read, coverage read, provider health, and serialization. |
| P0 | Promote paper-spine receipt to service-backed replay | A replay receipt links real Redis stream IDs, Timescale rows, API correlation IDs, risk results, order/fill records, and portfolio persistence. |
| P1 | Route inventory receipt | A single command probes dashboard routes including `/predictions`, `/reconciliation`, `/portfolio-builder`, `/news-lab`, `/news-impact-lab`, `/optimizer`, and `/signal-cockpit-demo`. |
| P1 | OpenBB readiness matrix | The research page and proof receipt show API process status, package availability, provider availability, allowed route, status code, and operator action. |
| P1 | Agent promotion dossier | Each new agent has tests, data window, calibration status, side-effect class, and explicit no-live-order boundary before strategy-host use. |

### Recommended next local sequence

1. Fix or cap `/data/coverage`, then rerun `uv run python scripts/route_smoke.py --base-url http://127.0.0.1:8010`.
2. Extend `scripts/paper_spine_replay.py` from fakeredis-only proof to a service-backed receipt when Redis/Timescale are available.
3. Add the dashboard route inventory smoke before expanding the new UI pages.
4. Split the large worktree into reviewable slices before any commit or PR preparation.

## 18. Automation Review Update - 2026-06-23 America/Chicago

This pass reviewed local files only and did not use GitHub. The branch is now
`codex/portfolio-optimizer-core` at `751d212`, with a large local commit stack
since the older `9c1aba1` automation baseline. The new committed work is
centered on Quant Foundry rather than the earlier mock terminal and
news-impact-only slices.

### Changes observed locally since the last analysis

| Area | Evidence | Roadmap response |
|---|---|---|
| Quant Foundry service vertical | Recent commits add `services/quant_foundry` modules for schemas, IDs/signatures, outbox/inbox, callbacks, RunPod training/inference clients, S3 artifacts, feature lake, feature snapshots, baseline families, settlement, shadow ledger/settlement, tournament, promotion, paper bridge, MoE router, conformal gate, drift sentinel, causal graph, and gateway budget controls. | Treat Quant Foundry as a major implemented vertical that needs a release-readiness receipt and review slicing, not as a speculative future idea. |
| Dashboard route expansion | New committed pages exist under `/quant-foundry`, `/quant-foundry/jobs`, `/quant-foundry/models`, `/quant-foundry/shadow`, `/quant-foundry/tournament`, and `/quant-foundry/promotion`, plus API client/type additions. | Add a Quant Foundry route smoke atlas before more dashboard surfaces are added. |
| Cloud/runtime planning | `docs/AWS_PRODUCTION_CONTROL_PLANE.md`, `docs/MODULE_RUNTIME_PLAN.md`, `docs/ON_DEMAND_MODULES.md`, `docs/RAILWAY_STAGING_GUIDE.md`, RunPod container folders, and gateway budget guard code are present. | Separate code-level dispatch/budget proof from live cloud acceptance; use dry-run drills before any GPU spend. |
| Live readiness review | `docs/LIMITED_LIVE_READINESS_REVIEW.md` records that the current live path is not ready and lists blockers. | Keep live-capital and production claims blocked until the limited-readiness blockers have dated receipts. |
| Provider evidence hardening | Provider evidence redaction and freshness receipt code/tests landed in `libs/fincept-db` and API/provider tests. | Promote redaction/freshness into the standard release receipt instead of leaving it as a one-off task. |
| Shadow receipt script drift | `node scripts/run-shadow-news-impact-tests.cjs` still passes directly, but `apps/dashboard/package.json` no longer exposes `test:shadow-news-impact`; only `test:source-health` and `test:strategy-readiness` are currently listed for this proof family. | Restore the npm alias or update the receipt runner so the canonical command is stable before future automations rely on it. |
| Current dirty tree | The tracked dirty diff is only `uv.lock`, but many untracked local artifacts remain under dashboard docs/routes, tool folders, reports, research, docs, and agent areas. | Before staging anything, classify `uv.lock` as required or local churn and keep untracked local artifacts out of the Quant Foundry review slice unless intentionally promoted. |

### Roadmap refinement

| Priority | Work | Actionable exit criteria |
|---|---|---|
| P0 | Quant Foundry release receipt | One artifact records focused service tests, dashboard type check or route smoke, provider redaction/freshness proof, budget-gateway proof, branch, commit, skipped live dependencies, and open blockers. |
| P0 | Promotion safety ledger | A model state machine combines settlement, tournament score, conformal interval, drift status, retirement flags, and paper-only bridge state into `shadow`, `paper-only`, `promotable`, `blocked`, or `retired`. |
| P0 | Gateway budget drill | RunPod dispatch is proven fail-closed for kill switch, exhausted budget, duplicate callback, and bad signature cases without spending GPU dollars. |
| P1 | Quant Foundry route smoke | A single command probes every `/quant-foundry/*` page for load status, latency, mode, and missing dependency message. |
| P1 | Lockfile and artifact hygiene | The current `uv.lock` delta is explained, and untracked local tool/report artifacts stay excluded from product review slices unless intentionally promoted. |
| P1 | Shadow helper script contract | `test:shadow-news-impact` is restored in `apps/dashboard/package.json` or the receipt docs name `node scripts/run-shadow-news-impact-tests.cjs` as the canonical command. |
| P1 | Existing shadow/news-impact backlog | The older `/news-impact/signals` receipt, malformed-row accounting, source-health/readiness receipt, mock route atlas, and `.devin` path check stay visible as separate slices. |

### Recommended next local sequence

1. Classify the `uv.lock` delta and exclude local/generated tool folders from
   the Quant Foundry review slice.
2. Build a Quant Foundry release-readiness receipt around focused service tests,
   dashboard checks, provider redaction/freshness, and live blockers.
3. Add the promotion safety ledger before any production or live-capital claim.
4. Dry-run the RunPod gateway budget and callback safety path.
5. Add route smoke for all `/quant-foundry/*` pages.
6. Resume the prior shadow news-impact receipt and mock-route atlas queue.

## 19. Automation Review Update - 2026-06-26 America/Chicago

This pass reviewed local files only and did not use GitHub. The branch is
`codex/portfolio-optimizer-core` at `d737124`, ahead of the 2026-06-23
automation baseline `751d212`. The local tree still has untracked artifacts, but
the clean commit history now contains a major remediation and reliability stack,
not just speculative docs.

### Changes observed locally since the last analysis

| Area | Evidence | Roadmap response |
|---|---|---|
| Runtime hardening and service reliability | New local history includes `fincept-bus` DLQ/backoff/batch ACK work, Redis connection pooling, state persistence for kill switch/outstanding orders/target state, service stats heartbeats, entrypoint smoke tests, and callback ingestion extraction. | Treat reliability receipts as a first-class release gate alongside ML model receipts. |
| RunPod and callback boundary | Commits since `751d212` pin RunPod container dependencies, validate handlers, consolidate hardcoded IDs, extract `GatewayCallbackMixin`, fix DLQ fields, and cap retry/backoff test behavior. | Add explicit untrusted-container and signed-callback tests before more GPU automation. |
| Quant Foundry architecture docs | New untracked docs describe RunPod training architecture, dataset/data structure, the closed evidence loop, and seven model-defense layers. | Promote those docs into executable receipts: evidence-loop stage status and seven-layer defense receipt. |
| Dashboard UI audit | `UI_AUDIT_2026-06-26.md` identifies undefined degraded `amber` tokens, token/spec conflict, radius drift, dead canonical components, reduced-motion gaps, primary/accent mismatch, and nav divergence. | Fix token/nav/a11y defects before expanding dashboard pages again. |
| Swarm implementation analysis | `spec/SWARM_IMPLEMENTATION_ANALYSIS.md` explains the spec-driven paste loop and six-builder Quant Foundry swarm with file-disjoint tracks. | Convert swarm history into a review-slice ledger so the large local work can be reviewed safely. |
| Worktree hygiene | Tracked dirty state remains `uv.lock`; untracked docs and local outputs include `.agents/`, new architecture/audit docs, `e2e_output.txt`, and `spec/SWARM_IMPLEMENTATION_ANALYSIS.md`. | Classify docs worth promoting separately from local output files; keep `uv.lock` tied to dependency evidence or revert plan. |

### Roadmap refinement

| Priority | Work | Actionable exit criteria |
|---|---|---|
| P0 | Evidence-loop receipt | `reports/quant-foundry/evidence-loop-<date>.md` records dataset manifest, dossier, shadow prediction, settlement, sentinel, tournament, promotion, branch, commit, and missing blockers for one model. |
| P0 | RunPod trust-boundary tests | Training and inference handler tests prove no broker secrets, Redis stream writers, unsigned callbacks, or direct execution paths are available inside GPU containers. |
| P0 | Callback reliability receipt | A local fixture proves invalid signature, duplicate callback, schema mismatch, handler exception, DLQ accounting, and retry backoff all produce distinct durable records. |
| P1 | Dashboard token/nav remediation | `amber` degraded state is defined or remapped, primary/focus intent matches the chosen design spec, reduced-motion is handled, and nav route sets share one registry. |
| P1 | Swarm review ledger | A generated Markdown report maps builder tracks/commits to file groups, tests, receipts, and unresolved blockers for review slicing. |
| P1 | Dependency/artifact classification | `uv.lock`, new untracked docs, `.agents/`, and `e2e_output.txt` are classified as product docs, required dependency lock, generated output, or local-only artifact. |

### Recommended next local sequence

1. Promote the new architecture/audit docs that are intended to be durable, and
   keep local outputs such as `e2e_output.txt` out of product review slices.
2. Build the Quant Foundry evidence-loop receipt from the currently documented
   RunPod/data/training/settlement/promotion stages.
3. Add RunPod trust-boundary and callback reliability fixtures before increasing
   autonomous dispatch or callback complexity.
4. Fix the dashboard degraded-token/nav drift from `UI_AUDIT_2026-06-26.md`.
5. Generate a swarm review ledger so the large local commit series can be
   reviewed in service, dashboard, infra, docs, and generated-artifact slices.
