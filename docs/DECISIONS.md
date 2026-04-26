# Architecture Decision Records (ADRs)

Short, versioned records of foundational tech choices. Status legend: `proposed` | `accepted` | `rejected` | `superseded`.

---

## ADR-0001 — Primary language: Python

**Status:** proposed (blocks Phase 0 kickoff)
**Context:** Blueprint specifies C++ core + embedded Python. A greenfield team of 4–6 cannot deliver a production C++ HFT stack in 12 months. The blueprint's sub-100μs target is not achievable without co-lo + FPGA + seasoned HFT engineers.
**Decision:** Python 3.12 for all services in MVP. Rewrite individual hotspots in Rust only when a profiled bottleneck demands it.
**Consequences:** We give up sub-millisecond latency. We gain velocity, hiring flexibility, and a unified research/production language. Rewrites later are not free but localizable.
**Alternatives rejected:** C++ (too slow to build), Rust-first (hiring is hard, Python's quant ecosystem is unmatched), Go (weaker ML/quant libs).

---

## ADR-0002 — Primary datastore: PostgreSQL + TimescaleDB

**Status:** proposed
**Context:** Need relational data (positions, orders, users) + time-series (ticks, bars). Blueprint lists Timescale, ClickHouse, InfluxDB.
**Decision:** Postgres with TimescaleDB extension. Single operational system. Migrate ticks to ClickHouse or Parquet lake only if query load exceeds Timescale capacity (>1B rows per hypertable with slow scans).
**Consequences:** One DB to operate. SQL for everything. Compression (10x) handles most needs. ClickHouse deferred.
**Alternatives rejected:** ClickHouse-primary (ops burden, weaker relational), InfluxDB (weaker SQL, smaller ecosystem), kdb+ (license cost, hiring).

---

## ADR-0003 — Message bus: Redis Streams

**Status:** proposed
**Context:** Need durable pub/sub between ingestor, OMS, strategies, agents. Options: Redis Streams, NATS JetStream, Kafka, RabbitMQ.
**Decision:** Redis Streams for MVP. Already deploying Redis for caching. Consumer groups cover our patterns. Migrate to Kafka only if retention >7 days or partition count >100 becomes required.
**Consequences:** Simpler ops. Less retention than Kafka. Adequate for phase 1-4 throughput (<50k msg/sec per stream).
**Alternatives rejected:** Kafka (operational weight, Zookeeper/KRaft), NATS (fine choice, but not strictly better and adds a system), RabbitMQ (weaker streaming semantics).

---

## ADR-0004 — UI framework: Next.js (not Qt6)

**Status:** proposed
**Context:** Blueprint mandates Qt6 desktop with Bloomberg-style command mnemonics. Building a production Qt6 app is 6–12 months of specialized work, and we have no Qt engineers.
**Decision:** Next.js 16 + Tailwind + shadcn/ui. Wrap with Tauri if a desktop shell is later demanded by traders. Command palette via `cmdk` gets 80% of Bloomberg mnemonic UX.
**Consequences:** Faster delivery, larger hiring pool, CI/CD trivial. Lose native performance and some multi-monitor affordances. Acceptable given user base is internal quants, not floor traders.
**Alternatives rejected:** Qt6 (cost, hiring), Electron (heavier than Next.js + browser, no material win), native Windows WPF (platform lock-in).

---

## ADR-0005 — No live capital before paper-trading gate

**Status:** proposed
**Context:** Blueprint's Phase 3/4 plan races toward live trading. Most trading system failures are config + operational, not alpha.
**Decision:** Paper-only until (a) 2 weeks live-data paper trading without unplanned outage >5 min, (b) P&L reconciliation with backtest within 20% after costs, (c) risk committee sign-off.
**Consequences:** Explicit gate delays revenue. Prevents the most common mode of losing the firm.
**Alternatives rejected:** "Small real positions early" (the discipline cost is higher than the learning benefit).

---

## Open decisions (not yet written)

- ADR-0006 — feature store: Feast vs custom Timescale.
- ADR-0007 — orchestration: Kubernetes vs docker-compose + systemd (scale question).
- ADR-0008 — cloud provider: AWS vs GCP vs on-prem colo.
- ADR-0009 — backtester: vectorbt vs custom event-driven (perf vs fidelity).
- ADR-0010 — exchange connectivity lib: CCXT Pro vs venue-native SDKs.
