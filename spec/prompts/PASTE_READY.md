# Paste-Ready Prompts — Complete Index

> **The operator's working file.** Every paste-ready prompt for every task across every phase, in one searchable document. Each prompt is fully self-contained and optimized for modern coding agents (Claude Sonnet 4.5+, GPT-4o+).

---

## How to use this document

1. **First time in any session:** paste `spec/prompts/SESSION_OPENER.md` once. Wait for acknowledgment.
2. **Entering a new phase:** paste the phase kickoff block from this file.
3. **Per task:** paste the corresponding task block from this file.
4. **End of phase:** paste the phase exit verification block from this file.

Each paste block is enclosed in a fenced code block tagged `text` and bracketed by `### ▼ PASTE START` / `### ▲ PASTE END` markers. Copy everything BETWEEN the markers, not including the markers themselves.

---

## Table of contents

- [Phase F — Foundation](#phase-f--foundation) — TASK-001..006
- [Phase D — Data Spine](#phase-d--data-spine) — TASK-010..017
- [Phase B — Backtesting](#phase-b--backtesting) — TASK-020..024
- [Phase A — Agents v1](#phase-a--agents-v1) — TASK-030..033
- [Phase O — Orchestrator + Risk + OMS](#phase-o--orchestrator--risk--oms) — TASK-040..045
- [Phase U — UI + API](#phase-u--ui--api) — TASK-050..057
- [Phase X — Cutting Edge](#phase-x--cutting-edge) — TASK-060..066
- [Phase H — Hardening](#phase-h--hardening) — TASK-070..076
- [Phase X+ — Profitability Layer](#phase-x--profitability-layer) — TASK-080..089

---

# Phase F — Foundation

**Goal:** Monorepo skeleton, shared libraries, CI. The plumbing every later phase depends on.
**Checkpoint:** `make dev` spins up the stack; `pytest libs/` is green; CI passes on a PR.

## Phase F — Kickoff

### ▼ PASTE START
```text
ENTERING PHASE F — FOUNDATION.

The session opener norms apply. In addition, this phase has the following phase-specific rules:

1. NO BUSINESS LOGIC YET. Phase F builds plumbing, not strategy. Anything resembling "how to trade" is out of scope until Phase A.

2. LIBRARIES OVER SERVICES. Everything in this phase ships under libs/. No services/ directories are populated yet.

3. PYTHON: uv-based monorepo. Each library has its own pyproject.toml + src/ + tests/. Tests do NOT have a tests/__init__.py file (mypy duplicate-module rule).

4. TS: pnpm workspaces under apps/dashboard with TypeScript strict mode.

5. MIGRATIONS-FIRST. The fincept-db library defines the schema via alembic. ORM models are generated from the schema, not the other way around.

6. CI MUST BE GREEN BEFORE PHASE D. Phase F exits when GitHub Actions runs lint + typecheck + test on every PR and is green.

CONTEXT TO LOAD (in addition to session-opener context):
- spec/LAYOUT.md §libs/* and §root-level files.
- spec/CONTRACTS.md §1 (envelope), §2 (event union types).

Tasks in this phase: TASK-001..006.

Acknowledge by listing the 6 phase-F-specific rules. State which task is next per spec/BUILD_ORDER.md. Wait for the per-task prompt.
```
### ▲ PASTE END

## TASK-001 — Monorepo skeleton

**Files:** root (`pnpm-workspace.yaml`, `pyproject.toml`, `Makefile`, `docker-compose.yml`, `.pre-commit-config.yaml`, `.gitignore`)
**Depends on:** —

### ▼ PASTE START
```text
TASK-001 — Monorepo skeleton.

Create the root scaffolding for a uv-based Python + pnpm-based TypeScript monorepo with docker-compose for local infra.

DELIVERABLES:
- pnpm-workspace.yaml — declares apps/* and packages/* workspaces.
- pyproject.toml at root — uv workspace declaring libs/* and services/* members. Python >=3.12.
- Makefile with targets: dev (start docker-compose + watchers), test (run all pytest + pnpm test), lint, typecheck, format, clean.
- docker-compose.yml — postgres:16-timescaledb (with timescaledb extension), redis:7, prometheus, grafana, jaeger.
- .pre-commit-config.yaml — ruff (lint + format), mypy (strict), prettier for TS, end-of-file-fixer, trailing-whitespace, check-yaml.
- .gitignore — Python (__pycache__, .venv, *.egg-info, .pytest_cache, .mypy_cache, .ruff_cache), TS (node_modules, .next, dist), env (.env*, .pem, *.key), data (data/, *.db, *.sqlite), models (*.pth, *.onnx, *.safetensors), IDE (.idea, .vscode/settings.json).
- README.md at root — one paragraph, points to spec/.

LANDMINES:
- docker-compose.yml: Timescale uses image "timescale/timescaledb:latest-pg16" not "postgres:16-timescaledb". Verify before pinning.
- pre-commit: do NOT include "black" since ruff format replaces it. Including both fights.
- uv workspace: each member's pyproject.toml needs `[tool.uv.sources]` if it depends on a sibling.

DEFINITION OF DONE:
- `make dev` brings up the stack without errors.
- `make lint && make typecheck && make test` all exit 0 (will be empty/no-op for now; that's fine).
- `pre-commit run --all-files` is green.
- Author spec/tasks/TASK-001-monorepo.md documenting the structure.

VERIFICATION:
  make dev        # in one terminal; ctrl-c after services come up
  make lint
  make typecheck
  make test
  pre-commit run --all-files

REPORT in the standard format from the session opener.
```
### ▲ PASTE END

## TASK-002 — `fincept-core` library

**Files:** `libs/fincept-core/{pyproject.toml,src/fincept_core/{schemas,events,config,clock,ids,errors,logging,tracing}.py,tests/test_*.py}`
**Depends on:** TASK-001

### ▼ PASTE START
```text
TASK-002 — fincept-core library.

Implement libs/fincept-core, the canonical types-and-utilities library every other library and service depends on.

DELIVERABLES:
- libs/fincept-core/pyproject.toml — package name "fincept-core", deps: pydantic>=2.7, structlog>=24, opentelemetry-api>=1.25, python-ulid>=2.7.
- src/fincept_core/schemas.py — every type from spec/CONTRACTS.md §1 (envelope) and §2 (event union types). pydantic v2 BaseModel. All `Decimal` fields use `Decimal` (not float). All timestamps are int ns.
- src/fincept_core/events.py — `Event` envelope; `make_event(type, payload, ...)` factory; `parse_event(raw_dict) -> Event` validator that selects the correct payload schema.
- src/fincept_core/config.py — pydantic-settings BaseSettings with TRADING_MODE, DB_URL, REDIS_URL, etc. Env-prefix "FINCEPT_". `Settings()` is the singleton-load.
- src/fincept_core/clock.py — Clock ABC with `now_ns() -> int`, `MonotonicClock`, `FrozenClock(now_ns)` for tests. Production code MUST inject Clock; never call time.time_ns() directly outside this module.
- src/fincept_core/ids.py — `new_id() -> str` returns ULID (26-char base32). Sortable.
- src/fincept_core/errors.py — base FinceptError + subclasses: ContractError, ConfigError, ConnectionError, RiskError, KillSwitchActive.
- src/fincept_core/logging.py — structlog.configure() with JSON output, ISO timestamps, correlation_id from contextvars.
- src/fincept_core/tracing.py — OpenTelemetry init helper; default OTLP HTTP exporter to localhost:4318.
- tests/test_schemas.py, test_events.py, test_clock.py, test_ids.py — pytest, NOT under tests/__init__.py.

LANDMINES:
- pydantic v2: use `field_validator` not `validator`. Use `model_config = ConfigDict(...)` not Meta class.
- Decimal in pydantic v2: declare as `Decimal` directly (no need for arbitrary_types_allowed).
- structlog: configure once at process start; tests should NOT reconfigure (use a fixture that captures).
- ULIDs: use python-ulid; do not handroll.
- The events module's parse_event must dispatch on the discriminator field "type" exactly as defined in CONTRACTS §2.

DEFINITION OF DONE:
- `uv run pytest libs/fincept-core` green.
- `uv run mypy --strict libs/fincept-core/src` clean.
- `from fincept_core import schemas, events, config, clock, ids, errors, logging, tracing` succeeds.
- Round-trip test: every event type in CONTRACTS §2 can be `make_event`'d and `parse_event`'d losslessly.
- Author spec/tasks/TASK-002-fincept-core.md (or update if exists).

VERIFICATION:
  cd libs/fincept-core
  uv sync
  uv run pytest -v
  uv run mypy --strict src

REPORT in the standard format. CONTRACTS USED must include §1 and §2.
```
### ▲ PASTE END

## TASK-003 — `fincept-bus` library (Redis Streams)

**Files:** `libs/fincept-bus/{pyproject.toml,src/fincept_bus/{producer,consumer,streams,types}.py,tests/}`
**Depends on:** TASK-002

### ▼ PASTE START
```text
TASK-003 — fincept-bus library.

Implement Redis Streams pub/sub primitives that every service uses for event-bus communication.

DELIVERABLES:
- libs/fincept-bus/pyproject.toml — deps: redis>=5.0 (with hiredis), fincept-core (workspace).
- src/fincept_bus/streams.py — frozen constants for every stream name from CONTRACTS §6. Examples: MD_TICKS = "md.ticks", SIG_PREDICT = "sig.predict", ORD_DECISIONS = "ord.decisions", ORD_ORDERS = "ord.orders", ORD_FILLS = "ord.fills", EVENTS_ALERTS = "events.alerts". Never hardcode these strings elsewhere.
- src/fincept_bus/producer.py — `Producer` class: `async def publish(stream: str, event: Event) -> str` returns Redis stream ID. Serializes via fincept_core.events. MAXLEN trimming with approximate ~ flag.
- src/fincept_bus/consumer.py — `Consumer` class with consumer-group semantics: `async def consume(streams: list[str], group: str, consumer_name: str, handler: Callable[[Event], Awaitable[None]]) -> None`. Auto-XGROUP CREATE MKSTREAM. Implements XREADGROUP + XACK on success. XCLAIM for pending entries on consumer crash.
- src/fincept_bus/types.py — internal types: StreamID, ConsumerGroupName.
- tests/test_producer.py, test_consumer.py — use fakeredis.aioredis or a docker-compose'd redis.

LANDMINES:
- redis-py async API: use `redis.asyncio.Redis`, not the sync client.
- XADD with MAXLEN: use `~` (approximate) flag, NOT `=`. Approximate is O(1); exact trims block.
- Consumer-group offset: NEVER ack before the handler returns successfully. If handler raises, leave entry pending.
- Backpressure: handler latency must be < block_ms. Document this; tests should assert it.
- Idempotency: handler may receive the same entry twice (after a crash + claim). Document this; consumers must be idempotent.
- Don't catch CancelledError (asyncio shutdown). Let it propagate.

DEFINITION OF DONE:
- `uv run pytest libs/fincept-bus` green (use fakeredis for unit tests).
- `uv run mypy --strict libs/fincept-bus/src` clean.
- Integration test: producer publishes 1000 events, consumer consumes all, no losses, all acked.
- Round-trip latency test: p99 < 5ms on a single-node redis.
- Author spec/tasks/TASK-003-fincept-bus.md.

VERIFICATION:
  cd libs/fincept-bus
  uv sync
  uv run pytest -v
  uv run mypy --strict src

REPORT in the standard format. CONTRACTS USED must include §1, §2, §6.
```
### ▲ PASTE END

## TASK-004 — `fincept-db` library (SQLAlchemy + alembic + Timescale)

**Files:** `libs/fincept-db/{pyproject.toml,src/fincept_db/{engine,models,migrations,access}.py,migrations/}`
**Depends on:** TASK-002

### ▼ PASTE START
```text
TASK-004 — fincept-db library.

Implement async SQLAlchemy engine, ORM models, alembic migrations, and high-level data access for ticks/bars/positions/orders/fills.

DELIVERABLES:
- libs/fincept-db/pyproject.toml — deps: sqlalchemy[asyncio]>=2.0, asyncpg>=0.29, alembic>=1.13, fincept-core (workspace).
- src/fincept_db/engine.py — `make_engine(dsn) -> AsyncEngine` with QueuePool sized 5–20, `get_session()` async context manager.
- src/fincept_db/models/ — declarative ORM: Tick, Bar1m, Bar1d, Position, Order, Fill, Decision, AuditLogEntry, StrategyMetricsDaily. Foreign keys + indexes per CONTRACTS §5.
- src/fincept_db/access/ticks.py, bars.py, positions.py, orders.py — query helpers (async).
- src/fincept_db/access/pit.py — point-in-time join helper used by the feature store: `pit_join(left, right, on, ts_col, asof_col) -> AsyncIterator[dict]`. Critical for backtest correctness.
- migrations/ — alembic scripts. Initial migration creates every table with hypertables for ticks/bars (Timescale). Continuous aggregates for 1m → 1h → 1d.
- alembic.ini at libs/fincept-db root.
- tests/ — use a docker-compose'd Timescale; fixture creates a fresh schema per test run.

LANDMINES:
- Timescale hypertables: ticks and bars must be hypertables (CREATE HYPERTABLE), not regular tables. Indexes on (symbol, ts_ns DESC) for read-recent.
- Decimal columns: use NUMERIC(38, 18) in postgres. SQLAlchemy: `Numeric(38, 18, asdecimal=True)`.
- ts_ns: BIGINT, indexed. Never store TIMESTAMP for tick data; lossy and slower.
- PIT join: must NEVER look forward. Test it with a synthetic dataset where every join key has a known asof; assert no future leak.
- Continuous aggregates: refresh policies must NOT auto-refresh during tests; configure them off in the test fixture.
- Async session: never share sessions across asyncio tasks. Use one per request/handler.

DEFINITION OF DONE:
- `uv run pytest libs/fincept-db` green.
- `uv run mypy --strict libs/fincept-db/src` clean.
- `alembic upgrade head` succeeds against a fresh Timescale.
- PIT join regression test: 100 random scenarios, zero forward leakage.
- Author spec/tasks/TASK-004-fincept-db.md.

VERIFICATION:
  cd libs/fincept-db
  uv sync
  alembic upgrade head     # against test postgres
  uv run pytest -v
  uv run mypy --strict src

REPORT in the standard format. CONTRACTS USED must include §5.
```
### ▲ PASTE END

## TASK-005 — `fincept-tools` library (typed tool registry)

**Files:** `libs/fincept-tools/{pyproject.toml,src/fincept_tools/{protocol,registry,data,analytics,exec}.py,tests/}`
**Depends on:** TASK-002

### ▼ PASTE START
```text
TASK-005 — fincept-tools library.

Implement the typed tool protocol and registry that LLM agents and the orchestrator use to perform side-effecting and read operations. This is the boundary between "agents" and "everything else."

DELIVERABLES:
- libs/fincept-tools/pyproject.toml — deps: pydantic>=2.7, fincept-core, fincept-db, fincept-bus (workspace).
- src/fincept_tools/protocol.py — Tool protocol per CONTRACTS §8: name, description, args_schema (pydantic class), result_schema (pydantic class), `async run(args) -> result`. ToolResult union: ToolOK | ToolError.
- src/fincept_tools/registry.py — ToolRegistry: `register(tool)`, `get(name) -> Tool`, `list() -> list[ToolMeta]`. Also exports `to_openai_function_spec(tool)` and `to_anthropic_tool_spec(tool)` for LLM tool-use APIs.
- src/fincept_tools/data/ — read-only tools: get_bars, get_features, get_positions, get_universe, entity.resolve.
- src/fincept_tools/analytics/ — pure-compute tools: compute_returns, compute_vol, compute_correlation, compute_sharpe, compute_drawdown.
- src/fincept_tools/exec/ — side-effecting tools (paper-only by default): submit_order, cancel_order, get_order_status. These write to ord.orders stream via fincept-bus.
- tests/ — every tool has a positive + negative test. Tools that touch data use a fixture-loaded test db.

LANDMINES:
- Tool args/results MUST be pydantic models, not dicts. The whole point is structured I/O.
- Side-effecting tools MUST check `settings.trading_mode`. If "paper", route to paper OMS. If "live", check Phase H prerequisites (deferred until Phase H).
- entity.resolve: takes a string like "AAPL" or "Apple Inc." or "$AAPL" and returns the canonical universe symbol or raises NotInUniverse. LLMs hallucinate; this is the gate.
- Tool errors are TYPED. Never `raise Exception(...)`; raise specific subclasses of FinceptError.
- Cost-tracking: every tool call emits a SpanEvent with tool name, args size, result size, latency. The orchestrator aggregates.

DEFINITION OF DONE:
- `uv run pytest libs/fincept-tools` green.
- `uv run mypy --strict libs/fincept-tools/src` clean.
- Round-trip: register a tool, retrieve it, call it, get a typed result. All via the registry.
- to_openai_function_spec output validates against OpenAI's tool schema.
- Author spec/tasks/TASK-005-fincept-tools.md.

VERIFICATION:
  cd libs/fincept-tools
  uv sync
  uv run pytest -v
  uv run mypy --strict src

REPORT in the standard format. CONTRACTS USED must include §8.
```
### ▲ PASTE END

## TASK-006 — CI pipeline (GitHub Actions)

**Files:** `.github/workflows/{ci,nightly}.yml`
**Depends on:** TASK-001

### ▼ PASTE START
```text
TASK-006 — CI pipeline.

Implement GitHub Actions workflows for PR validation and nightly builds.

DELIVERABLES:
- .github/workflows/ci.yml — runs on PR + push to main:
    matrix: python 3.12 only (for now).
    services: postgres:16, redis:7 as services-block (not docker-compose; faster startup).
    steps: checkout, setup-uv, uv sync (root + members), uv run ruff check, uv run mypy --strict on all libs/, uv run pytest libs/ -v, pnpm install, pnpm -r typecheck, pnpm -r test.
    cache: ~/.cache/uv, pnpm store path.
- .github/workflows/nightly.yml — runs on cron @ 03:00 UTC:
    full integration: docker-compose up -d, run pytest tests/integration/, take down.
    runs the chaos suite (TASK-070; will be empty placeholder until Phase H).
- branch protection (documented in this file as a comment): require ci.yml green to merge to main.

LANDMINES:
- uv-action: use `astral-sh/setup-uv@v3` (latest stable). Pin a specific version to prevent drift.
- pnpm caching: pnpm store path varies per OS; use `pnpm store path` then cache that.
- pytest in libs/: aggregate via `uv run pytest libs/` (uv understands the workspace), not per-library loop.
- mypy: --strict on libs/ src/ only, NOT on tests/. Tests can use Any for fixtures.
- Don't commit caches (.uv/, .pnpm-store/).

DEFINITION OF DONE:
- A trivial PR (whitespace-only) triggers ci.yml and exits green.
- A PR with a failing test triggers ci.yml and exits red.
- Nightly workflow has a "manual run" button (workflow_dispatch trigger).
- Author spec/tasks/TASK-006-ci.md.

VERIFICATION:
  # Local: run the same commands ci.yml runs.
  uv sync
  uv run ruff check .
  uv run mypy --strict libs/*/src
  uv run pytest libs/ -v
  pnpm install
  pnpm -r typecheck
  pnpm -r test

REPORT in the standard format.
```
### ▲ PASTE END

## Phase F — Exit verification

### ▼ PASTE START
```text
PHASE F EXIT VERIFICATION.

Run the Phase F checkpoint validation. Do not advance to Phase D until every check is green.

CHECKLIST:
1. `make dev` brings up the full local stack without errors. Postgres, Redis, Prometheus, Grafana, Jaeger all healthy.
2. `uv run pytest libs/` runs ≥ 4 library test suites and exits 0.
3. `uv run mypy --strict libs/*/src` exits 0 across all libraries.
4. `uv run ruff check .` exits 0.
5. `pre-commit run --all-files` exits 0.
6. CI workflow on a real PR exits green.
7. spec/tasks/TASK-001..006.md all exist and document what shipped.
8. Mark TASK-001..006 as [x] in spec/BUILD_ORDER.md.

If all green: declare Phase F COMPLETE. Add the line "Checkpoint F: passed YYYY-MM-DD" to BUILD_ORDER.md. Proceed to spec/prompts/PASTE_READY.md → Phase D.

If any red: do NOT advance. Identify and fix the root cause; re-run the full checklist.

REPORT the checklist with green/red per item, plus any rework needed.
```
### ▲ PASTE END

---

# Phase D — Data Spine

**Goal:** Ingestion, feature store, point-in-time discipline. The data layer that every backtest and every agent reads from.
**Checkpoint:** 24-hour soak test on 5 crypto pairs with zero dropped messages; feature store serves online reads in <10ms p99; offline backfill reproduces live features bit-exact.

## Phase D — Kickoff

### ▼ PASTE START
```text
ENTERING PHASE D — DATA SPINE.

The session opener norms apply. Phase F is complete. In addition, this phase has the following phase-specific rules:

1. PRECISION. Every price, size, fee is decimal.Decimal. Never float. Never. The fincept-db NUMERIC(38, 18) columns enforce this; your Python code must match. Float in this phase is a bug.

2. POINT-IN-TIME OR DEATH. Every join the feature store does is PIT. Use fincept_db.access.pit.pit_join. Lookahead leakage is the single most expensive bug in this codebase; it makes a backtest profitable on paper and worthless in production.

3. MONOTONIC TIME. Use fincept_core.clock.MonotonicClock. Wall-clock can go backward (NTP) and ruin event ordering. ts_ns is monotonic, source of truth.

4. EXACTLY-ONCE INGESTION. Each venue's adapter dedups via (venue, symbol, ts_ns, sequence_id). Never write the same tick twice; always survive a reconnect cleanly.

5. BACKPRESSURE IS REAL. If the writer can't keep up with the wire, drop with metrics, don't silently fall behind. The quality monitor (TASK-014) watches for this.

6. PRODUCT IS DATA, NOT JUST CODE. A green test suite with bad data is worse than no data. Every ingestor has a quality contract documented in its task spec; the quality monitor enforces it.

CONTEXT TO LOAD:
- spec/CONTRACTS.md §3 (market-data event types).
- spec/LAYOUT.md §services/ingestor and §services/features.
- libs/fincept-db (TASK-004) for the DB primitives.
- libs/fincept-bus (TASK-003) for the streams.

Tasks: TASK-010..017.

Acknowledge by listing the 6 phase-D-specific rules. State which task is next. Wait for the per-task prompt.
```
### ▲ PASTE END

## TASK-010 — Ingestor base + normalizer + writer

**Files:** `services/ingestor/src/ingestor/{base,normalizer,writer}.py`
**Depends on:** TASK-002, TASK-003, TASK-004

### ▼ PASTE START
```text
TASK-010 — Ingestor base class + normalizer + writer.

Implement the venue-agnostic skeleton that every venue adapter (TASK-011..013) inherits.

DELIVERABLES:
- services/ingestor/src/ingestor/base.py — IngestorBase ABC: `async connect()`, `async subscribe(symbols)`, `async run()`, abstract `async _on_message(raw)`. Implements: reconnect with exponential backoff, heartbeat monitoring, metrics emission.
- services/ingestor/src/ingestor/normalizer.py — Normalizer: takes venue-native dict, returns canonical Tick/Quote/Trade/Bar event matching CONTRACTS §3. Per-venue subclasses override field maps.
- services/ingestor/src/ingestor/writer.py — Writer: dual-write to Redis stream (md.ticks etc., real-time) AND Timescale (md.ticks hypertable, batched 100/100ms whichever first). Backpressure-aware: drops with counter increment if Redis or Timescale falls behind by > N seconds.
- tests/ — fixture replays a captured WebSocket transcript against a mocked venue; asserts canonical events emitted.

LANDMINES:
- Reconnect: capture (last_seq_id, last_ts_ns) before disconnect; replay from there. Many venues support "resume from sequence."
- Heartbeat: most venues send pings; respond within ms. Don't conflate venue heartbeat with our internal liveness check.
- Decimal parsing: venues return strings ("0.00012345") — use Decimal(str), never Decimal(float). Float→Decimal corrupts.
- Timescale batching: COPY-style insert via asyncpg's copy_records_to_table is 10x faster than executemany. Use it.
- ts_ns: prefer venue-provided exchange-side timestamp. Local-receive ts can be exposed as `ts_local_ns` for latency tracking, never as the canonical ts.
- Backpressure metric: `ingestor.backpressure.dropped_total{venue,reason}` counter; alert if > 0 over 5min.

DEFINITION OF DONE:
- `uv run pytest services/ingestor/tests/test_base.py services/ingestor/tests/test_writer.py` green.
- Replay test: 10000-tick transcript replays into normalizer + writer; all 10000 land in Timescale, all 10000 in Redis stream, no drops.
- mypy --strict clean.
- Author spec/tasks/TASK-010-ingestor-base.md.

VERIFICATION:
  cd services/ingestor
  uv run pytest -v
  uv run mypy --strict src

REPORT. CONTRACTS USED: §3.
```
### ▲ PASTE END

## TASK-011 — Binance spot WS adapter

**Files:** `services/ingestor/src/ingestor/binance.py`
**Depends on:** TASK-010

### ▼ PASTE START
```text
TASK-011 — Binance spot WebSocket adapter.

Subclass IngestorBase to consume Binance spot's combined-streams WebSocket.

DELIVERABLES:
- services/ingestor/src/ingestor/binance.py — BinanceSpotIngestor.
- Subscribes to: <symbol>@trade, <symbol>@bookTicker, <symbol>@kline_1m for every symbol in the universe.
- Uses combined-stream endpoint wss://stream.binance.com:9443/stream?streams=...
- Maps Binance fields to canonical Tick/Quote/Bar1m via Normalizer subclass.
- Handles 24h disconnect rotation (Binance forces a reconnect every 24h).
- Tests use captured Binance JSON transcripts.

LANDMINES:
- Symbols are LOWERCASE in the URL but UPPERCASE in payloads. Normalize to UPPERCASE canonical.
- "T" in trade payload is event time (ms); "t" is trade ID; venue ts_ns = T * 1_000_000.
- Combined-stream URL has a max length; chunk subscriptions if universe > ~50 symbols.
- Listen-key is for user-data WS, NOT public market data — don't conflate.
- Rate limits: connection limit 5 per IP per 5 min. Stagger reconnects.
- bookTicker gives only top-of-book; for deeper data use depth5 or depth20 streams (defer to L2 task).

DEFINITION OF DONE:
- `uv run pytest services/ingestor/tests/test_binance.py` green.
- Run live for 10 minutes against testnet (or mainnet): no errors, 5000+ ticks ingested.
- mypy --strict clean.
- Author spec/tasks/TASK-011-binance.md.

VERIFICATION:
  uv run pytest services/ingestor/tests/test_binance.py -v
  uv run python -m ingestor.binance --symbols BTCUSDT,ETHUSDT --duration 600  # 10-min live test

REPORT. CONTRACTS USED: §3.
```
### ▲ PASTE END

## TASK-012 — Coinbase Advanced Trade adapter

**Files:** `services/ingestor/src/ingestor/coinbase.py`
**Depends on:** TASK-010

### ▼ PASTE START
```text
TASK-012 — Coinbase Advanced Trade WebSocket adapter.

Subclass IngestorBase to consume Coinbase Advanced Trade's WebSocket.

DELIVERABLES:
- services/ingestor/src/ingestor/coinbase.py — CoinbaseAdvancedTradeIngestor.
- Subscribes to channels: market_trades, ticker, ticker_batch (1Hz), candles (1m).
- Endpoint: wss://advanced-trade-ws.coinbase.com
- Uses JWT auth for non-public channels (deferred for now; market data is public).
- Maps Coinbase fields to canonical events.

LANDMINES:
- Coinbase product_id is "BTC-USD" not "BTCUSD". Universe normalization required.
- Sequence numbers reset on reconnect; use them ONLY for in-session ordering, not cross-session dedup.
- ticker_batch is 1Hz aggregated quotes; useful for low-rate symbols, redundant for liquid ones.
- Coinbase has occasional gaps (their problem). Quality monitor (TASK-014) detects.

DEFINITION OF DONE:
- pytest green; 10-min live ingestion test passes.
- mypy --strict clean.
- Author spec/tasks/TASK-012-coinbase.md.

VERIFICATION:
  uv run pytest services/ingestor/tests/test_coinbase.py -v
  uv run python -m ingestor.coinbase --symbols BTC-USD,ETH-USD --duration 600

REPORT. CONTRACTS USED: §3.
```
### ▲ PASTE END

## TASK-013 — Kraken WS adapter

**Files:** `services/ingestor/src/ingestor/kraken.py`
**Depends on:** TASK-010

### ▼ PASTE START
```text
TASK-013 — Kraken WebSocket v2 adapter.

Subclass IngestorBase to consume Kraken's WebSocket v2 public feeds.

DELIVERABLES:
- services/ingestor/src/ingestor/kraken.py — KrakenIngestor.
- Endpoint: wss://ws.kraken.com/v2
- Subscribes: trade, ticker, ohlc (1m).
- Maps Kraken pair names ("XBT/USD") to canonical ("BTCUSD") via universe.

LANDMINES:
- Kraken renames BTC to XBT historically. Map both ways in entity.resolve.
- Kraken's WS v1 is deprecated; do NOT implement it. v2 only.
- Subscription batching: Kraken accepts a list of symbols per subscribe message; use that.

DEFINITION OF DONE:
- pytest + 10-min live test green.
- mypy --strict clean.
- Author spec/tasks/TASK-013-kraken.md.

VERIFICATION:
  uv run pytest services/ingestor/tests/test_kraken.py -v
  uv run python -m ingestor.kraken --symbols BTC/USD,ETH/USD --duration 600

REPORT. CONTRACTS USED: §3.
```
### ▲ PASTE END

## TASK-014 — Quality monitor

**Files:** `services/ingestor/src/ingestor/quality.py`
**Depends on:** TASK-011

### ▼ PASTE START
```text
TASK-014 — Quality monitor for ingestion.

Implement a per-venue + per-symbol monitor that detects gap, staleness, cross-spread, and backpressure issues, emitting AlertEvents on events.alerts.

DELIVERABLES:
- services/ingestor/src/ingestor/quality.py — QualityMonitor.
- Detectors:
    GAP: > 10 seconds between consecutive ticks for a symbol that normally has multiple per second.
    STALENESS: no tick for symbol X for > N seconds where N is per-symbol baseline + 3σ.
    CROSS_SPREAD: bid > ask (data corruption or stale crossover).
    NEGATIVE_PRICE: price <= 0.
    BACKPRESSURE: ingestor's writer drop counter increments.
- Emits AlertEvent with severity in {info, warning, critical}.
- Per-symbol baselines computed via online EWMA over last 1 hour.

LANDMINES:
- Cold start: until baseline is established (first 10 min for a symbol), suppress staleness alerts.
- Trading-hours awareness for equities (deferred — for crypto, 24/7 baseline is fine).
- Halt detection: a regulatory halt looks like staleness; flag, don't auto-resolve. The Risk gate decides.
- Alerts must be debounced: same alert type for same (venue, symbol) within 60s = suppress duplicate.

DEFINITION OF DONE:
- `uv run pytest services/ingestor/tests/test_quality.py` green.
- Synthetic test: inject a 30-second gap → GAP alert fires within 5s of the gap closing.
- Author spec/tasks/TASK-014-quality.md.

VERIFICATION:
  uv run pytest services/ingestor/tests/test_quality.py -v

REPORT. CONTRACTS USED: §3, §7 (alerts).
```
### ▲ PASTE END

## TASK-015 — EOD equity loader (yfinance → bars_1d)

**Files:** `services/ingestor/src/ingestor/eod_equity.py`
**Depends on:** TASK-004

### ▼ PASTE START
```text
TASK-015 — End-of-day equity loader.

Implement a daily job that fetches EOD bars for the equity universe via yfinance and writes to the bars_1d Timescale table.

DELIVERABLES:
- services/ingestor/src/ingestor/eod_equity.py — EodEquityLoader.
- Reads universe from db.universe table (where asset_class = 'equity').
- Fetches via yfinance.download(symbols, start, end, interval='1d', auto_adjust=True).
- Writes to bars_1d via fincept_db.access.bars; idempotent (ON CONFLICT DO NOTHING via natural key (symbol, ts_ns)).
- Scheduled via services/jobs/ (cron-style; runs at 23:30 ET on US trading days).

LANDMINES:
- yfinance returns adjusted prices when auto_adjust=True. Document this; ensure the orchestrator/backtester knows whether bars are split-adjusted or raw.
- Splits / dividends: auto_adjust handles splits; dividends are folded into adjusted close. For PIT correctness in backtest, you'll want both adjusted and raw — store adjusted by default; flag in tags.adjustment if raw is added later.
- Trading calendar: don't fetch on weekends or US market holidays. Use pandas_market_calendars.NYSE.
- yfinance is unofficial; rate-limit to 5 req/sec, exponential backoff on 429.
- Survivorship: yfinance returns NaN for delisted symbols. Don't fail the job; flag the symbol as inactive.

DEFINITION OF DONE:
- pytest with mocked yfinance covers happy path + 429 retry + delisted-symbol case.
- Live run against 50-symbol equity universe completes in < 5 minutes, all bars in db.
- Author spec/tasks/TASK-015-eod-equity.md.

VERIFICATION:
  uv run pytest services/ingestor/tests/test_eod_equity.py -v
  uv run python -m ingestor.eod_equity --universe equity_minor --date 2025-01-15

REPORT. CONTRACTS USED: §3.
```
### ▲ PASTE END

## TASK-016 — Features: online transforms

**Files:** `services/features/src/features/{online,transforms/*}.py`
**Depends on:** TASK-002, TASK-004, TASK-011

### ▼ PASTE START
```text
TASK-016 — Online feature transforms.

Implement streaming-update feature transforms (returns, vol, microstructure) that consume from md.* and emit to feat.* streams.

DELIVERABLES:
- services/features/src/features/online.py — OnlineFeatureEngine: subscribes to md.ticks/md.quotes/md.bars; computes per-symbol features online; publishes to feat.online stream and updates the online feature store (Redis hash per symbol).
- services/features/src/features/transforms/returns.py — log returns at 1m, 5m, 15m, 1h windows.
- services/features/src/features/transforms/vol.py — realized vol (EWMA), Yang-Zhang OHLC vol, Garman-Klass.
- services/features/src/features/transforms/micro.py — bid-ask spread, mid-price, weighted mid (size-weighted), order-imbalance (TASK-096 in Phase Y goes deeper).
- Each transform is a small class with `update(tick) -> feature_dict` for online state.

LANDMINES:
- State per symbol per transform = potentially many small objects. Use slots or dataclasses with __slots__.
- Vol transforms need warm-up; emit None or omit from feature dict until N observations.
- Numerical precision: log returns for tiny prices can underflow float64. Use Decimal where reasonable, or guard with small-value floor.
- Don't recompute features at write — compute once online, serve from store.

DEFINITION OF DONE:
- pytest green (synthetic tick stream → expected feature trajectories).
- Performance: 100k ticks/sec sustained per symbol on a single core.
- Author spec/tasks/TASK-016-features-online.md.

VERIFICATION:
  uv run pytest services/features/tests/test_online.py -v
  uv run python -m features.bench.online_throughput

REPORT. CONTRACTS USED: §3 (md.*), §4 (feat.*).
```
### ▲ PASTE END

## TASK-017 — Features: online + offline store with PIT joins

**Files:** `services/features/src/features/{store,pit}.py`
**Depends on:** TASK-016

### ▼ PASTE START
```text
TASK-017 — Feature store with online + offline reads and PIT joins.

Implement a feature store that serves online reads (Redis hash, <10ms p99) and offline reads (Timescale, PIT-correct).

DELIVERABLES:
- services/features/src/features/store.py — FeatureStore: get_online(symbol, names) -> dict, get_offline(symbol, names, asof_ns) -> dict, write(symbol, name, ts_ns, value).
- services/features/src/features/pit.py — uses fincept_db.access.pit.pit_join under the hood; ensures NO feature read for asof_ns t can return a value computed at ts > t.
- Online: Redis hash per symbol; key f"feat:{symbol}", fields are feature names, value is JSON-encoded {ts_ns, value}.
- Offline: Timescale table feature_history (symbol, name, ts_ns, value).
- Backfill job: replays historical bars/ticks through the online engine to populate offline store. Bit-exact match required between live online and replayed offline.

LANDMINES:
- The online → offline match test is THE test of this task. Run it on every PR.
- Redis hash size: cap at last-N values per feature; older values flushed to Timescale only.
- PIT: any code that does a join MUST go through fincept_db.access.pit. No exceptions.
- Asof-precision: round asof_ns DOWN to the nearest emitted feature ts_ns; never round up (would leak).

DEFINITION OF DONE:
- pytest green, including the live-online vs replay-offline bit-exact test.
- Online reads p99 < 10ms over 1000 random reads.
- PIT regression test: 100 random asofs, 0 lookahead leaks.
- Author spec/tasks/TASK-017-features-store.md.

VERIFICATION:
  uv run pytest services/features/tests/test_store.py services/features/tests/test_pit.py -v
  uv run python -m features.bench.online_latency

REPORT. CONTRACTS USED: §4.
```
### ▲ PASTE END

## Phase D — Exit verification

### ▼ PASTE START
```text
PHASE D EXIT VERIFICATION.

Run the Phase D checkpoint validation. This phase is the foundation of every backtest and every live-traded decision; do not advance to Phase B until every check is green.

CHECKLIST:
1. 24-hour soak test: 5 crypto pairs (BTCUSDT, ETHUSDT, BTC-USD, ETH-USD, BTC/USD across Binance, Coinbase, Kraken) ingested for ≥ 24 hours straight. Zero dropped messages (writer.dropped_total == 0). At least one venue reconnect handled cleanly during the soak.
2. Quality monitor (TASK-014) fired and resolved at least one synthetic alert during the soak.
3. Online feature reads p99 latency < 10ms over a 10-minute synthetic load.
4. Offline backfill: replay 7 days of historical ticks through OnlineFeatureEngine; resulting feature_history matches live-emitted features bit-exact.
5. PIT regression test: 100 random (asof, symbol, feature) tuples; 0 leaks.
6. EOD equity loader runs nightly and populates bars_1d for the equity universe.
7. mypy --strict clean across services/ingestor and services/features.
8. spec/tasks/TASK-010..017.md exist and are accurate.
9. Mark TASK-010..017 [x] in spec/BUILD_ORDER.md.

If all green: declare Phase D COMPLETE. Add "Checkpoint D: passed YYYY-MM-DD". Proceed to Phase B.

If any red: do NOT advance. Most common cause: PIT leak detected. Fix and re-run the full PIT regression with a larger random sample (1000 tuples) before declaring fixed.

REPORT the checklist with green/red per item, plus rework if any.
```
### ▲ PASTE END

---

# Phase B — Backtesting

**Goal:** Deterministic, leak-proof backtester. The scoreboard before any agent competes for capital.
**Checkpoint:** Reference MA-crossover strategy produces known-good Sharpe on 2 yr of BTC 1m bars; walk-forward respects PIT; bit-identical re-runs.

## Phase B — Kickoff

### ▼ PASTE START
```text
ENTERING PHASE B — BACKTESTING.

Session opener norms apply. Phases F and D complete. Phase-specific rules:

1. DETERMINISM. Same inputs + same seed + same code-version produce a byte-identical blotter. Test on every PR.
2. NO LOOKAHEAD. The lookahead-leakage regression test in services/backtester/tests/ is THE most important test in the codebase. Never weaken it.
3. COSTS ARE PART OF THE BACKTEST. Spread + slippage + fees + borrow. A backtest with zero costs is misleading.
4. WALK-FORWARD ONLY. PIT-correct rolling windows; multiple-comparison correction over hyperparameter searches.
5. REUSE THE LIVE PRIMITIVES. Backtester uses fincept_db, fincept_core, and the same feature store as live.
6. THE BACKTESTER IS A SERVICE. Talks via fincept-bus; emits same Decision/Order/Fill events as live.

CONTEXT: spec/CONTRACTS.md §3, §5, §6. Tasks: TASK-020..024.

Acknowledge by listing the 6 rules. Wait.
```
### ▲ PASTE END

## TASK-020 — Backtester engine

### ▼ PASTE START
```text
TASK-020 — Backtester engine.

Files: services/backtester/src/backtester/{datasource,engine}.py + ResultsWriter + CLI.

Behavior: deterministic event-driven loop, replays from Timescale chronologically, drives signal→decision→risk→fill via the SAME orchestrator/risk/OMS code as live (running on FrozenClock + in-memory bus). Emits blotter parquet + per-bar P&L parquet.

LANDMINES:
- Determinism: pin asyncio scheduler, sort all set/dict iterations, seed every RNG.
- FrozenClock advances ONLY on next event ts_ns.
- Streaming, not load-all.
- The "same code as live" claim must be tested end-to-end.

DONE WHEN:
- pytest green: deterministic-replay test (run twice, byte-identical blotter).
- 1-month BTCUSDT buy-and-hold smoke runs in <30s.
- mypy --strict clean.
- spec/tasks/TASK-020-backtester-engine.md authored.

VERIFY:
  uv run pytest services/backtester/tests/test_engine.py -v
  uv run python -m backtester.engine --strategy buy_and_hold --start 2024-01-01 --end 2024-02-01 --seed 42

REPORT in standard format. CONTRACTS: §3, §4, §6.
```
### ▲ PASTE END

## TASK-021 — Cost model

### ▼ PASTE START
```text
TASK-021 — Cost model (spread + slippage + fees + borrow).

File: services/backtester/src/backtester/costs.py.

Components: SpreadCost (half-spread on market orders), SlippageCost (linear or sqrt-impact based on participation rate), FeeCost (per-venue maker/taker schedule), BorrowCost (annualized, accrues per-bar for shorts). Composable; called by broker (TASK-022) at fill time.

LANDMINES:
- Crypto fees vary by tier; default to taker.
- Equity borrow is real; default configurable %/year.
- Slippage needs ADV; default 50bps participation impact when ADV unknown.

DONE WHEN:
- pytest green; deterministic.
- With-vs-without-costs P&L delta sanity-checked.
- spec/tasks/TASK-021-costs.md authored.

VERIFY: uv run pytest services/backtester/tests/test_costs.py -v
REPORT.
```
### ▲ PASTE END

## TASK-022 — Broker simulator

### ▼ PASTE START
```text
TASK-022 — Broker simulator (paper fills, partials, cancellations).

File: services/backtester/src/backtester/broker.py. Implements BrokerSimulator: market orders fill at next tick mid + slippage; limit orders fill only when next TRADE crosses the limit; partials when size > top-of-book; cancel succeeds if not yet filled. Idempotent on client_order_id (DUPLICATE rejection). Emits Fills to ord.fills. Simulates configurable submit→fill latency.

LANDMINES:
- Conservative limit-fill rule (TRADE crosses, not quote crosses).
- Partial fills push remainder back into book.
- Latency queue, not instant fills.

DONE WHEN:
- pytest green: market, limit, partial, cancel-after-fill rejection, duplicate rejection.
- spec/tasks/TASK-022-broker.md authored.

VERIFY: uv run pytest services/backtester/tests/test_broker.py -v
REPORT. CONTRACTS: §4.
```
### ▲ PASTE END

## TASK-023 — Walk-forward + report

### ▼ PASTE START
```text
TASK-023 — Walk-forward runner + report.

Files: services/backtester/src/backtester/{walk_forward,report}.py.

WalkForwardRunner: takes (start, end, train_window, test_window, step), generates rolling splits, runs backtest per split with PIT-correct HPO. Multiple-comparison correction (Benjamini-Hochberg) on per-trial p-values. Report: QuantStats + custom additions (turnover, capacity, factor exposures); HTML + PDF + parquet.

LANDMINES:
- Splits must respect PIT — train features cannot include data with ts >= test window start.
- HPO inside walk-forward: pick best on IS, freeze for OOS. Never re-tune on OOS.
- Multiple comparisons: 200-trial search produces false positives without correction. The post-correction Sharpe is the deployable number.

DONE WHEN:
- pytest green; PIT regression on synthetic data.
- Smoke produces an HTML report.
- spec/tasks/TASK-023-walk-forward.md authored.

VERIFY:
  uv run pytest services/backtester/tests/test_walk_forward.py -v
  uv run python -m backtester.walk_forward --strategy buy_and_hold --start 2022-01-01 --end 2024-01-01 --train 180 --test 30
REPORT.
```
### ▲ PASTE END

## TASK-024 — SDK Strategy base

### ▼ PASTE START
```text
TASK-024 — SDK Strategy base + StrategyContext + backtest runner.

Files: libs/fincept-sdk/src/fincept_sdk/{strategy,runner}.py + libs/fincept-sdk/examples/ma_crossover.py.

Strategy ABC: on_bar(ctx, bar), on_tick(ctx, tick), on_signal(ctx, signal). StrategyContext exposes: positions, place_order, cancel_order, get_features, get_universe, log — ALL via fincept-tools (TASK-005), never direct DB/Redis. Same import works in backtester process and orchestrator process. backtest(strategy_class, start, end, seed) -> Report.

LANDMINES:
- Strategy is sandboxed: no fs writes outside fincept_sdk.io helpers; no direct network.
- Tools-only access keeps backtest and live identical.

DONE WHEN:
- pytest green.
- ma_crossover example reproduces a known Sharpe on 2 yr BTC 1m.
- spec/tasks/TASK-024-strategy-sdk.md authored.

VERIFY:
  uv run pytest libs/fincept-sdk -v
  uv run python -m fincept_sdk.examples.ma_crossover
REPORT. CONTRACTS: §8.
```
### ▲ PASTE END

## Phase B — Exit verification

### ▼ PASTE START
```text
PHASE B EXIT.

CHECKLIST:
1. Determinism: same backtest run twice → byte-identical blotter parquet (hash equality).
2. Lookahead regression: cheating-strategy that peeks 1 bar ahead is run; engine prevents the peek; realized P&L equals no-peek baseline.
3. Reference MA-crossover on BTCUSDT 1m 2022→2024: Sharpe within ±5% of pre-known good.
4. Walk-forward HTML report has IS+OOS Sharpe with multiple-comparison correction.
5. Cost on/off shows expected P&L gap.
6. mypy --strict clean across services/backtester + libs/fincept-sdk.
7. TASK-020..024 specs exist; [x] in BUILD_ORDER.md.

If green: Phase B COMPLETE. Add "Checkpoint B: passed YYYY-MM-DD". Proceed to Phase A.

REPORT.
```
### ▲ PASTE END

---

# Phase A — Agents v1

**Goal:** Three non-LLM baseline agents that establish the floor for Phase X to beat.
**Checkpoint:** GBM ≥52% directional acc. (p<0.05); regime model labels 3 historical transitions correctly; pairs Sharpe>0.5 OOS on ≥2 cointegrated pairs.

## Phase A — Kickoff

### ▼ PASTE START
```text
ENTERING PHASE A — AGENTS V1 (NON-LLM BASELINE).

Session opener norms apply. Phases F, D, B complete. Phase-specific rules:

1. CALIBRATED, NOT JUST ACCURATE. 55%-accurate with calibrated confidence beats 60%-accurate with random confidence. Brier score + reliability diagrams matter.
2. WALK-FORWARD VALIDATION (TASK-023 harness). No single split.
3. NO LOOKAHEAD IN FEATURES. Every feature ts_ns < label ts_ns. Feature store enforces; verify in tests.
4. AGENT IS A PROCESS. Subscribes to streams; publishes Predictions/SentimentSignals/RegimeSignals.
5. STATIONARITY CHECKS. Feature distributions stable over rolling windows; alert on drift.
6. THIS IS THE BASELINE. Phase X must beat THIS phase's ensemble. Don't be sloppy here.

CONTEXT: spec/CONTRACTS.md §3. Tasks: TASK-030..033.

Acknowledge by listing the 6 rules. Wait.
```
### ▲ PASTE END

## TASK-030 — Agent base class

### ▼ PASTE START
```text
TASK-030 — Agent base class.

File: services/agents/src/agents/base.py.

AgentBase ABC: __init__(name, version, redis, db); async setup(), on_event(event), run(). Subscribes via fincept-bus consumer-group (one group per agent name). Publishes via producer. Heartbeat to events.heartbeats every 5s. Graceful shutdown on SIGTERM/SIGINT. Idempotent on duplicate events. Cost-tracking helper logs every external call.

LANDMINES:
- One agent process = one consumer-name. Multiple instances of same agent share work (stateless OK; stateful needs leader election).
- Don't catch CancelledError.

DONE WHEN:
- pytest green: agent receives event, processes, publishes result.
- mypy --strict clean.
- spec/tasks/TASK-030-agent-base.md authored.

VERIFY: uv run pytest services/agents/tests/test_base.py -v
REPORT.
```
### ▲ PASTE END

## TASK-031 — gbm_predictor (LightGBM)

### ▼ PASTE START
```text
TASK-031 — gbm_predictor: LightGBM directional model.

Files: services/agents/src/agents/gbm_predictor/{features,trainer,inference,main}.py.

Features: returns at multiple horizons, vol regimes, microstructure — PIT via TASK-017. Trainer (offline, runs in services/jobs/): LightGBM with early stopping, walk-forward validation via TASK-023, saves to MLflow. Inference (subclasses AgentBase): on every new bar, predict probability of up move next horizon, emit Prediction. Calibrate via Platt or isotonic.

LANDMINES:
- Labels: forward-N-bar returns net of costs. Including costs in the label is critical.
- Confidence MUST be calibrated. Reliability diagram on validation; binned predictions match realized hit rate.
- Feature drift: log mean+std in production; alert >3σ vs training.
- LightGBM determinism: deterministic=True, num_threads=1.

DONE WHEN:
- pytest green.
- Walk-forward OOS directional acc ≥52%, p<0.05 on ≥1 in-universe symbol.
- Reliability diagram monotonic across 5 confidence buckets.
- Inference emits Predictions on every bar.
- spec/tasks/TASK-031-gbm-predictor.md authored.

VERIFY:
  uv run pytest services/agents/tests/test_gbm_predictor.py -v
  uv run python -m agents.gbm_predictor.trainer --symbol BTCUSDT --start 2023-01-01 --end 2024-12-31
  uv run python -m agents.gbm_predictor.main &
  sleep 600
  redis-cli XLEN sig.predict   # > 0
REPORT. CONTRACTS: §3 (Prediction).
```
### ▲ PASTE END

## TASK-032 — regime (HMM)

### ▼ PASTE START
```text
TASK-032 — regime: HMM-based regime detector.

Files: services/agents/src/agents/regime/{features,model,main}.py.

Features: realized vol, trend strength (z-score of MA distance), volume regime. Model: Gaussian HMM (hmmlearn) K=4 hidden states, trained offline, saved to MLflow. Inference: on every new bar, infer state via online forward pass, emit RegimeSignal.

LANDMINES:
- HMM state ordering arbitrary; map states to labels by feature-mean inspection.
- HMMs flicker; smoothing layer requires ≥5 consecutive bars in new state before emitting transition.
- Re-train monthly; verify state-to-label mapping stable.

DONE WHEN:
- pytest green.
- Manual inspection: 3 historical transitions (e.g., Mar 2020 crash, late-2022 BTC bear, 2024 rate-cut rally) labeled correctly.
- spec/tasks/TASK-032-regime.md authored.

VERIFY:
  uv run pytest services/agents/tests/test_regime.py -v
  uv run python -m agents.regime.train --symbol SPY --start 2015-01-01 --end 2024-12-31
  uv run python -m agents.regime.main &
REPORT. CONTRACTS: §3 (RegimeSignal).
```
### ▲ PASTE END

## TASK-033 — pairs (cointegration)

### ▼ PASTE START
```text
TASK-033 — pairs: cointegration pairs trading.

Files: services/agents/src/agents/pairs/{coint,strategy,main}.py.

coint: Engle-Granger or Johansen test on rolling window; selects cointegrated pairs from candidate set. strategy: z-score of spread; entry |z|>2, exit z crosses 0.5, stop |z|>4 OR coint p>0.1. Main: candidates from config.

LANDMINES:
- Cointegration unstable; in-sample p<0.05 → OOS p>0.5 common. Walk-forward mandatory.
- Hedge ratio: rolling re-fit (60-day window). Don't freeze.
- Costs eat pairs P&L fast; tight cost model required.

DONE WHEN:
- pytest green.
- Walk-forward OOS Sharpe>0.5 on ≥2 pairs (e.g., GLD/SLV, KO/PEP).
- spec/tasks/TASK-033-pairs.md authored.

VERIFY:
  uv run pytest services/agents/tests/test_pairs.py -v
  uv run python -m agents.pairs.backtest --pair GLD,SLV --start 2020-01-01 --end 2024-12-31
REPORT. CONTRACTS: §3.
```
### ▲ PASTE END

## Phase A — Exit verification

### ▼ PASTE START
```text
PHASE A EXIT.

CHECKLIST:
1. gbm_predictor OOS directional acc ≥52%, p<0.05 (walk-forward).
2. gbm_predictor reliability diagram monotonic.
3. regime model labels 3 historical transitions correctly.
4. pairs OOS Sharpe>0.5 on ≥2 cointegrated pairs.
5. All three agents emit during 1-hour live ingestion run.
6. Drift monitor: gbm_predictor feature distributions logged; no drift alerts in validation period.
7. mypy --strict clean.
8. TASK-030..033 specs exist; [x] in BUILD_ORDER.md.

If green: Phase A COMPLETE. Add "Checkpoint A1: passed YYYY-MM-DD". Proceed to Phase O.

REPORT.
```
### ▲ PASTE END

---

# Phase O — Orchestrator + Risk + OMS

**Goal:** End-to-end paper trading: signal → consensus → risk → order → fill → position.
**Checkpoint:** End-to-end paper trade for one strategy works with full audit trail reconstructable from `ord.*` streams.

## Phase O — Kickoff

### ▼ PASTE START
```text
ENTERING PHASE O — ORCHESTRATOR + RISK + OMS.

Session opener norms apply. Phases F, D, B, A complete. Phase-specific rules:

1. SINGLETONS. Orchestrator, Risk, OMS, Portfolio: exactly-one-instance per cluster. Two = double-trades = ruin. Leader election (Redis SETNX with TTL or etcd).
2. KILL SWITCH IS SACRED. Works even when rest of system is degraded. Latency from button to halt < 3 seconds.
3. PRE-TRADE RISK GATE BLOCKS BAD DECISIONS. Position limits, notional limits, VaR limits, drawdown circuit breakers, kill_switch state — all checked before Order leaves OMS.
4. PAPER FIRST, ALWAYS. TRADING_MODE=paper unless Phase H gates pass. Live = denied at startup if Phase H incomplete.
5. AUDIT TRAIL IS PRODUCTION-GRADE. Every Decision/Order/Fill → audit_log. From this stream alone, reconstruct any trade end-to-end.
6. FAIL-CLOSED. If risk checks fail or audit can't be written, REJECT. Never fill open with degraded checks.

CONTEXT: spec/CONTRACTS.md §4, §5, §7. Tasks: TASK-040..045.

Acknowledge by listing the 6 rules. Wait.
```
### ▲ PASTE END

## TASK-040 — Orchestrator (consensus + decisions)

### ▼ PASTE START
```text
TASK-040 — Orchestrator: leader, router, regime weighting, consensus, allocator, decisions.

Files: services/orchestrator/src/orchestrator/{leader,router,regime,consensus,allocator,decisions}.py.

leader: Redis SETNX with TTL renewal (default 10s TTL, 3s renew). Standbys idle. router: consumes sig.predict/sentiment/regime, fans to per-symbol consensus. regime: consumes RegimeSignal, computes per-strategy weight modulation. consensus: combines signals into per-symbol composite (z-score normalize each source, weighted sum). allocator: composite → target notional with max-position constraints. decisions: emits Decision events to ord.decisions.

LANDMINES:
- Leader TTL too short → flapping; too long → slow recovery.
- Stale signals: drop signals older than max_age_ns (default 60s for sig.predict).
- Composite score: z-score-normalize sources first, else larger-scale sources dominate.

DONE WHEN:
- pytest green; singleton test (kill leader → standby promotes <15s, no double-trades).
- E2E synthetic-signal → expected Decision.
- mypy --strict clean.
- spec/tasks/TASK-040-orchestrator.md authored.

VERIFY: uv run pytest services/orchestrator/tests/ -v
REPORT. CONTRACTS: §3, §4.
```
### ▲ PASTE END

## TASK-041 — Risk gate + kill switch

### ▼ PASTE START
```text
TASK-041 — Risk gate + kill switch.

Files: services/risk/src/risk/{limits,gate,kill_switch}.py.

limits: per-symbol max position, per-account gross/net notional, per-day max loss, max drawdown, per-venue per-asset-class limits. gate: consumes ord.decisions; applies limits + sizing (TASK-042) + var (TASK-043) + kill_switch state; emits ord.orders if approved or rejection AlertEvent if denied. kill_switch: persistent state in db.kill_switch; activated via API or UI; once active ALL Decisions rejected; clearing requires admin approval logged in audit.

LANDMINES:
- Fail-closed on every check: if check raises, REJECT.
- Kill switch state: in-memory cache + db sync on every change. Read from db on startup.
- Drawdown circuit breaker: daily P&L < -MAX_DAILY_LOSS_USD → auto-activate kill switch.
- Audit every approve AND reject with reason + applied limit values.

DONE WHEN:
- pytest green per limit + chain.
- Kill switch latency: API call → next Decision rejected < 1s.
- Drawdown auto-trigger tested with synthetic P&L feed.
- spec/tasks/TASK-041-risk-gate.md authored.

VERIFY: uv run pytest services/risk/tests/ -v
REPORT. CONTRACTS: §4, §7.
```
### ▲ PASTE END

## TASK-042 — Kelly sizing (correlated assets)

### ▼ PASTE START
```text
TASK-042 — Kelly-optimal sizing (correlated-assets variant).

File: services/risk/src/risk/kelly.py.

KellySizer: takes (edge, vol, correlation matrix, current positions) → optimal fraction. Solves constrained QP: max growth subject to gross/net limits. Defaults to fractional Kelly (0.25× full Kelly).

LANDMINES:
- Full Kelly is too aggressive for noisy edges. ALWAYS fractional (0.25–0.5×).
- Correlation matrix must be PSD; use Ledoit-Wolf shrinkage.
- Edge estimate from noisy signals dominates error; shrink edge estimate too.

DONE WHEN:
- pytest green: known-answer 2-asset uncorrelated case + correlation reduction case.
- spec/tasks/TASK-042-kelly.md authored.

VERIFY: uv run pytest services/risk/tests/test_kelly.py -v
REPORT.
```
### ▲ PASTE END

## TASK-043 — Real-time VaR

### ▼ PASTE START
```text
TASK-043 — Real-time Value-at-Risk.

File: services/risk/src/risk/var.py.

VarCalculator: parametric VaR (variance-covariance, normal); historical VaR (1-day, 99%, 252-day rolling); expected shortfall (CVaR) at 99%. Updates on every Fill via consumer. Persists to db.var_metrics_minute.

LANDMINES:
- Parametric underestimates tail risk during regime shifts; always report alongside historical.
- Crypto P&L is fat-tailed; report 99.5% in addition to 99%.
- Refit weekly minimum; don't use stale variance estimates.

DONE WHEN:
- pytest green.
- Live test: 1 hour of paper fills updates VaR plausibly.
- spec/tasks/TASK-043-var.md authored.

VERIFY: uv run pytest services/risk/tests/test_var.py -v
REPORT.
```
### ▲ PASTE END

## TASK-044 — Paper OMS

### ▼ PASTE START
```text
TASK-044 — Paper OMS (singleton fill simulator using live mid + random latency).

Files: services/oms/src/oms/{main,state,venue/base,paper,audit}.py.

main: leader election; consumes ord.orders. state: in-memory order book by client_order_id, persisted to db.orders for crash recovery. VenueAdapter ABC: submit_order, cancel_order, on_fill. PaperVenueAdapter: subscribes to live md.quotes; market orders fill at mid + random 50–150ms latency; limit orders fill when next quote crosses. audit: every action → audit_log.

LANDMINES:
- Idempotency on client_order_id: same id twice → second rejected.
- Crash recovery: on startup, reconcile open orders from db with live state.
- Paper adapter MUST use real live quotes; without TASK-011 ingestor, paper P&L is fictional.
- TRADING_MODE=live rejected at startup until Phase H complete.

DONE WHEN:
- pytest green.
- E2E: market order → Fill within 200ms; positions updated.
- Singleton test passes.
- Audit reconstruction: replay any order's full history from audit_log alone.
- spec/tasks/TASK-044-paper-oms.md authored.

VERIFY: uv run pytest services/oms/tests/ -v
REPORT. CONTRACTS: §4, §7.
```
### ▲ PASTE END

## TASK-045 — Portfolio service

### ▼ PASTE START
```text
TASK-045 — Portfolio service (positions, P&L, attribution).

Files: services/portfolio/src/portfolio/{main,positions,pnl,attribution}.py.

main: leader election. positions: consumes ord.fills; updates positions; emits position.update. pnl: realized + unrealized per position/strategy/account; recomputes on every mark. attribution: per-strategy / per-signal-source / per-symbol; persists to db.pnl_attribution.

LANDMINES:
- Marks: bid/ask mid for unrealized; trade-side for realized.
- Average-cost by default; document.
- Attribution proportional to signal weights when multiple signals contribute.
- P&L per-currency; convert to USD at marks for display, never lose native.

DONE WHEN:
- pytest green.
- E2E: order → fill → position updated → P&L matches expected.
- Attribution: 2 strategies trade same symbol → attribution sums to total P&L.
- spec/tasks/TASK-045-portfolio.md authored.

VERIFY: uv run pytest services/portfolio/tests/ -v
REPORT.
```
### ▲ PASTE END

## Phase O — Exit verification

### ▼ PASTE START
```text
PHASE O EXIT.

CHECKLIST:
1. E2E paper-trade: synthetic signal → Decision → Risk approve → Order → Fill → Position → P&L → audit_log. Every link traces cleanly.
2. Singleton: kill leader of orchestrator/risk/oms/portfolio; standby promotes <15s; no double-trades during transition.
3. Kill switch latency: button-press → next decision rejected <3s.
4. Drawdown auto-trigger: synthetic loss feed crosses MAX_DAILY_LOSS; kill switch activates automatically.
5. Audit reconstruction: random fill → full chain (signal → decision → order → fill → position) reconstructable from audit_log.
6. mypy --strict clean across all four services.
7. TASK-040..045 specs exist; [x] in BUILD_ORDER.md.

If green: Phase O COMPLETE. Add "Checkpoint O: passed YYYY-MM-DD". Proceed to Phase U.

REPORT.
```
### ▲ PASTE END

---

# Phase U — UI + API

**Goal:** Operator dashboard. Sign in, watch P&L update at 10 Hz, start/stop strategies, hit kill switch in <3s.
**Checkpoint:** Operator can sign in, see live P&L update at 10 Hz, start/stop a strategy, and trigger kill switch in under 3 seconds.

## Phase U — Kickoff

### ▼ PASTE START
```text
ENTERING PHASE U — UI + API.

Session opener norms apply. Phases F, D, B, A, O complete. Phase-specific rules:

1. THE UI IS A READ MODEL. Never makes the trading decision. Displays state and emits commands (start/stop/kill). Reads P&L from services/portfolio; never computes it.
2. TYPED FROM TOP TO BOTTOM. FastAPI generates OpenAPI; dashboard consumes via openapi-typescript. No hand-rolled API types.
3. WEBSOCKETS FOR LIVE DATA. REST for control.
4. AUTH ON EVERY ROUTE. JWT bearer (never query strings); refresh-token rotation; logging includes user_id but not the token.
5. KILL SWITCH IS PROMINENT AND FAST. Big red button, <3s click→halt, visible from every page.
6. MOBILE-FRIENDLY DEGRADES GRACEFULLY. Desktop primary; mobile shows positions + kill switch + alerts.
7. NO BUSINESS LOGIC IN UI. If UI needs something API can't supply, ADD an endpoint. Never compute trading state in JS.

CONTEXT: spec/CONTRACTS.md §9 (API), §10 (WS protocol). spec/LAYOUT.md §services/api, §apps/dashboard.

Tasks: TASK-050..057.

Acknowledge by listing the 7 rules. Wait.
```
### ▲ PASTE END

## TASK-050 — FastAPI app + auth

### ▼ PASTE START
```text
TASK-050 — FastAPI app + JWT auth + OpenAPI.

Files: services/api/src/api/{main,auth,deps,routes/*}.py.

main: FastAPI app; OpenAPI generation; CORS for localhost dev. auth: JWT (access + refresh), bcrypt password hashing, per-user roles (operator, admin, readonly). deps: dependencies for current_user, db_session, redis_client. routes/: stub for /health, /me, /universe, /positions, /strategies, /risk, /alerts.

LANDMINES:
- JWT secret in env var, never committed.
- Refresh-token rotation (single-use refresh tokens).
- Per-user rate limiting via slowapi.
- CORS production config = explicit allowed origins, never *.

DONE WHEN:
- pytest green: auth flow (login → access → refresh → logout).
- OpenAPI spec at /openapi.json validates.
- mypy --strict clean.
- spec/tasks/TASK-050-api.md authored.

VERIFY:
  uv run pytest services/api/tests/ -v
  uv run uvicorn api.main:app &
  curl http://localhost:8000/health
REPORT. CONTRACTS: §9.
```
### ▲ PASTE END

## TASK-051 — WebSocket streaming

### ▼ PASTE START
```text
TASK-051 — WebSocket /ws live-state streaming.

File: services/api/src/api/ws.py.

Single multiplexed WebSocket endpoint /ws. Auth via Sec-WebSocket-Protocol header containing Bearer JWT. Subscribe-message protocol: {"type": "subscribe", "topics": ["positions", "fills", "predictions", "alerts"]}. Backend reads from Redis streams, fans to subscribed clients. 10Hz throttling on per-client basis.

LANDMINES:
- Auth on connect, NOT on every message (handshake-only).
- Backpressure: per-client send queue capped at 100; drop and disconnect on overflow.
- Reconnect: client provides last seen event_id; server replays from there (within window).
- No global broadcasts; every client filters its own subscription.

DONE WHEN:
- pytest green using websockets test client.
- Soak: 100 simultaneous clients for 10 minutes; no leaks; p99 latency event-publish→client-receive <100ms.
- spec/tasks/TASK-051-ws.md authored.

VERIFY: uv run pytest services/api/tests/test_ws.py -v
REPORT. CONTRACTS: §10.
```
### ▲ PASTE END

## TASK-052 — Next.js shell + auth

### ▼ PASTE START
```text
TASK-052 — Next.js dashboard shell + auth flow.

Files: apps/dashboard/{app/layout.tsx,app/login/page.tsx,app/(protected)/layout.tsx,lib/api/client.ts,lib/api/auth.ts}.

Next.js 14 app-router; TypeScript strict. Auth flow: /login posts to FastAPI; access token in memory + refresh in httpOnly cookie. Protected layout wraps all auth-required pages. API client uses openapi-typescript-generated types; never hand-rolled. Tailwind + shadcn/ui for components.

LANDMINES:
- Access token in memory (not localStorage; XSS risk).
- Refresh token httpOnly + Secure + SameSite=strict.
- 401 on API → silent refresh once, then redirect to /login on second 401.
- Don't import server-only modules in client components.

DONE WHEN:
- Login flow works against running API.
- Protected route redirects to /login when unauthenticated.
- pnpm typecheck + pnpm test green.
- spec/tasks/TASK-052-dashboard-shell.md authored.

VERIFY:
  cd apps/dashboard
  pnpm typecheck
  pnpm test
  pnpm dev   # manual smoke against running API
REPORT.
```
### ▲ PASTE END

## TASK-053 — Positions + P&L panel

### ▼ PASTE START
```text
TASK-053 — Positions table + P&L sparkline panel.

Files: apps/dashboard/app/(protected)/positions/page.tsx + components/positions/{table,pnl-chart}.tsx.

Subscribes via /ws to positions + fills topics. Renders sortable/filterable table: symbol, qty, avg cost, mark, unrealized P&L, realized P&L today. Sparkline of cumulative P&L last 60 minutes. Right-click row → context menu (close-position, view audit).

LANDMINES:
- 10Hz updates require virtualization (react-window) for >50 rows; otherwise renders thrash.
- Decimal precision: render via fincept-sdk-ts utility, not parseFloat (rounding bugs).
- Sparkline: aggregate at 1s buckets server-side; don't push every fill to chart.

DONE WHEN:
- E2E test (Playwright): login → positions page → see synthetic position update at 10Hz.
- Pixel smoke screenshot.
- spec/tasks/TASK-053-positions.md authored.

VERIFY: pnpm test apps/dashboard/test/positions.spec.ts
REPORT.
```
### ▲ PASTE END

## TASK-054 — Strategy control panel

### ▼ PASTE START
```text
TASK-054 — Strategy control panel (start/stop/pause + risk allocation per strategy).

Files: apps/dashboard/app/(protected)/strategies/page.tsx + services/api/routes/strategies.py.

API: GET /strategies, POST /strategies/{id}/start, /stop, /pause, PATCH /strategies/{id}/allocation. UI: card per strategy with status, current allocation %, today's P&L, button row. Confirmation modal on start/stop/pause with reason field (logged to audit).

LANDMINES:
- All control actions require operator role minimum.
- Reason field is mandatory and logged to audit_log.
- Optimistic UI updates ARE OK as long as we revert on API error.

DONE WHEN:
- E2E: start a stopped strategy → status updates within 5s.
- Audit log shows the reason from the modal.
- spec/tasks/TASK-054-strategy-control.md authored.

VERIFY:
  uv run pytest services/api/tests/test_strategies.py -v
  pnpm test apps/dashboard/test/strategies.spec.ts
REPORT.
```
### ▲ PASTE END

## TASK-055 — Live chart

### ▼ PASTE START
```text
TASK-055 — Live multi-pane chart with bars + features + signals overlay.

Files: apps/dashboard/components/chart/{chart,panes,overlays}.tsx + services/api/routes/chart.py.

API: /chart/{symbol}?interval=1m&range=4h returns bars + selected features + recent Predictions/SentimentSignals/Decisions. UI: lightweight-charts (TradingView) main pane + indicators sub-panes + signal markers (predictions = arrows, decisions = triangles).

LANDMINES:
- Don't push bar updates via WS for active chart range; let client poll /chart at 5s for ranges >1h. Real-time only for the latest bar.
- Signal markers can crowd the chart; aggregate within 30s windows above N markers/window.

DONE WHEN:
- Chart renders BTCUSDT 1m bars with Predictions overlaid.
- Performance: pan/zoom on 4h of 1m bars (240 candles) is smooth.
- spec/tasks/TASK-055-chart.md authored.

VERIFY:
  uv run pytest services/api/tests/test_chart.py -v
  pnpm test apps/dashboard/test/chart.spec.ts
REPORT.
```
### ▲ PASTE END

## TASK-056 — Command palette

### ▼ PASTE START
```text
TASK-056 — Bloomberg-style command palette (cmd-K).

Files: apps/dashboard/components/palette/{palette,parser,commands}.tsx + services/api/routes/palette.py.

Cmd-K opens a fuzzy command palette. Commands: navigate (positions/strategies/risk/alerts), actions (kill-switch, start-strategy <id>), queries (price <symbol>, position <symbol>, pnl today/week/month). Parser handles unambiguous abbreviations (POS = position, STR = strategy).

LANDMINES:
- Destructive commands (kill-switch, stop-all) require typed confirmation (type "CONFIRM").
- Don't parse natural language with an LLM here; keep it deterministic.

DONE WHEN:
- E2E: cmd-K → "kill" → confirm → kill switch active <3s.
- Palette closes on Esc; opens on cmd-K from any route.
- spec/tasks/TASK-056-palette.md authored.

VERIFY: pnpm test apps/dashboard/test/palette.spec.ts
REPORT.
```
### ▲ PASTE END

## TASK-057 — Risk panel + kill switch UI

### ▼ PASTE START
```text
TASK-057 — Risk panel (limits, VaR, drawdown) + prominent kill-switch button.

Files: apps/dashboard/app/(protected)/risk/page.tsx + components/risk/{limits,var,kill}.tsx + services/api/routes/risk.py.

Risk page: current usage of every limit (gross, net, per-symbol), realized vs limit; VaR/CVaR over time; daily drawdown chart. Kill-switch button: persistent header, big red, requires "type CONFIRM to engage" modal, 1-click to engage from confirm.

LANDMINES:
- Kill-switch availability: even if API is down, the button must work via direct WS message to risk service.
- Kill-switch state must be visible from EVERY page (not just /risk).
- Recovery from kill: requires admin role; reason logged.

DONE WHEN:
- Kill switch latency: button click → next decision rejected <3s (E2E).
- Limit-usage page accurate against synthetic positions.
- spec/tasks/TASK-057-risk-ui.md authored.

VERIFY:
  uv run pytest services/api/tests/test_risk.py -v
  pnpm test apps/dashboard/test/risk.spec.ts
  pnpm test apps/dashboard/test/kill-switch.spec.ts
REPORT.
```
### ▲ PASTE END

## Phase U — Exit verification

### ▼ PASTE START
```text
PHASE U EXIT.

CHECKLIST:
1. Operator can sign in via /login with valid credentials.
2. Positions page renders live updates at 10Hz over WS.
3. Strategy control: start/stop/pause works end-to-end with audit logging of reason.
4. Live chart renders 4h of 1m bars with signal overlays.
5. Cmd-K palette opens from any page; "kill" command engages kill switch <3s.
6. Kill-switch button on risk page engages <3s from click → next decision rejected.
7. Mobile breakpoint: positions + kill switch render correctly on a 375px-wide viewport.
8. Playwright E2E suite green.
9. mypy --strict clean on services/api; pnpm typecheck clean on apps/dashboard.
10. TASK-050..057 specs exist; [x] in BUILD_ORDER.md.

If green: Phase U COMPLETE. Add "Checkpoint U: passed YYYY-MM-DD". Proceed to Phase X.

REPORT.
```
### ▲ PASTE END

---

# Phase X — Cutting Edge

**Goal:** Heavy AI/ML — vector memory, time-series foundation models, LLM sentiment, ensemble orchestration. Optional gradient on top of MVP.
**Checkpoint:** 4-week shadow ensemble (gbm + ts-foundation + llm-sentiment + regime + pairs) Sharpe ≥ Phase A baseline + 0.5; no agent crashes the orchestrator; total LLM cost per trading day ≤ $50 in MVP universe.

## Phase X — Kickoff

### ▼ PASTE START
```text
ENTERING PHASE X — CUTTING EDGE.

Session opener norms apply. Phases F, D, B, A, O, U complete. Phase-specific rules:

1. EVERY MODEL HAS AN EVAL SUITE BEFORE IT GOES LIVE. No exceptions. Eval before training, eval after each model swap.
2. CALIBRATION OVER ACCURACY. Reliability diagrams are mandatory.
3. COST IS PRODUCT WORK. LLM tokens cost real money. Track per-call. Cap budgets. Fail closed on budget exceed.
4. STRUCTURED OUTPUTS ONLY. Every LLM call uses tool-use API or JSON mode. NEVER parse free-text.
5. SHADOW BEFORE LIVE. Phase X agents enter the orchestrator with weight=0 and shadow ≥ 4 weeks before non-zero weight.
6. CACHE AGGRESSIVELY. Vector memory (TASK-060) for semantic dedup. Same news article → same embedding → cached extraction.
7. ENSEMBLE > BEST-MODEL. The point of Phase X is the ensemble outperforming any single agent; not finding the magical agent.

CONTEXT: spec/CONTRACTS.md §3, §8 (tools). spec/EDGE_ROADMAP.md §3 (Phase X intent).

Tasks: TASK-060..066.

Acknowledge by listing the 7 rules. Wait.
```
### ▲ PASTE END

## TASK-060 — Vector memory layer

### ▼ PASTE START
```text
TASK-060 — Vector memory (Qdrant + embeddings) for semantic dedup + retrieval.

Files: services/agents/src/agents/memory/{client,embedder,store}.py.

Qdrant collection per data type (news, tweets, transcripts). Embedder: sentence-transformers/all-MiniLM-L6-v2 default; configurable. Store: upsert(text, metadata, ts_ns) → vector_id; search(text, top_k, filters) → [(score, metadata)]. Used by sentiment + transcript agents to dedup and to retrieve similar past examples for few-shot.

LANDMINES:
- Embedding model versioning: changing the model invalidates the index. Track model_version in metadata; on model change, re-embed.
- Cost: embedding 1000 articles/day at $0.0001 each = trivial; LLM extraction is the dominant cost.
- Filter precision: Qdrant filters are precise; the score is approximate. Filter first, then score.

DONE WHEN:
- pytest green: roundtrip upsert + search.
- 10k-doc index, p99 query latency <50ms.
- spec/tasks/TASK-060-vector-memory.md authored.

VERIFY: uv run pytest services/agents/tests/test_memory.py -v
REPORT.
```
### ▲ PASTE END

## TASK-061 — Time-series foundation model

### ▼ PASTE START
```text
TASK-061 — Time-series foundation model agent (Chronos / Lag-Llama / TimesFM).

Files: services/agents/src/agents/ts_foundation/{main,model,inference}.py.

Choose ONE foundation model (Amazon Chronos preferred, on HuggingFace). Wraps it as an Agent: on every new bar, runs zero-shot forecast for next N bars; emits Prediction with quantile bands (p10, p50, p90) → confidence from spread.

LANDMINES:
- Foundation models are LARGE. GPU recommended; CPU inference works for 1m bars but >100ms latency.
- Confidence from quantile spread: tighter band = higher confidence. Calibrate via reliability diagram on held-out validation period.
- Foundation models are generic; they may not capture asset-specific microstructure. Use ALONGSIDE gbm_predictor (TASK-031), not instead of.
- License: Chronos is Apache-2.0; verify before deploying.

DONE WHEN:
- pytest green.
- Walk-forward validation on BTCUSDT 1m: directional accuracy ≥ 51% (foundation model is more useful for confidence calibration than raw accuracy).
- Reliability diagram monotonic.
- spec/tasks/TASK-061-ts-foundation.md authored.

VERIFY:
  uv run pytest services/agents/tests/test_ts_foundation.py -v
  uv run python -m agents.ts_foundation.main &
REPORT. CONTRACTS: §3 (Prediction).
```
### ▲ PASTE END

## TASK-062 — LLM news sentiment

### ▼ PASTE START
```text
TASK-062 — LLM-based news sentiment agent.

Files: services/agents/src/agents/llm_sentiment/{main,fetcher,extractor,eval/cases.jsonl}.

Fetcher: pulls news from configured sources (RSS + APIs). Extractor: LLM (Claude or GPT-4o) with structured output schema {symbol, sentiment_score, confidence, key_phrases}. Dedup via TASK-060 vector memory before LLM call. Emits SentimentSignal.

LANDMINES:
- HALLUCINATION CONTROL: LLM must validate symbol against universe via fincept-tools entity.resolve. Drop signals where the LLM emits an out-of-universe symbol.
- COST: every article through LLM = $0.001-$0.01. 10k articles/day = $10-$100. Vector dedup cuts this 50–80%.
- LATENCY: < 30s from article fetch to SentimentSignal emission (end-to-end).
- EVAL SUITE FIRST: 100 hand-labeled headlines. Re-eval on every model/prompt change.
- LICENSE: news APIs vary; respect terms of use. Cache responses.

DONE WHEN:
- pytest green.
- Eval: macro F1 ≥ 0.7 on 100-headline labeled set.
- Live test: 1 hour of news ingestion produces SentimentSignals; cost < $1/hr.
- spec/tasks/TASK-062-llm-sentiment.md authored.

VERIFY:
  uv run pytest services/agents/tests/test_llm_sentiment.py -v
  uv run python -m agents.llm_sentiment.eval --cases ./eval/cases.jsonl
REPORT. CONTRACTS: §3 (SentimentSignal).
```
### ▲ PASTE END

## TASK-063 — Anomaly detector

### ▼ PASTE START
```text
TASK-063 — Multivariate anomaly detector (Isolation Forest + autoencoder).

Files: services/agents/src/agents/anomaly/{main,model,features}.py.

Features: vol of vol, spread anomaly, volume z-score, cross-asset correlation breaks. Model ensemble: Isolation Forest (fast, online) + autoencoder reconstruction error (deeper, batch). Anomaly score → AlertEvent (severity by score). Used as risk gate input (TASK-041) for de-leveraging.

LANDMINES:
- Anomalies are 1% of data. Class imbalance kills naive metrics; use precision@K.
- Online retrain monthly; daily evaluation against labeled "expected anomalous" days.
- False positives are expensive (de-leverage at the wrong time). Tune threshold for high precision.

DONE WHEN:
- pytest green.
- Backtest: anomaly detector flags ≥80% of known stress days (Mar 2020, May 2010 flash crash, etc.) with precision ≥0.5.
- spec/tasks/TASK-063-anomaly.md authored.

VERIFY: uv run pytest services/agents/tests/test_anomaly.py -v
REPORT. CONTRACTS: §3 (AlertEvent).
```
### ▲ PASTE END

## TASK-064 — LLM decision-loop wrapper

### ▼ PASTE START
```text
TASK-064 — LLM decision-loop integration.

Files: services/orchestrator/src/orchestrator/llm_loop.py + prompts/decision.md.

After numerical orchestrator (TASK-040) computes a candidate Decision, optionally route to an LLM for sanity-check + rationale. Tool-use enabled (LLM can call entity.resolve, get_positions, get_recent_news). Output: approve/modify/reject + 1-paragraph rationale.

LANDMINES:
- LLM CANNOT BE THE PRIMARY DECIDER. Numerical orchestrator decides; LLM only sanity-checks. If LLM disagrees, log and proceed UNLESS the LLM rejects with high-confidence catastrophic-risk reasoning.
- COST PER DECISION: $0.01-$0.05. Multiply by decision rate (e.g., 100/day) → $1–$5/day. Acceptable.
- LATENCY: p99 < 1.5s end-to-end. Cache on (signal-hash, portfolio-hash, 60s).
- FALLBACK: any LLM error → numerical-only Decision + alert.

DONE WHEN:
- pytest green.
- A/B harness: 50% LLM-checked vs 50% pure-numerical. After 4 weeks of shadow, LLM-checked Decisions show ≥ 5% improvement in calibration without Sharpe degradation.
- spec/tasks/TASK-064-llm-loop.md authored.

VERIFY: uv run pytest services/orchestrator/tests/test_llm_loop.py -v
REPORT. CONTRACTS: §4.
```
### ▲ PASTE END

## TASK-065 — Reinforcement learning execution

### ▼ PASTE START
```text
TASK-065 — RL-based order execution agent (TWAP/VWAP-aware).

Files: services/oms/src/oms/execution/{rl_agent,environment,replay}.py.

PPO agent that learns to slice a parent order into child orders to minimize implementation shortfall. Environment: simulated LOB from historical L2 data + cost model. Reward: -implementation_shortfall. Slow path: trains offline on weeks of L2 history. Hot path: serves predictions in <10ms.

LANDMINES:
- L2 data is required; if not yet ingested, this task waits.
- Sim-to-real gap is the dominant risk; deploy in shadow first, compare RL fills vs naive TWAP fills on the same parent orders.
- Reward shaping: pure shortfall is sparse; add intermediate dense reward (queue position, fill rate).

DONE WHEN:
- pytest green.
- Shadow: 4 weeks side-by-side vs TWAP. RL fills ≥ TWAP on average implementation shortfall (lower is better).
- spec/tasks/TASK-065-rl-execution.md authored.

VERIFY: uv run pytest services/oms/tests/test_rl_execution.py -v
REPORT.
```
### ▲ PASTE END

## TASK-066 — Adaptive ensemble orchestrator

### ▼ PASTE START
```text
TASK-066 — Adaptive ensemble weights for orchestrator.

File: services/orchestrator/src/orchestrator/adaptive.py (extends TASK-040 consensus).

Replaces fixed signal weights with regime- and recency-adaptive weights. Per-strategy rolling Sharpe → weight modulation. Per-regime weight maps (learned from historical regime-conditional Sharpes). Decay: stale strategies down-weighted automatically.

LANDMINES:
- Adaptive weights have low signal-to-noise; smoothing is essential (EMA on weights, not raw Sharpe).
- Cold start for new agents: default weight=0 for first 4 weeks of shadow.
- Regime miss-classification cascades into miss-weighting; calibrate the regime classifier well first.

DONE WHEN:
- pytest green.
- Shadow: ensemble Sharpe ≥ best individual agent + 0.3.
- spec/tasks/TASK-066-adaptive-ensemble.md authored.

VERIFY: uv run pytest services/orchestrator/tests/test_adaptive.py -v
REPORT.
```
### ▲ PASTE END

## Phase X — Exit verification

### ▼ PASTE START
```text
PHASE X EXIT.

CHECKLIST:
1. 4-week shadow ensemble (gbm + ts_foundation + llm_sentiment + regime + pairs + anomaly): Sharpe ≥ Phase A baseline + 0.5.
2. Block-bootstrap p < 0.10 on the Sharpe improvement.
3. No agent crashed the orchestrator during the 4-week shadow (max one graceful restart).
4. Total LLM cost per trading day ≤ $50 in MVP universe.
5. Reliability diagrams for every Prediction-emitting agent (gbm, ts_foundation) are monotonic.
6. Eval suites for llm_sentiment + any LLM-using component are checked into the repo and run on every model swap.
7. mypy --strict clean across all Phase X services.
8. TASK-060..066 specs exist; [x] in BUILD_ORDER.md.

If green: Phase X COMPLETE. Add "Checkpoint X: passed YYYY-MM-DD". Phase H may now begin (and Phase X+ may begin in parallel once Phase X is checkpointed).

If shadow Sharpe < baseline + 0.5: Phase X is partially validated. Decide: iterate further OR accept the simpler Phase A baseline as the production ensemble. Do NOT scale capital based on partial Phase X.

REPORT.
```
### ▲ PASTE END

---

# Phase H — Hardening (path to live)

**Goal:** Production-grade resilience, security, observability. The gates between paper and live capital.
**Checkpoint:** SOC-2-style internal audit passes; DR drill recovers in <30 minutes; chaos suite green; live trading connector tested in sandbox; runbook covers top 20 incident classes.

## Phase H — Kickoff

### ▼ PASTE START
```text
ENTERING PHASE H — HARDENING.

Session opener norms apply. Phases F, D, B, A, O, U, X complete. Phase-specific rules:

1. ONE-WAY DOOR. Every Phase H change makes the system more conservative, more observable, more recoverable. Never the reverse.
2. CHAOS BEFORE LIVE. Network partitions, OOMs, broker disconnects, exchange halts — all simulated and survived BEFORE live capital touches the system.
3. SECRETS ARE SECRETS. Vault or AWS Secrets Manager; nothing in env vars in production. Rotation policy documented.
4. RUNBOOK FIRST, LIVE SECOND. Every alert has a runbook entry. Every incident class has a documented response.
5. POST-MORTEMS ARE LITERATURE. Blameless. Every production incident gets one within 48 hours. Lessons feed back into chaos tests.
6. LIVE = TIGHT LIMITS. First live week: 1% of intended capital, max 1 strategy, 30-minute trading windows only. Scale weekly subject to no-incident review.
7. SECURITY IS PRODUCT WORK. Pen test the API. Threat model the WS. Audit the kill switch.

CONTEXT: spec/CONTRACTS.md §11 (audit/sec). spec/LAYOUT.md §services/security, §infra. EDGE_ROADMAP.md §4 (risk/ops gaps).

Tasks: TASK-070..076.

Acknowledge by listing the 7 rules. Wait.
```
### ▲ PASTE END

## TASK-070 — Chaos suite

### ▼ PASTE START
```text
TASK-070 — Chaos engineering suite.

Files: services/chaos/src/chaos/{scenarios,runner,assertions}.py + .github/workflows/nightly.yml updates.

Scenarios: kill_redis (during live trading), kill_postgres, kill_orchestrator (leader), kill_oms (leader), drop_ingestor (Binance disconnect), simulate_exchange_halt (paper venue stops responding), inject_clock_skew (5min ahead/behind), inject_network_latency (1s lag on bus). Runner: schedules each scenario, invokes the Phase O E2E test under chaos, asserts the system either recovers gracefully or fails closed (no data corruption, no double-trades).

LANDMINES:
- Don't run chaos against production. Dedicated staging environment.
- Each scenario must clean up after itself; one chaos run shouldn't poison the next.
- Document expected behavior per scenario; assertions match documented behavior, not implementation.

DONE WHEN:
- pytest green for the local subset (kill processes; not network-level).
- Nightly chaos suite runs all scenarios in staging; green.
- spec/tasks/TASK-070-chaos.md authored.

VERIFY: uv run pytest services/chaos/tests/ -v
REPORT.
```
### ▲ PASTE END

## TASK-071 — Disaster recovery drill

### ▼ PASTE START
```text
TASK-071 — Disaster recovery procedure + automated drill.

Files: docs/runbooks/dr-procedure.md + scripts/dr_drill.py + scripts/restore_from_backup.py.

Procedure: nightly Postgres + Timescale base backups + WAL archiving to S3-compatible store. Redis Streams: per-stream snapshot at 5-min intervals + AOF. Recovery: from backup snapshot, apply WAL to point-in-time, restart services, validate first 100 events round-trip. Target: RTO <30 min, RPO <5 min.

Automated drill: scripts/dr_drill.py spins up an isolated copy of the system from yesterday's backup, runs a synthetic trading day, asserts blotter matches expected.

LANDMINES:
- WAL archiving must succeed on every commit; alarm on any archiving lag.
- Backup encryption: server-side with rotated keys; never plaintext.
- DR test cadence: monthly minimum; on quarterly cycle, do a "cold metal" full restore.

DONE WHEN:
- Drill recovers in <30 min on staging hardware.
- Backup encryption verified.
- Runbook documents every step (with screenshots).
- spec/tasks/TASK-071-dr.md authored.

VERIFY:
  ./scripts/dr_drill.py --backup yesterday --target staging-dr
REPORT.
```
### ▲ PASTE END

## TASK-072 — Secrets management + rotation

### ▼ PASTE START
```text
TASK-072 — Secrets management (Vault) + rotation policy.

Files: services/security/src/security/{vault,rotation}.py + infra/vault/.

HashiCorp Vault (or AWS Secrets Manager) holds: DB credentials, exchange API keys, JWT signing keys, OpenAI/Anthropic API keys. Each service authenticates to Vault at startup; secrets injected as ephemeral env vars. Rotation: monthly automated rotation for DB; per-exchange-policy for API keys; immediate manual rotation on suspected compromise.

LANDMINES:
- No secret EVER touches the disk in plaintext, including dev. .env.example only has placeholders.
- Rotation must NOT cause downtime; services accept both old + new credentials during a 1-minute overlap window.
- Audit log every secret access (who/what/when), retained 1 year minimum.

DONE WHEN:
- Vault dev mode runs locally; production Vault deployment plan documented.
- Rotation drill: manually rotate a secret; no service interruption observed.
- spec/tasks/TASK-072-secrets.md authored.

VERIFY: uv run pytest services/security/tests/test_vault.py -v
REPORT.
```
### ▲ PASTE END

## TASK-073 — Observability complete

### ▼ PASTE START
```text
TASK-073 — Metrics + tracing + logs unified observability.

Files: infra/grafana/dashboards/*.json + services/*/src/**/{metrics,tracing}.py audit.

Grafana dashboards: per-service health, latency p50/p99, error rates, business metrics (P&L, position count, fills/min). OpenTelemetry traces from API → orchestrator → risk → OMS → broker, end-to-end. Logs: every log line has correlation_id; Grafana Loki or equivalent for search. Alerts wired to PagerDuty or equivalent for: stream lag >10s, error rate >1%, kill switch active >1 min, drawdown >50% of MAX_DAILY_LOSS, ingestor backpressure, leader election flap.

LANDMINES:
- Don't alert on what you can't action. Every alert maps to a runbook.
- Sampling: 100% trace sampling on the trading hot path; 10% elsewhere.
- Cost of observability: monitor it; observability stack should be <5% of total infra cost.

DONE WHEN:
- All dashboards populated with synthetic data.
- Trace from API call → fill is end-to-end visible in Jaeger.
- 5 representative alerts fire and resolve cleanly during a chaos scenario.
- spec/tasks/TASK-073-obs.md authored.

VERIFY: visual inspection of Grafana + Jaeger after running TASK-070 chaos.
REPORT.
```
### ▲ PASTE END

## TASK-074 — Security audit + pen test

### ▼ PASTE START
```text
TASK-074 — Security audit + internal pen test.

Files: docs/security/{threat-model.md,audit-log.md,pen-test-report.md}.

Threat model: STRIDE-like enumeration over the API, WS, kill switch, secrets, broker connectors, audit log, DB. Pen test: external party (or internal red team) attacks the staging deployment; report scoped to API auth, WS auth, kill switch tampering, audit log tampering, broker credential exfiltration, deserialization. Fix all critical + high; document medium + low for tracking.

LANDMINES:
- Pen test on staging only. Never on prod.
- Time-box pen test (1–2 weeks); document scope explicitly.
- Re-test after fixes; one-shot pen tests are theater.

DONE WHEN:
- Threat model exists with mitigations mapped.
- Pen test report exists; all critical + high resolved.
- Audit log integrity verified (tamper-evident, append-only).
- spec/tasks/TASK-074-sec-audit.md authored.

VERIFY: doc review + re-test report.
REPORT.
```
### ▲ PASTE END

## TASK-075 — Live broker connector

### ▼ PASTE START
```text
TASK-075 — Live broker connector (one venue, e.g., IBKR or Coinbase Advanced Trade live).

Files: services/oms/src/oms/venue/{live_ibkr,live_coinbase}.py + tests/integration/.

Implements VenueAdapter for ONE live venue. Supports submit, cancel, fill events, account balance, position reconciliation. Sandbox-tested before TRADING_MODE=live is enabled.

LANDMINES:
- Sandbox != production. Some venues' sandboxes diverge significantly.
- Reconciliation: at startup, fetch venue's view of positions + open orders; compare to local; alert on mismatch.
- TRADING_MODE=live MUST require explicit feature flag plus Phase H checkpoint signed off by operator.
- Every order tagged with client_order_id for idempotent retries.
- Rate limits: respect venue's API rate limits; back off on 429.

DONE WHEN:
- Sandbox: 100 paper orders submitted, all filled or cancelled cleanly, audit reconciles.
- Production-readiness checklist completed in spec/tasks/TASK-075-live-broker.md.

VERIFY: uv run pytest services/oms/tests/integration/test_live_sandbox.py -v
REPORT.
```
### ▲ PASTE END

## TASK-076 — Runbook + on-call

### ▼ PASTE START
```text
TASK-076 — Runbook + on-call rotation + post-mortem template.

Files: docs/runbooks/*.md + docs/oncall/{rotation,handoff,template}.md.

Runbook covers top 20 incident classes (drawdown circuit, ingestor disconnect, broker disconnect, leader split-brain, DB exhaust, etc.) with severity, detection, diagnosis, mitigation, escalation. On-call: single primary + single backup, 1-week rotation, handoff checklist. Post-mortem template: timeline, root cause, contributing factors, action items (with owners + dates), lessons.

LANDMINES:
- Runbooks rot. Quarterly review; mark stale entries.
- On-call without compensation = burnout. Document expectations.
- Post-mortems are blameless; never name names in lessons.

DONE WHEN:
- Top 20 incident classes documented.
- On-call rotation scheduled for the first month.
- Post-mortem template applied to a chaos-test incident as a dry run.
- spec/tasks/TASK-076-runbook.md authored.

VERIFY: doc review.
REPORT.
```
### ▲ PASTE END

## Phase H — Exit verification

### ▼ PASTE START
```text
PHASE H EXIT.

CHECKLIST:
1. Chaos suite (TASK-070) green: every documented scenario survived gracefully.
2. DR drill (TASK-071) recovers in <30 min; verified on staging.
3. Vault deployed; all secrets migrated; rotation drill successful.
4. Grafana dashboards populated; 5 representative alerts fire cleanly during chaos.
5. Threat model documented; pen test report has all critical + high resolved.
6. Live broker (TASK-075) sandbox tests pass.
7. Runbook covers top 20 incident classes.
8. On-call rotation set up.
9. mypy --strict clean across all services.
10. TASK-070..076 specs exist; [x] in BUILD_ORDER.md.

If green: Phase H COMPLETE. Add "Checkpoint H: passed YYYY-MM-DD".

LIVE ROLLOUT (after Phase H):
- Week 1: 1% of intended capital, 1 strategy, 30-min windows. Daily review.
- Week 2-4: 5% capital, 2 strategies, daily windows. Weekly review.
- Month 2: 25% capital, full strategies. Twice-weekly review.
- Month 3+: 100% capital. Weekly review.
- Any incident at any week → halt, post-mortem, prove fix, resume from previous step.

REPORT.
```
### ▲ PASTE END

---

# Phase X+ — Profitability Layer

**Goal:** The additions whose absence is the single biggest reason most retail/small-firm systematic platforms fail to outperform passive benchmarks.
**Checkpoint:** 8-week shadow ensemble Sharpe ≥ Phase X baseline + 0.7, max DD ≤ benchmark, p < 0.05 via block bootstrap, LLM cost ≤ 30% of attributed alpha.

**Strategic context:** `spec/EDGE_ROADMAP.md`. **Full detail:** `spec/prompts/phase-Xplus.md`.

## Phase X+ — Kickoff

### ▼ PASTE START
```text
ENTERING PHASE X+ — PROFITABILITY LAYER.

Session opener norms apply. Phases F, D, B, A, O, U, X complete. Phase H may be running in parallel. Phase-specific rules:

1. CAUSAL HYPOTHESIS REQUIRED. Every alpha addition has a documented economic / behavioral mechanism BEFORE you build it. "It backtests well" is not a hypothesis. If you cannot articulate why this should work in 2 sentences, STOP and report.
2. CALIBRATION OVER ACCURACY. The bottleneck is correctly weighting predictors under uncertainty.
3. COST DISCIPLINE IS PRODUCT WORK. Track every LLM token. Phase X+ checkpoint requires LLM cost ≤ 30% of attributed alpha.
4. ORTHOGONALITY IS A FEATURE. Demonstrate orthogonality to existing alphas (correlation matrix of signal P&Ls) before deploying any extension.
5. SHADOW BEFORE LIVE. Phase X+ agents enter with weight=0 and shadow ≥ 4 weeks before non-zero weight.
6. DECAY IS THE NORM. TASK-085 (decay monitor) and TASK-088 (correlation-breakdown) are not optional polish.
7. CAPACITY-AWARE FROM DAY ONE. Each strategy has a capacity curve.
8. CONTRACTS ARE STILL IMMUTABLE. No new event types.

CONTEXT: spec/EDGE_ROADMAP.md (mandatory), spec/CONTRACTS.md, spec/prompts/phase-Xplus.md (fuller per-task detail than below).

Tasks: TASK-080..089. **TASK-085 lands EARLY** — later tasks depend on its decay infrastructure.

Acknowledge by listing the 8 rules. State the causal hypothesis for the first task. Wait.
```
### ▲ PASTE END

## TASK-080 — Options flow agent

### ▼ PASTE START
```text
TASK-080 — Options flow agent.

Hypothesis: Outsized OPENING flow (OTM, short-dated, large vs OI) is a noisy but persistent signal of forward equity moves over 1–10 trading days. Mechanism: information asymmetry + leverage preference.

Files: services/agents/options_flow/{main,screener,datasource,contract_validator}.py.

LANDMINES:
- HALLUCINATED CONTRACTS: cross-check every strike+expiry+underlying against real chain via fincept-tools. Drop unvalidated.
- HEDGING FILTER: volume > 3× ADV, OTM 2–10%, DTE < 45d, net premium > $100k.
- OPENING vs CLOSING: estimate from prior-day OI delta vs trade volume.
- SKIP MULTI-LEG SPREADS in v1; flag tags.has_multileg if uncertain.
- CONFIDENCE CAP at 0.7. Inherently noisy.

DONE WHEN:
- pytest green; backtest IC ≥ 0.05 vs forward 5d returns over 6mo replay.
- Emits Prediction (horizon 1–10 trading days, confidence ∈ [0.4, 0.7]).
- spec/tasks/TASK-080-options-flow.md authored with documented hypothesis.

VERIFY: uv run pytest services/agents/tests/test_options_flow.py -v
REPORT. CONTRACTS: §3 (Prediction).
```
### ▲ PASTE END

## TASK-081 — Earnings call transcripts

### ▼ PASTE START
```text
TASK-081 — Earnings call transcript LLM agent.

Hypothesis: Tone, hedging language, and forward-guidance changes during earnings calls contain forward-return information over 3–60 days not fully priced into the post-print move. Mechanism: slow info diffusion via analyst notes + investor digestion.

Files: services/agents/earnings_calls/{main,fetcher,extractor}.py + eval/cases.jsonl (≥50 hand-labeled).

LANDMINES:
- DELAY 1–4 hours; signal is for SECOND wave (3–60 day horizon).
- STRUCTURED OUTPUT REQUIRED: tone_score ∈ [-1,1], guidance_change enum, analyst_pushback_severity, headwinds[], tailwinds[], confidence ∈ [0,1].
- ENTITY RESOLUTION ONCE per transcript; drop transcripts whose vendor ticker disagrees with universe.
- EVAL SUITE FIRST: 50 labeled across (beat-raised, beat-lowered, missed-bullish, missed-bearish, mixed). Re-eval before any model swap.
- COST: truncate Q&A to first N if budget pressure; prepared remarks retain most signal.

DONE WHEN:
- pytest green.
- Macro precision ≥ 0.75, recall ≥ 0.6 on labeled set.
- Daily LLM cost < $3 in MVP universe.
- spec/tasks/TASK-081-earnings-transcripts.md authored.

VERIFY:
  uv run pytest services/agents/tests/test_earnings_extractor.py -v
  uv run python -m agents.earnings_calls.eval --cases ./eval/cases.jsonl
REPORT. CONTRACTS: §3 (SentimentSignal, event_type="earnings_call").
```
### ▲ PASTE END

## TASK-082 — Insider Form 4 + short interest

### ▼ PASTE START
```text
TASK-082 — Insider Form 4 + short interest agents.

Hypotheses:
- Clustered insider open-market PURCHASES (≥3 insiders, 30 days) predict 3–12mo positive returns. Mechanism: information asymmetry.
- High SI + positive catalyst within 5 days creates squeeze potential.

Files: services/agents/insider_short/{main,edgar_form4,finra_si,insider_analyzer,short_squeeze}.py.

LANDMINES:
- 80% INSIDER NOISE: drop sales, 10b5-1, exercise+sell. Keep open-market purchases by named officers + directors only.
- SHORT INTEREST LAGGED 2 WEEKS; combine with CURRENT price action (breakout, beat).
- HIGH SI ALONE IS NOT BUY. Require co-occurring positive catalyst.
- UNIVERSE FILTER: restrict to in-universe symbols (Form 4 firehose is huge).
- EMIT ≤ 1 hour of Form 4 filing.

DONE WHEN:
- pytest green; replay 12mo data → IC ≥ 0.04 vs forward 60d returns.
- Insider cluster: SentimentSignal event_type="insider_cluster_buy", score=+0.7, confidence ~ cluster size.
- Squeeze: SentimentSignal event_type="short_squeeze_candidate", score=+0.4 (long-only), horizon 20 trading days.
- spec/tasks/TASK-082-insider-short.md authored.

VERIFY: uv run pytest services/agents/tests/test_form4_filter.py -v
REPORT. CONTRACTS: §3 (SentimentSignal).
```
### ▲ PASTE END

## TASK-083 — Cross-sectional ranking

### ▼ PASTE START
```text
TASK-083 — Cross-sectional ranking layer.

Hypothesis: Within a correlated universe, relative strength on composite-quality measures predicts cross-sectional outperformance. Long top-decile + short bottom-decile diversifies away market beta. The most durable equity strategy for 30+ years (with episodic crashes).

Files: services/orchestrator/{cross_section,composite_score}.py.

Implementation:
- Composite score per symbol: weighted sum of normalized signal scores (regime-adaptive weights from TASK-040 + TASK-066).
- Universe rank: percentile within universe.
- Decisions: long top X% (default 10%), short bottom X%, market-neutral.
- Rebalance frequency: configurable (default weekly Mon).

LANDMINES:
- MOMENTUM CRASH RISK at regime transitions. Regime gate: high-vol or transition → reduce gross or skip rebalance.
- TURNOVER: only rebalance positions whose rank-percentile shifted ≥ 10pp.
- SECTOR NEUTRALITY flag (default off): rank within sector if on.
- SURVIVORSHIP: universe MUST include delisted/bankrupt names in backtest.
- CAPACITY at small AUM is killed by commissions. Document min-AUM gate.

DONE WHEN:
- pytest green.
- 5-yr survivorship-bias-free walk-forward, weekly rebalance: Sharpe ≥ 1.0 net of 5bps round-trip, max DD ≤ 20%.
- Decisions on ord.decisions, batched per rebalance, tags include strategy + long/short counts + rebalance_id.
- spec/tasks/TASK-083-cross-section.md authored.

VERIFY: uv run pytest services/orchestrator/tests/test_cross_section.py -v
REPORT. CONTRACTS: §4 (Decision).
```
### ▲ PASTE END

## TASK-084 — Portfolio vol targeting

### ▼ PASTE START
```text
TASK-084 — Portfolio-level vol targeting.

Hypothesis: Per-signal Kelly ignores portfolio-level vol clustering. Constant-vol-targeted portfolios outperform constant-leverage because realized vol scales inversely with future Sharpe; reducing exposure during high-vol regimes improves risk-adjusted returns.

Files: services/risk/{vol_target,realized_vol}.py.

Implementation:
- EWMA realized vol (21–63 day half-life).
- Target: configurable annualized vol (default 10%).
- Scale total gross by (target / realized), capped [0.25×, 2.0×] of base.
- Apply at Risk gate, AFTER Kelly, BEFORE OMS.

LANDMINES:
- PROCYCLIC DELEVERAGING (selling at the bottom). Cap downscaling at 0.5× per day; longer-window vol during transitions.
- VOL-OF-VOL whipsaw. 5-day EMA on the scaler.
- COSTS from churn. Track turnover; back off responsiveness if over budget.
- EQUITY VS CRYPTO realized vols differ 3–5×. Document choice (per asset class vs portfolio-level dollar-weighted).
- LEVERAGE CAP: 2.0× must respect account limits.

DONE WHEN:
- pytest green.
- Backtest: 10%-targeted vs unconstrained, 3yr period. Targeted Sharpe ≥ unconstrained + 0.2.
- Emits PortfolioVolMetric to sig.metrics each cycle.
- spec/tasks/TASK-084-vol-target.md authored.

VERIFY: uv run pytest services/risk/tests/test_vol_target.py -v
REPORT.
```
### ▲ PASTE END

## TASK-085 — Strategy decay + capacity (LAND EARLY)

### ▼ PASTE START
```text
TASK-085 — Strategy decay monitor + capacity curves. Land this EARLY in the phase.

Hypothesis: Every alpha decays — by arbitrage, mechanism shift, or self-competition at scale. Without monitoring, capital flows to dead strategies. Without capacity curves, the system over-allocates to capacity-bound strategies whose alpha vanishes at scale.

Files: services/jobs/strategy_decay.py + services/risk/capacity.py + libs/fincept-db/migrations/00X_strategy_metrics.sql.

Decay monitor:
- Daily per strategy: rolling 21d/90d Sharpe, hit rate, turnover, IC vs forward return.
- Alert: 90d Sharpe < 0.3 for 30 consecutive days, OR Sharpe drop > 1.0 vs prior 90d.
- On alert: recommend weight × 0.5 for orchestrator.

Capacity curve:
- Fit Sharpe(N) = a · N^(-b) on (allocation, realized_pnl) history. Refit weekly.
- Recommended max: N where marginal Sharpe = 0.3.

LANDMINES:
- SAMPLE-SIZE NOISE. Block-bootstrap the alert threshold. No single-week alerts.
- ATTRIBUTION QUALITY. Verify TASK-045 portfolio attribution before trusting.
- COLD-START CAPACITY CURVE: no cap until ≥3 distinct allocation levels each ≥30 days observed.
- MANUAL OVERRIDE TABLE with required justification field.

DONE WHEN:
- pytest green for both decay + capacity.
- Synthetic: strategy Sharpe 1.5 → -0.2 over 60d → alert within 30d of cliff.
- db.strategy_metrics_daily; AlertEvent on events.alerts; /api/strategies/{id}/capacity endpoint.
- spec/tasks/TASK-085-decay-capacity.md authored.

VERIFY:
  uv run pytest services/jobs/tests/test_strategy_decay.py -v
  uv run pytest services/risk/tests/test_capacity.py -v
REPORT. CONTRACTS: §7 (AlertEvent).
```
### ▲ PASTE END

## TASK-086 — Multi-agent LLM debate

### ▼ PASTE START
```text
TASK-086 — Multi-agent LLM debate (replaces TASK-064 single-shot).

Hypothesis: Single-shot LLM exhibits motivated reasoning. Adversarial multi-agent debate (bull, bear, judge) consistently improves calibration across domains. Cost: 3× tokens; alpha: better-calibrated decisions.

Files: services/orchestrator/llm_debate.py + prompts/{bull,bear,judge}.md.

Pattern:
1. Numerical orchestrator computes candidate Decision.
2. Bull and bear agents in PARALLEL (asyncio.gather), each blind to the other.
3. Judge: receives Decision + bull rationale + bear rationale + portfolio state. Outputs approve/modify/reject + confidence + rationale.

LANDMINES:
- TRUE ADVERSARIAL FRAMING: bull and bear MUST NOT see each other's outputs.
- JUDGE BIAS toward longer rationale. Judge prompt MUST require engaging with the strongest counterpoint from the opposite side before deciding.
- COST cap 8k tokens total across the three calls.
- LATENCY p99 < 4s (3× single-shot 1.5s budget).
- CACHE on (signal-hash, portfolio-hash) within 60s.
- FALLBACK on any timeout/malformed JSON: numerical-only Decision + alert. No improvising.

DONE WHEN:
- pytest green.
- 4-week A/B (50% single-shot vs 50% debate): debate ≥ 5% Brier-score improvement, no Sharpe degradation.
- tags include llm_pattern="debate", bull/bear/judge confidences, tokens_used, cost_usd. audit_log includes all three rationales.
- spec/tasks/TASK-086-llm-debate.md authored.

VERIFY: uv run pytest services/orchestrator/tests/test_llm_debate.py -v
REPORT. CONTRACTS: §4 (Decision), §7 (audit).
```
### ▲ PASTE END

## TASK-087 — Sector rotation

### ▼ PASTE START
```text
TASK-087 — Sector rotation overlay.

Hypothesis: Macro regimes (early/mid/late/recession) systematically favor different sectors. Macro signals (yield curve, HY spreads, ISM, NFP) classify regime months ahead of equity confirmation. Mechanism: sector earnings-cycle sensitivity + regime-positioning lags.

Files: services/agents/sector_rotation/{main,macro,regime_classifier,sector_map}.py.

Implementation:
- Macro features (FRED + Treasury + ISM, daily): 10y-2y slope, BAML HY OAS, ISM PMI (lagged 1mo), real Fed Funds, YoY NFP change.
- Classifier: 4-class (early/mid/late/recession). Logistic regression or shallow tree (interpretability matters). Trained on lagged-NBER + economist-classified expansions.
- Sector map (HAND-CURATED, not learned):
  - Early: cyclicals, financials, materials.
  - Mid: tech, comm services, discretionary.
  - Late: energy, staples, healthcare.
  - Recession: utilities, staples, healthcare, gold/USD.

LANDMINES:
- POINT-IN-TIME LABELS: NBER announces ~6mo late. Apply with that delay or the classifier looks prophetic in backtest.
- MACRO REVISIONS: use vintage data (FRED ALFRED) for backtest, not current revisions.
- REGIME FREQUENCY 12–60mo. Smoothing: ≥30 days consistent classification before regime change.
- NEVER LEARN THE SECTOR MAP: few transitions in history → guaranteed overfit. Hand-curated table.

DONE WHEN:
- pytest green.
- 1990–present walk-forward: sector-tilted long-only outperforms equal-weight benchmark by ≥1.5% annualized after costs, lower max DD.
- Emits RegimeSignal regime_type="macro_cycle", tags.sector_tilts dict.
- spec/tasks/TASK-087-sector-rotation.md authored.

VERIFY: uv run pytest services/agents/tests/test_sector_rotation.py -v
REPORT. CONTRACTS: §3 (RegimeSignal).
```
### ▲ PASTE END

## TASK-088 — Correlation breakdown alerts

### ▼ PASTE START
```text
TASK-088 — Correlation breakdown alerts.

Hypothesis: Multi-strategy systems are sized assuming approximate strategy independence. During stress, strategies sharing an underlying risk factor simultaneously lose. Realized vol explodes vs predicted; Kelly built on prior correlations becomes severely overlevered. Detecting the transition early allows preemptive deleveraging.

Files: services/risk/{corr_monitor,regime_alert}.py.

Implementation:
- Per-strategy daily P&L for last 252d.
- Pairwise correlation on rolling 60d window.
- Top eigenvalue of correlation matrix.
- Baseline: rolling 90d median of top eigenvalue.
- Alert: top eigenvalue > baseline × 1.5 for 5 consecutive days.
- Risk gate response: gross × 0.5 until eigenvalue normalizes.

LANDMINES:
- MISSING-DATA FALSE POSITIVES. Strategy with zero P&L for 5d artificially correlates. Filter strategies with ≥80% non-zero P&L days.
- BASE-RATE NOISE. Block-bootstrap the threshold per strategy mix; do not flat 1.5×.
- LIVE VS PAPER. Paper correlations are simulated; tune threshold separately.
- POST-ALERT COOLDOWN: ≥5 days below baseline before unwinding (eigenvalue normalizes during the deleveraging itself).

DONE WHEN:
- pytest green.
- Replay March 2020 on multi-strategy portfolio: alert within 5 trading days of correlation spike, before worst of drawdown.
- AlertEvent on events.alerts, severity="critical".
- spec/tasks/TASK-088-corr-monitor.md authored.

VERIFY: uv run pytest services/risk/tests/test_corr_monitor.py -v
REPORT. CONTRACTS: §7 (AlertEvent).
```
### ▲ PASTE END

## TASK-089 — Liquidity stress test

### ▼ PASTE START
```text
TASK-089 — Liquidity stress test.

Hypothesis: Vol-based sizing ignores exit cost in adverse markets. Strategies that look profitable on paper can be uneconomical at scale because exit slippage exceeds expected alpha. Daily simulation of "exit 50% of book in 1 trading day" caps tail position size.

Files: services/risk/{liquidity_stress,market_impact}.py.

Implementation:
- Per open position daily: % of ADV; estimated slippage to exit X% in 1 day via Almgren-style square-root model. Calibrate from TASK-022 broker history; defaults from literature.
- Aggregate: total estimated cost to exit 50% of book in 1 day, % of NAV.
- Cap: if 50%-exit cost > threshold (default 100bps NAV), prevent NEW positions that worsen the metric. Existing positions ride.
- Daily report per-symbol contribution.

LANDMINES:
- ADV FOR CRYPTO inflated by wash trading. Use top-3-venue volume; cap any single venue.
- ADV FOR EQUITIES varies 5×. Rolling 21d MEDIAN, not mean (mean dominated by FOMC days).
- IMPACT MODEL CALIBRATION per asset class. Almgren defaults are large-cap equity; crypto and small-cap have higher impact.
- MULTI-LEG POSITIONS don't decompose. Phase X+ scope: single-leg only; flag tags.exit_estimate_unreliable.
- SOFT GATE, not hard: prevents NEW additions, does NOT force-liquidate (would be self-fulfilling).

DONE WHEN:
- pytest green.
- Synthetic: 5% ADV in 10 illiquid names → flag and prevent further accumulation.
- Calibration: estimated slippage on TASK-022 historical fills agrees within 30% of realized.
- LiquidityStressMetric on sig.metrics. Risk gate rejects new positions exceeding threshold with reason="liquidity_stress_cap".
- spec/tasks/TASK-089-liquidity-stress.md authored.

VERIFY: uv run pytest services/risk/tests/test_liquidity_stress.py -v
REPORT.
```
### ▲ PASTE END

## Phase X+ — Exit verification (the profitability gate)

### ▼ PASTE START
```text
PHASE X+ EXIT — THE PROFITABILITY GATE.

Run rigorously. This is the gate that determines whether the Phase X investment paid off.

CHECKLIST:
1. CAUSAL-HYPOTHESIS AUDIT: every spec/tasks/TASK-08x.md begins with a documented causal hypothesis. Reviewed against EDGE_ROADMAP §5 decision principles. Any hypothesis amounting to "it backtested well" is FAIL.
2. 8-WEEK SHADOW: full ensemble (10 X+ tasks + Phase X agents) running shadow alongside Phase X baseline. Sharpe ≥ baseline + 0.7. p < 0.05 via block bootstrap (block = 1d, B = 10000). Max drawdown ≤ S&P 500 over the same window. Realized portfolio vol within ±20% of TASK-084 target.
3. COST DISCIPLINE: LLM spend over 8 weeks reported. Cost per dollar of attributed alpha ≤ 30%.
4. DECAY INFRASTRUCTURE LIVE: TASK-085 monitor running ≥4 weeks; ≥1 synthetic decay drill executed cleanly; capacity curves populated for ≥5 strategies with ≥3 distinct allocation levels each.
5. RISK ADDITIONS LIVE: TASK-088 fired (or synthetic test demonstrated firing); TASK-089 caps applied at least once; no positions accumulated past 100bps exit threshold.
6. ORTHOGONALITY: correlation matrix of strategy-level P&Ls computed. Top eigenvalue ≤ 0.6 of total variance; ≥4 strategies with pairwise |corr| < 0.3.
7. OPERATIONAL STABILITY: total agent crashes ≤ 5 over 8 weeks. Multi-agent debate (TASK-086) graceful fallback to numerical-only on LLM outage tested.
8. AUDIT: random Decision from shadow → reconstruct source signals → composite score → cross-sectional rank → numerical consensus → debate rationale → vol-target scaling → liquidity gate → final Decision. Every link traces cleanly via audit_log.

If all 8 pass: PHASE X+ COMPLETE. Mark TASK-080..089 [x]. Add "Checkpoint X+: passed YYYY-MM-DD". The system is now a credible candidate for sustained S&P outperformance, validated in shadow. Risk-committee may consider increasing live capital allocation per Phase H rollout schedule. Phase Y (differentiation) may begin in parallel.

If shadow Sharpe < baseline + 0.7: profitability thesis is partially validated but not fully. Decide:
- Iterate Phase X+ further (most common cause: weights, calibration, or one decayed agent dragging the ensemble).
- Pivot scope (the system is still a strong baseline platform without Phase X+ checkpoint claim).
- Do NOT scale live capital beyond Phase H Week-1 limits until passed.

REPORT.
```
### ▲ PASTE END

---

# Phase Y — Differentiation Layer

**Goal:** Capabilities that are durable and hard to replicate — non-equity alpha (on-chain, macro), tail-risk protection, concept-drift defense, microstructure extraction.
**Checkpoint:** 12-week shadow + paper. Outperforms benchmark across ≥3 distinct macro regimes. Capacity stress: 10× AUM does not degrade Sharpe by more than 20%. ≥40% of new alpha attribution from non-equity sources.

**Strategic context:** `spec/EDGE_ROADMAP.md §4`. **Full detail:** `spec/prompts/phase-Y-differentiation.md`.

## Phase Y — Kickoff

### ▼ PASTE START
```text
ENTERING PHASE Y — DIFFERENTIATION.

Session opener norms apply. Phases F, D, B, A, O, U, X, H, X+ complete or in maintenance. Phase-specific rules:

1. EVERY ADDITION MUST PASS THE "WHO ELSE?" TEST. If "every retail platform with a Discord" publishes this signal, DO NOT BUILD IT.
2. NON-EQUITY DIVERSIFICATION. ≥40% of new alpha by intent must come from non-equity sources (on-chain, macro, alt-data).
3. TAIL RISK IS NON-NEGOTIABLE. After TASK-092 ships, NO new strategy may go live without explicit tail-hedge budget allocation.
4. CONCEPT DRIFT IS A FIRST-CLASS PROBLEM. TASK-095 required for any non-trivial supervised agent at non-zero weight.
5. CAPACITY STRESS BEFORE WEIGHTS. Every Phase Y agent must publish its capacity curve (TASK-085 infrastructure) before non-zero allocation.
6. ALT-DATA: ONE VENDOR FIRST. TASK-093 enforces ROI-positive single vendor before second.
7. SHADOW PERIODS ARE LONGER (≥6 weeks vs 4 in Phase X+). Macro signals are slow.
8. CONTRACTS ARE STILL IMMUTABLE. Microstructure → FeatureFrame.tags; on-chain → SentimentSignal event_type="onchain_*"; macro → RegimeSignal regime_type="macro_*". No new event classes.

CONTEXT: spec/EDGE_ROADMAP.md §4 (mandatory), spec/CONTRACTS.md §3, §6, spec/prompts/phase-Y-differentiation.md (fuller per-task detail than below).

Tasks: TASK-090..096. Recommended order: 091 (macro_regime) → 094 (bandit_allocator) → 095 (online_drift) → 096 (microstructure) → 092 (tail_hedge) → 090 (onchain) → 093 (altdata).

Acknowledge by listing the 8 rules. State the causal hypothesis for the first task. Wait.
```
### ▲ PASTE END

## TASK-090 — On-chain analytics

### ▼ PASTE START
```text
TASK-090 — On-chain analytics agent (whale wallets, exchange flows, DeFi TVL, miner reserves).

Hypothesis: Crypto markets settle on a public ledger. Large wallet movements, exchange reserve trends, stablecoin issuance, miner reserves are LEADING indicators over 6h–7d horizons.

Files: services/agents/onchain/{main,client,whale_detector,exchange_flow,miner_reserve,defi_tvl}.py + services/ingestor/onchain/{etherscan,glassnode_free,blockchair}.py.

LANDMINES:
- ATTRIBUTION: wallet labels are noisy; require multi-source confirmation; flag low-confidence.
- API RATE LIMITS: free tiers 5–20 req/min. Cache aggressively.
- REORGS: confirm 6 blocks before acting on whale events.
- CHAIN HALTS: detect + pause signals during ETH/BTC client splits.
- SCOPE: BTC, ETH, top-10 stablecoins only in v1. Alt-L1s = noise-to-signal collapse.

DONE WHEN:
- pytest green.
- 3-month replay: whale-movement alerts precede ≥30% of >5% same-day moves on BTC/ETH.
- Cost: $0 (free APIs).
- Emits: AlertEvent (whale moves), SentimentSignal event_type="onchain_*" with confidence ∈ [0.3, 0.6].
- spec/tasks/TASK-090-onchain.md authored.

VERIFY: uv run pytest services/agents/tests/test_onchain.py -v
REPORT. CONTRACTS: §3 (SentimentSignal, AlertEvent).
```
### ▲ PASTE END

## TASK-091 — Macro regime classifier

### ▼ PASTE START
```text
TASK-091 — Cross-asset macro regime classifier (inflation × growth × liquidity).

Hypothesis: Returns conditional on macro regime are >2× more predictable than unconditional. 8-state regime decomposition yields distinct expected returns across equity, crypto, USD, gold, bonds.

Files: services/agents/macro_regime/{main,classifier,features,asset_response}.py.

Features (FRED ALFRED + Treasury + ISM, daily): 5y5y forward, sticky-CPI YoY, ISM PMI/prices-paid, NFP YoY, real retail YoY, Fed balance sheet, RRP, M2 YoY, FCI.

Classifier: HMM 8-state OR shallow tree. Soft probabilities. Smoothing: ≥10 days posterior > 0.5 before regime change.

Asset-response table HAND-CURATED (8 cells, NOT learned).

LANDMINES:
- POINT-IN-TIME via FRED ALFRED vintage.
- NBER labels announced 6+ months late; apply matching delay in backtest.
- DO NOT LEARN the table — too few transitions in 70y to fit 8 cells.
- Use as TILT (TASK-094) + GATE (TASK-088), NOT as direct alpha.

DONE WHEN:
- pytest green.
- 1990–present walk-forward: regime-conditional buy-and-hold outperforms equal-weight by ≥1.0% annualized after costs.
- Emits RegimeSignal regime_type="macro_3axis" with p_inflation_up, p_growth_up, p_liquidity_up tags.
- spec/tasks/TASK-091-macro-regime.md authored.

VERIFY: uv run pytest services/agents/tests/test_macro_regime.py -v
REPORT. CONTRACTS: §3 (RegimeSignal).
```
### ▲ PASTE END

## TASK-092 — Tail-risk hedge

### ▼ PASTE START
```text
TASK-092 — Systematic tail-risk hedge (OTM SPX puts + crypto OTM puts).

Hypothesis: Persistent OTM-put allocation is negative-EV in normal times but bounds drawdowns during left-tail events. Cost ~1–3% annualized; benefit max-DD bounded.

Files: services/risk/tail_hedge.py + services/oms/options_paper.py + services/oms/venue/options_sim.py.

Implementation:
- Configurable tail_hedge_budget_bps (default 200bps annualized).
- Roll OTM SPX puts ~10% OTM ~90 DTE quarterly. BTC OTM puts (Deribit) at proportional notional.
- Fund from dedicated budget; do NOT cannibalize alpha capital.
- On VIX > 40: pause new hedge purchases.
- Paper-trade entire hedge book in v1 (live = Phase H gated).

LANDMINES:
- BUDGET DISCIPLINE: feels wasteful for 95% of time. Make INVISIBLE in normal P&L view; visible only in stress reports. Hard cap; do not cut.
- ROLL TIMING: skip 3 days around earnings/FOMC.
- BASIS RISK: SPX hedges equity; BTC hedges crypto; NOT cross-fungible.
- LIQUIDITY: deep OTM puts have wide spreads; cap to reasonable max half-spread.

DONE WHEN:
- pytest green.
- Backtest 2008+2020: portfolio with 200bps hedge shows max-DD ≤ 60% of unhedged max-DD.
- Hedge cost over 2010–2019 ≤ 3% annualized drag.
- Emits Decision tagged strategy="tail_hedge" with separate audit trail.
- spec/tasks/TASK-092-tail-hedge.md authored.

VERIFY: uv run pytest services/risk/tests/test_tail_hedge.py -v
REPORT. CONTRACTS: §4 (Decision).
```
### ▲ PASTE END

## TASK-093 — Selective alt-data integration

### ▼ PASTE START
```text
TASK-093 — Selective alt-data integration (ONE ROI-positive vendor first).

Hypothesis: SOME alt-data feeds contain alpha; MOST do not. Wrong answer = $5k/mo with no measurable lift; right answer = $5k/mo for +0.3 Sharpe.

Files: services/ingestor/altdata/{base,vendor_a}.py + services/agents/altdata/{main,evaluator}.py.

PROCESS (not just code):
1. Pre-purchase eval (≥3mo trial data): attribution + orthogonality.
2. Decision gate: projected attribution > 1.5× annual cost AND |corr| < 0.3 vs existing.
3. Implement adapter ONLY for approved vendor.
4. Monthly re-eval; auto-cancel if < 1.0× for 2 consecutive months.

CONSIDER (eval only, not commitment): SimilarWeb/data.ai, Earnest credit-card, paid Glassnode tier. SKIP: generic "social sentiment" (already arbitraged).

LANDMINES:
- LICENSING: most alt-data is non-transferable, forbids reselling signals. Read carefully.
- POINT-IN-TIME: vendors restate; verify or simulate lag.
- SURVIVORSHIP: vendor sample data is curated to look good. Demand random period.
- NULL HYPOTHESIS WINS by default. Reject vendors that don't clear it after 3mo.

DONE WHEN:
- pytest green.
- ≥1 vendor evaluated end-to-end (even if rejected).
- Adapter built for ≥1 vendor that PASSED gate (or documented decision none passed).
- spec/tasks/TASK-093-altdata.md authored with vendor evaluation log.

VERIFY: uv run pytest services/ingestor/altdata/tests/ -v
REPORT.
```
### ▲ PASTE END

## TASK-094 — Bandit allocator

### ▼ PASTE START
```text
TASK-094 — Multi-arm bandit strategy allocator (Thompson sampling above orchestrator).

Hypothesis: Fixed strategy weights stale within weeks. Thompson sampling over per-strategy posterior Sharpe converges faster than equal-weight or fixed-Sharpe and adapts gracefully when strategies enter/decay.

Files: services/orchestrator/bandit_allocator.py + libs/fincept-db/migrations/00X_strategy_posteriors.sql.

Implementation:
- Beta(α,β) posterior on Sharpe quintile-rank within active strategy pool.
- Daily sample → allocate proportional → normalize.
- Decay: prior α,β multiply by 0.99 daily.
- Cold start: prior matches median strategy; min 30 days observation before full sampling weight.
- Sits ABOVE orchestrator (TASK-040) — scales each strategy's gross.

LANDMINES:
- DEPENDS ON TASK-085 strategy_metrics_daily.
- THOMPSON HIGH VARIANCE early. Cap turnover ≤ ±20% per week per strategy.
- REGIME LAG 2–4 weeks. Optionally condition posterior on TASK-091 (regime-conditional).
- BUDGET: Σ(weights × gross) ≤ portfolio gross cap. Enforce at boundary.

DONE WHEN:
- pytest green.
- Synthetic 6-strategy pool with one decayer: bandit reallocates within 4 weeks of cliff.
- Live shadow: matches fixed-Sharpe within 5% in stable regimes; diverges sensibly when one decays.
- Emits AllocatorDecision to ord.allocator with audit trail.
- spec/tasks/TASK-094-bandit-allocator.md authored.

VERIFY: uv run pytest services/orchestrator/tests/test_bandit_allocator.py -v
REPORT.
```
### ▲ PASTE END

## TASK-095 — Online learning + drift

### ▼ PASTE START
```text
TASK-095 — Online learning + concept drift detection (river integration).

Hypothesis: Static GBMs (TASK-031) decay as microstructure evolves. Online learning + drift detection maintains relevance with minimal retrain cost.

Files: services/agents/gbm_predictor/online.py + services/features/online_drift.py + libs/fincept-tools/drift_detector.py.

Implementation:
- river HoeffdingTree or AdaptiveRandomForest alongside batch LightGBM.
- Online: per-bar updates. Batch: nightly full window.
- Drift: ADWIN/DDM on residuals. Alert on detection.
- On drift: weight × 0.5 until next batch confirms recovery.
- Feature drift: PSI daily. PSI > 0.25 warn; > 0.5 pause.

LANDMINES:
- ONLINE UNDER-PERFORMS BATCH on stationary periods. Keep both; ensemble (~30% online normal, ~70% post-drift).
- ADWIN delta=0.002 fires on noise; tune delta=0.0001.
- ONLINE FEATURES must use SAME code path as offline (TASK-016/017).
- DRIFT ON LIVE > DRIFT ON PAPER. Don't auto-retrain on first 2 weeks live.

DONE WHEN:
- pytest green.
- Synthetic shift at known timestamp → ADWIN alerts within 200 samples.
- Live shadow: online+batch ensemble Sharpe ≥ batch-only (no degradation).
- Emits AlertEvent severity="warning" on drift.
- spec/tasks/TASK-095-online-drift.md authored.

VERIFY: uv run pytest services/agents/tests/test_online_drift.py -v
REPORT. CONTRACTS: §3 (Prediction), §7 (AlertEvent).
```
### ▲ PASTE END

## TASK-096 — L2 microstructure features

### ▼ PASTE START
```text
TASK-096 — L2 microstructure features (book imbalance, hidden liquidity, flow toxicity).

Hypothesis: Order-book microstructure contains short-horizon directional info invisible to bar features. Three signals: book imbalance, hidden liquidity, VPIN.

Files: services/features/microstructure.py + services/ingestor/binance_l2.py (extend).

Implementation:
- Book imbalance: (bid_size − ask_size)/(bid_size + ask_size) at top-N=5 levels.
- Hidden liquidity: (effective_spread − quoted_spread)/quoted_spread.
- VPIN: Easley/López de Prado, constant-volume buckets.
- Features → FeatureFrame.tags prefix "micro_".

LANDMINES:
- L2 INGESTION 100× L1 bandwidth. Sample top-5 + 100ms snapshot in prod.
- VENUE-SPECIFIC book formats. Per-venue normalizers.
- EQUITY L2 LICENSED with redistribution restrictions; v1 = crypto only.
- VPIN IS A REGIME INDICATOR, NOT A PREDICTION. High VPIN → reduce execution size, NOT short.
- LATENCY: book-imbalance feature within 50ms of book update.

DONE WHEN:
- pytest green.
- Live: 1h BTCUSDT L2 → micro features at >100Hz, p99 latency <50ms to feature store.
- Predictive: book imbalance IC ≥ 0.05 vs next-bar return on 1m bars over 3-month replay.
- spec/tasks/TASK-096-microstructure.md authored.

VERIFY: uv run pytest services/features/tests/test_microstructure.py -v
REPORT. CONTRACTS: §3 (FeatureFrame).
```
### ▲ PASTE END

## Phase Y — Exit verification

### ▼ PASTE START
```text
PHASE Y EXIT — DIFFERENTIATION GATE.

CHECKLIST:
1. WHO-ELSE AUDIT: every spec/tasks/TASK-09x.md begins with documented "who else publishes this signal" reviewed against EDGE_ROADMAP §4. The answer is NOT "every retail platform".
2. NON-EQUITY DIVERSIFICATION: ≥40% of new alpha attribution over the 12-week window comes from non-equity sources (onchain, macro, microstructure-on-crypto).
3. 12-WEEK SHADOW + PAPER: ensemble outperforms benchmark across ≥3 distinct macro regimes (per TASK-091 classifier).
4. CAPACITY STRESS: simulated 10× current AUM does not degrade Sharpe by more than 20%.
5. TAIL-HEDGE BUDGET ENFORCED: every live strategy has allocated tail-hedge budget; total spend during shadow ≤ configured cap.
6. CONCEPT DRIFT DETECTORS LIVE: TASK-095 running on all supervised agents; drift events documented; weight reductions applied where flagged.
7. ALT-DATA ROI: TASK-093 vendor either passed gate (kept) or failed gate (cancelled). No "indefinite trial".
8. ORTHOGONALITY: top eigenvalue ≤ 0.5 of total variance; ≥5 strategies with pairwise |corr| < 0.3.
9. mypy --strict clean across all Phase Y services.
10. TASK-090..096 specs exist; [x] in BUILD_ORDER.md.

If green: Phase Y COMPLETE. Add "Checkpoint Y: passed YYYY-MM-DD". Phase Z may begin.

If <3 macro regimes occurred: extend shadow ≥18 weeks before evaluation. Macro is slow.
If non-equity attribution <40%: iterate OR explicitly accept "differentiated on X+ axes alone".

REPORT.
```
### ▲ PASTE END

---

# Phase Z — Research Frontier

**Goal:** Frontier capabilities — options as alpha, generative scenarios, GNNs, causal inference, federated learning. Each module ships only after a published internal whitepaper with reproducible OOS evaluation.
**Checkpoint:** Each module independently meets Phase X+ criteria at scoped capital. Portfolio criterion: ≥2 of 5 modules ship to non-zero allocation within 18 months; ≥1 whitepaper published externally.
**Funding:** Phase X+ / Y alpha. Do not begin if those phases have not produced positive attributable alpha.

**Strategic context:** `spec/EDGE_ROADMAP.md §5`. **Full detail:** `spec/prompts/phase-Z-frontier.md`.

## Phase Z — Kickoff

### ▼ PASTE START
```text
ENTERING PHASE Z — RESEARCH FRONTIER.

Session opener norms apply. Phases F, D, B, A, O, U, X, H, X+, Y complete or in maintenance. Phase Z rules:

1. WHITEPAPER FIRST. 5–10 page internal whitepaper before any code: hypothesis, mechanism, prior art (≥10 papers), methodology, evaluation, expected effect size + CI, KILL CRITERIA.
2. KILL CRITERIA NON-NEGOTIABLE. Result misses → project killed; post-mortem; archive artifact; do NOT iterate to passing (= p-hacking).
3. REPRODUCIBLE OOS on data not existing at whitepaper time.
4. INDIVIDUAL X+ CRITERIA. Each module independently meets Phase X+ exit criteria at scoped capital before any production weight.
5. SHADOW ≥12 WEEKS. Frontier noise demands longer windows.
6. EXTERNAL REVIEW WELCOME. Senior external reviewer (former quant, academic, peer at another firm) under NDA before code.
7. DURABILITY OVER ELEGANCE. Boring projects compounding 5y > beautiful papers dead by year 3.
8. CONTRACTS ARE STILL IMMUTABLE EXCEPT VIA RFC. New event types possible but only via formal RFC; default extension via tags + audit_log.

CONTEXT: spec/EDGE_ROADMAP.md §5 (mandatory), spec/CONTRACTS.md (RFC for extensions).

Tasks: TASK-100..104. Recommended priority: 102 (graph) → 103 (causal) → 100 (options) → 101 (scenarios) → 104 (federated, only if multi-tenant deploy).

Acknowledge by listing the 8 rules. State which task you are starting. Confirm whitepaper EXISTS (not the idea — the document). Wait.
```
### ▲ PASTE END

## TASK-100 — Options as alpha

### ▼ PASTE START
```text
TASK-100 — Options as alpha (vol-harvesting, dispersion, asymmetric event).

Hypothesis: Options markets contain THREE durable alpha sources: (a) equity volatility risk premium (selling SPX vol +EV), (b) dispersion (index IV > weighted constituent IV due to correlation premium), (c) asymmetric event-driven (long gamma+vega 24h pre-event).

Files: services/agents/options_alpha/{main,vol_harvest,dispersion,asymmetric_event}.py + services/oms/venue/options.py + libs/fincept-tools/options_pricing.py.

Whitepaper requirements (BEFORE code): literature review, specific strategy spec, kill criteria per sub-strategy.

LANDMINES:
- TAIL RISK BIGGER than Phase X+ tail hedge. Hard cap = 50% of expected return per strategy; CIRCUIT BREAKER on VIX > 35.
- HEDGING DRIFT eats 50–70% of vol premium empirically. Conservative expected returns.
- DISPERSION CAPACITY: small-cap vol illiquid; capacity $5–$20M notional; document curve.
- EVENT GAMMA: pre-event IV already high; verify empirically per event type.
- LICENSING: real options trading requires options-permission account; v1 = sandbox.

DONE WHEN:
- Whitepaper merged + ≥1 external reviewer.
- pytest green.
- Backtest 2010–2023: vol-harvest Sharpe ≥ 0.7, dispersion ≥ 0.5, event N≥12 with positive avg.
- 12-week shadow: ≥1 of 3 sub-strategies meets X+ criteria at scoped capital.
- spec/tasks/TASK-100-options-alpha.md authored with whitepaper link + kill criteria.

VERIFY: uv run pytest services/agents/tests/test_options_alpha.py -v
REPORT.
```
### ▲ PASTE END

## TASK-101 — Generative scenario simulation

### ▼ PASTE START
```text
TASK-101 — Generative scenario simulation (TimeGAN / diffusion / flows).

Hypothesis: Backtests sample only OBSERVED return distribution. Generative models produce SYNTHETIC scenarios preserving covariance, vol clustering, jumps. Use for stress-testing, NOT for trading scenarios.

Files: services/agents/scenario_gan/{main,trainer,sampler,validator}.py + libs/fincept-tools/scenarios.py.

Whitepaper requirements: literature review (TimeGAN, CSDI, Lopez de Prado), architecture, validation (Wasserstein, KS, ACF, tail-stat), kill criterion (5/6 statistical tests fail vs holdout).

LANDMINES:
- MODE COLLAPSE: GANs generate narrow distributions. Validate diversity; consider diffusion if collapse.
- LOOK-AHEAD via training: train on rolling window, test on subsequent unseen window.
- TAIL EXTRAPOLATION: model trained without 1987/2008/2020 cannot generate one. Don't claim tail safety.
- THIS IS RESEARCH SCAFFOLDING, NOT ALPHA. Generate to stress; do NOT trade scenarios.

DONE WHEN:
- Whitepaper merged + reviewed.
- pytest green.
- Generator passes ≥5/6 statistical tests on holdout.
- Stress-test report: orchestrator + risk gate against 1000 scenarios; worst-case bounded.
- spec/tasks/TASK-101-scenario-gan.md authored.

VERIFY: uv run pytest services/agents/tests/test_scenario_gan.py -v
REPORT.
```
### ▲ PASTE END

## TASK-102 — GNN supply-chain

### ▼ PASTE START
```text
TASK-102 — GNN over supply-chain + customer-supplier graphs.

Hypothesis: Firm price reflects supplier + customer health. Standard ML treats firms as independent. GNNs explicitly model graph; shocks propagate 2–6 weeks before being priced.

Files: services/agents/gnn/{main,graph_builder,gnn_model,inference}.py + services/ingestor/supply_chain.py.

Whitepaper requirements: literature (Cohen-Frazzini, Menzly-Ozbas, GNN-finance), graph spec (10-K + LLM news + optional Factset), model (GraphSAGE/GAT), kill criterion (GNN IC ≤ baseline GBM IC = no graph value).

LANDMINES:
- GRAPH CONSTRUCTION 80% OF WORK. Validate edge precision via random sampling + manual review.
- DATA-LEAKAGE via graph: customer firm price → strict PIT validation.
- COLD-START: new IPO has no graph; default zero-edge → reverts to GBM behavior; flag low-confidence.
- INTERPRETABILITY: GNNExplainer or SubgraphX for top predictions.
- COMPUTE: ≥5000 firms × 10y monthly = nontrivial GPU cost. Budget first.

DONE WHEN:
- Whitepaper merged + reviewed.
- pytest green.
- Graph for ≥500 firms with edge precision ≥80% (random sample manual review).
- 5y walk-forward: GNN IC > GBM IC at 60d horizon by ≥0.02.
- 12-week shadow meets X+ criteria at scoped capital.
- spec/tasks/TASK-102-gnn.md authored.

VERIFY: uv run pytest services/agents/tests/test_gnn.py -v
REPORT.
```
### ▲ PASTE END

## TASK-103 — Causal inference layer

### ▼ PASTE START
```text
TASK-103 — Causal inference layer (DoWhy / EconML).

Hypothesis: ML predictions are CORRELATIONAL. Causal techniques (IV, RD, propensity, double-ML) separate correlation from causation, enabling counterfactual P&L, confound detection, execution-effect analysis.

Files: services/agents/causal/{main,dowhy_wrapper,counterfactual}.py + services/jobs/causal_attribution.py.

Whitepaper requirements: literature (Pearl, Chernozhukov, Athey-Imbens, Lopez de Prado), specific applications (counterfactual P&L, confound detection, execution-effect), kill criterion (causal vs naïve attribution differs <10% over 6mo = no information).

LANDMINES:
- CAUSAL NEEDS ASSUMPTIONS. State; sensitivity-test.
- IV NEEDS AN INSTRUMENT. Most finance contexts lack natural ones; document carefully.
- THIS IS DIAGNOSTIC, NOT ALPHA. Do NOT generate Predictions; use to interpret/debug.
- DON'T OVERCLAIM. "Consistent with causal X under assumptions Y", not "X causes Y".

DONE WHEN:
- Whitepaper merged + reviewed.
- pytest green.
- 3 case studies on production strategies: causal vs naïve differs ≥10% in ≥1; documented.
- Counterfactual report monthly auto-generated to attribution dashboard.
- spec/tasks/TASK-103-causal.md authored.

VERIFY: uv run pytest services/agents/tests/test_causal.py -v
REPORT.
```
### ▲ PASTE END

## TASK-104 — Federated learning

### ▼ PASTE START
```text
TASK-104 — Federated learning across tenants (CONDITIONAL on multi-tenant deploy).

GATING: ONLY if multi-tenant ≥3 customers + signed federation consent. Single-tenant/self-hosted = N/A; mark in BUILD_ORDER.md.

Hypothesis: Federated learning trains shared models on aggregate signal across customers WITHOUT exposing per-customer data. Result: better-than-single-tenant models with privacy preserved.

Files: services/agents/fedlearn/{main,coordinator,worker,aggregator}.py + libs/fincept-core/federation.py.

Whitepaper requirements: literature (FedAvg, secure aggregation, DP in FL), architecture (local LightGBM → gradient → DP-aggregated → redistributed), consent flow, kill criterion (federated OOS ≤ single-tenant OOS = no value).

LANDMINES:
- PRIVACY GUARANTEES SUBTLE. Hire privacy expert.
- FREE-RIDER RISK. Reputation/contribution-weighted aggregation.
- LEGAL: GDPR/CCPA cross-jurisdiction. Document data flows; customer legal review.
- REGULATORY: MiFID etc. complicate audit.
- ATTACK SURFACE: malicious customers can poison. Robust aggregation (median/trimmed-mean, Byzantine-tolerant), NOT naïve averaging.

DONE WHEN:
- Whitepaper merged + reviewed (incl external privacy expert).
- pytest green for synthetic 5-tenant simulation: federated > any single-tenant on OOS.
- Privacy: ε ≤ 1 per round; total ε accounted across rounds.
- Customer consent flow shipped in UI.
- spec/tasks/TASK-104-fedlearn.md authored with whitepaper + kill criteria + legal review log.

VERIFY: uv run pytest services/agents/tests/test_fedlearn.py -v
REPORT.
```
### ▲ PASTE END

## Phase Z — Exit verification

### ▼ PASTE START
```text
PHASE Z EXIT — RESEARCH FRONTIER GATE.

Phase Z does NOT have a single exit checkpoint. Each module exits on its OWN terms:

PER-TASK exit criteria:
1. Whitepaper merged + ≥1 external reviewer signed off.
2. Reproducible OOS evaluation passes (training + post-training holdout).
3. Module independently meets Phase X+ criteria at scoped capital.
4. Kill criteria explicitly checked; if missed, project killed (NOT iterated to passing).
5. spec/tasks/TASK-1XX.md authored with whitepaper link, kill criteria, final result documented.
6. mypy --strict clean, pytest green.

PORTFOLIO criteria (system as a whole):
- ≥2 of 5 modules ship to non-zero allocation within 18 months of Phase Z kickoff.
- ≥1 whitepaper published externally (open source or SSRN) — recruitment + intellectual-moat.
- Phase Z modules contribute ≥10% of total ensemble Sharpe at year 2.

If <2 ship: Phase Z partially validated. Slow Phase Z headcount; prioritize X+/Y maintenance.
If ≥1 published externally: recruitment + moat win independent of P&L; continue investment.

REPORT cadence: quarterly review with whitepaper merges, kill events, P&L attribution.
```
### ▲ PASTE END

---

# End of paste-ready prompts

All phases F, D, B, A, O, U, X, H, X+, Y, Z are now authored in this document. See `spec/BUILD_ORDER.md` for sequencing and `spec/EDGE_ROADMAP.md` for the alpha-tier strategic thesis behind X+/Y/Z.

