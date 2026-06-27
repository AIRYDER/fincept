# Datasets & Data Structure

A visualization of how training data is structured, validated, and
flowed through the Quant Foundry — from raw market/news inputs down to
the exact CSV/Parquet layout the RunPod trainer expects.

This document covers:

1. **High-level** — the data pipeline, the layers, and the leakage guards.
2. **Low-level** — the exact row/column shapes, the point-in-time rules,
   the purged-k-fold structure, and concrete examples.

---

## 1. High-Level View

### 1.1 The Data Pipeline

Data flows through four layers, each with a strict contract. The
RunPod trainer never touches a database — it only reads a file URI
referenced by a manifest.

```
 ┌──────────────────────────────────────────────────────────────────────┐
 │  LAYER 1 — RAW SOURCES (point-in-time, vendor-availability stamped) │
 │                                                                      │
 │   fincept_db.bars   │  Alpaca bars API  │  News events  │  ...       │
 │   (PricePoint)      │  (PricePoint)      │  (NewsEvent)  │            │
 │   ts_ns, close      │  ts_ns, close      │  available_at_ns           │
 └──────────────────────────────────────────────────────────────────────┘
                              │
                              ▼  (as-of joins only, no forward joins)
 ┌──────────────────────────────────────────────────────────────────────┐
 │  LAYER 2 — FEATURE LAKE (point-in-time rows, PIT-proof)              │
 │                                                                      │
 │   FeatureRow                                                         │
 │   ├── symbol                                                         │
 │   ├── event_ts          (bar close / event time)                     │
 │   ├── decision_time      (PIT cutoff — when a decision could happen) │
 │   ├── features: (FeatureValue(name, value, observed_at), ...)        │
 │   └── label_horizon_ns  (e.g. 1 day = 86_400_000_000_000 ns)         │
 │                                                                      │
 │   UniverseEntry(symbol, listed_until, renamed_from)                  │
 │   ↑ includes delisted/renamed symbols (no survivorship bias)         │
 └──────────────────────────────────────────────────────────────────────┘
                              │
                              ▼  (build_manifest + export)
 ┌──────────────────────────────────────────────────────────────────────┐
 │  LAYER 3 — MANIFEST + EXPORTED FILE (the training reference)         │
 │                                                                      │
 │   FeatureLakeManifest                                                │
 │   ├── dataset_id, feature_schema_hash, label_schema_hash             │
 │   ├── as_of_ts, universe_hash, row_count, checksum                   │
 │   ├── folds: PurgedFoldSpec (leakage-safe train/val splits)          │
 │   ├── pit_proof_verified: True                                       │
 │   └── manifest_hash()  ← SHA-256 over canonical content              │
 │                                                                      │
 │   training_reference() = { kind, dataset_id, manifest_hash }         │
 │   ↑ NO DB credentials ever cross this boundary                       │
 │                                                                      │
 │   Exported file:  features.csv  OR  features.parquet                 │
 └──────────────────────────────────────────────────────────────────────┘
                              │
                              ▼  (RunPodTrainingRequest.dataset_manifest_ref)
 ┌──────────────────────────────────────────────────────────────────────┐
 │  LAYER 4 — RUNPOD TRAINER (reads the file, trains)                   │
 │                                                                      │
 │   RealLightGBMTrainer._load_dataset(ref)                             │
 │   ├── file://  → local Path                                          │
 │   ├── s3://    → StorageBackend.download_to_temp()                   │
 │   ├── bare path → Path                                               │
 │   ├── .csv     → numpy.genfromtxt                                    │
 │   └── .parquet → pyarrow / pandas                                    │
 │                                                                      │
 │   Returns:  X (n_rows, n_features), y (n_rows,), timestamps (n_rows,)│
 └──────────────────────────────────────────────────────────────────────┘
```

### 1.2 The Layers at a Glance

| Layer | Owner | Lives in | Key type | Leakage guard |
|---|---|---|---|---|
| 1. Raw sources | `fincept_core` / adapters | `market_data_adapter.py`, news sources | `PricePoint`, `NewsEvent` | vendor `ts_ns` / `available_at_ns` |
| 2. Feature lake | Quant Foundry builder | `feature_lake.py` | `FeatureRow`, `FeatureValue`, `UniverseEntry` | `observed_at <= decision_time` (PIT proof) |
| 3. Manifest + file | Quant Foundry builder | `dataset_manifest.py`, `feature_snapshot_export.py` | `FeatureLakeManifest`, `PurgedFoldSpec` | purged-k-fold + embargo >= max label horizon |
| 4. Trainer | RunPod container | `real_trainer.py` | `X`, `y`, `timestamps` (numpy) | walk-forward expanding window |

### 1.3 The Three Leakage Guards (non-negotiable)

These are enforced in code, not just documented:

```
  GUARD 1 — Point-in-time proof (feature_lake.py)
  ─────────────────────────────────────────────────
  For every FeatureValue in every FeatureRow:
      observed_at <= decision_time
  Violation → LeakyFeatureError (rejected at export, never silently included)

  GUARD 2 — As-of universe / no forward joins (feature_lake.py)
  ─────────────────────────────────────────────────────────────
  For every row:
      row.symbol ∈ universe
      row.decision_time <= listed_until (if delisted)
  Violation → ValueError at FeatureLakeBuilder construction

  GUARD 3 — Purged-k-fold + embargo (dataset_manifest.py)
  ────────────────────────────────────────────────────────
  embargo_ns >= max_label_horizon_ns
  For every fold: val_start >= purge_end, purge_start >= train_end
  Violation → ValidationError on FoldBoundary / PurgedFoldSpec
```

---

## 2. Low-Level View: The Exact Data Shapes

### 2.1 The Atomic Units

#### `PricePoint` (raw market data)
```python
@dataclass(frozen=True)
class PricePoint:
    ts_ns: int    # nanoseconds since epoch — when the bar was observed
    close: float  # close price at that time
```

#### `NewsEvent` (raw news, point-in-time)
```python
@dataclass(frozen=True)
class NewsEvent:
    event_id: str
    available_at_ns: int          # when the SYSTEM could act on it (not authoring time)
    source: str
    headline: str
    body: str = ""
    symbols: tuple[str, ...] = ()
    event_type: str = "general"
```

#### `FeatureValue` (one feature cell, with availability time)
```python
@dataclass(frozen=True)
class FeatureValue:
    name: str
    value: float
    observed_at: int   # ns since epoch — when the VENDOR made this value available
```
The `observed_at` is the critical field — it's what makes the dataset
point-in-time proof. A feature value whose `observed_at` is after the
row's `decision_time` would not have been knowable at decision time, so
including it would leak the future.

#### `FeatureRow` (one point-in-time observation)
```python
@dataclass(frozen=True)
class FeatureRow:
    symbol: str
    event_ts: int                              # event time (e.g. bar close)
    decision_time: int                         # PIT cutoff
    features: tuple[FeatureValue, ...]
    label_horizon_ns: int = 86_400_000_000_000 # 1 day default
```

#### `UniverseEntry` (as-of universe member)
```python
@dataclass(frozen=True)
class UniverseEntry:
    symbol: str
    listed_until: int | None     # None = still listed; else delisting time
    renamed_from: str | None     # original ticker if renamed
```
Including delisted/renamed symbols is what prevents **survivorship
bias** — a backtest that only sees currently-listed companies will
overstate returns.

### 2.2 How a `FeatureRow` Looks in Practice

From the test fixtures (`test_feature_lake.py`), a clean PIT-correct
dataset:

```python
NS_PER_DAY = 86_400_000_000_000

def _ts(day: int) -> int:
    return day * NS_PER_DAY

rows = (
    FeatureRow(
        symbol="AAPL",
        event_ts=_ts(10),
        decision_time=_ts(10),
        features=(
            FeatureValue("ret_1d",  0.01, observed_at=_ts(10)),  # ✓ known at t=10
            FeatureValue("vol_20d", 0.20, observed_at=_ts(9)),   # ✓ known at t=9
        ),
    ),
    FeatureRow(
        symbol="MSFT",
        event_ts=_ts(10),
        decision_time=_ts(10),
        features=(
            FeatureValue("ret_1d", -0.005, observed_at=_ts(10)),
            FeatureValue("vol_20d", 0.18,  observed_at=_ts(9)),
        ),
    ),
    FeatureRow(
        symbol="AAPL",
        event_ts=_ts(11),
        decision_time=_ts(11),
        features=(
            FeatureValue("ret_1d",  0.02,  observed_at=_ts(11)),
            FeatureValue("vol_20d", 0.21,  observed_at=_ts(10)),
        ),
    ),
)

universe = (
    UniverseEntry(symbol="AAPL", listed_until=None),               # still listed
    UniverseEntry(symbol="MSFT", listed_until=None),
    UniverseEntry(symbol="OLDCO", listed_until=_ts(40)),           # delisted at t=40
)
```

A **leaky** row (rejected at export):
```python
# feature observed at t+1 but decision at t=10 → LOOK-AHEAD LEAK
leaky = FeatureRow(
    symbol="AAPL",
    event_ts=_ts(10),
    decision_time=_ts(10),
    features=(FeatureValue("future_leak", 0.5, observed_at=_ts(11)),),  # ✗
)
# → raises LeakyFeatureError at build_manifest()
```

A **forward-join** row (rejected at construction):
```python
# decision_time after delisting → using data the as-of universe forbids
fwd = FeatureRow(
    symbol="OLDCO",
    event_ts=_ts(50),
    decision_time=_ts(50),   # ✗ after listed_until=_ts(40)
    features=(FeatureValue("ret_1d", 0.01, observed_at=_ts(50)),),
)
# → raises ValueError at FeatureLakeBuilder(...)
```

---

## 3. The Exported File Layout (what the trainer reads)

The RunPod trainer's `_load_dataset()` reads the file referenced by
`dataset_manifest_ref`. The file must be **CSV or Parquet**.

### 3.1 CSV Layout (required)

```
timestamp,f1,f2,f3,f4,label
0,0.123,-0.456,0.789,0.012,1
1,-0.234,0.567,-0.890,0.034,0
2,0.345,-0.678,0.901,-0.045,1
...
```

**Rules enforced by `_load_csv()`:**

| Position | Content | Notes |
|---|---|---|
| Column 0 | `timestamp` | int64, ns since epoch (or any sortable int) |
| Columns 1..k | features | float64, one per feature |
| Last column | `label` | float64, **binary** (0.0 or 1.0) |
| Row 0 | header | required (`skip_header=1`) |

**Hard constraints:**
- At least 3 columns (`timestamp`, ≥1 feature, `label`) — else `insufficient_features`.
- At least 10 rows for walk-forward validation — else `insufficient_data`.
- `label` must be binary (the LightGBM `objective` is `"binary"`).
- Rows are **re-sorted by timestamp** inside the trainer (stable sort),
  so the file does not need to be pre-sorted, but sorting is recommended.

**Concrete example** (from `test_real_trainer.py`):
```python
import numpy as np
rng = np.random.RandomState(42)
n = 200
timestamps = np.arange(n, dtype=np.int64)
f1, f2, f3, f4 = (rng.randn(n) for _ in range(4))
logit = 0.8*f1 + 0.5*f2 - 0.6*f3 + 0.05*rng.randn(n)
label = (logit > 0).astype(float)
data = np.column_stack([timestamps, f1, f2, f3, f4, label])
np.savetxt("features.csv", data, delimiter=",",
           header="timestamp,f1,f2,f3,f4,label", comments="")
```

### 3.2 Parquet Layout

```
┌──────────┬──────────┬──────────┬──────────┬──────────┬──────────┐
│ timestamp│ ret_1d   │ vol_20d  │ rsi_14   │ ...      │ label    │
│ (int64)  │ (double) │ (double) │ (double) │ (double) │ (double) │
├──────────┼──────────┼──────────┼──────────┼──────────┼──────────┤
│ ...      │ ...      │ ...      │ ...      │ ...      │ 0.0/1.0  │
└──────────┴──────────┴──────────┴──────────┴──────────┴──────────┘
```

**Rules enforced by `_load_parquet()`:**

| Rule | How it's resolved |
|---|---|
| Label column | Use `"label"` if present, else the **last** column |
| Timestamp column | Search for `timestamp`, then `decision_time`, then `ts`, then `event_ts`; else synthesize `arange(n)` |
| Feature columns | All columns except the label and timestamp columns |
| Loader | `pyarrow.parquet` preferred; falls back to `pandas.read_parquet`; else `missing_dependency` |

### 3.3 The `dataset_manifest_ref` URI Schemes

The trainer's `_resolve_path()` accepts three URI forms:

| Scheme | Example | Resolution |
|---|---|---|
| `file://` | `file:///data/features.csv` | Direct `Path` (Windows path fixup applied) |
| bare path | `/data/features.csv` or `data/features.csv` | Direct `Path` |
| `s3://` | `s3://my-bucket/datasets/features.parquet` | `StorageBackend.download_to_temp()` → temp `Path` |
| anything else | `http://...` | `unsupported_uri` error |

For E2E tests, the handler also accepts an `inline_dataset_csv` string
(not part of the schema) that is written to a temp file and overrides
`dataset_manifest_ref`.

---

## 4. The Manifest: `FeatureLakeManifest`

This is the **only** thing a training job references instead of DB
credentials. It's a frozen, hash-verifiable record of exactly what was
exported.

### 4.1 Fields

```python
class FeatureLakeManifest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int = 1
    dataset_id: str
    feature_schema_hash: str        # hash of the feature column schema
    label_schema_hash: str          # hash of the label schema
    as_of_ts: int                   # max decision_time in the export
    universe_hash: str              # SHA-256 over the as-of universe
    row_count: int
    checksum: str                   # SHA-256 over canonical row content
    folds: PurgedFoldSpec           # leakage-safe train/val splits
    pit_proof_verified: bool        # True only after PIT assertion passed
    source_vintage_refs: list[str]  # provenance (which raw sources)
```

### 4.2 The Manifest Hash

```python
manifest_hash = SHA256(canonical_json({
    schema_version, dataset_id, feature_schema_hash, label_schema_hash,
    as_of_ts, universe_hash, row_count, checksum, folds, pit_proof_verified,
    source_vintage_refs
}, sort_keys=True))
```

- **Deterministic**: identical inputs → identical hash.
- **Any field change alters the hash** — a single changed row changes
  `checksum`, which changes `manifest_hash`.
- Used as the training-reference identifier. **No DSN, password, or
  connection string is ever present.**

### 4.3 The Training Reference

```python
def training_reference(self) -> dict:
    return {
        "kind": "feature_lake_manifest_ref",
        "dataset_id": self.dataset_id,
        "manifest_hash": self.manifest_hash(),
    }
```

This is what gets embedded in a `RunPodTrainingRequest` (via
`TrainingManifest.to_dispatch_request()`) — the worker verifies what it
is training on without any DB access.

---

## 5. The Purged-k-Fold Structure

The manifest carries the leakage-safe fold boundaries so training and
the tournament use the **same** splits.

### 5.1 `FoldBoundary`

```python
class FoldBoundary(BaseModel):
    schema_version: int = 1
    fold_id: int
    train_start: int    # ns since epoch (inclusive)
    train_end: int      # ns (exclusive-ish; train is [start, end])
    val_start: int
    val_end: int
    purge_start: int    # gap between train and val
    purge_end: int
```

**Validated ordering** (enforced in `model_validator`):
```
  train_start <= train_end
  val_start   <= val_end
  purge_start >= train_end      (no train bleeding into purge)
  val_start   >= purge_end      (no purge bleeding into val)
  purge_end   >  purge_start    (non-empty purge window)
```

### 5.2 `PurgedFoldSpec`

```python
class PurgedFoldSpec(BaseModel):
    schema_version: int = 1
    folds: tuple[FoldBoundary, ...]
    embargo_ns: int              # >= max_label_horizon_ns
    max_label_horizon_ns: int
```

**Validated**: `embargo_ns >= max_label_horizon_ns` — so no training
row's label window overlaps a validation row's feature window.

### 5.3 Visual: One Fold

```
  time →
  ┌────────────────────┬─────────────┬──────────────────┐
  │      TRAIN         │   PURGE     │    VALIDATION    │
  │  train_start..end  │ purge..end  │  val_start..end  │
  └────────────────────┴─────────────┴──────────────────┘
                        ◄─ embargo ─►
                        (>= max label horizon)
```

### 5.4 Visual: Multiple Folds (expanding window)

```
  time →
  Fold 0:  [==TRAIN==][P][==VAL==]
  Fold 1:  [====TRAIN====][P][==VAL==]
  Fold 2:  [======TRAIN======][P][==VAL==]
  ...
  P = purge gap of length embargo_ns (>= max_label_horizon_ns)
```

The purge gap is what prevents **label overlap**: a training row near
the boundary has a label that extends `label_horizon_ns` into the
future; the purge ensures that future doesn't bleed into validation
features.

---

## 6. The Operator-Facing Staging Manifest: `TrainingManifest`

While `FeatureLakeManifest` describes the *data*, `TrainingManifest`
describes a *training job* — the operator-facing contract that packages
the dataset reference + model config + walk-forward windows + budget.

### 6.1 Fields

```python
class TrainingManifest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int = 1
    manifest_id: str
    feature_lake_manifest_ref: str     # dataset_id from FeatureLakeManifest
    feature_lake_manifest_hash: str    # 64-char hex SHA-256
    model_family: ModelFamily           # gbm | catboost | logreg | linear
    hyperparameters: dict[str, float]
    train_window_ns: int
    val_window_ns: int
    test_window_ns: int
    label_horizon_ns: int
    random_seed: int | None
    hardware_class: str | None
    walk_forward_enabled: bool = True
    budget_cents: int = 0
    timeout_seconds: int = 600
    operator_note: str = ""
    content_hash: str = ""             # auto-computed
```

### 6.2 Validation (fail-closed at the schema boundary)

| Check | Rule |
|---|---|
| `feature_lake_manifest_hash` | Must be 64-char hex SHA-256 |
| `model_family` | Must be in allowlist (`gbm`, `catboost`, `logreg`, `linear`) |
| `hyperparameters` | Keys must be defined for the chosen `model_family`; values within bounds |
| Windows | `train_window_ns`, `val_window_ns`, `test_window_ns`, `label_horizon_ns` all `> 0` |
| Budget | `budget_cents >= 0`, `timeout_seconds >= 0` |
| Secret rejection | Any field name containing `password`/`token`/`secret`/`api_key`/`credential`/`private_key`/`dsn`/`connection_string` is rejected |
| Name format | Feature/hyperparameter names must match `^[a-zA-Z][a-zA-Z0-9_.\-]{0,63}$` |

### 6.3 Hyperparameter Bounds (per model family)

| Family | Hyperparameter | Bounds |
|---|---|---|
| `gbm` | `n_estimators` | [10, 5000] |
| `gbm` | `max_depth` | [2, 12] |
| `gbm` | `learning_rate` | [1e-4, 1.0] |
| `gbm` | `min_child_samples` | [1, 200] |
| `catboost` | `iterations` | [10, 5000] |
| `catboost` | `depth` | [2, 10] |
| `catboost` | `learning_rate` | [1e-4, 1.0] |
| `logreg` | `C` | [1e-4, 100] |
| `logreg` | `max_iter` | [50, 5000] |
| `linear` | `alpha` | [1e-4, 10] |
| `linear` | `max_iter` | [100, 10000] |

### 6.4 The Walk-Forward Window (operator side)

`derive_walk_forward_window()` produces a single `(train, val, test)`
triple ending at `as_of_ts`:

```
  oldest                                          newest (as_of_ts)
  ┌──────────────┐gap┌──────────┐gap┌──────────────┐
  │    TRAIN     │   │   VAL    │   │     TEST     │
  │ train_window │   │val_window│   │ test_window  │
  └──────────────┘   └──────────┘   └──────────────┘
                   ◄label_horizon►◄label_horizon►
                   (embargo between windows)
```

The label horizon acts as an embargo between consecutive windows so a
training row's label does not overlap validation or test.

### 6.5 Translation to `RunPodTrainingRequest`

`to_dispatch_request()` flattens the manifest into the cross-boundary
dict the worker sees:

```python
{
    "schema_version": 1,
    "job_id": "<assigned>",
    "dataset_manifest_ref": "{dataset_id}:{manifest_hash[:16]}",
    "model_family": "gbm",
    "search_space": {},                    # operator pinned; no search for baseline
    "random_seed": 42,
    "hardware_class": "cpu",
    "extra_constraints": {
        "train_window_ns": "...",
        "val_window_ns": "...",
        "test_window_ns": "...",
        "label_horizon_ns": "...",
        "walk_forward_enabled": "1",
        "manifest_content_hash": "..."
    }
}
```

Note: the worker sees **only** what it needs. Budget, operator notes,
and walk-forward splits stay on the Fincept side.

---

## 7. Feature Availability

`FeatureAvailabilityReport` is emitted alongside the manifest so a
training job can refuse to train on a dataset where a required feature
is mostly missing, and so the tournament can apply a
feature-availability penalty.

### 7.1 The Report

```python
class FeatureAvailabilityReport(BaseModel):
    schema_version: int = 1
    total_rows: int
    expected_features: tuple[str, ...]
    per_feature: dict[str, int]    # feature → count of rows where present
```

### 7.2 Queries

| Method | Returns |
|---|---|
| `availability_pct(feature)` | `100 * per_feature[f] / total_rows` |
| `missing_features()` | features with 0% availability |

### 7.3 In the Tournament

Feature availability feeds the `feature_availability_penalty` component
(weight 0.05) of the tournament score — a model trained on mostly-missing
features is penalized.

---

## 8. Feature Snapshots (for inference, not training)

For shadow inference (TASK-0602), the feature lake is exported as
**compact float vectors** rather than full `FeatureRow` objects. This
minimizes network transfer to the RunPod inference worker.

### 8.1 `FeatureSnapshot`

```python
class FeatureSnapshot(BaseModel):
    symbols: list[str]
    features: dict[str, list[float]]   # symbol → compact float vector
    availability: dict[str, bool]      # symbol → degraded?
    ts_event: int                      # decision time
    freshness_ns: int                  # decision_time - max(observed_at)
```

### 8.2 Export Config

```python
class SnapshotExportConfig(BaseModel):
    min_availability_pct: float = 80.0      # below → symbol marked degraded
    max_freshness_ns: int = 60_000_000_000  # 60s; above → stale
```

A symbol with availability below the threshold is marked
`availability=False` — the inference worker **abstains** rather than
predicting on incomplete data.

---

## 9. Concrete End-to-End Example

Putting it all together — from raw rows to a training request:

```python
from quant_foundry.feature_lake import (
    FeatureLakeBuilder, FeatureRow, FeatureValue, UniverseEntry,
)
from quant_foundry.feature_availability import FeatureAvailabilityReport
from quant_foundry.dataset_manifest import FeatureLakeManifest

NS_PER_DAY = 86_400_000_000_000

# 1. Build the as-of universe (include delisted symbols!)
universe = (
    UniverseEntry("AAPL", listed_until=None),
    UniverseEntry("MSFT", listed_until=None),
    UniverseEntry("OLDCO", listed_until=40 * NS_PER_DAY),
)

# 2. Build PIT-correct rows (observed_at <= decision_time for every feature)
rows = (
    FeatureRow("AAPL", decision_time=10*NS_PER_DAY, event_ts=10*NS_PER_DAY,
               features=(FeatureValue("ret_1d", 0.01, observed_at=10*NS_PER_DAY),
                         FeatureValue("vol_20d", 0.20, observed_at=9*NS_PER_DAY))),
    FeatureRow("MSFT", decision_time=10*NS_PER_DAY, event_ts=10*NS_PER_DAY,
               features=(FeatureValue("ret_1d", -0.005, observed_at=10*NS_PER_DAY),
                         FeatureValue("vol_20d", 0.18, observed_at=9*NS_PER_DAY))),
    # ... more rows
)

# 3. Build the manifest (validates PIT proof + folds + embargo)
builder = FeatureLakeBuilder(
    dataset_id="ds-baseline-v1",
    universe=universe,
    rows=rows,
    feature_schema_hash="fsh-v1",
    label_schema_hash="lsh-binary-v1",
    max_label_horizon_ns=NS_PER_DAY,
    n_folds=3,
)
manifest = builder.build_manifest()
# → pit_proof_verified=True, folds.embargo_ns >= max_label_horizon_ns

# 4. Export the training file (CSV or Parquet) to a path/URI
#    The file layout must match §3 above.

# 5. Reference it in a training request (NO DB credentials)
request = {
    "schema_version": 1,
    "job_id": "qf:train:gbm:h1:1",
    "dataset_manifest_ref": "file:///data/ds-baseline-v1.csv",
    "model_family": "gbm",
    "search_space": {"n_estimators": [100], "num_leaves": [31], "learning_rate": [0.05]},
    "random_seed": 42,
    "hardware_class": "cpu",
    "extra_constraints": {},
}

# 6. Dispatch to RunPod. The trainer:
#    - loads the CSV → X, y, timestamps
#    - sorts by timestamps (point-in-time order)
#    - runs walk-forward LightGBM (expanding window)
#    - computes OOS metrics → ModelDossier
#    - signs the callback → returns to Fincept
```

---

## 10. File Map

```
services/quant_foundry/src/quant_foundry/
├── feature_lake.py              # FeatureRow, FeatureValue, UniverseEntry, FeatureLakeBuilder
├── feature_availability.py      # FeatureAvailabilityReport
├── feature_snapshot_export.py   # compact snapshots for inference
├── dataset_manifest.py          # FeatureLakeManifest, FoldBoundary, PurgedFoldSpec
├── training_manifest.py         # TrainingManifest, WalkForwardWindow (operator-facing)
├── market_data_adapter.py       # BarDataAdapter, PricePoint (raw price source)
├── real_trainer.py              # _load_csv / _load_parquet (the file readers)
└── schemas.py                   # DatasetManifest (minimal cross-boundary base)

libs/fincept-core/src/fincept_core/datasets/
└── cv.py                        # canonical Fold, make_folds, derive_walk_forward_window

experiments/news-impact-model/src/news_impact_model/
├── schema.py                    # NewsEvent, PricePoint, ImpactLabels (alt label scheme)
├── labels.py                    # abnormal-return labeling from prices
└── features.py                  # HashingTextEmbedder (text → feature vector)
```

---

## 11. TL;DR — The Data Rules

1. **Every feature value carries an `observed_at` time.** If
   `observed_at > decision_time`, the row is rejected as a look-ahead
   leak (`LeakyFeatureError`).
2. **The universe includes delisted/renamed symbols.** A row whose
   `decision_time` is after a symbol's `listed_until` is a forward join
   and is rejected at construction.
3. **The exported file is CSV or Parquet.** CSV layout:
   `timestamp, feature_1..feature_k, label` (header required, label
   binary). Parquet: looks for `label`/`timestamp`/`decision_time`/`ts`/
   `event_ts` columns.
4. **The manifest is the only training reference.** It carries a
   content hash, not DB credentials. `training_reference()` returns
   `{kind, dataset_id, manifest_hash}` — nothing else.
5. **Folds are purged-k-fold with embargo >= max label horizon.** No
   training row's label window overlaps a validation row's feature
   window.
6. **Minimum 10 rows, minimum 3 columns.** Below that the trainer
   returns `insufficient_data` / `insufficient_features` (safe terminal).
7. **The label is binary (0/1).** LightGBM runs with
   `objective="binary"`, `metric="binary_logloss"`.
8. **Determinism is enforced.** `seed`, `deterministic=True`,
   `num_threads=1` — same seed + same data + same hardware → same
   `artifact_id` / `sha256`.
9. **No secrets in manifests.** Field names containing `password`,
   `token`, `secret`, `api_key`, etc. are rejected at the schema
   boundary.
10. **Feature availability is measured and penalized.** A model trained
    on mostly-missing features loses tournament points
    (`feature_availability_penalty`, weight 0.05).
