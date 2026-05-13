# Fincept Terminal — Ticket-Level Task Breakdown

> Scoped to the **MVP Track (Bet A)** from `ROADMAP.md`. Import into GitHub Projects / Linear / Jira.
>
> **Estimates:** Fibonacci story points. 1pt ≈ half a day, 2pt ≈ 1 day, 3pt ≈ 1.5 days, 5pt ≈ 3 days, 8pt ≈ 1 week, 13pt ≈ 2 weeks. Assumes one mid-senior engineer.
>
> **Priority:** P0 = blocker, P1 = must-have, P2 = should-have, P3 = nice-to-have.
>
> **Current-state note:** this file is now a historical ticket backlog, not the live source of truth. The codebase has advanced beyond several unchecked boxes below. Use `spec/BUILD_ORDER.md`, the focused `spec/tasks/TASK-*.md` files, `README.md` local progression snapshots, and `SYSTEM_OVERVIEW.md` for implementation status.

---

## Phase 0 — Foundation

### P0-01 · Monorepo bootstrap · 3pt · P0

Create repo layout:

```text
fincept-terminal/
  services/            # Python services (each its own pyproject.toml)
    ingestor/
    oms/
    api/
  libs/                # shared Python libs
    fincept-core/
    fincept-sdk/
  apps/
    dashboard/         # Next.js
  infra/
    docker-compose.yml # local dev stack
    terraform/         # deferred
  docs/
  Makefile
```

- [x] uv workspace for Python
- [x] pnpm workspace for JS
- [x] Root `Makefile` with development/test/lint targets
- [x] pre-commit hooks: ruff, mypy, prettier/eslint equivalents where configured

**Acceptance:** fresh clone → `make dev` → Postgres+Timescale+Redis running in Docker, all libs importable.

**Depends on:** —

---

### P0-02 · CI pipeline · 3pt · P0

- [ ] GitHub Actions workflow: lint → typecheck → unit tests → build
- [ ] Matrix over Python 3.12 and Node 22
- [ ] Cache uv + pnpm
- [ ] Fail PR on any step failure
- [ ] <5 min total runtime target

**Acceptance:** PR cannot merge with failing CI.

**Depends on:** P0-01.

---

### P0-03 · Secrets abstraction · 2pt · P0

- [x] `fincept-core.config.Settings` reads from env + optional Vault backend
- [x] `.env.example` checked in, `.env` gitignored
- [x] Docstring covers rotation procedure

**Acceptance:** no secret appears in `git log --all -p`.

**Depends on:** P0-01.

---

### P0-04 · Observability baseline · 5pt · P1

- [ ] OpenTelemetry SDK in `fincept-core`
- [ ] Every service emits traces + metrics + structured JSON logs
- [ ] Grafana Cloud OTLP endpoint wired
- [ ] One reference dashboard: request rate, p50/p95/p99 latency, error rate

**Acceptance:** a request through ingestor → DB shows up as one trace in Grafana.

**Depends on:** P0-01.

---

### P0-05 · ADR template + first 5 ADRs · 3pt · P0

- [ ] `docs/DECISIONS.md` index
- [ ] ADR-0001: Python as primary language
- [ ] ADR-0002: Timescale + Postgres as primary DB
- [ ] ADR-0003: Redis Streams as message bus (vs NATS)
- [ ] ADR-0004: Next.js as UI framework (vs Qt6)
- [ ] ADR-0005: Paper trading before live capital

**Acceptance:** all 5 signed off by tech lead + one other engineer.

**Depends on:** —

---

## Phase 1 — Data Spine

### P1-01 · Crypto WebSocket ingestor: Binance spot · 8pt · P0

- [ ] Async Python client (websockets lib)
- [ ] Subscribe trades + order book diff for N configured pairs
- [ ] Reconnect with exponential backoff + jitter
- [ ] Sequence gap detection → snapshot refetch
- [ ] Heartbeat/ping pong with stale-connection kill
- [ ] Emit normalized events to Redis Stream `md.binance.trades`, `md.binance.books`

**Acceptance:** 24h soak test, zero dropped messages, gap-recovery verified by kill-switch test.

**Depends on:** P0-01, P0-04.

---

### P1-02 · Coinbase Advanced Trade adapter · 5pt · P1

Same shape as P1-01, reusing base class. **Depends on:** P1-01.

---

### P1-03 · Kraken adapter · 5pt · P1

Same. **Depends on:** P1-01.

---

### P1-04 · Normalized event schema · 5pt · P0

- [ ] Protobuf or msgspec schemas in `fincept-core.schemas`
- [ ] Fields: `venue`, `symbol`, `ts_event` (ns), `ts_recv` (ns), `type`, `price`, `size`, `side`, `seq`
- [ ] Symbol normalization: `BTC-USD` canonical, adapters map to venue native
- [ ] Versioned (schema migration plan)

**Acceptance:** cross-venue consumer queries same schema regardless of source.

**Depends on:** P0-01.

---

### P1-05 · TimescaleDB schema + migrations · 5pt · P0

- [ ] Hypertables: `trades`, `book_deltas`, `bars_1m`, `bars_1h`, `bars_1d`
- [ ] Partition by `ts_event`, chunk interval 1 day
- [ ] Compression policy after 7 days (10x expected)
- [ ] Continuous aggregates for 1m/1h/1d bars
- [ ] Alembic migrations

**Acceptance:** ingest 1M trades, query 1m bars for any symbol in <200ms.

**Depends on:** P1-04.

---

### P1-06 · Ingestor → Timescale writer · 5pt · P0

- [ ] Consume Redis Streams, batch insert to Timescale (COPY)
- [ ] Backpressure: drop policy documented + metric
- [ ] Dedup on `(venue, symbol, seq)`

**Acceptance:** end-to-end WS → DB latency p99 <100ms at 5k msg/sec.

**Depends on:** P1-01, P1-05.

---

### P1-07 · EOD equity loader · 5pt · P1

- [ ] Daily job pulls `yfinance` or Polygon.io free tier
- [ ] Loads OHLCV for configured universe into `bars_1d`
- [ ] Idempotent re-runs
- [ ] Cron in Kubernetes CronJob or GitHub Actions

**Acceptance:** 500 tickers × 5 years loaded; job re-runs cleanly.

**Depends on:** P1-05.

---

### P1-08 · Data quality monitor · 5pt · P1

- [ ] Metrics: ingestion latency, message gap count, cross-spread detections, stale-feed alerts
- [ ] Grafana panel per venue
- [ ] PagerDuty/Slack alert on SLO breach

**Acceptance:** simulate feed outage, alert fires within 60s.

**Depends on:** P0-04, P1-06.

---

## Phase 2 — Research Environment

### P2-01 · Python SDK `fincept` · 8pt · P0

- [ ] `fincept.data.get_bars(symbol, start, end, freq)` → polars DataFrame
- [ ] `fincept.data.stream(symbols)` → async iterator of live ticks
- [ ] `fincept.universe.load(name)` → list of symbols
- [ ] Authenticated via config token; no raw DB access

**Acceptance:** notebook user queries 1 year of 1m bars in one line.

**Depends on:** P1-05.

---

### P2-02 · JupyterHub deployment · 5pt · P1

- [ ] Helm chart or docker-compose for single-node
- [ ] Shared kernel with `fincept` SDK preinstalled
- [ ] OAuth via company Google/GitHub

**Acceptance:** new quant signs in, opens notebook, pulls data in <5 min.

**Depends on:** P2-01.

---

### P2-03 · Event-driven backtester core · 13pt · P0

- [ ] `Event` base (BarEvent, TickEvent, OrderEvent, FillEvent)
- [ ] `Backtest.run(strategy, start, end)` → blotter + equity curve
- [ ] Deterministic: same seed → same fills
- [ ] Supports multi-asset portfolios

**Acceptance:** reference MA-crossover matches known-good numbers from QuantConnect within 1%.

**Depends on:** P2-01.

---

### P2-04 · Transaction cost model · 5pt · P1

- [ ] Spread cost (half-spread at fill)
- [ ] Slippage model (linear in notional / ADV)
- [ ] Fee schedule per venue
- [ ] Borrow cost for shorts (equity)

**Acceptance:** backtester with realistic costs shows Sharpe degradation consistent with published literature.

**Depends on:** P2-03.

---

### P2-05 · Strategy base class · 3pt · P0

- [ ] `class Strategy: on_bar(), on_tick(), on_fill(), on_timer()`
- [ ] Registry via entry points
- [ ] Param schema via pydantic for UI rendering later

**Acceptance:** two reference strategies inherit cleanly.

**Depends on:** P2-03.

---

### P2-06 · Reference strategies · 8pt · P2

- [ ] MA crossover
- [ ] Pairs (cointegration-selected)
- [ ] Mean reversion (Bollinger)
- [ ] Each with backtest notebook + README

**Acceptance:** all three have positive Sharpe on ≥2yr crypto backtest (for demo, not production).

**Depends on:** P2-05, P2-04.

---

### P2-07 · Performance report generator · 3pt · P2

- [ ] QuantStats HTML export
- [ ] Custom attribution (by symbol, by day-of-week)

**Acceptance:** one CLI command produces report.html.

**Depends on:** P2-03.

---

## Phase 3 — Paper Trading + Dashboard

### P3-01 · Paper OMS · 13pt · P0

- [ ] Receive orders via Redis Stream `oms.orders`
- [ ] Fill simulator uses live market data from P1 pipeline
- [ ] Order state machine: new → accepted → partial → filled/cancelled/rejected
- [ ] Event-sourced; full audit log in Postgres

**Acceptance:** submit 1000 orders, all fills reconcile to book state within 5bps.

**Depends on:** P1-06, P1-04.

---

### P3-02 · Strategy runner · 8pt · P0

- [ ] Each strategy runs in its own process (supervisor pattern)
- [ ] Hot reload of params via Redis pub/sub
- [ ] Graceful shutdown with position flatten option

**Acceptance:** kill -9 runner, restart, no duplicate orders, position reconciles.

**Depends on:** P2-05, P3-01.

---

### P3-03 · Position + P&L service · 8pt · P0

- [ ] Real-time mark-to-market
- [ ] Realized vs unrealized split
- [ ] Per-strategy and aggregate

**Acceptance:** P&L matches independent spreadsheet audit within 1 cent per position.

**Depends on:** P3-01.

---

### P3-04 · Pre-trade risk gate · 8pt · P0

- [ ] Notional limits per symbol/strategy/firm
- [ ] Concentration limit (% of NAV)
- [ ] Max daily loss kill switch
- [ ] <10ms check latency

**Acceptance:** chaos test with rogue strategy — trading suspends, position not exceeded.

**Depends on:** P3-03.

---

### P3-05 · Dashboard shell · 8pt · P1

- [x] Next.js 14 App Router + React 18 + Tailwind + Radix UI primitives
- [ ] Auth: NextAuth with Google/GitHub
- [ ] Layout: sidebar + tabbed panels

**Acceptance:** authenticated user sees empty dashboard; auth works on refresh.

**Depends on:** —

---

### P3-06 · Live positions + P&L panel · 8pt · P1

- [ ] WebSocket to `api` service
- [ ] Virtual-scrolled table, update at 10Hz max
- [ ] Color-coded P&L; mini sparkline per position

**Acceptance:** 100 positions updating smoothly at 60fps.

**Depends on:** P3-03, P3-05.

---

### P3-07 · Live chart panel · 5pt · P2

- [ ] TradingView Lightweight Charts
- [ ] Subscribe to live bars via WebSocket
- [ ] Overlay strategy fills

**Depends on:** P3-05, P3-03.

---

### P3-08 · Strategy control panel · 5pt · P1

- [ ] List strategies, status, per-strategy P&L
- [ ] Start/stop buttons (with confirmation)
- [ ] Param editor driven by pydantic schema

**Depends on:** P3-05, P3-02.

---

### P3-09 · Alerting · 5pt · P1

- [ ] Slack webhook
- [ ] Email via SES/SendGrid
- [ ] Alert types: risk breach, feed down, strategy error, large P&L move

**Depends on:** P0-04.

---

## Phase 4 — AI Agent Layer

### P4-01 · Agent base + message bus · 8pt · P0

- [ ] `Agent` base class with lifecycle hooks
- [ ] Message types: `Observation`, `Prediction`, `Action`, `Feedback`
- [ ] Redis Streams transport with consumer groups

**Depends on:** P3-02.

---

### P4-02 · Feature store · 13pt · P1

- [ ] Feast OR custom on Timescale
- [ ] Offline store: historical features for training
- [ ] Online store: <10ms feature lookup at inference
- [ ] Point-in-time-correct joins (no leakage)

**Depends on:** P1-05.

---

### P4-03 · Predictive agent: LightGBM · 8pt · P0

- [ ] Training pipeline: features → LightGBM → artifact
- [ ] Walk-forward cross-validation
- [ ] Inference agent consumes live features, emits `Prediction`

**Acceptance:** ≥52% directional accuracy on held-out test, significant at p<0.05.

**Depends on:** P4-01, P4-02.

---

### P4-04 · Model registry + MLflow · 5pt · P1

- [ ] MLflow tracking server (Postgres backend, S3 artifacts)
- [ ] Every training run logged with params + metrics + artifact
- [ ] Promote model by tag (`staging`, `production`)

**Depends on:** P0-01.

---

### P4-05 · Shadow deployment harness · 8pt · P1

- [ ] Candidate model runs in parallel, predictions logged not acted on
- [ ] Calibration + accuracy report auto-generated weekly

**Depends on:** P4-03, P4-04.

---

### P4-06 · Research agent: nightly HPO · 8pt · P2

- [ ] Optuna-driven sweep over model + feature combos
- [ ] Runs on research cluster during overnight window
- [ ] Top-K results published to a dashboard

**Depends on:** P4-03.

---

### P4-07 · Consensus layer · 5pt · P2

- [ ] Weighted voting across ≥2 models
- [ ] Weights updated weekly based on rolling accuracy
- [ ] Confidence threshold below which no trade fires

**Depends on:** P4-03.

---

## Phase 5 — Hardening (gate-guarded)

High-level only; expand to tickets once Phase 4 gate met.

- Chaos testing suite (toxiproxy + kill scripts) — 13pt
- Postgres physical replication + documented failover — 13pt
- Exchange key HSM (YubiHSM or cloud HSM) — 13pt
- mTLS mesh (cert-manager + service identities) — 8pt
- Audit log archival (7yr, WORM storage) — 5pt
- External pen test — procurement, not a ticket
- Live-capital readiness review with risk committee — procedural

---

## Backlog / Deferred (NOT for MVP)

These come from the blueprint but are **explicitly out of scope** for the first 12 months. Listed here so they aren't forgotten, not promised.

- FIX protocol + equity direct market access
- Level 2 equity order books (SIP, direct feeds)
- FPGA feed handler / order entry
- Kernel bypass networking (DPDK/Solarflare)
- Qt6 native desktop terminal
- Command mnemonic CLI (Bloomberg-style)
- Multi-monitor workspace templates
- Hierarchical meta-agent orchestration
- Reinforcement learning strategy optimization
- On-chain / DeFi execution
- News sentiment is no longer fully deferred: the NewsAPI + LLM sentiment agent exists but remains optional/key-gated and needs calibration before it should affect sizing materially.
