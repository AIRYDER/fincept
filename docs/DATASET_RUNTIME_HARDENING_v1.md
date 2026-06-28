# Dataset Runtime Hardening v1 — Session Report

> **Date:** 2026-06-27
> **Branch:** `codex/portfolio-optimizer-core`
> **Goal:** Make the whole project run every time. Freeze the dataset
> architecture, harden the runtime, complete the evidence loop, then
> scale real data.

This document records every change made in the "Dataset Runtime
Hardening v1" build session so that downstream agents can pick up
where this session left off, understand the new contracts, and avoid
re-discovering the same insertion points.

---

## 0. TL;DR

| Priority | What changed | Tests added |
|---|---|---|
| P1 — Training run recovery | `resumable_failed` status, `heartbeat()`, `resume_run()`, `POST /models/runs/{id}/resume` | 5 |
| P2 — Feature snapshot write path | `read_by_prediction_id()` on the store, outcomes endpoint now joins snapshots, shadow loop writes snapshots | 7 |
| P3 — Real dataset expansion | New `data_ingestion/` module: equities, news, macro, vendors | 13 |
| P4 — Golden E2E smoke test | 4 tests covering the full evidence spine + resume endpoint | 4 |
| P5 — Dataset quality reports | `DatasetQualityReport` model + `compute_quality_report()` | (covered by P3 tests) |
| P6 — Schema versioning | `feature_schema_version` field + `schema_compat.py` compatibility checker | 9 |
| P7 — Worker durability | Per-fold checkpointing in trainer, `ArtifactManifest` emission, heartbeat loop | (covered by P1 tests) |

**Total: 124 tests pass, ruff clean, mypy clean.**

---

## 1. Architecture Decisions (frozen)

The leakage-safe dataset architecture is **frozen**. Do not rewrite it.
The four-layer pipeline (raw sources -> feature lake -> manifest + file
-> trainer) and the three leakage guards (PIT proof, as-of universe,
purged-k-fold + embargo) are the good part. This session hardened the
*runtime* around that architecture, not the architecture itself.

New contracts introduced this session:

1. **`resumable_failed`** — a new training run status. A run that was
   `queued` or `running` when the API restarted is now flipped to
   `resumable_failed` (not `failed`) so the operator can resume it.
2. **`feature_schema_version: int = 1`** — a new field on
   `DatasetManifest`, `ArtifactManifest`, and `FeatureSnapshot`. A
   version bump means "the feature pipeline changed in a way that
   invalidates models trained on the previous version." Orthogonal to
   the hash (which identifies the exact feature set).
3. **`ArtifactManifest` emission** — the trainer now writes
   `artifact_manifest.json` alongside `model.txt` and `meta.json`.
4. **`DatasetQualityReport`** — a new sidecar (`dataset.quality.json`)
   written alongside every dataset export.

---

## 2. File-by-File Changes

### 2.1 Training run lifecycle (P1 + P7)

#### `services/api/src/api/training.py`

**`TrainingRun` dataclass** — 5 new fields:

```python
heartbeat_at: float | None = None
dataset_id: str | None = None
manifest_hash: str | None = None
artifact_manifest_path: str | None = None
resume_token: str | None = None
```

All serialized in `to_payload()` and loaded in `_load_record()`.

**`_reload_from_disk()`** — stale runs now become `resumable_failed`:

```python
if run.status in ("queued", "running"):
    run.status = "resumable_failed"
    run.error = "api restarted while this run was active; subprocess state lost (resumable)"
```

**`heartbeat(run_id)` method** — updates `heartbeat_at` and persists.
Returns `True` if the run exists.

**`resume_run(run_id)` async method** — re-launches a
`resumable_failed` run. Validates status, checks concurrency cap,
resets lifecycle fields, assigns a `resume_token`, schedules a new
subprocess.

**`_run_subprocess()`** — now runs a periodic heartbeat loop
(every 5s) alongside the subprocess wait:

```python
async def _heartbeat_loop():
    while proc.poll() is None:
        self.heartbeat(run.run_id)
        await asyncio.sleep(5.0)

heartbeat_task = asyncio.create_task(_heartbeat_loop())
try:
    exit_code = await asyncio.to_thread(proc.wait)
finally:
    heartbeat_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await heartbeat_task
```

**Import added:** `import contextlib`.

#### `services/agents/src/agents/gbm_predictor/train.py`

**Per-fold checkpointing** — `walk_forward_cv()` now accepts
`checkpoint_dir` and `resume_from_fold` parameters. After each fold's
`lgb.train()`, the booster is saved to `fold_<idx>_model.txt` plus a
`fold_<idx>_meta.json` with fold metadata. For resume, folds below
`resume_from_fold` load the checkpoint from disk and record metrics
with `"resumed": True`.

**New CLI args:**

```
--checkpoint-dir <path>     (default: <out-dir>/checkpoints)
--resume-from-fold <int>    (default: None)
```

**`ArtifactManifest` emission** — after the final model is saved,
`main()` writes `artifact_manifest.json`:

```python
artifact_manifest = ArtifactManifest(
    artifact_id=f"gbm-{out_dir.name}",
    sha256=hashlib.sha256(model_path.read_bytes()).hexdigest(),
    size_bytes=model_path.stat().st_size,
    uri=str(model_path),
    model_family="gbm",
    created_at_ns=time.time_ns(),
    feature_schema_hash=_compute_feature_schema_hash(FEATURES),
    label_schema_hash=hashlib.sha256(
        f"binary_forward_return_{args.horizon_bars}bars".encode()
    ).hexdigest(),
)
```

**Imports added:** `hashlib`, `_compute_feature_schema_hash` from
`agents.gbm_predictor.features`, `ArtifactManifest` from
`fincept_core.datasets`.

#### `services/api/src/api/routes/models.py` (orchestrator changes)

**`POST /models/runs/{run_id}/resume`** — new endpoint (202/404/409/429):

```python
@router.post("/runs/{run_id}/resume", status_code=202)
async def post_resume_run(run_id: str, _: dict = Depends(require_user)):
    try:
        run = await get_store().resume_run(run_id)
    except TrainingValidationError as exc:
        msg = str(exc)
        if "not found" in msg:
            raise HTTPException(status_code=404, detail=msg) from exc
        if "not resumable" in msg:
            raise HTTPException(status_code=409, detail=msg) from exc
        status = 429 if "in flight" in msg else 400
        raise HTTPException(status_code=status, detail=msg) from exc
    return run.to_payload()
```

**Status whitelist** — `resumable_failed` added to the `?status=`
filter and the summary counts in `GET /models/runs`.

---

### 2.2 Evidence receipt completion (P2)

#### `libs/fincept-core/src/fincept_core/datasets/feature_snapshot.py`

**`read_by_prediction_id()` method** on `FeatureSnapshotStore`:

```python
def read_by_prediction_id(
    self, prediction_id: str, *, agent_id: str,
) -> FeatureSnapshot | None:
```

Scans the agent's JSONL snapshot file for a matching `prediction_id`.
Returns `None` if the file doesn't exist or no match is found. Malformed
lines are skipped (same tolerance pattern as the read path).

#### `services/api/src/api/routes/models.py` (Builder B changes)

**`FeatureSnapshotStore` import** added to the existing
`fincept_core.datasets` import block.

**`_get_snapshot_store()`** — lazy singleton (module-level
`_snapshot_store` variable), following the existing
`_get_settlement_store` pattern.

**`GET /models/{name}/outcomes`** — the `feature_snapshot=None` list
comprehension is replaced with a loop that looks up each prediction's
feature snapshot:

```python
snapshot_store = _get_snapshot_store()
outcomes = []
for pred in predictions:
    snapshot = snapshot_store.read_by_prediction_id(
        pred.id, agent_id=pred.agent_id,
    )
    outcomes.append(
        build_evidence_receipt(
            prediction=pred,
            settlement=settlement_by_pid.get(pred.id),
            feature_snapshot=snapshot,
        )
    )
```

#### `services/agents/src/agents/gbm_predictor/main.py`

**`_shadow_loop`** — now accepts
`feature_snapshot_store: FeatureSnapshotStore | None = None` and
writes feature snapshots after each prediction (mirroring the logic
already in `_publish_loop` lines 632-667). Both `shadow_task` creation
sites (lines 418-424 and 527-533) now pass
`feature_snapshot_store=feature_snapshot_store`.

**Key finding:** The feature snapshot write path in `_publish_loop`
was *already fully wired* before this session. The reason
`data/feature_snapshots/` didn't exist on disk was simply that the
agent hadn't been run live — the predictions on disk were from
tests/fixtures. The real gaps were (a) the outcomes endpoint passing
`feature_snapshot=None` and (b) the shadow loop not writing snapshots.

---

### 2.3 Real dataset expansion + quality reports (P3 + P5)

New module: `services/quant_foundry/src/quant_foundry/data_ingestion/`

#### `quality_report.py`

**`DatasetQualityReport`** — frozen Pydantic v2 model
(`extra="forbid"`) with 20 fields:

- Coverage: `total_rows`, `total_symbols`, `time_span_start_ns`,
  `time_span_end_ns`
- Feature quality: `feature_names`, `feature_coverage_pct`,
  `feature_missing_count`
- Label quality: `label_balance`, `label_missing_count`
- Fold quality: `fold_count`, `fold_train_counts`, `fold_val_counts`
- Leakage checks: `pit_proof_verified`, `embargo_sufficient`,
  `no_forward_joins`
- Drift indicators: `mean_feature_values`, `std_feature_values`

**`compute_quality_report(parquet_path, manifest, ...)`** — reads a
parquet with polars (lazy import) + a `FeatureLakeManifest` and
derives all stats. Has `to_json()` and `write()` helpers.

#### `equities.py`

**`IngestionResult`** — frozen dataclass shared across all ingesters:
`parquet_path`, `manifest_path`, `receipt_path`, `quality_path`,
`manifest`, `quality_report`.

**`ingest_equity_bars(bars_path, output_dir, dataset_id, ...)`** —
ingests OHLCV bars, computes the 5 standard features
(`ret_1d`, `ret_5d`, `vol_20d`, `mom_10d`, `vol_ratio`), builds a
`FeatureLakeManifest` via `FeatureLakeBuilder`, exports parquet +
manifest.json + receipt.json + quality.json. Reuses the shared
pipeline from `scripts/build_dataset_manifest.py` via sys.path
injection (without modifying it).

#### `news.py`

**`ingest_news_events(events_path, output_dir, dataset_id, ...)`** —
loads vendor news events via `load_vendor_news_events` from the
news-impact-model experiment. Computes 5 text-derived features:
`headline_len`, `body_len`, `sentiment_proxy`, `event_type_count`,
`symbol_count`. Labels are binary (subsequent event for same symbol
within horizon).

#### `macro.py`

**`ingest_macro_indicators(csv_path, output_dir, dataset_id, ...)`** —
reads a CSV (`date, indicator, value`), computes 3 features
(`value`, `value_diff_1`, `value_pct_change_1`) per indicator, binary
label (next observation direction). Minimal but functional.

#### `vendors.py`

**`VENDOR_INGESTERS`** registry mapping `equity_bars`,
`news_events`, `macro_indicators` to their ingestion functions.
**`get_ingester(vendor)`** returns the function or raises
`ValueError` on unknown vendors.

#### `__init__.py`

Re-exports the full public surface:
`DatasetQualityReport`, `compute_quality_report`,
`ingest_equity_bars`, `ingest_news_events`,
`ingest_macro_indicators`, `VENDOR_INGESTERS`, `get_ingester`.

---

### 2.4 Feature schema versioning (P6)

#### `libs/fincept-core/src/fincept_core/datasets/schemas.py`

**`feature_schema_version: int = 1`** added to:
- `DatasetManifest` (after `schema_version`)
- `ArtifactManifest` (after `schema_version`)
- `FeatureSnapshot` (after `schema_version`)

The field has a default of `1`, so old serialized data without the
field deserializes cleanly (backward-compatible). `extra="forbid"` and
`frozen=True` are preserved.

#### `libs/fincept-core/src/fincept_core/datasets/schema_compat.py` (NEW)

**`SchemaIncompatibilityError(ValueError)`** — with a stable `.code`
attribute: `"version_mismatch"`, `"missing_features"`,
`"extra_features"`.

**`SchemaCompatResult`** — frozen dataclass (`compatible: bool`,
`error: SchemaIncompatibilityError | None`).

**`check_feature_schema_compatibility(...)`** — enforces 4 rules in
order:
1. Version must match exactly (hard failure).
2. If hashes match, fully compatible.
3. Missing features (artifact has a feature the snapshot doesn't) =
   hard failure.
4. Extra features in snapshot = allowed by default, can be rejected
   with `allow_extra_features=False`.

**`assert_feature_schema_compatible(...)`** — raises on failure.

#### `libs/fincept-core/src/fincept_core/datasets/__init__.py`

Re-exports `SchemaCompatResult`, `SchemaIncompatibilityError`,
`assert_feature_schema_compatible`,
`check_feature_schema_compatibility`. All added to `__all__`
(alphabetically sorted for ruff `I001`).

---

### 2.5 Golden E2E smoke test (P4)

#### `services/api/tests/test_golden_e2e_smoke.py` (NEW)

4 tests:

1. **`test_golden_e2e_evidence_spine`** — the full loop:
   artifact manifest -> schema compat check -> prediction ->
   feature snapshot -> settlement -> verify
   `GET /models/{name}/outcomes` returns a complete receipt with all
   three legs (prediction + settlement + `feature_schema_hash`).

2. **`test_golden_e2e_pending_prediction`** — prediction + snapshot
   but no settlement -> `settlement_status: "pending_time"`,
   `feature_schema_hash` still present.

3. **`test_golden_e2e_resume_endpoint`** — `resumable_failed` run
   resumed via `POST /models/runs/{id}/resume` -> 202 + `resume_token`.

4. **`test_golden_e2e_resume_rejects_completed`** — resume endpoint
   rejects a `completed` run with 409.

---

## 3. New APIs and Endpoints

### HTTP endpoints

| Method | Path | Status codes | Purpose |
|---|---|---|---|
| `POST` | `/models/runs/{run_id}/resume` | 202, 404, 409, 429, 400 | Re-launch a `resumable_failed` training run |
| `GET` | `/models/runs?status=resumable_failed` | 200 | Filter runs by the new status |

### Python APIs

| Module | Symbol | Purpose |
|---|---|---|
| `fincept_core.datasets` | `SchemaIncompatibilityError` | Raised on schema mismatch (has `.code`) |
| `fincept_core.datasets` | `check_feature_schema_compatibility()` | Non-raising compatibility check |
| `fincept_core.datasets` | `assert_feature_schema_compatible()` | Raising compatibility check |
| `fincept_core.datasets` | `SchemaCompatResult` | Result of non-raising check |
| `fincept_core.datasets.FeatureSnapshotStore` | `.read_by_prediction_id()` | Look up a snapshot by prediction_id |
| `api.training.TrainingStore` | `.heartbeat(run_id)` | Update heartbeat_at for a run |
| `api.training.TrainingStore` | `.resume_run(run_id)` | Re-launch a resumable_failed run |
| `quant_foundry.data_ingestion` | `DatasetQualityReport` | Comprehensive quality metrics model |
| `quant_foundry.data_ingestion` | `compute_quality_report()` | Compute quality from parquet + manifest |
| `quant_foundry.data_ingestion` | `ingest_equity_bars()` | Ingest OHLCV bars -> full dataset |
| `quant_foundry.data_ingestion` | `ingest_news_events()` | Ingest vendor news -> dataset |
| `quant_foundry.data_ingestion` | `ingest_macro_indicators()` | Ingest macro CSV -> dataset |
| `quant_foundry.data_ingestion` | `get_ingester(vendor)` | Vendor registry lookup |

### CLI args (trainer)

| Arg | Default | Purpose |
|---|---|---|
| `--checkpoint-dir` | `<out-dir>/checkpoints` | Directory for per-fold model checkpoints |
| `--resume-from-fold` | `None` | Resume CV from this fold index |

---

## 4. Training Run State Machine (updated)

```
  queued    -- record created, subprocess not yet spawned
  running   -- subprocess started, heartbeat active
  completed -- subprocess exited 0 + writes to out_dir succeeded
  failed    -- subprocess exited non-zero or pre-launch validation failed
  resumable_failed -- API restarted while run was active (subprocess state lost)

  Transitions:
    queued -> running -> completed
    queued -> running -> failed
    queued -> running -> resumable_failed  (on API restart)
    resumable_failed -> queued  (via POST /models/runs/{id}/resume)
```

---

## 5. How to Run Things

### Tests

```bash
# All tests in this session's scope
uv run pytest \
  libs/fincept-core/tests/test_schema_compat.py \
  libs/fincept-core/tests/test_datasets_dossier.py \
  libs/fincept-core/tests/test_feature_snapshots.py \
  services/api/tests/test_training.py \
  services/api/tests/test_models_outcomes.py \
  services/api/tests/test_golden_e2e_smoke.py \
  services/quant_foundry/tests/test_data_ingestion.py \
  services/agents/tests/test_gbm_train.py \
  services/agents/tests/test_gbm_feature_health.py \
  -x -q

# Lint
uv run ruff check \
  libs/fincept-core/src/fincept_core/datasets/ \
  services/api/src/api/training.py \
  services/api/src/api/routes/models.py \
  services/agents/src/agents/gbm_predictor/train.py \
  services/agents/src/agents/gbm_predictor/main.py \
  services/quant_foundry/src/quant_foundry/data_ingestion/

# Type check
uv run mypy libs/fincept-core/src/fincept_core/datasets/
```

**Important:** Use `uv run` (not `python -m pytest` or `py -3.12`).
The repo is a uv workspace; the `.venv` has all local packages
installed. The default `python` on PATH is 3.10.6 which cannot import
the packages.

### Build a synthetic dataset

```bash
uv run python scripts/build_synthetic_dataset.py \
    --n-symbols 5 --n-days 500 --seed 42 \
    --manifest-dir data/datasets/
```

### Ingest real equity bars

```python
from quant_foundry.data_ingestion import ingest_equity_bars
from pathlib import Path

result = ingest_equity_bars(
    bars_path=Path("data/bars/AAPL.parquet"),
    output_dir=Path("data/datasets/"),
    dataset_id="real_aapl_v1",
    label_horizon_days=5,
    n_folds=3,
)
# result.parquet_path, result.manifest_path, result.receipt_path,
# result.quality_path, result.manifest, result.quality_report
```

### Resume a failed training run

```bash
# Via API
curl -X POST http://localhost:8000/models/runs/{run_id}/resume \
     -H "Authorization: Bearer <token>"

# Via Python
from api.training import TrainingStore
store = TrainingStore()
await store.resume_run(run_id)
```

---

## 6. Notes for Downstream Agents

### Gotchas

1. **`data/synth_bars.parquet` is NOT OHLCV format.** It has
   pre-computed features (`close, ret_1m, ret_5m, ...`), not
   `open/high/low/close/volume`. The equity ingestion test generates
   synthetic OHLCV via `build_synthetic_dataset.generate_synthetic_bars`
   instead. If you need real OHLCV for testing, use that generator or
   the `data/datasets/backtest_synthetic/` fixture.

2. **The manifest JSON on disk includes extra keys** (`availability`,
   `feature_names`, `manifest_hash`) that are not part of the
   `FeatureLakeManifest` Pydantic model. When loading a manifest from
   disk for validation, pop these keys before `model_validate`, or use
   `FeatureLakeManifest.model_validate_json()` which ignores extra
   keys when `extra="forbid"` is NOT set (but ours IS set, so you must
   pop them).

3. **The `patched_training` fixture in `test_training.py`** now also
   monkeypatches `api.training.default_approved_roots` (not just
   `api.routes.models._get_approved_roots`). The store-level
   validation uses `default_approved_roots` directly. If you add new
   training tests that call `store.start_run` with tmp_path inputs,
   make sure the fixture is applied.

4. **`resumable_failed` is a new status.** Any code that switches on
   run status or counts statuses needs to handle it. The
   `GET /models/runs` endpoint and summary already do. If you add
   status-based logic elsewhere, include `resumable_failed`.

5. **The feature snapshot write path in `_publish_loop` was already
   wired before this session.** The reason `data/feature_snapshots/`
   didn't exist was that the agent hadn't been run live. Don't
   re-implement it — just run the agent.

### Patterns to follow

1. **Frozen Pydantic v2 models with `extra="forbid"`** for all new
   schemas. This makes them tamper-evident and round-trip exact.

2. **Lazy imports for heavy deps** (numpy, polars, pyarrow, lightgbm)
   inside functions, not at module top-level. This keeps the module
   importable without the ML stack.

3. **Atomic writes** via temp-file-then-rename (`os.replace`). The
   `_persist()` function in `training.py` already does this. Follow
   the same pattern for any new disk-writing code.

4. **Best-effort sidecar writes** in the prediction hot path. A
   feature snapshot / feature health write failure is logged and
   swallowed — it must never block predictions from being published.
   Follow the same `try/except + log.warning` pattern.

5. **No secrets in manifests.** Field names containing `password`,
   `token`, `secret`, `api_key`, etc. are rejected at the schema
   boundary. The `resume_token` field is safe because it's a
   short-lived operational token, not a credential.

### What's NOT done (future work)

1. **RunPod worker-side durability** — ~~the handler.py in
   `runpod/quant-foundry-training/` does not yet write a status file
   or heartbeat to `/runpod-volume/`.~~ **RESOLVED**: Both training and
   inference handlers now write status files + heartbeats via
   `runpod/shared/worker_status.py`. The gateway reads status files via
   `heartbeats()` and `detect_stale_workers()` when
   `QUANT_FOUNDRY_WORKER_STATUS_DIR` is configured. Status validation
   enforces allowed values. **Remaining operational gap**: mount the
   RunPod network volume at the configured path in production.

2. **Schema compat check at inference load time** — ~~the
   `assert_feature_schema_compatible()` function exists but is not
   yet called in `GBMPredictor.setup()`~~ **RESOLVED**: Wired into
   `GBMPredictor.setup()` via `_check_schema_compatibility()`.

3. **Real data at scale** — the `data_ingestion/` module provides the
   ingestion functions, but no real vendor data has been ingested
   beyond the tiny `real_eq_2024h1.parquet` (8.5 KB) and
   `news_alpha_candidate.parquet` (9.7 KB). The next step is to wire
   these ingesters to actual data sources (Alpaca API, news vendors,
   FRED macro data).

4. **Quality report in the manifest** — ~~the `DatasetQualityReport` is
   written as a sidecar (`dataset.quality.json`) but is not yet
   embedded in or referenced by the `FeatureLakeManifest`.~~ **RESOLVED**:
   `FeatureLakeManifest` now has a `quality_report_hash` field included
   in the canonical payload. All three ingestion pipelines (equities,
   macro, news) compute the quality report first, then embed its hash
   via `model_copy` before writing the manifest.

5. **The 3 failed training runs in `data/training_runs/`** still have
   status `failed` (they were written before this session's changes).
   They will not be auto-migrated to `resumable_failed`. An operator
   can manually resume them via the new endpoint if desired, but they
   need to be re-tagged first (or just re-run from scratch).

---

## 7. Test Inventory

| File | Tests | What it covers |
|---|---|---|
| `libs/fincept-core/tests/test_schema_compat.py` | 9 | All 4 compat rules + error codes + assert variant |
| `libs/fincept-core/tests/test_feature_snapshots.py` | 5 (new) + existing | `read_by_prediction_id` happy/sad paths |
| `libs/fincept-core/tests/test_datasets_dossier.py` | 9 (existing, no regressions) | Dossier + calibration sidecar |
| `services/api/tests/test_training.py` | 5 (new) + 25 (existing) | resumable_failed, resume_run, heartbeat |
| `services/api/tests/test_models_outcomes.py` | 2 (new) + existing | Outcomes with/without snapshots |
| `services/api/tests/test_golden_e2e_smoke.py` | 4 (new) | Full evidence spine + resume endpoint |
| `services/quant_foundry/tests/test_data_ingestion.py` | 13 (new) | Quality report, equity/news/macro ingestion, vendor registry |
| `services/agents/tests/test_gbm_train.py` | 13 (existing, no regressions) | Trainer with checkpointing changes |
| `services/agents/tests/test_gbm_feature_health.py` | 15 (existing, no regressions) | Feature health + snapshot path |
