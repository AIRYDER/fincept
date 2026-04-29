# Fincept Terminal — Pragmatic Roadmap

> **Source:** Derived from `BLUEPRINT.md` with realistic scoping applied.
> **Audience:** Engineering leadership, product, founding team.
> **Last updated:** 2026-04-28

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

**Explicitly out of scope:** FIX, SIP, Level 2 equity, on-chain metrics, news sentiment. All deferred to Phase 4+.

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
