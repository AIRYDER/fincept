# ML Dataset Evidence Spine — Context Document

**Session date:** 2026-06-26
**Branch:** `codex/portfolio-optimizer-core`
**Commit range:** `7dc5fc1..HEAD` (29 commits, 61 files, +10,469/-199 lines)
**Plan file:** `.omo/plans/ml-dataset-evidence-spine.md`

---

## 1. What the Evidence Spine Is

The **ML Dataset Evidence Spine** is a shared data layer that connects three
things that were previously siloed:

1. **What the agent said** — the prediction log (`PredictionRow`)
2. **What actually happened** — the settlement ledger (`SettlementRecord`)
3. **What the agent saw** — the feature snapshot (`FeatureSnapshot`)

Together these form an **evidence receipt** — a single JSON dict that a
dashboard or auditor can render to answer: "For prediction X, what features
did the model see, what did it predict, and what was the realized outcome?"

Before this work, the codebase had:
- A prediction log (`fincept_core.prediction_log`) — working, production-wired
- A settlement system (`quant_foundry.settlement`) — working, but keyed by
  `model_id`, with a cost model (`cm-v1` = 10/5/3 bps) that didn't match the
  new spine's design, and tightly coupled to the quant_foundry gateway
- No feature snapshots — the agent recorded feature-availability counters
  (missing/defaulted/aliased) but not the actual feature values
- No approved-roots gate — training/backtest paths were not validated
- No shared CV utility — walk-forward fold math was duplicated in 3 places

The spine introduces a new `fincept_core.datasets` package that provides
all of these as shared, self-contained primitives with no `services/*`
dependencies, plus a new `services/settlements` worker and new API routes.

---

## 2. Architecture

### 2.1 Layering

```
┌─────────────────────────────────────────────────────────────────┐
│  services/api  (FastAPI lifespan + routes)                      │
│    ├── /models/{name}/outcomes  (reads SettlementStore)         │
│    ├── /models/train            (approved-roots gate)           │
│    ├── /backtest/run            (approved-roots gate)           │
│    ├── settlements_poller.py    (periodic settlements.worker)   │
│    └── approved_roots.py        (shared exception handler)      │
├─────────────────────────────────────────────────────────────────┤
│  services/agents   (gbm_predictor)                              │
│    ├── main.py     (publish loop: writes FeatureSnapshot)       │
│    ├── infer.py    (GBMPredictor: stores last_feature_vector)   │
│    ├── features.py (load_live + _compute_feature_schema_hash)   │
│    └── baselines/  (LogRegBaseline — stdlib-only)               │
├─────────────────────────────────────────────────────────────────┤
│  services/settlements  (new service)                            │
│    ├── worker.py            (tick: tails prediction log)        │
│    └── market_data_bridge.py (BarDataAdapter → async source)    │
├─────────────────────────────────────────────────────────────────┤
│  services/quant_foundry  (existing — not modified except gateway)│
│    ├── settlement_sweep.py (OLD sweep → SettlementLedger)       │
│    ├── gateway.py          (removed _compat_sign_callback)      │
│    ├── callback_metrics.py (durable rejection-rate store)       │
│    └── training_manifest.py (delegates CV to fincept_core)      │
├─────────────────────────────────────────────────────────────────┤
│  services/backtester                                           │
│    └── walk_forward.py     (delegates make_folds to fincept_core)│
├─────────────────────────────────────────────────────────────────┤
│  libs/fincept-core  (shared library — NO services/* imports)    │
│    └── fincept_core/datasets/                                   │
│        ├── __init__.py        (facade + build_evidence_receipt)  │
│        ├── approved_roots.py  (fail-closed path gate)           │
│        ├── schemas.py         (DatasetManifest, FeatureSnapshot) │
│        ├── settlement.py      (SettlementStore + SettlementRecord)│
│        ├── feature_snapshot.py (FeatureSnapshotStore)           │
│        ├── cv.py              (make_folds, derive_walk_forward) │
│        └── dossier.py         (ECE, Brier, calibration helpers) │
└─────────────────────────────────────────────────────────────────┘
```

### 2.2 The circular-import rule

The #1 architectural risk was a circular import between
`services/quant_foundry` and `libs/fincept-core`. The rule is:

> `fincept_core.datasets` must NEVER import from `services/*`.

This was verified three ways: grep for `from services` in the datasets
package (0 matches), grep for `import quant_foundry` (0 matches), and a
runtime import test that imports all three packages simultaneously.

### 2.3 The two settlement systems (coexistence strategy)

Two parallel settlement systems now coexist:

| Aspect | New (`fincept_core.datasets`) | Old (`quant_foundry`) |
|--------|-------------------------------|----------------------|
| Store | `SettlementStore` (JSONL) | `SettlementLedger` (JSONL) |
| Key | `agent_id` + `prediction_id` | `model_id` + `prediction_id` |
| Cost model | `v1.default` (5/3/0 bps) | `cm-v1` (10/5/3 bps) |
| Writer | `settlements.worker.tick` (poller) | `gateway.run_settlement_sweep` |
| Reader | `GET /models/{name}/outcomes` | quant_foundry dashboard |

Both run side-by-side: the new worker reads the same `data/predictions/`
log but writes to a separate store. Full consolidation is deferred pending
operational validation. The reconciliation strategy is documented in
`services/api/src/api/settlements_poller.py`.

---

## 3. Core Components

### 3.1 ApprovedRoots (`approved_roots.py`)

A fail-closed filesystem gate. Every training, backtest, and settlement
input path must pass through `ApprovedRoots.resolve()` before it reaches
the orchestrator.

**What it rejects:**
- Absolute paths not inside any approved root
- `..` traversal anywhere in the candidate
- Symlink escapes (symlinks are disallowed by default, even if they
  currently resolve inside a root, to block TOCTOU swaps)

**Configuration:**
- `FINCEPT_APPROVED_DATA_ROOTS` env var (comma-separated)
- Default: `["data", "models"]` (fail-closed if empty)

**API integration:**
- `backtest.py`: `Depends(get_approved_roots)` → `approved_roots.resolve(body.bars_path)` → uses `resolved.path` downstream (closes TOCTOU)
- `models.py`: `_get_approved_roots().resolve(body.input_path)` → uses `resolved.path` downstream (closes TOCTOU)
- Shared exception handler in `api/approved_roots.py` renders uniform 422 body
- `X-Approved-Roots-Code` header carries the rejection reason (`outside_root`, `traversal`, `symlink_escape`, `no_roots`)

### 3.2 SettlementStore (`settlement.py`)

An append-only JSONL ledger at `data/settlements/<agent_id>.jsonl`.

**SettlementRecord fields:**
- `prediction_id`, `agent_id`, `model_name`, `symbol`
- `ts_event`, `horizon_ns`
- `decision_window_start_ns`, `decision_window_end_ns`
- `cost_model_version` (default: `v1.default`)
- `realized_return_gross`, `realized_return_net`
- `cost_breakdown_fee_bps` (5.0), `cost_breakdown_spread_bps` (3.0), `cost_breakdown_slippage_bps` (0.0)
- `brier_component`
- `status`: `pending_time` | `pending_data` | `settled` | `failed`
- `settled_at_ns`, `failure_reason`

**Guards:**
- **Look-ahead guard**: refuses any append where `decision_window_end_ns > now_ns`
- **Terminal-row idempotency**: a `settled` or `failed` row for the same `(prediction_id, cost_model_version)` raises `SettlementError(code="duplicate")`
- **Pending rows can be superseded**: a `pending_data` → `settled` transition is allowed (the pending row is retained as history)
- **`_find` returns the LAST match** (not the first) — this was a bug we found and fixed during the audit. The first-match version would miss a terminal row that follows a pending row, allowing a duplicate settled row.

### 3.3 FeatureSnapshotStore (`feature_snapshot.py`)

An append-only JSONL ledger at `data/feature_snapshots/<agent_id>.jsonl`.

**FeatureSnapshot fields:**
- `schema_version` (1)
- `decision_time_ns` — the as-of timestamp that gates which rows are eligible
- `rows: list[FeatureRow]` — each row has `symbol`, `ts`, `features: dict[str, float]`
- `feature_schema_hash` — 64-char hex SHA-256 of the sorted feature name list

**Look-ahead guard:** `FeatureSnapshot._no_lookahead` validator rejects any `FeatureRow` with `ts > decision_time_ns`.

**Idempotency:** `append_if_missing(prediction_id, snapshot, *, agent_id)` — keyed by `prediction_id`, lazily loads a seen-set from disk, skips if already present.

### 3.4 CV utility (`cv.py`)

Shared walk-forward cross-validation math, extracted from the backtester
and quant_foundry into the core library.

**Exports:**
- `make_folds(n_bars, *, n_folds, train_min_bars, val_bars, purge_bars, embargo_bars)` → `list[Fold]`
- `derive_walk_forward_window(as_of_ts, *, train_window_ns, test_window_ns, label_horizon_ns)` → `WalkForwardWindow`
- `Fold` / `WalkForwardWindow` — Pydantic frozen models
- `fold_iter_to_dicts` — serializer

**Convergence:** All three call sites now delegate to `fincept_core.datasets.cv`:
1. `services/agents/gbm_predictor/train.py` — imports `make_folds` from the facade
2. `services/backtester/walk_forward.py` — delegates via `_make_folds_local` → `_shared_make_folds`; public `make_folds` is a deprecated shim
3. `services/quant_foundry/training_manifest.py` — delegates `derive_walk_forward_window` via a thin re-wrapper

### 3.5 Dossier + calibration helpers (`dossier.py`)

Pure functions for model evaluation:
- `build_dossier(...)` — ECE (expected calibration error), Brier score, bucketed reliability
- `build_calibration_sidecar(val_predictions, val_labels, n_buckets)` — calibration curve

These are "parity helpers" — they mirror the `quant_foundry.dossier` shapes
but are not yet consumed by any service (forward-looking utilities).

### 3.6 Schemas (`schemas.py`)

Pydantic v2 frozen models:
- `DatasetManifest` — dataset metadata with hex-shape validators
- `ArtifactManifest` — model artifact metadata
- `FeatureRow` — a single point-in-time feature row
- `FeatureSnapshot` — a frozen snapshot of feature rows with look-ahead guard

### 3.7 Facade (`__init__.py`)

The `fincept_core.datasets` package facade:
- Explicit re-exports (no star-imports), auditable `__all__`
- `build_evidence_receipt(*, prediction, settlement, feature_snapshot, feature_health)` — the join function that produces the flat JSON dict consumed by `GET /models/{name}/outcomes`
- `try/except ImportError` guard for `cv.py` (safety net — binds to `None` on import failure)

---

## 4. Services

### 4.1 Settlements worker (`services/settlements/`)

**`worker.py`** — `tick(now_ns, *, predictions_dir, settlements_dir, market_data_source)`:
1. Scans `<agent_id>.jsonl` files under `predictions_dir` for due predictions (`ts_event + horizon_ns <= now_ns`)
2. Skips already-settled predictions (idempotent via `_existing_status` which returns the LAST match)
3. Queries `market_data_source(symbol, ts_event, ts_event)` for entry price and `market_data_source(symbol, ts_event, ts_event + horizon_ns)` for exit price
4. Computes `realized_return_gross = (close_t2 / close_t1) - 1`, `realized_return_net = gross - 8e-4` (5 bps fee + 3 bps spread), `brier_component = (prob_up - actual_up) ** 2`
5. Appends a `SettlementRecord` with `status="settled"` or `status="pending_data"` when prices are unavailable

**`market_data_bridge.py`** — `make_async_market_data_source(bar_adapter)`:
- Wraps the sync `quant_foundry.BarDataAdapter` into the worker's async `market_data_source` contract
- Uses `asyncio.to_thread` for blocking DB/HTTP calls
- Handles both `get_close` (single-bar lookup, used by tests) and `get_prices` (range lookup, used by production adapter)

### 4.2 Settlements poller (`services/api/src/api/settlements_poller.py`)

Periodic poller wired into the FastAPI lifespan:
- `_poll_settlements_worker(interval)` — runs `tick` every `interval` seconds
- Best-effort: failures logged as `settlements.worker_poll_failed` and swallowed
- Configurable via `SETTLEMENTS_WORKER_POLL_S` env var (default 60s, 0 to disable)
- Runs regardless of gateway mode (so `/outcomes` is fed even when quant_foundry is disabled)
- Cancelled in the lifespan `finally` block (same pattern as existing pollers)

### 4.3 API routes

**`GET /models/{name}/outcomes`** (`models.py`):
- Left-joins predictions from `PredictionLog` with settlements from `SettlementStore` via `build_evidence_receipt`
- Query params: `limit` (1..1000), `since_ns` (optional nanosecond cutoff)
- Returns `{"count": N, "outcomes": [receipt, ...]}`

**`POST /models/train`** (`models.py`):
- Approved-roots gate on `input_path` (fail-closed, 422 on violation)
- Uses `resolved.path` downstream (TOCTOU fix)
- Error propagates to shared exception handler

**`POST /backtest/run`** (`backtest.py`):
- Approved-roots gate on `bars_path` (fail-closed, 422 on violation)
- Uses `resolved.path` downstream (TOCTOU fix)
- Error propagates to shared exception handler

### 4.4 GBM predictor publish loop (`services/agents/`)

The publish loop (`main.py:_publish_loop`) now writes three sidecars per
emitted prediction:

1. **PredictionRow** → `PredictionLog` (existing)
2. **FeatureHealthRow** → `FeatureHealthLog` (feature-availability counters: missing/defaulted/aliased)
3. **FeatureSnapshot** → `FeatureSnapshotStore` (the actual feature values + schema hash)

All three writes are best-effort — failures are logged and swallowed so a
broken sidecar never stops predictions from being published.

**Feature schema hash:** `_compute_feature_schema_hash(feature_names)` computes
a 64-char hex SHA-256 from the sorted feature-name list. This is order-independent
and deterministic, binding each snapshot to the feature schema the prediction
was made against.

**Frame timestamp capture:** `load_live()` now accepts an optional
`frame_ts_out: list[int] | None` sink parameter. When provided, the frame's
`ts_event` is appended to the sink on a successful read. This lets the
publish loop capture the feature timestamp without changing `load_live`'s
return type (which would have broken existing unpacking patterns).

### 4.5 Callback security (`services/quant_foundry/`)

**`gateway.py`** — removed `_compat_sign_callback`:
- No remaining code path accepts unsigned callbacks
- When `_extract_callback_fields()` returns `None` (unsigned/missing), the gateway fail-closes: records a `rejected` metric, marks the job `FAILED` with `error_code="missing_runpod_callback_fields"`
- `sign_callback` is imported with `# noqa: F401` only so tests can monkey-patch it to assert the poller never calls it

**`callback_metrics.py`** — durable `callback_rejection_rate` store:
- Append-only JSONL at `data/callback_metrics/<source>.jsonl`
- Records only `{ts_ns, event, reason_code}` — no secrets, no payloads, no signatures
- `rejection_rate(source, *, window_s)` computes the rate over a sliding window

**HMAC verification** (`signatures.py`):
- `hmac.compare_digest` (constant-time comparison)
- 5-minute skew window (`MAX_TS_SKEW_SECONDS = 300`)
- Job_id binding (prevents cross-job replay)
- Fail-closed on bad signature (no durable trace created)

### 4.6 LogReg baseline (`services/agents/src/agents/baselines/`)

A stdlib-only logistic regression baseline (no sklearn dependency):
- `LogRegBaseline` — frozen dataclass holding weights, bias, n_features
- `fit_logreg_baseline(X, y, *, max_iter, C)` — gradient descent with L2 regularization
- `predict_proba(X)` — sigmoid decision function
- `roc_auc(y_true, y_score)` — pairwise comparison via broadcasting

### 4.7 Paper spine replay (`scripts/paper_spine_replay.py`)

End-to-end proof script that:
1. Runs a synthetic prediction through the paper trading spine
2. Runs the settlement worker against the fixture predictions
3. Verifies the evidence receipt values: `settlement_hit_rate=1.0`, `pending_count=0`, `brier=0.0`
4. Writes a receipt to `reports/paper-spine/latest.json`

---

## 5. What Was Done This Session

### Phase 1: Implementation (21 todos)

The 21 implementation todos from the plan were executed in parallel batches
using subagents. Each todo was committed individually with a precise commit
message. The todos were:

| # | Todo | Commit |
|---|------|--------|
| 1 | ApprovedRoots module | `a6d36cb` |
| 2 | Manifest schemas | `571733a` |
| 3 | Settlement schema + side-store | `5dd41ed` |
| 4 | FeatureSnapshotStore | `248530c` |
| 5 | datasets __init__ facade | `6b14d3f` |
| 6 | Approved-root on TrainBody | `74ed884` |
| 7 | Approved-root on backtest | `b4a3b20` |
| 8 | /models/{name}/outcomes route | (bundled in `74ed884`) |
| 9 | Feature-availability sidecar | `bd17e80` |
| 10 | Dossier + calibration helpers | `902b7ef` |
| 11 | Settlement worker MVP | `6fe5e1b` |
| 12 | Settlement side-store tests | `4ebd318` |
| 13 | Remove _compat_sign_callback | `65be033` |
| 14 | Durable callback_rejection_rate | `52d99be` |
| 15 | Scheduler polling health test | `4790795` |
| 16 | LogReg baseline scaffold | `515ccae` |
| 17 | Extract make_folds → cv.py | `77edc9a` |
| 18 | Migrate gbm_predictor walk_forward | `38d8d9c` |
| 19 | Migrate backtester make_folds | `3360709` |
| 20 | Migrate quant_foundry window | `f038cc2` |
| 21 | paper_spine_replay --with-settlement | `2a8b16b` |

### Phase 2: Verification (F1-F4)

- **F1**: Plan compliance audit — ALL PASS
- **F2**: Code quality review — ruff + mypy clean
- **F4**: Scope fidelity — all 11 guardrails held

### Phase 3: In-depth review (6 parallel audit subagents)

6 specialized read-only subagents audited the work:

| Specialty | Verdict | Key finding |
|-----------|---------|-------------|
| Security | STRONG, 1 HIGH | TOCTOU: API routes discard resolved path |
| Architecture | 0 flaws, 4 concerns | Two parallel settlement systems diverge |
| Data integrity | 1 BUG | `_find` returns first match, breaks idempotency |
| Test quality | PASS | 2031 tests, 0 failures |
| Code style | PASS | Near-perfect convention match, 6 mypy errors |
| Scope fidelity | PASS | All 11 guardrails held |

Audit files: `.omo/evidence/audit-{security,architecture,data-integrity,test-quality,code-style,scope-fidelity}.md`
Compiled review: `.omo/evidence/in-depth-review.md`

### Phase 4: Hardening fixes (audit findings)

Issues found by the audit and fixed:

| Commit | Severity | Fix |
|--------|----------|-----|
| `77edc9a` | CRITICAL | Commit untracked `cv.py` + `test_cv.py` (todo 17's deferred commit) |
| `d536eda` | MEDIUM | Generate missing final receipts (callback-rejection + cv-convergence) |
| `79bb738` | — | In-depth review document + 6 audit reports |
| `2f9b923` | HIGH | **TOCTOU fix**: use `resolved.path` downstream in both API routes instead of re-parsing raw user input |
| `6289e84` | MEDIUM | **`_find` first-match bug**: changed to return last match + added regression test for pending→settled→settled sequence |
| `1e072b6` | LOW | 6 mypy `no-any-return` errors fixed (logreg.py, paper_spine_replay.py); `_can_symlink()` probe moved to `tempfile.gettempdir()`; `close_t2 == 0` guard added to settlement worker; `from __future__ import annotations` added to `baselines/__init__.py` |
| `80acd4f` | LOW | Remove unused `pathlib` import after TOCTOU fix |

### Phase 5: Architectural wiring (3 tasks, 2 parallel subagents)

The audit identified 3 architectural concerns that were "incomplete wiring"
rather than code bugs. These were addressed with 2 parallel subagents:

**Task 2 — Wire FeatureSnapshotStore into publish loop** (commit `3d64917`):
- `features.py`: Added `_compute_feature_schema_hash()` + `frame_ts_out` sink parameter to `load_live()`
- `infer.py`: Added `last_feature_vector` + `last_feature_frame_ts` attributes on GBMPredictor
- `main.py`: Added best-effort `FeatureSnapshot` write in `_publish_loop` using `append_if_missing`
- 7 new tests (happy path, no-vector skip, write failure, hash determinism, frame_ts sink)
- **Result**: `feature_schema_hash` leg of evidence receipt is now populated in production

**Tasks 1+3 — Wire settlements.worker into production poller** (commit `e51b757`):
- `market_data_bridge.py` (new): Wraps sync `BarDataAdapter` into async `market_data_source` contract
- `settlements_poller.py` (new): Periodic poller with documented reconciliation strategy
- `main.py`: Wired poller into lifespan (runs regardless of gateway mode, cancelled on shutdown)
- 7 bridge tests + 9 poller tests
- Config: `SETTLEMENTS_WORKER_POLL_S` env var (default 60s, 0 to disable)
- **Result**: `/models/{name}/outcomes` now has a production writer

---

## 6. Data Flow

### 6.1 Prediction → Settlement → Outcome

```
GBMPredictor.run()
    │
    ├── load_live(store, symbol, feature_names)
    │     └── returns (feature_vector, FeatureHealth) + frame_ts via sink
    │
    ├── _predict(symbol, row) → Prediction
    │
    └── yield Prediction
          │
          ▼
_publish_loop()
    │
    ├── prediction_log.append(...) → PredictionRow (data/predictions/<agent_id>.jsonl)
    │
    ├── feature_health_log.append(...) → FeatureHealthRow (data/feature_health/<agent_id>.jsonl)
    │     └── best-effort, swallowed on failure
    │
    └── feature_snapshot_store.append_if_missing(...) → FeatureSnapshot (data/feature_snapshots/<agent_id>.jsonl)
          └── best-effort, swallowed on failure
```

```
settlements_poller (every 60s)
    │
    ├── tick(now_ns, predictions_dir, settlements_dir, market_data_source)
    │     ├── _load_due_predictions(predictions_dir, now_ns)
    │     │     └── scans all <agent_id>.jsonl files for ts_event + horizon_ns <= now_ns
    │     │
    │     ├── for each due prediction:
    │     │     ├── _existing_status(store, agent_id, prediction_id, cost_model_version)
    │     │     │     └── returns LAST match (settled → skip, pending_data → retry)
    │     │     │
    │     │     ├── market_data_source(symbol, ts_event, ts_event) → close_t1
    │     │     ├── market_data_source(symbol, ts_event, ts_event + horizon_ns) → close_t2
    │     │     │
    │     │     ├── if prices available:
    │     │     │     realized_return_gross = (close_t2 / close_t1) - 1
    │     │     │     realized_return_net = gross - 8e-4
    │     │     │     brier_component = (prob_up - actual_up) ** 2
    │     │     │     → append SettlementRecord(status="settled")
    │     │     │
    │     │     └── if prices missing:
    │     │           → append SettlementRecord(status="pending_data")
    │     │
    │     └── returns list[SettlementRecord]
    │
    └── log.info("settlements.worker.tick", settled=len(records))
```

```
GET /models/{name}/outcomes
    │
    ├── prediction_log.read_for_agent(agent_id, limit, since_ns) → list[PredictionRow]
    │
    ├── settlement_store.read_for_agent(agent_id, limit) → list[SettlementRecord]
    │
    ├── for each prediction:
    │     └── build_evidence_receipt(prediction=pred, settlement=sett, feature_snapshot=None)
    │           → flat dict with prediction + settlement + feature_schema_hash fields
    │
    └── returns {"count": N, "outcomes": [receipt, ...]}
```

### 6.2 Training path with approved-roots gate

```
POST /models/train
    │
    ├── body.input_path validation:
    │     ├── non-empty string check (422 if empty)
    │     └── _get_approved_roots().resolve(body.input_path)
    │           ├── reject ".." traversal (422, code="traversal")
    │           ├── resolve symlinks (strict=False)
    │           ├── check resolved path inside approved root (422, code="outside_root")
    │           ├── reject symlinked components (422, code="symlink_escape")
    │           └── return ResolvedPath(path=resolved, inside_root=root)
    │
    ├── req = TrainingRequest(input_path=str(resolved.path), ...)
    │     └── uses the symlink-resolved absolute path (closes TOCTOU)
    │
    └── store.start_run(req) → training run
```

### 6.3 Callback security flow

```
RunPod callback → gateway.receive_callback(payload)
    │
    ├── _extract_callback_fields(payload) → (job_id, callback_ts, signature) | None
    │
    ├── if None (unsigned/missing):
    │     ├── callback_metrics.append(event="rejected", reason_code="missing_runpod_callback_fields")
    │     ├── mark job FAILED with error_code="missing_runpod_callback_fields"
    │     └── continue to next job (fail-closed, no durable trace created)
    │
    ├── verify_callback(job_id, callback_ts, signature, secret)
    │     ├── hmac.compare_digest (constant-time)
    │     ├── |callback_ts - now| <= 300s (skew window)
    │     ├── signature binds to job_id (no cross-job replay)
    │     └── returns True/False (not exception)
    │
    ├── if False (bad signature):
    │     ├── callback_metrics.append(event="rejected", reason_code="bad_signature")
    │     ├── mark job FAILED
    │     └── continue (fail-closed)
    │
    └── if True:
          ├── callback_metrics.append(event="accepted")
          └── process callback result
```

---

## 7. Test Coverage

### 7.1 Test counts

| Package | Tests | Status |
|---------|-------|--------|
| `libs/fincept-core` | 286 | 286 passed |
| `services/api` | 475 | 475 passed |
| `services/agents` | 147 | 147 passed |
| `services/backtester` | 198 | 198 passed |
| `services/quant_foundry` | 926 | 926 passed, 2 skipped (onnxruntime) |
| `services/settlements` | 22 | 22 passed |
| **TOTAL** | **2054** | **2052 passed, 2 skipped, 0 failed** |

### 7.2 Key test files

| File | Tests | What it covers |
|------|-------|----------------|
| `test_approved_roots.py` | 13 | Symlink, traversal, encoding, env-var, fail-closed |
| `test_settlement_ledger.py` | 32 | Idempotency, look-ahead, pending→settled, duplicate detection |
| `test_feature_snapshots.py` | 12 | Append, read, idempotency, malformed-line tolerance |
| `test_cv.py` | 21 | Fold math, window derivation, invalid args, boundary conditions |
| `test_worker.py` | 15 | Tick state machine, pending→settled, idempotent rerun, close_t2==0 |
| `test_market_data_bridge.py` | 7 | get_close wrapping, get_prices fallback, None handling |
| `test_settlements_poller.py` | 9 | Interval parsing, tick invocation, failure swallowing |
| `test_gbm_feature_health.py` | 16 | FeatureHealth + FeatureSnapshot writes, best-effort failure |
| `test_gateway_callbacks.py` | 5 | No-compat path, signed/unsigned, bad signature, ts skew |
| `test_callback_metrics.py` | 10 | Happy path, persistence, malformed lines, no-secret-leak |
| `test_models_outcomes.py` | 11 | Left-join, limit/since_ns, missing files, clock granularity |
| `test_models_train.py` | 5 | Approved-roots gate, non-empty check |
| `test_backtest.py` | 4 | Approved-roots gate on backtest |

---

## 8. Configuration

### 8.1 Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `FINCEPT_APPROVED_DATA_ROOTS` | `data,models` | Comma-separated approved root paths |
| `SETTLEMENTS_DIR` | `data/settlements` | Settlement store directory |
| `PREDICTIONS_DIR` | `data/predictions` | Prediction log directory |
| `FEATURE_HEALTH_DIR` | `data/feature_health` | Feature health sidecar directory |
| `SETTLEMENTS_WORKER_POLL_S` | `60` | Settlements worker poll interval (0 to disable) |
| `FINCEPT_REPLAY_DRY_RUN` | `1` | Paper spine replay dry-run mode |

### 8.2 Cost models

| Model | Fee bps | Spread bps | Slippage bps | Used by |
|-------|---------|------------|--------------|---------|
| `v1.default` | 5.0 | 3.0 | 0.0 | New `SettlementStore` |
| `cm-v1` | 10.0 | 5.0 | 3.0 | Old `quant_foundry.SettlementLedger` |

---

## 9. Filesystem Layout

```
data/
├── predictions/          # PredictionLog (existing)
│   └── <agent_id>.jsonl
├── settlements/          # SettlementStore (new)
│   └── <agent_id>.jsonl
├── feature_snapshots/    # FeatureSnapshotStore (new)
│   └── <agent_id>.jsonl
├── feature_health/       # FeatureHealthLog (new sidecar)
│   └── <agent_id>.jsonl
└── callback_metrics/     # CallbackMetricsStore (new)
    └── <source>.jsonl

reports/
├── paper-spine/
│   └── latest.json       # Paper spine replay receipt
├── quant-foundry/
│   └── callback-rejection-receipt.json  # Callback security receipt
└── cv-convergence-receipt.json          # CV convergence receipt

.omo/
├── plans/
│   └── ml-dataset-evidence-spine.md     # The plan
└── evidence/
    ├── in-depth-review.md               # Compiled audit document
    ├── audit-security.md
    ├── audit-architecture.md
    ├── audit-data-integrity.md
    ├── audit-test-quality.md
    ├── audit-code-style.md
    ├── audit-scope-fidelity.md
    └── task-{1..21}*.report.md          # Per-todo evidence files
```

---

## 10. Guardrails That Were Enforced

All 11 "Must NOT have" guardrails from the plan were verified to hold:

1. No changes to `PredictionRow` schema (0 lines changed in `prediction_log.py`)
2. No schema-version-2 unified prediction row
3. No DuckDB/Parquet as first storage layer
4. No live trading unlock / `paper_bridge`
5. No full RunPod serverless deployment
6. No Cloud spend
7. No foundation-model/diffusion/debate/allocator pieces
8. No Optuna/Hyperband/triple-barrier/meta-labeling/conformal
9. No touching dashboard receipts beyond `gateway.py`
10. No vague "improve model" todos
11. No forbidden imports (sklearn/optuna/hyperopt)

---

## 11. Outstanding Items

These are deferred items, not bugs:

1. **Consolidate the two settlement systems** — The new `SettlementStore` (agent_id, v1.default) and old `SettlementLedger` (model_id, cm-v1) coexist. Full consolidation requires reconciling the cost models and keying, and migrating the quant_foundry dashboard to read from the new store. Deferred pending operational validation.

2. **Consume `build_dossier` / `build_calibration_sidecar`** — The parity helpers in `dossier.py` are defined but not yet consumed by any service. They will displace the `quant_foundry.dossier` internal implementations in a future task.

3. **Pre-existing flaky test** — `test_real_trainer_inference_e2e::test_full_pipeline_train_inference_ledger` occasionally fails with `ResourceWarning: unclosed event loop` when running all 6 packages together on Windows. Passes in isolation. Unrelated to this work — it's a Windows asyncio cleanup issue.

---

## 12. Commit History

```
e51b757 feat(api): wire settlements.worker into production poller with BarDataAdapter bridge
3d64917 feat(agents): wire FeatureSnapshotStore into gbm_predictor publish loop
80acd4f fix(api): remove unused pathlib import after TOCTOU fix
1e072b6 fix: resolve mypy no-any-return errors, _can_symlink probe isolation, close_t2==0 guard
6289e84 fix(fincept-core): SettlementStore._find returns last match to fix terminal-row idempotency bug
2f9b923 security(api): close TOCTOU in approved-roots gate by using resolved path downstream
79bb738 chore(evidence): add in-depth review document + 6 specialty audit reports
d536eda chore(reports): add missing final receipts for callback-rejection + cv-convergence
77edc9a feat(fincept-core): extract shared purged+embargoed walk-forward CV utility
d1a40a5 fix: resolve mypy type-ignore + outcomes test clock-granularity flakiness
2a8b16b feat(scripts): extend paper_spine_replay with synthetic settlement proof
4790795 test(quant-foundry): assert scheduler polling produces durable health state
52d99be feat(quant-foundry): durable callback_rejection_rate from append-only metrics store
4ebd318 test(fincept-core): expand settlement side-store state-machine coverage
3360709 refactor(backtester): delegate make_folds to fincept_core.datasets.cv with deprecation shim
6fe5e1b feat(settlements): add worker tick that tails prediction_log and writes side-store
38d8d9c refactor(agents): gbm trainer delegates walk-forward math to fincept_core.datasets.cv
f038cc2 refactor(quant-foundry): training_manifest derives walk-forward window from fincept_core
65be033 security(quant-foundry): remove _compat_sign_callback, fail closed on unsigned
515ccae chore(agents): add stdlib-only logreg baseline scaffold
bd17e80 feat(agents): record feature availability counter as JSONL sidecar
74ed884 feat(api): enforce approved-root allowlist on training input path
b4a3b20 feat(api): enforce approved-root allowlist on backtest input paths
902b7ef feat(fincept-core): add dossier + calibration sidecar helpers
6b14d3f feat(fincept-core): datasets package facade with stable public surface
248530c feat(fincept-core): add FeatureSnapshotStore for prediction-cycle snapshots
5dd41ed feat(fincept-core): add settlement side-store with idempotency + look-ahead guard
571733a feat(fincept-core): add dataset/manifest/freeze schemas for shared evidence spine
a6d36cb feat(fincept-core): add ApprovedRoots with env-configurable fail-closed allowlist
```
