# Architecture — One-Page Mental Model

## The loop

```
┌──────────┐   ticks/books    ┌──────────┐   features    ┌──────────┐   signals    ┌──────────┐
│ ingestor │ ──────────────▶  │ features │ ────────────▶ │  agents  │ ───────────▶ │orchestrat│
└──────────┘                  └──────────┘               └──────────┘              └─────┬────┘
     │                              │                         ▲                         │ decisions
     │                              │                         │ observations            ▼
     ▼                              ▼                         │                   ┌──────────┐
┌──────────┐                  ┌──────────┐              ┌─────┴────┐  orders     │   risk   │
│timescale │ ◀─history───────│feat_store│              │  memory  │ ◀──────────│   gate   │
└──────────┘                  └──────────┘              └──────────┘             └─────┬────┘
                                                                                        │ approved
                                                                                        ▼
                                                                                  ┌──────────┐
                                                                                  │  paper   │
                                                                                  │   OMS    │
                                                                                  └─────┬────┘
                                                                                        │ fills
                                                                                        ▼
                                                                                  ┌──────────┐
                                                                                  │positions │
                                                                                  │   p&l    │
                                                                                  └──────────┘
```

Every arrow is a typed event in `spec/CONTRACTS.md`. Every box is a service in `spec/LAYOUT.md`. Every box has one or more implementation tasks in `spec/tasks/`.

## Module boundaries (hard rules)

| Layer            | Owns                                                         | Never does                                                 |
| ---------------- | ------------------------------------------------------------ | ---------------------------------------------------------- |
| **ingestor**     | Raw venue data → normalized events                           | Compute features, make decisions                           |
| **features**     | Deterministic transforms, feature store reads/writes         | Call external APIs, make trading decisions                 |
| **agents**       | Predictions, signals, LLM calls, tool use                    | Direct order submission, mutate portfolio state            |
| **orchestrator** | Combine agent outputs into decisions, allocate capital       | Train models, ingest data                                  |
| **risk**         | Approve/reject/reshape decisions; kill switch                | Generate ideas or execute                                  |
| **oms**          | Order state, fill simulation (paper) or venue routing (live) | Compute features or train models                           |
| **portfolio**    | Positions, P&L, attribution                                  | Trade                                                      |
| **api**          | Expose read models to UI over HTTP/WS                        | Mutate trading state outside of explicit control endpoints |
| **ui**           | Render read models, issue control commands                   | Business logic                                             |

If you find yourself crossing a boundary, you're doing it wrong. Fix the layering before you fix the code.

## The three message buses

Use Redis Streams throughout, three logical streams:

1. **`md.*`** — market data events (trades, book deltas, bars). High volume, ephemeral (1-day retention).
2. **`sig.*`** — signals, predictions, decisions. Medium volume, 30-day retention for audit.
3. **`ord.*`** — orders, fills, position updates. Low volume, infinite retention (WORM requirement for compliance).

## The two clocks

- **Event time** (`ts_event`): when the market event occurred (venue timestamp).
- **Ingestion time** (`ts_recv`): when we received it.

Backtests use event time. Latency SLOs use `ts_recv - ts_event`. Never confuse them.

## The cutting-edge components (and where they plug in)

| Capability                        | File(s)                                | Plugs into                                      |
| --------------------------------- | -------------------------------------- | ----------------------------------------------- |
| LLM news sentiment                | `services/agents/llm_sentiment/*`      | `agents` layer; publishes `sig.sentiment.*`     |
| Time-series foundation model      | `services/agents/ts_foundation/*`      | `agents` layer; publishes `sig.forecast.*`      |
| Multi-agent tool-use orchestrator | `services/orchestrator/*`              | Consumes all `sig.*`, publishes `ord.decisions` |
| RL execution agent                | `services/agents/execution_rl/*`       | Receives parent orders, slices child orders     |
| Kelly-optimal sizing              | `services/risk/kelly.py`               | Sits in the risk gate                           |
| Regime-adaptive allocation        | `services/orchestrator/regime.py`      | Weights agents by detected regime               |
| Vector memory for agents          | `services/agents/memory.py` (chromadb) | Long-horizon context for LLM agents             |
| MCP-style agent tools             | `libs/fincept-tools/*`                 | Typed tool protocol all agents share            |

Each is a task in `spec/tasks/`. Each has a non-cutting-edge fallback so the system works even if a specific model is weak.

## Deployment (single-node MVP)

```
docker compose (local dev)          ──▶ Kubernetes (staging+prod)
  postgres + timescale                    same, HA
  redis                                   redis-cluster
  ingestor (1 pod)                        ingestor (N pods, sharded by symbol)
  features worker (1)                     N
  agents (one process per agent type)     same, auto-scaled
  orchestrator (singleton)                leader-elected via Redis lock
  risk (singleton)                        same
  oms (singleton)                         same, hot-standby
  api (1)                                 N behind load balancer
  ui (next.js, 1)                         CDN-fronted
```

**Important:** orchestrator, risk, oms are singletons. Running multiple instances causes split-brain trading. Use `libs/fincept-core/leadership.py` for leader election.

## What this architecture is NOT

- Not sub-100μs. We target <100ms end-to-end for signals, <500ms for decisions. Good enough for everything except HFT market making, which is deliberately out of scope.
- Not a Bloomberg replacement. It's a specialized AI trading cockpit.
- Not a framework for third parties. It's an internal platform.

If the user later wants HFT or Bloomberg parity, we add a second execution tier (Rust/C++) that reuses the data spine but replaces the orchestrator. The rest of the system does not change.
