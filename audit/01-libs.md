# Audit: libs/ (Shared Libraries)

## Executive Summary

`libs/` contains five shared libraries that form the foundation of the
fincept-terminal platform: `fincept-core` (types/config/storage/events),
`fincept-bus` (Redis Streams messaging), `fincept-db` (SQLAlchemy +
TimescaleDB persistence), `fincept-sdk` (strategy author interface), and
`fincept-tools` (typed LLM/data/research tool registry).

The layering is clean and acyclic: `fincept-core` is the leaf dependency
depended on by all four others; `fincept-bus` and `fincept-db` depend only
on core; `fincept-sdk` depends only on core; `fincept-tools` depends on
core, db, and bus. No library imports from `services/`, preserving the
invariant that libs are below services in the dependency graph. Services
import from libs heavily (~450 `from fincept_*` matches across
`services/`).

Overall quality is **high** — schemas are frozen Pydantic v2 models with
`extra="forbid"`, the event spine is well-typed, the storage abstraction
has real path-traversal defense, the tool protocol has a coherent
typed-error + observability story, and the strategy SDK is minimal and
correct. Tests exist for every library and are mostly meaningful
(round-trips, edge cases, failure modes), not just smoke tests.

That said, the audit found a non-trivial number of real issues:

- **Bugs / correctness**: duplicate dead `return` statements in
  `events.py` and `heartbeat.py`; `clock.iso_to_ns` uses float
  multiplication that loses nanosecond precision; `bars.read_bars`
  discards the original `ts_recv` and substitutes `ts_event`; the
  `consumer._handle_message` "backpressure" timeout conflates the
  `block_ms` xreadgroup parameter with a handler deadline.
- **Design smells**: `exec.tools` publishes a bespoke envelope shape that
  bypasses the `fincept-bus` `Event`/`serialize`/`deserialize` contract;
  every Redis-touching tool opens a fresh `Redis.from_url(...)` per call
  with no pooling; `analytics.*` tools read the entire bar history then
  slice in Python instead of constraining the SQL window; the
  `core.config.Settings` singleton is implemented via `__new__` which is
  brittle under pydantic-settings.
- **DRY violations**: `_BAD_NAME_CHARS` / `_validate_agent_id` is copied
  verbatim across four core modules; a `.env` filesystem walker is
  duplicated in `research/exa.py` and `research/openbb.py`.
- **Scaling gaps**: filesystem append-only stores
  (`prediction_log`, `settlement`, `feature_snapshot`) have no rotation,
  no cross-process locking, and `read` paths that scan whole files;
  `audit.list_recent_orders` materializes the entire `oms.state` audit
  log into Python before collapsing; `settlement.read` scans every agent
  file per prediction lookup.
- **Test gaps**: `evidence_redaction.py` (security-critical secret
  redaction) has **no unit tests** in `libs/fincept-db/tests/`;
  `provider_receipts.py` has none; the `EventPayload` union ↔
  `_EVENT_SCHEMAS` mapping in `events.py` has no sync-invariant test;
  `fincept-bus` has no dead-letter / poison-message test.

None of these are show-stoppers for the current paper-trading scope, but
several will bite at production volume or when live trading lands. They
are itemised per-library below with file paths and line numbers.

---

## Library: fincept-core

### Purpose

The foundational library: canonical event/market schemas, process
configuration, structured logging, OpenTelemetry tracing, ID generation,
clock primitives, HTTP retry helper, Redis leader election + heartbeat,
filesystem-backed stores (predictions, settlements, feature snapshots,
strategy configs), provider-agnostic storage (local + S3), the ML
dataset evidence spine (approved roots, manifests, cross-validation,
dossier/calibration helpers), and shared portfolio math.

### Layout

```
src/fincept_core/
  __init__.py            # re-exports submodules (subset)
  clock.py               # Clock ABC + now_ns / ns_to_iso / iso_to_ns
  config.py              # pydantic-settings Settings + assert_safe_for_runtime
  errors.py              # FinceptError hierarchy
  events.py              # Event envelope + make/parse/serialize/deserialize
  heartbeat.py           # Redis liveness heartbeat
  http.py                # httpx retry wrapper + build_http_client
  ids.py                 # ULID + blake2b idempotency key
  leadership.py          # Redis leader election (Lua-scripted)
  logging.py             # structlog configuration + correlation_id
  portfolio.py           # apply_fill_to_position (shared position math)
  prediction_log.py      # filesystem JSONL prediction log
  schemas.py             # all event/market/order/position Pydantic models
  storage.py             # StorageBackend ABC + Local + S3 + factory
  strategy_config.py     # filesystem strategy config store + history
  tracing.py             # OTel TracerProvider setup
  datasets/
    __init__.py          # facade re-exporting the evidence spine
    approved_roots.py    # fail-closed filesystem root allowlist
    cv.py                # walk-forward fold + window math
    dossier.py           # dossier + calibration sidecar builders
    feature_snapshot.py  # filesystem feature-snapshot JSONL store
    schemas.py           # manifest / snapshot schemas (separate from events)
    settlement.py        # filesystem settlement ledger + look-ahead guard
```

### How Each Module Works

- **`schemas.py`** — Defines `Venue`, `AssetClass`, `Side`, `OrderType`,
  `TimeInForce`, `OrderStatus` StrEnums plus all event models
  (`MarketEvent` base, `TradeEvent`, `BookDeltaEvent`,
  `BookSnapshotEvent`, `BarEvent`, `Prediction`, `SentimentSignal`,
  `NewsImpactSignal`, `InformationEvent`, `RegimeSignal`, `Decision`,
  `OrderIntent`, `Order`, `Fill`, `Position`, `AlertEvent`,
  `FeatureFrame`, `RiskCheckResult`). Every model is
  `ConfigDict(frozen=True)` and most use `extra="forbid"`. `Position`
  (line 258) is the notable exception: it uses `extra="forbid"` but is
  **not** frozen — `portfolio.apply_fill_to_position` relies on
  `model_copy(update=...)` rather than mutation, so this is fine, but
  the inconsistency is unexplained.

- **`events.py`** — `Event` is a frozen envelope `{type, payload}` where
  `payload` is a union of all 16 schema models. `_EVENT_SCHEMAS`
  (lines 30-47) maps the string `type` to the model class.
  `make_event` (line 63) coerces a dict into the right model, injecting
  `event_type` only for schemas that declare it (the `event_type` field
  exists on market events, alerts, feature frames but not on
  Order/Fill/Position). `serialize` (line 89) flattens to a
  `{event_id, published_at, type, payload-json}` dict for Redis `xadd`.
  `deserialize` (line 98) decodes bytes keys/values and calls
  `parse_event`.

- **`config.py`** — `Settings(BaseSettings)` with `env_prefix="FINCEPT_"`
  reading `.env` from CWD. Holds DB_URL, REDIS_URL, API keys, JWT secret,
  risk limits, universe. `assert_safe_for_runtime` (line 128) is the
  startup guard that refuses to run in staging/production with the dev
  default JWT secret — invoked by every service entrypoint (enforced by
  the source-inspection test `test_startup_safety_matrix.py`).

- **`storage.py`** — `StorageBackend` ABC with `LocalStorageBackend`
  (`file://` + bare paths) and `S3StorageBackend` (lazy `boto3` import).
  `parse_s3_uri` / `parse_file_uri` reject `..` traversal and handle
  Windows drive-letter paths. `get_storage_backend()` is a cached
  singleton factory keyed off `StorageConfig` env vars
  (`FINCEPT_STORAGE_BACKEND`, default `local`).

- **`portfolio.py`** — `apply_fill_to_position(prev, fill, *, strategy_id)`
  implements the four cases (open, add-to-side, exact close, cross-flip)
  with explicit `Decimal` math and weighted-average cost. Documented as
  the single source of truth shared by `services/backtester/engine.py`
  and `services/portfolio/`.

- **`prediction_log.py` / `settlement.py` / `feature_snapshot.py` /
  `strategy_config.py`** — Four filesystem JSONL/JSON stores, each
  append-only, each with its own copy of `_BAD_NAME_CHARS` and
  `_validate_agent_id`. `settlement.SettlementStore.append` enforces a
  look-ahead guard (`decision_window_end_ns <= now_ns`) and a
  `(prediction_id, cost_model_version)` idempotency key with a
  `duplicate` error code. `strategy_config.StrategyConfigStore.upsert`
  does atomic temp-file-then-rename for the current config and appends
  to a `.history.jsonl` audit trail; `set_enabled` is idempotent;
  `delete` writes a tombstone to history.

- **`datasets/approved_roots.py`** — Fail-closed filesystem gate. Rejects
  `..`, resolves symlinks, and (by default) rejects any symlinked
  component on the path from root to leaf to block TOCTOU swaps. Roots
  never echoed in error messages.

- **`datasets/cv.py`** — `Fold` (bar-index space) + `WalkForwardWindow`
  (nanosecond space) Pydantic models, with `make_folds` and
  `derive_walk_forward_window`. Documented as the single shared home for
  walk-forward math used by both backtester and quant_foundry.

- **`leadership.py`** — Redis leader election: `SET NX EX` to acquire,
  Lua scripts for token-checked renew/release. Renewal loop runs every
  `ttl/3`.

- **`heartbeat.py`** — `beat_periodically` writes
  `service:heartbeat:{name}` with TTL every `interval_sec`; `read_all`
  scans the keyspace for the dashboard.

- **`http.py`** — `http_request` wraps `httpx.AsyncClient.request` with
  capped exponential backoff + jitter, retrying only on
  `ConnectError`/`ReadTimeout`/`WriteTimeout`/`PoolTimeout` and statuses
  `{408, 425, 429, 500, 502, 503, 504}`. Stamps `X-Request-ID`.

### Public API

Re-exported from `fincept_core.__init__`: `clock, config, errors, events,
ids, leadership, logging, schemas, storage, tracing` (as submodules).
**Not** re-exported: `http`, `heartbeat`, `portfolio`, `prediction_log`,
`strategy_config`, `datasets` — callers must use the full path
(`from fincept_core.http import http_request`). This is an inconsistency:
there is no reason `http` and `portfolio` shouldn't be in the top-level
surface while `storage` is.

### Connections

Consumed by every service (api, ingestor, orchestrator, oms,
strategy_host, features, jobs, portfolio, agents/*, backtester,
quant_foundry, risk, settlements). `fincept-bus`/`-db`/`-sdk`/`-tools`
all depend on it. `pyproject.toml` deps: pydantic, pydantic-settings,
structlog, opentelemetry-api/sdk/exporter-otlp, python-ulid, redis.

### What's Optimally Implemented

- **Frozen, `extra="forbid"` Pydantic v2 schemas** — tamper-evident,
  forward-incompatible-by-default. The right default for an event spine.
- **`portfolio.apply_fill_to_position`** — explicit four-case Decimal
  math, single source of truth, pinned by tests in both backtester and
  portfolio services. Excellent.
- **`storage.py` path-traversal defense** — rejects `..` on both local
  and S3 paths, handles Windows drive-letter quirks, lazy boto3 import
  so local-only deployments don't need it. Well-engineered.
- **`approved_roots.py`** — fail-closed, symlink-TOCTOU-aware, never
  leaks the root list in errors. Security-conscious.
- **`settlement.py` look-ahead guard + idempotency key** — refuses to
  settle before the horizon elapses; `(prediction_id, cost_model_version)`
  duplicate detection with a machine-readable `code`. Correct by
  construction.
- **`http.py` retry policy** — correctly avoids retrying 4xx (other than
  408/425/429), capped backoff with jitter, request-id propagation.
- **`assert_safe_for_runtime`** + the source-inspection test matrix — a
  genuinely good guard against deploying dev secrets to prod, enforced
  by AST inspection of every entrypoint.

### What Needs Work

- **Duplicate dead `return` statements** — `events.py:105-106` and
  `heartbeat.py:91-92` both end with two identical `return` lines. Dead
  code, harmless, but signals copy-paste sloppiness and should be
  removed.
  ```python
  # events.py:98-106
  def deserialize(fields):
      ...
      return parse_event({"type": decoded["type"], "payload": decoded["payload"]})
      return parse_event({"type": decoded["type"], "payload": decoded["payload"]})  # dead
  ```
- **`clock.iso_to_ns` loses precision** (clock.py:22-23):
  ```python
  def iso_to_ns(iso: str) -> int:
      return int(_dt.datetime.fromisoformat(iso).timestamp() * 1_000_000_000)
  ```
  `timestamp()` returns a float; multiplying by 1e9 and truncating
  introduces rounding error at the nanosecond scale for any timestamp
  past ~1973. Should use integer arithmetic on the `datetime` components
  (`days * 86400*1e9 + seconds*1e9 + microseconds*1000`). The inverse
  `ns_to_iso` is fine.
- **`errors.ConnectionError` shadows the builtin** (errors.py:13) —
  `fincept_core.errors.ConnectionError` is a distinct class but anyone
  who does `from fincept_core.errors import ConnectionError` (or
  `import *`) loses access to the builtin. Not exported from
  `__init__.py` today, but a footgun. Rename to `BusConnectionError` or
  `RedisConnectionError`.
- **`config.Settings` singleton via `__new__`** (config.py:103-107) —
  overrides `__new__` to cache a single instance, but pydantic-settings
  `__init__` still runs on every `Settings()` call, re-parsing env vars
  and re-validating. The cache only avoids re-allocating the object. The
  `clear_cache` mechanism is manual. This is an unusual and brittle
  pattern; a module-level `lru_cache` on a `_build_settings()` helper or
  a plain global like the one in `storage.py`/`engine.py` would be
  clearer.
- **`__init__.py` export surface is inconsistent** — `http`,
  `heartbeat`, `portfolio`, `prediction_log`, `strategy_config`, and the
  whole `datasets` package are not in `__all__` despite being core
  functionality. `test_imports.py` only checks the re-exported subset,
  so the gap is invisible to the import-smoke test.
- **`_BAD_NAME_CHARS` / `_validate_agent_id` duplicated four times** —
  in `prediction_log.py:69-78`, `settlement.py:78-87`,
  `feature_snapshot.py:72-81`, and `strategy_config.py:77-89` (the last
  raises `StrategyConfigError` instead of `ValueError` so it's not
  literally identical). Each copy includes a comment saying "keep in
  sync" — that is exactly the failure mode a shared helper avoids.
  Extract to `fincept_core.fs_names` or similar.
- **`leadership._loop` has no Redis-error tolerance** (leadership.py:68-74)
  — a transient `redis.exceptions.ConnectionError` inside `_step`
  propagates up and kills the leader task; the service keeps running
  believing it is (or isn't) leader based on stale state. Should catch
  transient Redis errors, log, and continue.
- **`heartbeat.read_all` uses `scan_iter`** (heartbeat.py:81) — fine for
  small keyspaces, but `SCAN` over a large Redis DB is non-trivial;
  there is no `COUNT` tuning hook beyond the hardcoded `100`.

### What Might Break

- **Filesystem stores under concurrent multi-process writers** — the
  comments in `prediction_log.py:230-235`, `settlement.py:296-299`, and
  `feature_snapshot.py:175-179` claim atomicity for sub-`PIPE_BUF`
  writes on POSIX. That holds for a single `f.write(line + "\n")` on
  POSIX, but (a) Python's buffered text-mode `write` does not guarantee
  a single `write(2)` syscall, and (b) on Windows NTFS the atomicity
  guarantee does not apply to concurrent appends across processes. Two
  agents writing to the same `<agent_id>.jsonl` from different processes
  could interleave partial lines. At paper-trading volumes this is
  unlikely; at scale it will corrupt a log line (which the readers do
  tolerate by skipping malformed lines — so it degrades to silent data
  loss rather than a crash).
- **`prediction_log.read` reads the whole file into memory** (line 275)
  then sorts. Comment acknowledges this is fine <100MB and promises
  rotation "once they cross that threshold". There is no rotation today
  and no guard that fails loudly when the threshold is crossed — a
  long-running agent will slowly degrade.
- **`settlement.read` scans every agent file per prediction lookup**
  (line 318) — O(total records) per call. The dashboard's
  `GET /models/{name}/outcomes` joins via this path; at scale it will
  be a hot O(n) scan with no index.
- **`strategy_config.list_all`** (line 240) globs `*.json` and parses
  each; a single corrupt file is skipped with a warning, but a
  directory with thousands of strategies does thousands of synchronous
  file reads on every dashboard refresh.

### What Isn't Implemented Yet

- No file rotation for any JSONL store.
- No cross-process locking (`fcntl`/`msvcrt.locking`) on the append
  paths.
- No shared `_validate_agent_id` helper.
- `tracing.py` has no span exporter fallback (no `ConsoleSpanExporter`
  for dev when OTLP endpoint is unreachable — it silently no-ops if
  `endpoint` is empty, line 21-22).
- `logging.configure` is idempotent but never re-configures if
  `LOG_LEVEL` changes at runtime — there is no `reconfigure()`.

### Better Approaches

- Replace the `__new__` singleton in `config.py` with
  `@functools.lru_cache(maxsize=1)` on a `_build_settings()` function;
  expose `get_settings()` (already exists) as the only entry point and
  make `Settings()` non-cached for tests.
- Extract `fincept_core.fs_safety` with `validate_safe_name(name,
  error_cls=...)` and have the four stores call it.
- Add a `rotate_if_large()` helper to the JSONL stores and a
  `--max-bytes` env knob.
- Use `os.open(O_APPEND|O_WRONLY)` + `os.write(fd, line.encode())` for
  the append path to guarantee a single syscall (and atomicity on POSIX
  for writes < `PIPE_BUF`).
- Re-export `http`, `portfolio`, `heartbeat`, `prediction_log`,
  `strategy_config`, `datasets` from `fincept_core.__init__` and extend
  `test_imports.py` to cover them.

---

## Library: fincept-bus

### Purpose

Thin Redis Streams messaging layer: stream name constants + retention
budgets, a `Producer` that serializes `Event`s, and a `Consumer` that
runs consumer groups with stale-entry reclaim and a backpressure
timeout.

### Layout

```
src/fincept_bus/
  __init__.py    # re-exports Consumer, Producer, ConsumerGroupName, StreamID
  streams.py     # 15 STREAM_* constants + RETENTION dict
  types.py       # StreamID = str, ConsumerGroupName = str (2 lines)
  producer.py    # Producer.publish
  consumer.py    # Consumer.consume / ensure_group / claim_pending
```

### How Each Module Works

- **`streams.py`** — 15 hardcoded stream names (`md.trades`, `md.books`,
  `md.bars.1m`, `sig.predict`, `sig.sentiment`, `sig.regime`,
  `sig.news_impact`, `info.raw`, `info.enriched`, `ord.decisions`,
  `ord.orders`, `ord.fills`, `ord.positions`, `events.alerts`,
  `features.online`) and a `RETENTION` dict mapping each to a
  `maxlen`-style cap (e.g. `md.trades: 1_000_000`,
  `features.online: 5_000_000`).
- **`producer.py`** — `Producer.publish(stream, event)` calls
  `redis.xadd(stream, serialize(event, new_id(), now_ns()),
  maxlen=RETENTION.get(stream), approximate=True)`. Returns the
  message id decoded to `str`.
- **`consumer.py`** — `Consumer.consume(...)` is an infinite loop:
  `ensure_groups` creates the group with `xgroup_create(id="0",
  mkstream=True)` (swallowing `BUSYGROUP`), then each iteration calls
  `_claim_stale` (reclaim pending entries idle > `claim_idle_ms`) and
  `xreadgroup` with `block_ms`. `_handle_message` deserializes the
  fields, calls the handler, measures elapsed ns, and **raises
  `TimeoutError` if the handler took longer than `block_ms * 1_000_000`
  ns** (line 128); on success it `xack`s. Handler exceptions are caught
  and return `False` (message stays pending, not acked).

### Public API

`Consumer`, `Producer`, `ConsumerGroupName`, `StreamID` from
`__init__.py`. The `streams` module is imported by path
(`from fincept_bus.streams import STREAM_ORDERS`).

### Connections

Depends on `fincept-core` (events, clock, ids) and `redis[hiredis]`.
Consumed by `services/orchestrator`, `oms`, `ingestor`, `agents/*`,
`features`, `portfolio`, `settlements`, and `fincept-tools/exec` and
`fincept-tools/data` (which publish/read streams directly).

### What's Optimally Implemented

- **`Producer` is minimal and correct** — uses `approximate=True` for
  `MAXLEN ~` which is the right choice for throughput.
- **`ensure_group` swallows only `BUSYGROUP`** (line 60) — re-raises
  other `ResponseError`s. Correct idempotent group creation.
- **`claim_pending` filters by `time_since_delivered >= min_idle_ms`**
  (line 79) rather than claiming everything — avoids stealing messages
  from a slow-but-alive consumer.
- **Test suite is genuinely good** — `test_consumer.py` covers
  ack-on-success, leave-pending-on-failure, crash recovery via
  `claim_pending`, a 1000-event integration round-trip with no loss,
  and a (skipped) p99 latency assertion. `test_producer.py` verifies
  serialization shape and retention usage.

### What Needs Work

- **The "backpressure" timeout is semantically wrong** (consumer.py:128):
  ```python
  if elapsed_ns > block_ms * 1_000_000:
      raise TimeoutError("consumer handler exceeded block_ms")
  ```
  `block_ms` is the `xreadgroup` *blocking* parameter (how long Redis
  waits if no new messages), not a handler deadline. A handler that
  legitimately takes 5 ms with `block_ms=1` will raise `TimeoutError`
  even though nothing is wrong. `test_slow_handler_violates_backpressure_contract`
  codifies this behaviour as intentional, but the contract is misnamed.
  If a handler deadline is wanted, it should be a separate
  `handler_timeout_ms` parameter with its own default.
- **Silent handler failures** (consumer.py:125-126) — `except Exception:
  return False` swallows the exception with no log. A perpetually-failing
  handler produces no diagnostic until someone notices the pending
  count climbing. At minimum `log.warning("consumer.handler_failed",
  stream=..., error=...)`.
- **No dead-letter / poison-queue** — a message whose handler always
  raises stays pending forever; `claim_pending` will re-deliver it on
  every recovery pass, fail again, and never be discarded. There is no
  `xack`-after-N-failures escape hatch. At production scale this is a
  classic stream-stuck incident.
- **`_claim_stale` runs on every loop iteration** (line 37) before
  `xreadgroup` — for each stream it issues `xpending_range` +
  potentially `xclaim`. With many streams this is per-iteration
  overhead even when nothing is stale.
- **`types.py` is two lines** — `StreamID = str` and
  `ConsumerGroupName = str`. Not worth a module; could live in
  `consumer.py` or `streams.py`.

### What Might Break

- **`consume` has no graceful shutdown** beyond `CancelledError` — the
  infinite `while True` (line 36) is fine for a task that gets
  cancelled, but there is no drain: in-flight `xreadgroup` blocks for
  up to `block_ms` after cancellation, and a handler mid-execution is
  not interrupted.
- **No retry/backoff between failed deliveries** — a failing message is
  re-delivered as soon as it crosses `claim_idle_ms` again, in a tight
  loop. Combined with the silent failure, this is a CPU-burn + log-gap
  scenario.
- **`xreadgroup` with `stream_offsets = {stream: ">" for stream in
  streams}`** (line 35) — never reads the pending list via `xreadgroup`
  (which requires `0` not `>`). Pending entries are only recovered via
  the separate `claim_pending` path. If `_claim_stale` is disabled or
  fails, pending entries are never re-delivered through `xreadgroup`.

### What Isn't Implemented Yet

- Dead-letter queue / poison-message discard.
- Handler deadline parameter distinct from `block_ms`.
- Logging on handler failure.
- Batch ack (acks one-at-a-time per message).
- A `stop()` method / `asyncio.Event` for graceful drain.

### Better Approaches

- Introduce `handler_timeout_ms: int | None = None` separate from
  `block_ms`; only enforce if set.
- Add `max_delivery_attempts: int` — after N failed deliveries, `xack`
  the message and publish it to a `<stream>.dlq` stream with the
  failure reason.
- Log every handler exception via `structlog` (the bus already depends
  on `fincept_core.logging`).
- Collapse `types.py` into `streams.py` or `consumer.py`.

---

## Library: fincept-db

### Purpose

SQLAlchemy 2.x async + TimescaleDB persistence: ORM models, async
engine/session factory, read/write helpers for trades/bars/book
deltas/features/audit/universe/provider data, Alembic migrations, and
the provider-evidence redaction + receipt helpers (TASK-0205).

### Layout

```
src/fincept_db/
  __init__.py              # Windows cyextension workaround + re-exports
  engine.py                # async engine/sessionmaker singletons + session_scope
  models.py                # ORM: Trade, Bar, BookDelta, AuditLog, Strategy,
                           #        UniverseSymbol, Feature, ProviderData
  bars.py                  # write_bars / read_bars / read_bar_coverage
  ticks.py                 # write/read trades + book deltas
  features.py              # write_features / read_features / read_latest_feature
  audit.py                 # append / read_by_correlation / list_recent_orders
  universe.py              # read_universe / upsert_universe_symbols
  provider_data.py         # build_*_record helpers + write/read provider_data
  provider_receipts.py     # ProviderEvidenceReceipt + freshness classification
  evidence_redaction.py    # redact_string / redact_dict (secret scrubbing)
  migrations/
    env.py
    versions/0001_initial.py     # tables + hypertables + compression + retention
    versions/0002_features.py
    versions/0003_provider_data.py
```

### How Each Module Works

- **`engine.py`** — `get_engine()` lazily builds a singleton
  `AsyncEngine` from `Settings.DB_URL` (`pool_size=20, max_overflow=10,
  pool_pre_ping=True`); `FINCEPT_DB_TEST_NULLPOOL=1` switches to
  `NullPool` for tests. `session_scope()` is an async context manager
  that commits on clean exit and rolls back on exception.
- **`models.py`** — `DeclarativeBase` + 8 tables. Prices/sizes use
  `Numeric(28, 12)`. `BookDelta.payload`, `AuditLog.payload`,
  `Strategy.config`, `Feature.values`/`tags`, `ProviderData.request`/
  `normalized`/`raw` are `JSONB`. Indexes on
  `(symbol, ts_event)`-style access patterns.
- **`bars.py` / `ticks.py` / `features.py`** — Each `write_*` builds
  rows from the core schemas and does a Postgres
  `INSERT ... ON CONFLICT DO UPDATE` (bars, features) or `DO NOTHING`
  (trades, book deltas) on the primary key. `read_*` rebuilds the core
  schema models from rows.
- **`audit.py`** — `append` inserts a ULID-keyed row with
  `ON CONFLICT DO NOTHING` (so a collision is silently dropped, but the
  function returns a *new* ULID each call — the test
  `test_append_is_append_only_idempotent_on_collision` confirms two
  calls produce two distinct ids and two rows).
  `list_recent_orders` loads all `oms.state` rows ordered by
  `(correlation_id, ts_event desc)` and collapses to the latest per
  `correlation_id` in Python.
- **`universe.py`** — `read_universe` returns plain dicts (API-friendly,
  no ORM leakage). `upsert_universe_symbols` uppercases symbols and
  upserts, but on conflict only updates `active` — **not**
  `asset_class` or `venue_default`.
- **`provider_data.py`** (544 lines) — `build_exa_record`,
  `build_openbb_quote_record`, `build_openbb_call_record`,
  `build_alpaca_news_record`, `build_alpaca_mark_record` each normalise
  a provider response into a `ProviderDataRecord`. `write_provider_data`
  upserts on `(record_id, ts_event)`. `_json_safe` coerces
  `Decimal`/`datetime`/`date` to JSON-safe shapes.
- **`provider_receipts.py`** — `ProviderEvidenceReceipt` frozen
  dataclass with freshness status (`fresh`/`stale`/`degraded`/`unknown`)
  and redaction metadata. `build_evidence_receipt` redacts the request
  dict before constructing the receipt.
- **`evidence_redaction.py`** — `redact_string` applies six regex
  patterns (credential URLs, query-param secrets, bearer tokens, API
  key prefixes, `key=`/`token=` KV secrets, generic 32+ char tokens).
  `redact_dict` recursively redacts values under sensitive field names
  (`api_key`, `token`, `secret`, `password`, `authorization`, etc.) and
  any string value containing token-shaped substrings.

### Public API

`__init__.py` re-exports `audit, bars, engine, features, models,
provider_data, ticks, universe` as submodules. `provider_receipts` and
`evidence_redaction` are **not** re-exported — callers must import them
by full path. Migrations live under `fincept_db.migrations`.

### Connections

Depends on `fincept-core` (config, schemas, clock, ids) and
`sqlalchemy[asyncio]`, `asyncpg`, `alembic`. Consumed by
`services/api`, `backtester`, `features`, `ingestor`, `oms`,
`portfolio`, `quant_foundry`, `settlements`, `strategy_host`, and by
`fincept-tools/data` + `fincept-tools/analytics` (which import
`fincept_db.bars`/`ticks`/`engine`/`models` at module top).

### What's Optimally Implemented

- **`session_scope`** — clean commit/rollback semantics with
  `expire_on_commit=False` so callers can use returned ORM objects
  after the session closes.
- **Upsert-based ingest** — `ON CONFLICT DO UPDATE`/`DO NOTHING` makes
  re-runs idempotent; the `features.write_features` docstring
  explicitly calls out the "re-run backfill replaces values" contract.
- **`read_latest_feature` PIT semantics** (features.py:87) — strict
  `<= as_of_ns` with `ORDER BY ts_event DESC LIMIT 1`, pinned by
  `test_read_latest_feature_respects_as_of_ns` and
  `test_read_latest_feature_inclusive_at_exact_match`. Correct.
- **Timescale hypertable + compression + retention policies** in
  `0001_initial.py` — `compress_segmentby` matches the access patterns
  (`venue, symbol` for trades; `venue, symbol, freq` for bars);
  `set_integer_now_func('fincept_now_ns')` for the bigint-time hypertables
  that have retention policies.
- **`conftest.py`** — session-scoped fixture that drops/creates a real
  `fincept_test` DB and truncates between tests; skips cleanly when
  Postgres is unreachable. Honest integration tests.
- **`evidence_redaction` conservative stance** — false positives
  (over-redaction) are explicitly preferred over false negatives
  (leaked secrets). The right security default.

### What Needs Work

- **`read_bars` discards `ts_recv`** (bars.py:89):
  ```python
  BarEvent(
      ...
      ts_event=row.ts_event,
      ts_recv=row.ts_event,   # <-- original ts_recv is gone
      ...
  )
  ```
  The `bars` table has no `ts_recv` column (see `models.py:33-49` and
  `0001_initial.py:38-52`), so on read-back `ts_recv` is synthesised as
  `ts_event`. This is silent information loss — a round-trip
  `write_bars([bar])` then `read_bars(...)` does not return the same
  `BarEvent`. `test_write_and_read_bars_roundtrip` does not assert
  `ts_recv` equality, so the bug is invisible to the test suite. Either
  add a `ts_recv` column or document that bars intentionally collapse
  it.
- **`audit.list_recent_orders` materialises the whole `oms.state` log**
  (audit.py:82) — `.scalars().all()` loads every row into Python before
  the `seen` collapse. The docstring (line 73) says "Postgres handles
  the DISTINCT ON pattern natively" but the implementation does **not**
  use `DISTINCT ON` — it does the collapse in Python. At high order
  volume this is unbounded memory. Either use
  `DISTINCT ON (correlation_id)` server-side or move to a dedicated
  `orders` table as the docstring suggests.
- **`universe.upsert_universe_symbols` only updates `active` on
  conflict** (universe.py:71-76) — you cannot re-point a symbol's
  `venue_default` or change its `asset_class` via upsert; you'd have to
  drop and re-add. Likely intentional (symbols are sticky) but
  surprising and undocumented.
- **`__init__.py` Windows cyextension hack** (line 7) — sets
  `SQLALCHEMY_DISABLE_CYEXT=1` on `os.name == "nt"` before any
  sqlalchemy import. Documented as a MemoryError workaround. This is a
  global side-effect from importing `fincept_db` — fine for the
  terminal's own process, but if `fincept_db` is ever imported into a
  host process that depends on the C extension for performance, this
  silently disables it. Worth a comment that this only applies to the
  terminal's own runtimes.
- **`provider_receipts` freshness thresholds are hardcoded** (line 34-36):
  `DEFAULT_FRESH_THRESHOLD_SEC = 5`, `STALE = 60`, `DEGRADED = 60`.
  `stale_threshold_sec` and `degraded_threshold_sec` are both 60, so
  the `degraded_threshold_sec` parameter is effectively dead — anything
  `>= 60s` is `degraded` regardless. Either the degraded threshold
  should be larger (e.g. 300) or the parameter should be removed.
- **`evidence_redaction._LONG_TOKEN_PATTERN`** (line 77) —
  `\b[A-Za-z0-9_\-=.+]{32,}\b` will redact SHA-256 hashes (64 chars),
  ULIDs (26 chars — safe), JWTs, and also benign long identifiers
  (e.g. a 40-char git SHA in a `code_git_sha` field). The docstring
  acknowledges this is aggressive, but for evidence receipts that
  *want* to show a git SHA or a model hash, this will scrub them. There
  is no allowlist mechanism.
- **`provider_receipts` and `evidence_redaction` are not in
  `__init__.py`** — inconsistent with the rest of the package's
  re-export policy.

### What Might Break

- **`0001_initial.py` does not call `set_integer_now_func` for `bars`**
  (line 139-140 only sets it for `trades` and `book_deltas`) — but
  `bars` is also a bigint-time hypertable (line 99-102). Timescale
  requires an integer-now function for retention policies on
  integer-time hypertables. The migration does **not** add a retention
  policy to `bars` (only compression, line 125), so this may be
  intentional — but if anyone later adds `add_retention_policy('bars',
  ...)` without first calling `set_integer_now_func('bars',
  'fincept_now_ns')`, the policy creation will fail. A latent footgun.
- **`audit.append` silently drops on ULID collision** — ULID collisions
  are astronomically unlikely, but the `ON CONFLICT DO NOTHING` means a
  collision would lose an audit row with no error. The test
  `test_append_is_append_only_idempotent_on_collision` actually shows
  that two calls produce two *different* ids, so the conflict path is
  not exercised in practice. Fine, but the `DO NOTHING` is the wrong
  default for an audit log — `DO RAISE` would be safer.
- **`provider_data.write_provider_data` upserts on
  `(record_id, ts_event)`** — but `record_id` is derived from a hash of
  the request in `_make_record` (via `_record_to_row`). If two distinct
  requests hash to the same `record_id` at different `ts_event`, both
  rows coexist; if the same request is re-played at the same
  `ts_event`, it upserts. The dedup key is correct, but there is no
  test for the "same record_id, different ts_event" case.

### What Isn't Implemented Yet

- **No tests for `evidence_redaction.py`** — this is security-critical
  secret scrubbing with six regex patterns and a recursive dict walker,
  and it has **zero** unit tests in `libs/fincept-db/tests/`. This is
  the single biggest test gap in the libs. A missed redaction pattern
  would leak an API key into a dashboard receipt.
- **No tests for `provider_receipts.py`** — `freshness_from_age_sec`
  boundary behaviour, `build_evidence_receipt` redaction integration,
  and `to_dict` flattening are all untested.
- **No `DISTINCT ON` optimisation** for `list_recent_orders`.
- **No `bars.ts_recv` column** — round-trip lossy.
- **No dedicated `orders` table** — the audit log is the canonical
  order store (acknowledged in the docstring).

### Better Approaches

- Add `evidence_redaction` tests immediately: positive (each pattern
  fires), negative (benign strings preserved), recursive dict cases,
  and a regression test that a real Alpaca/OpenAI/Anthropic key shape
  is redacted.
- Use `DISTINCT ON (correlation_id)` in `list_recent_orders` or
  materialise a `orders` table.
- Add `ts_recv` to the `bars` table or document the collapse.
- Re-export `provider_receipts` and `evidence_redaction` from
  `__init__.py`.
- Either fix `degraded_threshold_sec` default or remove the parameter.

---

## Library: fincept-sdk

### Purpose

The strategy author SDK: a `Strategy` ABC and a `StrategyContext`
Protocol. The same `Strategy` subclass runs unchanged in the
backtester, in live paper trading, and in walk-forward evaluation
because all three supply a `StrategyContext` with the same shape.

### Layout

```
src/fincept_sdk/
  __init__.py    # re-exports Strategy, StrategyContext
  strategy.py    # Strategy ABC + StrategyContext Protocol (104 lines)
```

### How Each Module Works

- **`strategy.py`** — `StrategyContext` is a `@runtime_checkable`
  `Protocol` with `now_ns: int`, `positions: dict[str, Position]`, and
  methods `submit(intent) -> str`, `cancel(order_id) -> None`,
  `get_feature(name, symbol) -> float | None`, `log(msg, **kwargs)`.
  `Strategy` is an `ABC` with `ClassVar` `strategy_id` and `symbols`
  and six `@abstractmethod` lifecycle hooks: `on_start`, `on_bar`,
  `on_tick`, `on_fill`, `on_signal`, `on_stop`. All hooks are abstract
  (no default impl) — subclasses must explicitly implement each.

### Public API

`Strategy`, `StrategyContext`. That's the entire SDK.

### Connections

Depends on `fincept-core` (schemas) and `pydantic`. Consumed by
`services/backtester` (which supplies a sync `StrategyContext` driven
by Timescale replay) and `services/strategy_host` (live paper). The
`StrategyContext` implementations live in services, not in the SDK.

### What's Optimally Implemented

- **All-hooks-abstract** (strategy.py:82-103) — forces strategy authors
  to explicitly decide whether each hook is a no-op. The docstring
  calls out that silent inheritance of empty hooks would hide bugs
  (e.g. forgetting `on_fill` and wondering why positions don't
  update). Correct call.
- **`@runtime_checkable` Protocol** — lets tests assert
  `isinstance(ctx, StrategyContext)` without subclassing. The test
  suite (`test_strategy.py`) verifies both acceptance of a conforming
  mock and rejection of one missing `now_ns`.
- **Zero runtime in the SDK** — pure interface, no I/O, no deps beyond
  schemas. The cleanest possible separation between "what a strategy
  looks like" and "how the runtime drives it".

### What Needs Work

- **No params schema** — `Strategy` declares `strategy_id` and
  `symbols` as `ClassVar`s but there is no declared schema for
  constructor parameters. `strategy_config.StrategyConfig.params` is a
  free-form `dict[str, Any]` and the host coerces `Decimal` strings at
  instantiation. This means (a) an LLM/operator can't introspect a
  strategy's parameters without reading the class, and (b) there is no
  validation that the params dict matches what the strategy expects
  until instantiation. A `params_schema: ClassVar[type[BaseModel]]`
  hook would close this.
- **`on_signal(signal: BaseModel)`** is maximally generic — the type is
  just `BaseModel`, so a strategy can't statically know what signal
  shapes it might receive. In practice the runtime dispatches
  `Prediction`/`SentimentSignal`/`RegimeSignal`/`NewsImpactSignal`;
  the SDK should at least document the union.
- **No reference implementation** — the SDK ships no `NoopStrategy` or
  `BuyAndHold` example. Authors have to read `services/backtester/
  strategies/` to see the pattern. A docstring example would help.

### What Might Break

- **`runtime_checkable` only checks attribute presence, not
  signatures** (documented in the docstring, line 40-41) — a context
  with a `submit` method taking the wrong args still passes
  `isinstance`. This is a Python limitation, not a bug, but strategy
  authors who rely on the runtime check will get late `TypeError`s.
- **`ClassVar[strategy_id]` collision** — if two strategy classes
  accidentally share a `strategy_id`, the host's config watcher can't
  distinguish them. There is no SDK-level guard; the strategy_config
  store keys on `strategy_id` and would silently overwrite.

### What Isn't Implemented Yet

- A params schema hook.
- A typed `Signal` union for `on_signal`.
- A reference/example strategy in the SDK package.
- Versioning — `strategy_id` is a free string; no `schema_version` or
  semver convention.

### Better Approaches

- Add `params_model: ClassVar[type[BaseModel] | None] = None` to
  `Strategy`; the host validates `StrategyConfig.params` against it on
  instantiation.
- Type `on_signal` as `on_signal(self, ctx, signal: Prediction |
  SentimentSignal | RegimeSignal | NewsImpactSignal)` (re-using the
  core union).
- Ship a `fincept_sdk.example.NoopStrategy` so the README can show a
  complete runnable example without pointing at services.

---

## Library: fincept-tools

### Purpose

Typed tool registry for LLM agents: a `Tool` protocol + `BaseTool`
base class with OTel tracing and typed-error handling, a process-wide
`REGISTRY`, OpenAI/Anthropic spec generators, and four tool families
(data, analytics, exec, research) that self-register at import time.

### Layout

```
src/fincept_tools/
  __init__.py            # re-exports protocol + registry surface
  protocol.py            # ToolInput / ToolOutput / Tool / BaseTool / ToolMeta
  registry.py            # ToolRegistry + REGISTRY + spec generators
  errors.py              # ToolError hierarchy (NotInUniverse, PaperOnlyExec, ...)
  data/tools.py          # 7 read-only data tools
  analytics/tools.py     # 6 pure-compute analytics tools
  exec/tools.py          # 3 paper-only exec tools
  research/exa.py        # Exa market research tool
  research/openbb.py     # OpenBB quote + generic call + health probes
```

### How Each Module Works

- **`protocol.py`** — `ToolInput`/`ToolOutput` are `BaseModel` with
  `extra="forbid"`; `ToolOutput` always carries `ok`, `error`,
  `error_type`. `BaseTool.__call__` (line 111) wraps `_run` in an OTel
  span (`tool.<name>`) with attributes (`args_size`, `result_size`,
  `duration_ns`, `ok`, `error_type`), catches `ToolError` subclasses
  and serialises them as `output_model(ok=False, error=...,
  error_type=type(exc).__name__)`, and lets untyped exceptions
  propagate. Subclasses override `_run`, never `__call__`.
- **`registry.py`** — `ToolRegistry` keeps insertion order, raises on
  duplicate name. `REGISTRY` is the singleton. `to_openai_function_spec`
  / `to_anthropic_tool_spec` produce the LLM-facing JSON schemas from
  `input_model.model_json_schema()`.
- **`errors.py`** — `ToolError(FinceptError)` base + `NotInUniverse`,
  `PaperOnlyExec`, `ToolValidationError`, `ToolBackendError`,
  `MissingExaApiKey`, `OpenBBUnavailable`.
- **`data/tools.py`** — `data.get_bars`, `data.get_quote`,
  `data.get_trades`, `data.get_universe`, `data.get_positions`,
  `data.get_features`, `entity.resolve`. Each is a `BaseTool` subclass
  with a typed `Input`/`Output` pair. `entity.resolve` is the
  hallucination gate: it resolves free-text to a canonical in-universe
  symbol and raises `NotInUniverse` on miss.
- **`analytics/tools.py`** — `compute_returns`, `compute_vol`,
  `compute_correlation`, `compute_vwap`, `compute_sharpe`,
  `compute_drawdown`. All PIT-safe (accept `end_ns` cutoff). Use numpy.
- **`exec/tools.py`** — `submit_order`, `cancel_order`,
  `get_order_status`. Each calls `_ensure_paper_mode()` which raises
  `PaperOnlyExec` unless `TRADING_MODE == "paper"`. `submit_order`
  builds an `OrderIntent` and publishes a custom envelope to
  `ord.orders`.
- **`research/exa.py`** — `ExaMarketResearchTool` posts to
  `api.exa.ai/search` with a structured `outputSchema`, returns a
  `ResearchBrief` with citations + grounding. Reads `EXA_API_KEY` from
  env or a `.env` filesystem walker.
- **`research/openbb.py`** — `OpenBBQuoteTool` (dual-path: local API
  then in-process `openbb` package fallback), `OpenBBCallTool` (generic
  `/api/v1/...` dispatcher with path regex + `..` ban),
  `check_openbb_health`/`check_openbb_readiness` probes. `_get_json`
  restricts HTTP to `127.0.0.1`/`localhost` (SSRF defense).

### Public API

`__init__.py` re-exports `REGISTRY`, `BaseTool`, `Tool`, `ToolInput`,
`ToolMeta`, `ToolOutput`, `register`, `to_anthropic_tool_spec`,
`to_openai_function_spec`. Tool families register on
`import fincept_tools.<family>` (side-effect imports).

### Connections

Depends on `fincept-core`, `fincept-db`, `fincept-bus`, `numpy`.
Consumed by `services/agents/*` (the LLM agents call tools via the
registry) and `services/api` (exposes tool specs / health probes).

### What's Optimally Implemented

- **`BaseTool.__call__` observability + error contract** — OTel span
  with `tool.name`, `args_size`, `result_size`, `duration_ns`, `ok`,
  `error_type`; `ToolError` subclasses caught and serialised; untyped
  exceptions propagate (programming errors stay visible). This is the
  correct pattern for an LLM tool layer.
- **`ToolInput`/`ToolOutput` `extra="forbid"`** — guards against LLM-
  hallucinated arguments. `test_get_bars_input_forbids_extra` pins it.
- **`entity.resolve` as the hallucination gate** — agents must resolve
  before emitting signals; `NotInUniverse` is a typed error the agent
  loop can branch on. Good security design.
- **`_ensure_paper_mode`** — centralised paper-only gate, identical
  across all exec tools.
- **OpenBB SSRF defense** — `_get_json` rejects non-local HTTP hosts
  (line 152-153); `OpenBBCallInput.path` is regex-locked to
  `/api/v1/[A-Za-z0-9._/-]+` with an explicit `..` ban.
- **Test coverage is broad** — `test_protocol.py` (typed-error
  catching, untyped propagation, Protocol conformance),
  `test_registry.py` (registration, duplicate detection, OpenAI/
  Anthropic spec shapes, global registry contents),
  `test_data_tools.py` (mocked DB), `test_analytics_tools.py` (mocked
  bars), `test_exec_tools.py` (fakeredis + monkeypatched settings),
  `test_research_exa.py` / `test_research_openbb.py` (fake loaders).

### What Needs Work

- **`exec.tools` bypasses the `fincept-bus` `Event` contract**
  (exec/tools.py:118-125):
  ```python
  fields: dict[str, str] = {
      "event_type": "order_intent",
      "order_id": order_id,
      "strategy_id": payload.strategy_id,
      "state": "submitted",
      "ts_event": str(ts),
      "payload": intent.model_dump_json(),
  }
  await r.xadd(STREAM_ORDERS, fields)
  ```
  This is a bespoke envelope, not the `{event_id, published_at, type,
  payload}` shape that `fincept_core.events.serialize` produces and
  that `fincept_bus.Consumer` deserialises via `deserialize()`. The OMS
  must therefore special-case this shape rather than using the shared
  `deserialize`. This is a layering violation: `fincept-tools` should
  use `Producer.publish(STREAM_ORDERS, make_event("order_intent",
  intent.model_dump()))` (or a dedicated `order_intent` event type) so
  the bus protocol stays uniform. The same applies to `cancel_order`.
- **Every Redis-touching tool opens a fresh connection per call** —
  `exec.submit_order` (line 128), `exec.cancel_order` (line 194),
  `exec.get_order_status` (line 263), `data.get_positions` (line 312),
  `data.get_features` (line 408) all do `Redis.from_url(...)` then
  `aclose()` per invocation. For an LLM agent calling tools in a tight
  loop this is per-call TCP setup + teardown. There is no shared
  client. Should inject a `Redis` instance (or use a module-level
  lazy singleton like `fincept_db.engine.get_engine`).
- **`analytics.*` read the entire bar history then slice in Python**
  (e.g. analytics/tools.py:99, 155, 214):
  ```python
  bars = await _safe_read_bars(payload.symbol, payload.freq, 0, payload.end_ns, ...)
  bars = bars[-(payload.lookback_bars + 1) :]
  ```
  `start_ns=0` means "read all bars up to `end_ns`" then keep the last
  N. At scale (millions of 1m bars) this pulls the whole table into
  memory per tool call. Should compute `start_ns = end_ns -
  lookback_bars * bar_ns` and push the window into the SQL `WHERE`.
- **`data.get_positions` hardcodes `count=500`** (data/tools.py:314) —
  `xrevrange("ord.positions", count=500)`. If a strategy has >500
  position updates in the stream's retention window, the latest
  snapshot for some (strategy_id, symbol) pairs may not be in the 500
  newest messages and the tool silently returns stale or missing
  positions. No pagination, no warning.
- **Duplicated `.env` filesystem walker** — `research/exa.py:64-86`
  (`_read_exa_api_key_from_dotenv`) and `research/openbb.py:98-119`
  (`_read_openbb_api_url_from_dotenv`) are near-identical
  implementations of "walk cwd, parents, and `__file__` parents looking
  for a key in `.env`". This duplicates what `fincept_core.config.
  Settings` already does via `pydantic-settings` (`env_file=".env"`).
  The right fix is to add `EXA_API_KEY` and `OPENBB_API_URL` to
  `Settings` and read them via `get_settings()`. The walker also
  searches `Path(__file__).resolve().parents` — if the installed
  package happens to live next to a stray `.env`, it would read that.
  Security smell.
- **`REGISTRY` is a process-global singleton with no namespacing** —
  `register()` raises `ValueError` on duplicate name (registry.py:91),
  so if two modules register `data.get_bars` the second import raises
  and crashes the process. There is no per-agent or per-tenant
  registry. Fine for v1; a limitation for multi-tenant.
- **`data.get_quote` uses `time.time_ns()` inside `_run`**
  (data/tools.py:135) — the tool computes "now" itself rather than
  accepting an `as_of_ns` parameter. This makes it non-PIT-safe and
  non-deterministic for backtests. The analytics tools correctly
  require `end_ns`; `get_quote` should too.
- **`BaseTool.__call__` measures `args_size` via
  `len(payload.model_dump_json())`** (protocol.py:117) — for a large
  payload this serialises to JSON twice (once for size, once for the
  actual call). Minor; only matters for hot paths.

### What Might Break

- **`exec.submit_order` publishes before the OMS is ready** — there is
  no `ensure_group` / stream-creation call; if `ord.orders` doesn't
  exist, `xadd` with no `mkstream` will fail. `Producer.publish` in
  `fincept-bus` also doesn't pass `mksh=True` to `xadd`, so the same
  applies there. The first publish after a fresh `FLUSHDB` will raise.
- **`data.get_features` returns string-encoded values** — the
  `features:online:<symbol>` Redis hash stores everything as strings
  (the docstring at line 379 says "string-encoded so heterogeneous
  types round-trip cleanly"). The tool returns `dict[str, str]`; an
  LLM consuming this gets `"0.0123"` not `0.0123` and must parse. If
  the LLM doesn't, downstream reasoning is off. There is no type
  metadata.
- **`OpenBBQuoteTool` fallback chain** (openbb.py:218-227) — on
  `OpenBBUnavailable` from the API path, it falls back to the in-
  process `openbb` package; if *that* also raises `OpenBBUnavailable`,
  it raises with a concatenated message `f"{api_exc} Also,
  {package_exc}"`. The error message is operator-unfriendly and the
  `from package_exc` chaining loses `api_exc`'s traceback context.

### What Isn't Implemented Yet

- A shared Redis client for tools.
- PIT-safe `as_of_ns` on `data.get_quote`.
- Pushing the lookback window into SQL for analytics tools.
- EXA/OpenBB config via `Settings` instead of `.env` walkers.
- Per-tenant / per-agent registry namespacing.
- A `tool.list` / `tool.help` meta-tool for agents to introspect the
  registry at runtime.

### Better Approaches

- Route `exec` tools through `fincept_bus.Producer` + a real
  `order_intent` event type so the OMS uses the shared `deserialize`.
- Add a `get_redis_client()` lazy singleton in `fincept_tools` (or
  accept an injected `Redis` in tool constructors) and reuse it.
- Compute `start_ns` from `lookback_bars` in analytics tools and pass
  it to `read_bars`.
- Move `EXA_API_KEY` / `OPENBB_API_URL` into `fincept_core.config.
  Settings` and delete the two `.env` walkers.
- Paginate `data.get_positions` or read from a materialised positions
  table (`fincept_db` could host one) instead of scanning the stream.

---

## Cross-Library Concerns

### Dependency graph is clean and acyclic

`fincept-core` is the leaf. `fincept-bus`, `fincept-db`, `fincept-sdk`
depend only on core. `fincept-tools` depends on core + db + bus. No
library imports from `services/`. No cycles. This is the single most
important structural property and it holds.

### `fincept-tools` pulls in the world at import time

`fincept_tools.data.tools` and `fincept_tools.analytics.tools` import
`fincept_db.bars` / `fincept_db.ticks` / `fincept_db.engine` /
`fincept_db.models` at module top. `fincept_tools.exec.tools` imports
`fincept_bus.streams`. So `import fincept_tools.data` transitively
requires `sqlalchemy[asyncio]` + `asyncpg` + `redis` to be installed.
An agent that only wants the registry / protocol still pays the full
import cost. Consider lazy imports inside `_run` (the way
`storage.py` lazily imports `boto3`) so the protocol/registry surface
is importable without the DB/Redis stack.

### Duplicated validation logic

`_BAD_NAME_CHARS = set('/\\:*?"<>|\0')` and the `_validate_agent_id`
function are copied verbatim across:
- `fincept_core/prediction_log.py:69-78`
- `fincept_core/datasets/settlement.py:78-87`
- `fincept_core/datasets/feature_snapshot.py:72-81`
- `fincept_core/strategy_config.py:77-89` (raises a different error
  class)

Each copy includes a comment saying "keep in sync with the others" —
which is exactly the bug class a shared helper eliminates. Extract to
`fincept_core.fs_safety.validate_safe_name(name, *, error_cls)`.

### Duplicated `.env` walker

`fincept_tools/research/exa.py:64-86` and
`fincept_tools/research/openbb.py:98-119` implement the same "walk
parents looking for a key in `.env`" logic. Both should be replaced by
fields on `fincept_core.config.Settings`.

### Inconsistent singleton patterns

- `fincept_core.config.Settings` — `__new__` override (brittle).
- `fincept_core.storage.get_storage_backend` — module global
  `_STORAGE_BACKEND` with `clear_storage_backend_cache()`.
- `fincept_db.engine.get_engine` — module global `_engine` with
  `reset_engine()`.
- `fincept_core.strategy_config.get_strategy_config_store` — module
  global `_store` with `reset_strategy_config_store()`.

Three of four use the same "module global + reset" pattern; the fourth
(`Settings`) is the outlier. Standardise on the module-global pattern.

### `fincept_core.errors.ConnectionError` shadows the builtin

Not exported from `fincept_core.__init__`, but importable by full path
and a footgun for anyone who does `from fincept_core.errors import *`.
Rename to `BusConnectionError` or `RedisConnectionError`.

### Event envelope inconsistency

`fincept_core.events.serialize` produces
`{event_id, published_at, type, payload}`. `fincept_bus.Producer`
publishes that shape. `fincept_bus.Consumer.deserialize` reads that
shape. But `fincept_tools.exec.tools.submit_order`/`cancel_order`
publish a *different* shape (`{event_type, order_id, strategy_id,
state, ts_event, payload}`). The OMS must therefore handle two envelope
shapes. This is the most consequential cross-library inconsistency in
the audit — it means the bus protocol is not actually uniform.

### Test coverage gaps (cross-library)

- `evidence_redaction.py` (security-critical) — no tests anywhere in
  `libs/`.
- `provider_receipts.py` — no tests.
- The `EventPayload` union ↔ `_EVENT_SCHEMAS` mapping in
  `fincept_core.events` — no sync-invariant test (a new schema added
  to the union but not to the dict would silently fail at runtime).
- `fincept_bus` — no dead-letter / poison-message / max-deliveries
  test.
- `fincept_core.clock.iso_to_ns` — no precision test (the float bug
  above is uncaught).

---

## Recommendations Summary

Prioritised by impact.

### P0 — Correctness / security

1. **Add tests for `evidence_redaction.py`** — six regex patterns +
   recursive walker, security-critical, currently zero coverage. A
   missed pattern leaks an API key into a dashboard receipt.
2. **Fix `exec.tools` envelope shape** — route through
   `fincept_bus.Producer` + a real event type so the OMS can use the
   shared `deserialize`. Eliminates the bus protocol split.
3. **Fix `clock.iso_to_ns` float precision** — use integer arithmetic
   on the `datetime` components instead of `timestamp() * 1e9`.
4. **Fix `bars.read_bars` `ts_recv` loss** — either add a `ts_recv`
   column to the `bars` table or document the collapse; currently
   round-trip is silently lossy and the test doesn't catch it.
5. **Move `EXA_API_KEY` / `OPENBB_API_URL` into `Settings`** and delete
   the two `.env` filesystem walkers (SSRF-adjacent: the walker
   searches `__file__` parents).

### P1 — Design / scaling

6. **Add a shared Redis client for `fincept-tools`** — per-call
   `Redis.from_url()` + `aclose()` is wasteful and has no pool.
7. **Push the lookback window into SQL in `analytics.*`** —
   `start_ns=0` reads the whole table; compute `start_ns` from
   `lookback_bars` and push into `WHERE`.
8. **`audit.list_recent_orders`** — use `DISTINCT ON` server-side or
   materialise a dedicated `orders` table; the current Python-side
   collapse loads the whole `oms.state` log.
9. **`fincept-bus` dead-letter queue + handler-failure logging** — a
   perpetually-failing message blocks the group with no diagnostic.
10. **Separate `handler_timeout_ms` from `block_ms`** in
    `fincept-bus.Consumer` — the current conflation is semantically
    wrong and the test codifies the wrong contract.
11. **`fincept-db` migrations: call `set_integer_now_func('bars',
    'fincept_now_ns')`** so a future retention policy on `bars` doesn't
    fail.

### P2 — Cleanup / consistency

12. **Extract `fincept_core.fs_safety.validate_safe_name`** and use it
    in all four stores (kills the DRY violation).
13. **Remove duplicate dead `return` statements** in `events.py:106`
    and `heartbeat.py:92`.
14. **Re-export `http`, `portfolio`, `heartbeat`, `prediction_log`,
    `strategy_config`, `datasets` from `fincept_core.__init__`** and
    extend `test_imports.py`.
15. **Rename `fincept_core.errors.ConnectionError`** to avoid shadowing
    the builtin.
16. **Standardise the singleton pattern** — replace `Settings.__new__`
    with `@lru_cache` on a `_build_settings()` helper.
17. **Re-export `provider_receipts` and `evidence_redaction`** from
    `fincept_db.__init__`.
18. **Add a sync-invariant test** that `EventPayload` union members and
    `_EVENT_SCHEMAS` keys stay in lockstep.
19. **Fix or remove `provider_receipts.degraded_threshold_sec`** (both
    it and `stale_threshold_sec` default to 60, making the parameter
    dead).
20. **Collapse `fincept_bus.types.py`** (two type aliases) into
    `streams.py` or `consumer.py`.
21. **Add a `params_model` hook to `fincept_sdk.Strategy`** so the host
    can validate `StrategyConfig.params` against the strategy class.

### P3 — Future-proofing

22. **File rotation + `os.open(O_APPEND)` single-syscall appends** for
    the JSONL stores in `fincept-core`.
23. **Lazy imports in `fincept_tools.data/analytics/exec`** so the
    protocol/registry surface is importable without the DB/Redis stack.
24. **Per-tenant registry namespacing** in `fincept-tools` if multi-
    tenancy is on the roadmap.
25. **A `Signal` union type for `Strategy.on_signal`** in `fincept-sdk`.
