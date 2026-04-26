# Repo Layout — Every File, One Purpose

Rule: if a file's purpose isn't described here, it should not exist in the repo. PR that adds a file must update this doc first.

```
fincept-terminal/
├── README.md                         # user-facing intro
├── IMPLEMENTATION.md                 # how to use the spec/ directory
├── Makefile                          # `make dev`, `make test`, `make lint`, `make build`
├── pyproject.toml                    # uv workspace root
├── pnpm-workspace.yaml               # JS workspace root
├── .env.example                      # required env vars with safe defaults
├── .pre-commit-config.yaml           # ruff + mypy + prettier + eslint
├── docker-compose.yml                # postgres + timescale + redis + minio for local dev
│
├── docs/                             # planning docs (roadmap, risks, ADRs, blueprint)
│   ├── BLUEPRINT.md
│   ├── ROADMAP.md
│   ├── TASKS.md
│   ├── DECISIONS.md
│   └── RISKS.md
│
├── spec/                             # implementation spec (this directory)
│   ├── ARCHITECTURE.md
│   ├── LAYOUT.md                     # this file
│   ├── CONTRACTS.md
│   ├── BUILD_ORDER.md
│   ├── PROMPTS.md
│   └── tasks/
│       └── TASK-*.md                 # atomic units
│
├── libs/                             # shared Python libs (no network I/O)
│   ├── fincept-core/                 # canonical schemas, config, logging, tracing
│   │   ├── pyproject.toml
│   │   ├── src/fincept_core/
│   │   │   ├── __init__.py
│   │   │   ├── schemas.py            # pydantic v2 models — EXACTLY per CONTRACTS.md
│   │   │   ├── events.py             # Redis Stream message envelopes
│   │   │   ├── config.py             # Settings(BaseSettings), env-driven
│   │   │   ├── logging.py            # structured JSON logging setup
│   │   │   ├── tracing.py            # OpenTelemetry setup
│   │   │   ├── clock.py              # event-time vs wall-time utilities
│   │   │   ├── ids.py                # ULID generators, idempotency keys
│   │   │   ├── leadership.py         # Redis-based leader election
│   │   │   └── errors.py             # exception hierarchy
│   │   └── tests/
│   │
│   ├── fincept-bus/                  # Redis Streams client wrappers
│   │   ├── src/fincept_bus/
│   │   │   ├── __init__.py
│   │   │   ├── producer.py           # typed publish with backpressure
│   │   │   ├── consumer.py           # consumer group reader with ack/retry
│   │   │   └── streams.py            # stream name constants + retention config
│   │   └── tests/
│   │
│   ├── fincept-db/                   # Timescale + Postgres access
│   │   ├── src/fincept_db/
│   │   │   ├── __init__.py
│   │   │   ├── engine.py             # async SQLAlchemy engine factory
│   │   │   ├── models.py             # ORM models
│   │   │   ├── migrations/           # alembic
│   │   │   ├── ticks.py              # writes/reads for trades, book_deltas
│   │   │   ├── bars.py               # writes/reads for bars_1m, 1h, 1d
│   │   │   └── audit.py              # append-only audit log writer
│   │   └── tests/
│   │
│   ├── fincept-tools/                # MCP-style tool protocol for agents
│   │   ├── src/fincept_tools/
│   │   │   ├── __init__.py
│   │   │   ├── protocol.py           # Tool, ToolResult abstractions
│   │   │   ├── registry.py           # global tool registry
│   │   │   ├── data.py               # tools: get_bars, get_position, get_quote
│   │   │   ├── analytics.py          # tools: compute_vwap, compute_vol
│   │   │   └── exec.py               # tools: submit_order, cancel_order (paper only)
│   │   └── tests/
│   │
│   └── fincept-sdk/                  # public Python SDK for notebook/research users
│       ├── src/fincept_sdk/
│       │   ├── __init__.py
│       │   ├── data.py               # get_bars, stream
│       │   ├── strategy.py           # Strategy base class, backtest runner
│       │   └── universe.py           # load_universe
│       └── tests/
│
├── services/                         # deployable Python services
│   ├── ingestor/                     # market data ingestion
│   │   ├── pyproject.toml
│   │   ├── src/ingestor/
│   │   │   ├── __init__.py
│   │   │   ├── main.py               # entrypoint; spawns adapters
│   │   │   ├── base.py               # VenueAdapter ABC
│   │   │   ├── binance.py            # Binance spot WS adapter
│   │   │   ├── coinbase.py           # Coinbase Advanced Trade WS adapter
│   │   │   ├── kraken.py             # Kraken WS adapter
│   │   │   ├── eod_equity.py         # daily yfinance/polygon loader
│   │   │   ├── normalizer.py         # venue-specific → canonical schema
│   │   │   ├── writer.py             # Redis Stream producer + Timescale batch writer
│   │   │   └── quality.py            # gap detection, cross-spread alarm
│   │   └── tests/
│   │
│   ├── features/                     # feature engineering + store
│   │   ├── src/features/
│   │   │   ├── __init__.py
│   │   │   ├── main.py               # worker entrypoint
│   │   │   ├── online.py             # real-time feature computation
│   │   │   ├── offline.py            # batch feature backfill
│   │   │   ├── store.py              # online store (Redis) + offline store (Timescale)
│   │   │   ├── pit.py                # point-in-time-correct joins (no leakage)
│   │   │   └── transforms/
│   │   │       ├── __init__.py
│   │   │       ├── price.py          # returns, log-returns, momentum
│   │   │       ├── volatility.py     # realized vol, Parkinson, Garman-Klass
│   │   │       ├── microstructure.py # imbalance, spread, VPIN
│   │   │       └── cross.py          # beta, correlation, z-scores across symbols
│   │   └── tests/
│   │
│   ├── agents/                       # all intelligence lives here
│   │   ├── src/agents/
│   │   │   ├── __init__.py
│   │   │   ├── base.py               # Agent ABC, lifecycle
│   │   │   ├── memory.py             # chromadb vector memory for LLM agents
│   │   │   ├── gbm_predictor/        # LightGBM directional predictor (baseline)
│   │   │   │   ├── main.py
│   │   │   │   ├── train.py
│   │   │   │   ├── infer.py
│   │   │   │   └── features.py       # adapter to feature store
│   │   │   ├── ts_foundation/        # time-series foundation model (cutting edge)
│   │   │   │   ├── main.py
│   │   │   │   ├── model.py          # wrapper around TimesFM / Lag-Llama / Moirai
│   │   │   │   └── zero_shot.py
│   │   │   ├── llm_sentiment/        # news / filings → sentiment + event tags
│   │   │   │   ├── main.py
│   │   │   │   ├── fetchers.py       # news APIs, SEC EDGAR
│   │   │   │   ├── extractor.py      # LLM structured extraction
│   │   │   │   └── entity.py         # entity resolution (ticker/company)
│   │   │   ├── event_miner/          # real-time event detection
│   │   │   │   ├── main.py
│   │   │   │   └── patterns.py
│   │   │   ├── regime/               # regime detection (HMM + ML)
│   │   │   │   ├── main.py
│   │   │   │   └── detector.py
│   │   │   ├── pairs/                # cointegration pairs strategy
│   │   │   │   ├── main.py
│   │   │   │   └── cointegration.py
│   │   │   ├── execution_rl/         # RL execution (PPO over child slicing)
│   │   │   │   ├── main.py
│   │   │   │   ├── env.py
│   │   │   │   ├── policy.py
│   │   │   │   └── train.py
│   │   │   └── research/             # offline: automated HPO, alpha discovery
│   │   │       ├── main.py           # nightly scheduler
│   │   │       ├── hpo.py            # Optuna driver
│   │   │       └── discovery.py      # genetic programming alpha search
│   │   └── tests/
│   │
│   ├── orchestrator/                 # combines agents into decisions
│   │   ├── src/orchestrator/
│   │   │   ├── __init__.py
│   │   │   ├── main.py               # singleton entrypoint
│   │   │   ├── router.py             # fan-in from agents via sig.* streams
│   │   │   ├── consensus.py          # weighted voting, Bayesian fusion
│   │   │   ├── regime.py             # regime-adaptive agent weighting
│   │   │   ├── allocator.py          # capital allocation across strategies
│   │   │   └── decisions.py          # Decision event emitter
│   │   └── tests/
│   │
│   ├── risk/                         # pre-trade + real-time risk
│   │   ├── src/risk/
│   │   │   ├── __init__.py
│   │   │   ├── main.py
│   │   │   ├── gate.py               # pre-trade checks
│   │   │   ├── limits.py             # configurable limits per scope
│   │   │   ├── kelly.py              # Kelly-optimal sizing
│   │   │   ├── var.py                # real-time VaR
│   │   │   ├── concentration.py
│   │   │   └── kill_switch.py        # emergency halt
│   │   └── tests/
│   │
│   ├── oms/                          # paper OMS (MVP), live adapter later
│   │   ├── src/oms/
│   │   │   ├── __init__.py
│   │   │   ├── main.py
│   │   │   ├── paper.py              # fill simulator using live prices
│   │   │   ├── state.py              # order state machine
│   │   │   ├── audit.py              # event-sourced order log
│   │   │   └── venue/                # live adapters (stubs until phase 5)
│   │   │       ├── __init__.py
│   │   │       ├── base.py
│   │   │       └── binance.py
│   │   └── tests/
│   │
│   ├── portfolio/                    # positions, P&L, attribution
│   │   ├── src/portfolio/
│   │   │   ├── __init__.py
│   │   │   ├── main.py
│   │   │   ├── positions.py
│   │   │   ├── pnl.py                # realized + unrealized mark-to-market
│   │   │   └── attribution.py        # by strategy, symbol, factor
│   │   └── tests/
│   │
│   ├── api/                          # FastAPI HTTP + WebSocket read model
│   │   ├── src/api/
│   │   │   ├── __init__.py
│   │   │   ├── main.py               # FastAPI app
│   │   │   ├── auth.py               # JWT / OAuth
│   │   │   ├── routes/
│   │   │   │   ├── data.py
│   │   │   │   ├── strategies.py
│   │   │   │   ├── positions.py
│   │   │   │   ├── orders.py
│   │   │   │   └── control.py        # start/stop strategies, kill switch
│   │   │   └── ws.py                 # WebSocket streaming endpoint
│   │   └── tests/
│   │
│   ├── backtester/                   # event-driven backtest engine
│   │   ├── src/backtester/
│   │   │   ├── __init__.py
│   │   │   ├── engine.py             # deterministic event loop
│   │   │   ├── broker.py             # fill simulator with costs
│   │   │   ├── costs.py              # spread + slippage + fees + borrow
│   │   │   ├── datasource.py         # replay from Timescale
│   │   │   ├── report.py             # QuantStats integration + custom
│   │   │   └── walk_forward.py
│   │   └── tests/
│   │
│   └── jobs/                         # scheduled jobs (APScheduler)
│       ├── src/jobs/
│       │   ├── __init__.py
│       │   ├── main.py
│       │   ├── nightly_retrain.py
│       │   ├── daily_eod_load.py
│       │   ├── weekly_report.py
│       │   └── compaction.py         # Timescale compression + retention
│       └── tests/
│
├── apps/
│   └── dashboard/                    # Next.js 16 UI
│       ├── package.json
│       ├── next.config.ts
│       ├── src/
│       │   ├── app/
│       │   │   ├── layout.tsx
│       │   │   ├── page.tsx          # overview
│       │   │   ├── strategies/
│       │   │   ├── positions/
│       │   │   ├── orders/
│       │   │   ├── backtests/
│       │   │   └── research/         # embeds Jupyter via iframe
│       │   ├── components/
│       │   │   ├── chart/            # TradingView Lightweight Charts
│       │   │   ├── table/            # virtual-scrolled tables
│       │   │   ├── risk-panel/
│       │   │   └── command-palette/  # cmdk — Bloomberg-style mnemonics
│       │   ├── lib/
│       │   │   ├── api.ts            # typed client for services/api
│       │   │   └── ws.ts             # WebSocket hook
│       │   └── styles/
│       └── tests/
│
├── notebooks/                        # research notebooks; not shipped
│   ├── 01-data-quality.ipynb
│   ├── 02-pairs-discovery.ipynb
│   └── ...
│
└── infra/
    ├── docker/
    │   ├── ingestor.Dockerfile
    │   ├── agents.Dockerfile
    │   └── api.Dockerfile
    ├── k8s/
    │   ├── namespace.yaml
    │   ├── ingestor.yaml
    │   ├── redis.yaml
    │   └── ...
    └── grafana/
        └── dashboards/               # pre-built panels
            ├── ingestion.json
            ├── trading.json
            └── risk.json
```

## Counting the work

- **Python services:** 11 (ingestor, features, agents [8 sub-agents], orchestrator, risk, oms, portfolio, api, backtester, jobs, plus libs: core, bus, db, tools, sdk)
- **Each service:** ~5–15 files, 1 pytest directory
- **Total Python files:** ~180–220 at MVP
- **Total TS files:** ~40–60 at MVP
- **Task specs needed:** ~30 to cover the critical path (one spec can implement multiple thin files)

See `spec/BUILD_ORDER.md` for the sequence.
