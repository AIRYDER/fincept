# Build Order — Sequenced Task Graph with Checkpoints

> Work top to bottom. Every task depends only on completed ones. Checkpoints are hard gates — do not proceed if a checkpoint fails.

## Phase F — Foundation

| # | Task | Depends on | File(s) | Status |
|---|---|---|---|---|
| 001 | Monorepo skeleton (uv, pnpm, Makefile, pre-commit, docker-compose) | — | root | [x] |
| 002 | `fincept-core`: schemas, events, config, logging, tracing, clock, ids, errors | 001 | `libs/fincept-core` | [x] |
| 003 | `fincept-bus`: Redis Streams producer, consumer, stream constants | 002 | `libs/fincept-bus` | [x] |
| 004 | `fincept-db`: async SQLAlchemy engine, ORM models, alembic migrations, ticks/bars access | 002 | `libs/fincept-db` | [x] |
| 005 | `fincept-tools`: tool protocol, registry, data tools, analytics tools, exec tools (paper) | 002 | `libs/fincept-tools` | [x] |
| 006 | CI pipeline (GitHub Actions): lint, typecheck, test, build matrix | 001 | `.github/workflows` | [x] |

**Checkpoint F:** `make dev` spins up the stack; `pytest libs/` is green; CI passes on a PR.

---

## Phase D — Data Spine

| # | Task | Depends on | File(s) |
|---|---|---|---|
| 010 | Ingestor base class + normalizer + writer (Redis + Timescale batch) | 002–004 | `services/ingestor/{base,normalizer,writer}.py` | [x] |
| 011 | Binance spot WS adapter | 010 | `services/ingestor/binance.py` | [x] |
| 012 | Coinbase Advanced Trade adapter | 010 | `services/ingestor/coinbase.py` | [x] |
| 013 | Kraken WS adapter | 010 | `services/ingestor/kraken.py` | [ ] |
| 014 | Quality monitor (gaps, cross-spread, staleness alerts) | 011 | `services/ingestor/quality.py` | [ ] |
| 015 | EOD equity loader (yfinance → bars_1d) | 004 | `services/ingestor/eod_equity.py` | [ ] |
| 016 | Features: online transforms (returns, vol, microstructure) | 002–004, 011 | `services/features/online.py`, `transforms/*` | [ ] |
| 017 | Features: online + offline store with PIT joins | 016 | `services/features/{store,pit}.py` | [ ] |

**Checkpoint D:** 24-hour soak test on 5 crypto pairs with zero dropped messages; feature store serves online reads in <10 ms at p99; offline backfill reproduces live features bit-exact.

---

## Phase B — Backtesting

Backtesting comes before live agents on purpose: we need the scoreboard before we build competitors.

| # | Task | Depends on | File(s) |
|---|---|---|---|
| 020 | Backtester engine (deterministic event loop, replay from Timescale) | 004, 017 | `services/backtester/{engine,datasource}.py` |
| 021 | Cost model (spread + slippage + fees + borrow) | 020 | `services/backtester/costs.py` |
| 022 | Broker simulator (fills, partial fills, cancellations) | 020, 021 | `services/backtester/broker.py` |
| 023 | Walk-forward runner + report (QuantStats + custom) | 020–022 | `services/backtester/{walk_forward,report}.py` |
| 024 | SDK Strategy base + StrategyContext + backtest runner | 020 | `libs/fincept-sdk/strategy.py` |

**Checkpoint B:** reference MA-crossover strategy produces known-good Sharpe on 2 yr of BTC 1m bars; walk-forward IS/OOS split respects PIT.

---

## Phase A — Agents (v1 baseline, non-LLM)

| # | Task | Depends on | File(s) |
|---|---|---|---|
| 030 | Agent base class + process template | 002–003 | `services/agents/base.py` |
| 031 | `gbm_predictor`: LightGBM trainer + online inference agent | 017, 030 | `services/agents/gbm_predictor/*` |
| 032 | `regime`: HMM-based regime detector | 017, 030 | `services/agents/regime/*` |
| 033 | `pairs`: cointegration pairs strategy agent | 017, 030 | `services/agents/pairs/*` |

**Checkpoint A1:** `gbm_predictor` ≥52% directional accuracy on held-out 3-month test set with p<0.05; regime labels align with manual inspection on ≥3 historical regime transitions.

---

## Phase O — Orchestrator + Risk + OMS

| # | Task | Depends on | File(s) |
|---|---|---|---|
| 040 | Orchestrator: fan-in router, regime-adaptive weighting, decisions emitter | 031–033 | `services/orchestrator/{router,regime,consensus,allocator,decisions}.py` |
| 041 | Risk gate: pre-trade checks + kill switch | 002 | `services/risk/{gate,limits,kill_switch}.py` |
| 042 | Kelly-optimal sizing (correlated-assets variant) | 041 | `services/risk/kelly.py` |
| 043 | Real-time VaR | 041 | `services/risk/var.py` |
| 044 | Paper OMS (fill simulator uses live mid + random latency) | 002–004, 011 | `services/oms/{main,paper,state,audit}.py` |
| 045 | Portfolio service (positions, P&L, attribution) | 044 | `services/portfolio/*` |

**Checkpoint O:** end-to-end paper trading — decision → risk → OMS → fill → position — works for one strategy with full audit trail reconstructable from `ord.*` streams.

---

## Phase U — UI + API

| # | Task | Depends on | File(s) |
|---|---|---|---|
| 050 | FastAPI app with auth, health, universe, bars, positions, orders, strategies routes | 004, 044 | `services/api/*` |
| 051 | WebSocket streaming endpoint (positions, fills, predictions) | 050 | `services/api/ws.py` |
| 052 | Next.js dashboard shell + auth + typed API client | 050 | `apps/dashboard/*` |
| 053 | Positions + P&L panel (WS-driven, 10 Hz) | 051, 052 | `apps/dashboard/src/app/positions/*` |
| 054 | Strategy control panel (start/stop/param) | 052 | `apps/dashboard/src/app/strategies/*` |
| 055 | Live chart (TradingView Lightweight Charts) + fill overlays | 052 | `apps/dashboard/src/components/chart/*` |
| 056 | Command palette (cmdk, Bloomberg-style mnemonics) | 052 | `apps/dashboard/src/components/command-palette/*` |
| 057 | Risk panel + kill switch button | 041, 052 | `apps/dashboard/src/components/risk-panel/*` |

**Checkpoint U:** operator can sign in, see live P&L update at 10 Hz, start/stop a strategy, and trigger kill switch in under 3 seconds.

---

## Phase X — Cutting Edge (the profitability bet)

These are the modules that move the system from "solid" to "possibly profitable."

| # | Task | Depends on | File(s) |
|---|---|---|---|
| 060 | Agent memory (chromadb vector store; semantic retrieval) | 030 | `services/agents/memory.py` |
| 061 | `llm_sentiment`: news + SEC filings → structured extraction | 005, 030, 060 | `services/agents/llm_sentiment/*` |
| 062 | `event_miner`: real-time pattern detection (earnings, macro prints, shocks) | 061 | `services/agents/event_miner/*` |
| 063 | `ts_foundation`: zero-shot forecast via TimesFM / Lag-Llama / Moirai wrapper | 017, 030 | `services/agents/ts_foundation/*` |
| 064 | LLM orchestrator loop: decide → use tools → reflect → act | 005, 040 | `services/orchestrator/llm_loop.py` |
| 065 | `execution_rl`: PPO over child-order slicing, trained on replay | 022 | `services/agents/execution_rl/*` |
| 066 | `research`: nightly Optuna HPO + genetic alpha discovery | 023 | `services/agents/research/*` |

**Checkpoint X:** shadow deployment over 4 weeks — ensemble of (gbm + ts_foundation + llm_sentiment) produces Sharpe ≥ baseline + 0.5 after costs on paper, with statistical significance p<0.05.

---

## Phase H — Hardening (gate to live capital)

Do not enter this phase without risk-committee approval.

| # | Task | Depends on | File(s) |
|---|---|---|---|
| 070 | Chaos suite (toxiproxy, kill scripts, failover drills) | all prior | `tests/chaos/*` |
| 071 | Postgres physical replication + documented failover | 004 | `infra/k8s/postgres.yaml` |
| 072 | Exchange API keys in HSM; withdrawal scope disabled | 044 | `infra/secrets/*` |
| 073 | mTLS between all services via service mesh | all | `infra/k8s/istio/*` |
| 074 | Audit log archival (7 yr, WORM to object storage) | 044 | `services/jobs/archive.py` |
| 075 | Live venue adapter (Binance) with staged limits | 044 | `services/oms/venue/binance.py` |
| 076 | Gradual rollout harness (simulation → paper → shadow → limited → full) | all | `services/oms/rollout.py` |

**Checkpoint H:** SOC-2-equivalent internal audit passes; DR drill completes within RTO; first $1k live capital allocation monitored 24×7 for 7 days without incident.

---

## Phase X+ — Profitability Layer (Tier 1 alpha additions)

> See `spec/EDGE_ROADMAP.md` for the strategic thesis behind every task in this phase.

These are the highest-leverage additions per engineer-week beyond Phase X. Each fits cleanly into the existing contracts (`spec/CONTRACTS.md`). Phase X+ may begin once **Phase X checkpoint passes** and `Phase H` is at least underway in parallel.

| # | Task | Depends on | File(s) |
|---|---|---|---|
| 080 | Options-flow agent (unusual options activity → `Prediction`) | 030, 060 | `services/agents/options_flow/*` |
| 081 | Earnings-call transcript LLM agent | 030, 060, 061 | `services/agents/earnings_calls/*` |
| 082 | Insider Form 4 + short-interest agents (SEC + FINRA, free) | 015, 030 | `services/agents/insider_short/*` |
| 083 | Cross-sectional ranking layer in orchestrator (universe-wide rank → portfolio) | 040 | `services/orchestrator/cross_section.py` |
| 084 | Portfolio-level vol targeting (above Kelly) | 042 | `services/risk/vol_target.py` |
| 085 | Strategy decay monitor + capacity curves | 023, 045 | `services/jobs/strategy_decay.py`, `services/risk/capacity.py` |
| 086 | Multi-agent LLM debate (bull / bear / judge) replacing single-shot in `llm_loop` | 064 | `services/orchestrator/llm_debate.py` |
| 087 | Sector-rotation overlay (macro-conditioned sector tilts) | 015, 030 | `services/agents/sector_rotation/*` |
| 088 | Correlation-breakdown alerts | 045 | `services/risk/corr_monitor.py` |
| 089 | Liquidity stress test (daily simulated forced exit) | 044, 045 | `services/risk/liquidity_stress.py` |

**Checkpoint X+:** 8-week shadow deployment of (Phase X agents + Phase X+ additions). Required: Sharpe ≥ baseline + 0.7, max drawdown ≤ benchmark, realized vol ≤ portfolio vol target ± 20%, p < 0.05 via block bootstrap (block = 1 day). LLM cost per dollar of attributed alpha ≤ 30%.

---

## Phase Y — Differentiation (Tier 2 alpha additions)

Real differentiation from generic retail platforms. Only after Phase X+ checkpoint passes.

| # | Task | Depends on | File(s) |
|---|---|---|---|
| 090 | On-chain analytics agent (whale wallets, exchange flows, DeFi TVL, miner reserves) | 030, 060 | `services/agents/onchain/*` |
| 091 | Cross-asset macro regime model (inflation × growth × liquidity) | 032 | `services/agents/macro_regime/*` |
| 092 | Tail-risk hedging budget (systematic OTM SPX puts) | 042, 044 | `services/risk/tail_hedge.py`, `services/oms/options_paper.py` |
| 093 | Selective alt-data integration (one ROI-positive vendor first) | 016, 030 | `services/ingestor/altdata/*`, `services/agents/altdata/*` |
| 094 | Multi-arm bandit strategy allocator (Thompson sampling above orchestrator) | 040, 085 | `services/orchestrator/bandit_allocator.py` |
| 095 | Online learning / concept drift (`river` integration for GBM + features) | 016, 031 | `services/agents/gbm_predictor/online.py`, `services/features/online_drift.py` |
| 096 | L2 microstructure features (order-book imbalance, hidden-liquidity, flow toxicity) | 011, 016 | `services/features/microstructure.py` |

**Checkpoint Y:** 12-week shadow + paper. Outperforms benchmark across ≥3 distinct macro regimes within the period. Capacity stress test: simulated $10× current AUM does not degrade Sharpe by more than 20%.

---

## Phase Z — Research Frontier (Tier 3 alpha additions)

High variance, durable payoff. Funded by Phase X+ / Y alpha. Each module ships only after a published internal whitepaper with reproducible OOS evaluation.

| # | Task | Depends on | File(s) |
|---|---|---|---|
| 100 | Options strategies as alpha sources (vol-harvesting, dispersion, asymmetric event) | 092 | `services/agents/options_alpha/*`, `services/oms/venue/options.py` |
| 101 | Generative scenario simulation (GAN/diffusion adversarial scenarios) | 020, 023 | `services/agents/scenario_gan/*` |
| 102 | Graph neural networks (supply-chain + customer-supplier graphs) | 016, 030 | `services/agents/gnn/*` |
| 103 | Causal inference layer (DoWhy / EconML) | 023, 066 | `services/agents/causal/*` |
| 104 | Federated learning across tenants (only if multi-tenant deployment) | 002, 031 | `services/agents/fedlearn/*` |

**Checkpoint Z:** Each module has its own internal whitepaper + reproducible OOS evaluation, and individually meets the Phase X+ checkpoint criteria for its scoped contribution before deployment.

---

## Legend

- `[ ]` not started
- `[~]` in progress
- `[x]` complete + tests green + checkpoint for its phase passed

## How long should this take?

Assuming 1 senior engineer + AI coding assistant, working focused, with no org drag:

| Phase | Tasks | Calendar time | Confidence |
|---|---|---|---|
| F | 6 | 1–2 weeks | high |
| D | 8 | 3–4 weeks | high |
| B | 5 | 2–3 weeks | high |
| A | 4 | 2–3 weeks | medium |
| O | 6 | 3–4 weeks | medium |
| U | 8 | 2–3 weeks | high |
| X | 7 | 6–10 weeks | low (model quality is the wildcard) |
| H | 7 | 4–8 weeks | depends on regulatory scope |
| X+ | 10 | 8–14 weeks | low–medium (alpha is the wildcard) |
| Y | 7 | 10–16 weeks | low |
| Z | 5 | open-ended | research-grade |
| **Total (F→H)** | **51** | **23–37 weeks** | MVP-to-live |
| **Total (F→X+)** | **61** | **31–51 weeks** | profitability bet |
| **Total (F→Y)** | **68** | **41–67 weeks** | differentiation |

With 3–4 engineers you can parallelize within phases (esp. U, X, X+) and cut wall-time roughly in half. Phases X+, Y, Z extend the alpha layer; their thesis lives in `spec/EDGE_ROADMAP.md`.
