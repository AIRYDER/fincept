# Tasks Index

Atomic implementation specs. One task → one cohesive piece of work → one run of a coding model.

## Authored

| ID | Title | Phase | Lines |
|---|---|---|---|
| [TASK-001](TASK-001-monorepo-skeleton.md) | Monorepo skeleton (uv, pnpm, Makefile, docker-compose, CI) | F | ~250 |
| [TASK-002](TASK-002-fincept-core.md) | `fincept-core` lib (schemas, config, logging, tracing, ids, leadership) | F | ~340 |
| [TASK-003](TASK-003-fincept-bus.md) | `fincept-bus` Redis Streams wrapper | F | ~150 |
| [TASK-010](TASK-010-ingestor-binance.md) | Ingestor base + Binance spot WS adapter | D | ~250 |
| [TASK-020](TASK-020-backtester.md) | Backtester engine + cost model + broker | B | ~280 |
| [TASK-031](TASK-031-gbm-predictor.md) | Agent base + LightGBM predictor | A | ~200 |
| [TASK-040](TASK-040-orchestrator.md) | Orchestrator: fan-in, consensus, regime weighting, decisions | O | ~270 |
| [TASK-041](TASK-041-risk-gate.md) | Risk gate + Kelly sizing + kill switch | O | ~250 |
| [TASK-044](TASK-044-paper-oms.md) | Paper OMS: state machine + fill simulator + audit | O | ~250 |
| [TASK-050](TASK-050-api.md) | FastAPI HTTP + WebSocket read model | U | ~250 |
| [TASK-061](TASK-061-llm-sentiment.md) | LLM sentiment agent (cutting edge) | X | ~280 |

## To author (on demand) — generate using `spec/PROMPTS.md` template

| ID | Title | Phase | Notes |
|---|---|---|---|
| TASK-004 | `fincept-db` (SQLAlchemy + alembic + ticks/bars/audit) | F | mirrors TASK-003 structure |
| TASK-005 | `fincept-tools` MCP-style protocol + data/analytics/exec tools | F | |
| TASK-006 | CI workflow refinement | F | already partly in TASK-001 |
| TASK-011 | Coinbase adapter | D | mirror TASK-010 |
| TASK-012 | Kraken adapter | D | mirror TASK-010 |
| TASK-013 | EOD equity loader | D | yfinance/polygon |
| TASK-014 | Quality monitor + reconnect supervisor | D | wraps TASK-010 |
| TASK-016 | Feature transforms (price, volatility, microstructure, cross) | D | |
| TASK-017 | Feature store online + offline + PIT joins | D | |
| TASK-021 | Cost model refinement (borrow, exchange-specific) | B | |
| TASK-022 | Broker simulator (already in TASK-020); refine partial fills | B | |
| TASK-023 | Walk-forward runner + report | B | |
| TASK-024 | SDK Strategy + StrategyContext + backtest CLI | B | |
| TASK-032 | Regime detector (HMM) | A | |
| TASK-033 | Pairs/cointegration agent | A | |
| TASK-042 | Risk gate refinements (concentration, restricted list, self-trade) | O | |
| TASK-043 | Real-time VaR | O | |
| TASK-045 | Portfolio service (positions, P&L, attribution) | O | |
| TASK-052 | Next.js dashboard shell + auth + API client | U | |
| TASK-053 | Positions + P&L panel | U | |
| TASK-054 | Strategy control panel | U | |
| TASK-055 | Live chart + fill overlays | U | |
| TASK-056 | Command palette (cmdk) | U | |
| TASK-057 | Risk panel + kill switch UI | U | |
| TASK-060 | Vector memory (chromadb) | X | |
| TASK-062 | Event miner agent | X | |
| TASK-063 | Time-series foundation model agent | X | |
| TASK-064 | LLM orchestrator loop (tool use + reflection) | X | |
| TASK-065 | RL execution agent (PPO over child slicing) | X | |
| TASK-066 | Research agent (Optuna HPO + GP alpha discovery) | X | |
| TASK-070..076 | Hardening tasks (chaos, replication, HSM, mTLS, archival, live adapter, rollout) | H | |

## How to add a new task spec

1. Open `spec/PROMPTS.md` and copy the **template**.
2. Save it as `spec/tasks/TASK-XXX-<slug>.md` with a free three-digit ID following the phase numbering convention (`0xx=F/D/B`, `3xx=A`, `4xx=O`, `5xx=U`, `6xx=X`, `7xx=H`).
3. Fill in:
   - Phase + dependencies
   - Files to create (must match `spec/LAYOUT.md`)
   - Contracts (reference `CONTRACTS.md` sections, or inline new ones)
   - Tests
   - Out of scope
4. Add a row to this index.
5. Add the task to `spec/BUILD_ORDER.md` in dependency order.

## Convention reminders

- Every task is **atomic**: completable in a single coding-model session.
- Every task has **tests that gate completion**.
- Every task has **explicit out-of-scope** to prevent scope creep.
- Every task references the **contracts** rather than redefining types.
