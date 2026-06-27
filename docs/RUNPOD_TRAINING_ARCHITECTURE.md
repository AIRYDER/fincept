# RunPod Model Training Architecture

A visualization of how the Quant Foundry trains models on RunPod GPU
infrastructure — from the high-level dispatch loop down to the bytes
that become tournament "points".

This document covers two views:

1. **High-level** — the end-to-end loop, components, and security boundary.
2. **Low-level** — how raw dataset rows flow through LightGBM, walk-forward
   folds, metric computation, signature, and finally into the tournament
   score that decides promotion.

---

## 1. High-Level View

### 1.1 The End-to-End Loop

The training system is a **dispatch → train → callback → verify → score**
loop. Fincept owns the dispatch side; RunPod owns the training side. The
two halves only talk through a signed JSON envelope.

```
 ┌────────────────────────────────────────────────────────────────────────┐
 │                         FINCEPT (trusted side)                         │
 │                                                                        │
 │   QuantFoundryGateway                                                  │
 │   ┌──────────────────────────────────────────────────────────────┐    │
 │   │  JobOutbox  ──▶  RunPodDispatcher  ──▶  HttpRunPodClient     │    │
 │   │  (QUEUED)         (budget guard)        (POST /run)           │    │
 │   └──────────────────────────────────────────────────────────────┘    │
 │            │                                          ▲                │
 │            │ enqueue                                  │ callback       │
 │            ▼                                          │ (signed)       │
 │   ┌──────────────────────────────────────────────────────────────┐    │
 │   │  CallbackInbox  ──▶  CallbackProcessor  ──▶  DossierStore    │    │
 │   │  (RECEIVED)           verify_callback         (ModelDossier) │    │
 │   │                       (HMAC-SHA256)                          │    │
 │   └──────────────────────────────────────────────────────────────┘    │
 │            │                                                          │
 │            ▼                                                          │
 │   Tournament.score()  ──▶  Leaderboard  ──▶  PromotionGate           │
 │   (weighted points)        (ranked)          (human-gated)           │
 └────────────────────────────────────────────────────────────────────────┘
                              │          ▲
              dispatch request│          │ signed callback envelope
                              ▼          │
 ┌────────────────────────────────────────────────────────────────────────┐
 │                  RUNPOD (untrusted GPU container)                     │
 │                                                                        │
 │   handler.py  ──▶  RunPodTrainingHandler.handle()                     │
 │            │                                                           │
 │            ▼                                                           │
 │   LocalTrainer  OR  RealLightGBMTrainer                                │
 │   (deterministic stub)   (real LightGBM + walk-forward)                │
 │            │                                                           │
 │            ▼                                                           │
 │   ArtifactManifest + ModelDossier  ──▶  sign_callback()               │
 │                                       (HMAC-SHA256)                    │
 └────────────────────────────────────────────────────────────────────────┘
```

### 1.2 The Components

| Component               | Lives in         | Role                                                                                         |
| ----------------------- | ---------------- | -------------------------------------------------------------------------------------------- |
| `QuantFoundryGateway`   | Fincept          | Facade: outbox + inbox + dispatcher + processor                                              |
| `JobOutbox`             | Fincept          | Durable JSONL queue of training jobs (`QUEUED → DISPATCHED → VALIDATING → COMPLETED/FAILED`) |
| `RunPodDispatcher`      | Fincept          | The **only** component allowed to talk to RunPod. Budget-guarded.                            |
| `HttpRunPodClient`      | Fincept          | `POST /v2/{endpoint_id}/run` to RunPod serverless API                                        |
| `BudgetGuard`           | Fincept          | Per-job + monthly GPU spend ceiling. Fails closed.                                           |
| `handler.py`            | RunPod container | RunPod serverless entrypoint. Parses event → request.                                        |
| `RunPodTrainingHandler` | RunPod container | Deadline enforcement + trainer selection + signing                                           |
| `LocalTrainer`          | RunPod container | Deterministic CPU stub (no ML deps). Contract proof.                                         |
| `RealLightGBMTrainer`   | RunPod container | Real LightGBM with walk-forward validation                                                   |
| `CallbackInbox`         | Fincept          | Append-only JSONL of inbound callbacks                                                       |
| `CallbackProcessor`     | Fincept          | Verifies HMAC, validates schema, applies domain effect                                       |
| `DossierStore`          | Fincept          | Persists `ModelDossier` records                                                              |
| `Tournament`            | Fincept          | Scores dossiers into weighted "points"                                                       |
| `Leaderboard`           | Fincept          | Ranks tournament results                                                                     |
| `PromotionGate`         | Fincept          | Human-gated promotion from `SHADOW_ONLY` → `paper_approved`                                  |

### 1.3 The Security Boundary (non-negotiable)

The RunPod container is **untrusted**. It is a pure function over its
inputs. It has:

- ❌ No broker credentials (`ALPACA_API_KEY`, `FINCEPT_JWT_SECRET`)
- ❌ No Redis URL, no stream producer, no `sig.predict` writer
- ❌ No trading access — cannot emit orders or live signals
- ✅ Only reads a request, trains, returns a **signed** callback

The callback is HMAC-SHA256 signed with `QUANT_FOUNDRY_CALLBACK_SECRET`.
The Fincept-side `CallbackProcessor` verifies the signature **before**
any domain effect is applied. Fail-closed on bad signature.

```
   ┌─────────────┐   signed envelope    ┌──────────────┐
   │  RunPod     │ ───────────────────▶ │  Fincept     │
   │  (untrusted)│   HMAC-SHA256        │  verify first│
   │             │   + ts skew check    │  then trust  │
   └─────────────┘                      └──────────────┘
```

Every `ModelDossier` always carries `authority = SHADOW_ONLY`. Promotion
to live/paper is a separate, human-gated decision (`PromotionGate`).

---

## 2. The Request/Response Contract

### 2.1 Input: `RunPodTrainingRequest`

```json
{
  "schema_version": 1,
  "job_id": "qf:train:gbm:h1:1",
  "dataset_manifest_ref": "file:///data/features.parquet",
  "model_family": "gbm",
  "search_space": {
    "n_estimators": [100, 200],
    "num_leaves": [31],
    "learning_rate": [0.05]
  },
  "random_seed": 42,
  "hardware_class": "mock-gpu",
  "extra_constraints": {}
}
```

- `dataset_manifest_ref` — a `file://`, `s3://`, or bare path to the
  training data (CSV or Parquet). For E2E tests, an `inline_dataset_csv`
  field can be passed at the handler level (not part of the schema) and
  is written to a temp file.
- `search_space` — hyperparameter candidates. The trainer picks the
  first value of each key (single-sweep baseline).
- `random_seed` — pinned for reproducibility. Same seed + same data +
  same hardware → same `artifact_id` / `sha256`.

### 2.2 Output: signed `RunPodCallbackEnvelope`

```json
{
  "job_id": "qf:train:gbm:h1:1",
  "callback_payload": "<JSON-encoded RunPodCallbackEnvelope>",
  "callback_signature": "<hex HMAC-SHA256>",
  "callback_ts": 1719000000,
  "artifact_id": "artifact:abc123def4567890",
  "dossier_id": "model:qf:train:gbm:h1:1"
}
```

On failure, the handler returns a **safe terminal** dict (never a crash):

```json
{
  "job_id": "qf:train:gbm:h1:1",
  "error_code": "timeout",
  "error_summary": "training deadline breached (deadline_seconds=600)"
}
```

### 2.3 The HMAC Signature

```
signature = HMAC_SHA256(
    key   = QUANT_FOUNDRY_CALLBACK_SECRET,
    msg   = "{ts}.{job_id}.{payload_hash}"
)
```

- `payload_hash` = SHA256 of the envelope bytes (constant sig size)
- `ts` skew window = 300s (replay protection)
- `job_id` binding prevents cross-job replay
- Verified with `hmac.compare_digest` (constant-time)

---

## 3. Low-Level View: How Data Becomes Points

This is the path inside `RealLightGBMTrainer.train()` — the real ML
engine. The `LocalTrainer` is a deterministic stub that skips all of
this and synthesizes metrics from the seed; it exists only to prove the
contract without ML deps.

### 3.1 The Data Flow Inside the Container

```
 RunPodTrainingRequest
        │
        ▼
 ┌──────────────────────────────────────────────────────────────┐
 │ 1. DEADLINE CHECK                                            │
 │    start_ns + deadline_seconds*1e9 = deadline_ns             │
 │    if now >= deadline_ns  →  TrainingFailure("timeout")      │
 └──────────────────────────────────────────────────────────────┘
        │
        ▼
 ┌──────────────────────────────────────────────────────────────┐
 │ 2. DEPENDENCY CHECK                                          │
 │    importlib.find_spec("lightgbm")  /  ("numpy")             │
 │    missing  →  TrainingFailure("missing_dependency")         │
 └──────────────────────────────────────────────────────────────┘
        │
        ▼
 ┌──────────────────────────────────────────────────────────────┐
 │ 3. DATASET LOAD  (_load_dataset)                             │
 │    ref → Path (file://, s3:// via StorageBackend, or bare)   │
 │    .parquet  →  pyarrow/pandas                               │
 │    .csv      →  numpy.genfromtxt                             │
 │                                                              │
 │    Returns:  X (n_rows, n_features)                          │
 │              y (n_rows,)            binary label              │
 │              timestamps (n_rows,)   int64                    │
 │                                                              │
 │    CSV layout:  [timestamp, feature_1..feature_k, label]     │
 │    Parquet:     looks for "label" col, else last col         │
 │                 looks for timestamp/decision_time/ts/event_ts│
 └──────────────────────────────────────────────────────────────┘
        │
        ▼
 ┌──────────────────────────────────────────────────────────────┐
 │ 4. WALK-FORWARD VALIDATION  (_walk_forward_validate)         │
 │    (see §3.2 below — this is where metrics come from)        │
 └──────────────────────────────────────────────────────────────┘
        │
        ▼
 ┌──────────────────────────────────────────────────────────────┐
 │ 5. FINAL MODEL TRAIN  (_train_final_model)                   │
 │    lgb.train(params, Dataset(X_all, y_all), n_estimators)    │
 │    → final_model                                             │
 └──────────────────────────────────────────────────────────────┘
        │
        ▼
 ┌──────────────────────────────────────────────────────────────┐
 │ 6. ARTIFACT HASH                                             │
 │    model_bytes = pickle.dumps(final_model)                   │
 │    sha256 = SHA256(model_bytes)        ← REAL hash, not stub │
 │    artifact_id = "artifact:" + sha256[:16]                   │
 └──────────────────────────────────────────────────────────────┘
        │
        ▼
 ┌──────────────────────────────────────────────────────────────┐
 │ 7. BUILD ArtifactManifest + ModelDossier                     │
 │    (see §3.4 — reproducibility pins)                         │
 └──────────────────────────────────────────────────────────────┘
        │
        ▼
 ┌──────────────────────────────────────────────────────────────┐
 │ 8. SIGN + RETURN                                             │
 │    RunPodCallbackEnvelope(payload={dossier, artifact})       │
 │    sign_callback(envelope_bytes, secret, ts, job_id)         │
 └──────────────────────────────────────────────────────────────┘
```

### 3.2 Walk-Forward Validation (the metric engine)

This is the heart of "how the architecture uses data to make points."
The trainer never evaluates on the data it trained on for the final
metrics — it uses an **expanding-window walk-forward** scheme.

```
  time →
  ┌──────────────────────────────────────────────────────────────┐
  │  Fold 0:  [====train====][==val==]                            │
  │  Fold 1:  [======train======][==val==]                        │
  │  Fold 2:  [==========train==========][==val==]                │
  │                                                                │
  │  min_train = max(10, n // (n_folds + 2))                       │
  │  fold_size = max(5, (n - min_train) // n_folds)                │
  └──────────────────────────────────────────────────────────────┘
```

For each fold:

1. Sort rows by `timestamps` (stable) — **point-in-time correctness**.
2. `X_train = X_s[:train_end]`, `y_train = y_s[:train_end]`
3. Skip fold if `y_train` has only one class (degenerate).
4. `lgb.train(params, Dataset(X_train, y_train), n_estimators)`
5. Predict on train (`train_acc`) and on val (`val_acc`).
6. Collect **out-of-sample** val predictions into `all_preds` / `all_labels`.

The out-of-sample predictions from all folds are concatenated — this is
the unbiased signal that feeds every metric below.

### 3.3 The LightGBM Parameters

Built by `_build_lgb_params()` from the request `search_space` + safe
defaults. **Determinism is enforced** so the same seed + data reproduces
the same model byte-for-byte:

```python
{
    "objective": "binary",
    "metric": "binary_logloss",
    "verbosity": -1,
    "seed": seed,
    "deterministic": True,      # ← reproducibility
    "num_threads": 1,           # ← reproducibility (no thread nondeterminism)
    "num_leaves": 31,
    "learning_rate": 0.05,
    "feature_fraction": 0.9,
    "bagging_fraction": 0.8,
    "bagging_freq": 5,
    "min_data_in_leaf": 5,
    "force_col_wise": True,
}
```

`search_space` overrides: `num_leaves`, `learning_rate`, `max_depth`,
`min_data_in_leaf`, `n_estimators`.

### 3.4 From Predictions to Metrics (`_compute_metrics`)

The concatenated out-of-sample `all_preds` (probabilities) and
`all_labels` (0/1) become every number in the dossier:

```
  all_preds (P(label=1)), all_labels (0 or 1)
        │
        ├──▶ accuracy       = mean( (pred > 0.5) == label )
        ├──▶ logloss        = -mean( label*log(p) + (1-label)*log(1-p) )
        ├──▶ brier_score    = mean( (pred - label)^2 )        ← calibration
        ├──▶ calibration    = 10 buckets: mean(pred) vs mean(label)
        │
        ├──▶ positions      = 2*pred - 1                      ← long/short
        ├──▶ returns        = positions * (2*label - 1)
        ├──▶ win_rate       = mean( returns > 0 )
        ├──▶ sharpe         = mean(returns)/std(returns) * sqrt(252)
        ├──▶ max_drawdown   = min(cumsum - running_max)
        │
        ├──▶ pbo            = count(val_acc < train_acc) / n_folds
        │                       ← Probability of Backtest Overfitting
        └──▶ deflated_sharpe = sharpe * (1 - pbo)
```

These metrics are split across two destinations in the `ModelDossier`:

| Field                     | Contents                                                                                                         |
| ------------------------- | ---------------------------------------------------------------------------------------------------------------- |
| `training_metrics` (dict) | `accuracy`, `logloss`, `brier_score`, `sharpe_ratio`, `max_drawdown`, `win_rate`                                 |
| `pbo` (float 0..1)        | Probability of Backtest Overfitting                                                                              |
| `deflated_sharpe` (float) | Sharpe penalized by PBO                                                                                          |
| `metadata` (dict)         | `trainer`, `n_features`, `n_rows`, `n_folds`, plus raw `brier_score`, `win_rate`, `max_drawdown`, `sharpe_ratio` |

### 3.5 Reproducibility Pins (`ArtifactManifest`)

The artifact is hash-verifiable. Re-running the same request on the same
hardware class **must** produce the same `artifact_id` / `sha256`.

| Pin                      | Source                                                                        |
| ------------------------ | ----------------------------------------------------------------------------- |
| `sha256`                 | SHA256 of pickled model bytes (real trainer) or canonical request JSON (stub) |
| `feature_schema_hash`    | `SHA256(dataset_ref:n_features=N)[:16]`                                       |
| `label_schema_hash`      | `SHA256(dataset_ref:label=binary)[:16]`                                       |
| `code_git_sha`           | Pinned at container build time                                                |
| `lockfile_hash`          | Pinned at container build time                                                |
| `container_image_digest` | Set at build time                                                             |
| `random_seed`            | From the request                                                              |
| `hardware_class`         | From the request                                                              |

Any known nondeterminism source is **recorded, not hidden**.

---

## 4. Back on the Fincept Side: Verification → Points → Promotion

### 4.1 Callback Ingestion (`CallbackProcessor.process`)

Strict fail-closed pipeline. Each check that fails marks the inbox
record `REJECTED` and the outbox job `FAILED` — no domain effect applied.

```
  inbox.get_by_job_id(job_id)
        │
        ▼
  already terminal?  ──yes──▶  skip (idempotent)
        │ no
        ▼
  already PROCESSED? ──yes──▶  skip (idempotent)
        │ no
        ▼
  signature_valid?   ──no───▶  REJECTED "bad_signature"
        │ yes
        ▼
  payload hash matches? ──no──▶  REJECTED "payload_tamper"
        │ yes
        ▼
  RunPodCallbackEnvelope validates? ──no──▶  REJECTED "invalid_schema"
        │ yes
        ▼
  envelope.job_id == job_id? ──no──▶  REJECTED "job_id_mismatch"
        │ yes
        ▼
  result_type == "training_complete"
        │
        ▼
  dossier_store.store(envelope.payload)   ← domain effect
        │
        ▼
  outbox → COMPLETED, inbox → PROCESSED
```

### 4.2 Tournament Scoring (data → points)

`Tournament.score(ScoringInput)` turns a model's settled out-of-sample
returns + dossier metrics into a single `total_score` with a
decomposition. This is the literal "points" system.

**Default weights** (positive components sum to 1.0; penalties subtract):

| Component                      | Weight | Direction                       |
| ------------------------------ | ------ | ------------------------------- |
| `net_edge`                     | 0.40   | + (mean net-of-cost OOS return) |
| `deflated_sharpe`              | 0.35   | + (squashed DSR)                |
| `calibration`                  | 0.25   | + (1 − Brier)                   |
| `drawdown_penalty`             | 0.10   | −                               |
| `turnover_penalty`             | 0.05   | −                               |
| `feature_availability_penalty` | 0.05   | −                               |
| `latency_penalty`              | 0.05   | −                               |
| `capacity_decay_penalty`       | 0.05   | −                               |

```
  total_score = Σ(positive contributions) − Σ(penalty contributions)
```

**Gates** (applied after scoring — a failed gate blocks promotion
regardless of score):

1. **Insufficient evidence** — `settled_count < min_settled_samples`
   → status `INSUFFICIENT_EVIDENCE`, score 0.
2. **Stale evidence** — last settled prediction too old → `STALE`.
3. **Significance** — bootstrap p-value vs zero-skill baseline must be
   `< p_value_threshold` (default 0.05); DSR must be `> dsr_threshold`.
4. **Net edge ≤ 0** — blocking issue (doesn't beat zero-skill net-of-cost).

**Recommendation**: `PROMOTE` / `HOLD` / `REJECT` based on score + gates.

### 4.3 Leaderboard → Promotion Gate

```
  TournamentResult[]  ──▶  Leaderboard.ranked()
  (sorted: ELIGIBLE > BLOCKED > STALE > INSUFFICIENT_EVIDENCE,
   then total_score descending)
        │
        ▼
  PromotionReviewQueue.submit(request, evidence)
        │
        ▼
  PromotionGate.evaluate()   ← human-gated, fail-closed
        │
        ▼
  PromotionReceipt { APPROVED | REJECTED }
```

Promotion from `SHADOW_ONLY` → `paper_approved` is the maximum the gate
allows automatically. Anything beyond requires a human waiver.

---

## 5. Two Trainers, One Contract

The system ships two interchangeable trainers behind the same
`TrainerProtocol`. Flipping between them is a single env var:

```
QUANT_FOUNDRY_USE_REAL_TRAINER=true   →  RealLightGBMTrainer
QUANT_FOUNDRY_USE_REAL_TRAINER=false  →  LocalTrainer  (default)
```

|                   | `LocalTrainer`                   | `RealLightGBMTrainer`                       |
| ----------------- | -------------------------------- | ------------------------------------------- |
| ML deps           | None                             | `lightgbm`, `numpy`                         |
| Model             | Stub (no model)                  | Real LightGBM booster                       |
| `sha256`          | SHA256 of canonical request JSON | SHA256 of pickled model bytes               |
| Metrics           | Synthesized from seed            | Real OOS walk-forward metrics               |
| Use case          | Contract proofs, CI without GPU  | Production training on RunPod               |
| Deterministic     | Yes (pure function of request)   | Yes (`deterministic=True`, `num_threads=1`) |
| Deadline enforced | Yes                              | Yes (checked at 4 points)                   |
| Authority         | `SHADOW_ONLY`                    | `SHADOW_ONLY`                               |

Both produce the **same** `RunPodCallbackEnvelope` shape and signature,
so flipping from mock to RunPod is a dispatcher-only change.

---

## 6. Failure Modes (all safe-terminal, never a crash)

| `error_code`               | Triggered when                                                                          |
| -------------------------- | --------------------------------------------------------------------------------------- |
| `bad_request`              | `event["input"]` is not a dict                                                          |
| `schema_validation_failed` | Input doesn't match `RunPodTrainingRequest`                                             |
| `timeout`                  | Deadline breached (checked before work, after dataset load, after validation, per fold) |
| `missing_dependency`       | `lightgbm` or `numpy` not installed                                                     |
| `dataset_not_found`        | Resolved path doesn't exist                                                             |
| `unsupported_uri`          | URI scheme not `file`/`s3`/bare, or no storage backend for `s3`                         |
| `unsupported_format`       | Not `.parquet` or `.csv`                                                                |
| `insufficient_features`    | CSV has < 3 columns                                                                     |
| `insufficient_data`        | `< 10` rows for walk-forward                                                            |
| `no_validation_data`       | No fold produced predictions (single-class or too small)                                |
| `training_error`           | Injected failure (`should_fail=True`) for tests                                         |

Every failure returns `{error_code, error_summary, job_id}` — the
dispatcher records it in the outbox `FAILED` transition. No raw
exception ever escapes the container.

---

## 7. Environment Variables

| Variable                                  | Required   | Default                                  | Purpose                                |
| ----------------------------------------- | ---------- | ---------------------------------------- | -------------------------------------- |
| `QUANT_FOUNDRY_CALLBACK_SECRET`           | yes (prod) | `dev-callback-secret-DO-NOT-USE-IN-PROD` | HMAC secret for signing callbacks      |
| `QUANT_FOUNDRY_TRAINING_DEADLINE_SECONDS` | no         | `600`                                    | Max wall-clock seconds for training    |
| `QUANT_FOUNDRY_USE_REAL_TRAINER`          | no         | `false`                                  | `true` = real LightGBM; `false` = stub |
| `QUANT_FOUNDRY_MODE`                      | no         | `local_mock`                             | `runpod` enables real dispatch         |
| `QUANT_FOUNDRY_ENABLED`                   | no         | `false`                                  | Master switch for the gateway          |
| `QUANT_FOUNDRY_SHADOW_ONLY`               | no         | `true`                                   | Force `authority=SHADOW_ONLY`          |

---

## 8. File Map

```
runpod/quant-foundry-training/
├── Dockerfile              # Container image (pins git sha, lockfile hash)
├── README.md               # Operator-facing docs
└── handler.py              # RunPod serverless entrypoint

services/quant_foundry/src/quant_foundry/
├── schemas.py              # RunPodTrainingRequest, ModelDossier, etc. (Pydantic, frozen)
├── signatures.py           # sign_callback / verify_callback (HMAC-SHA256)
├── runpod_training.py      # RunPodTrainingHandler + LocalTrainer + TrainerProtocol
├── real_trainer.py         # RealLightGBMTrainer (LightGBM + walk-forward)
├── runpod_client.py        # HttpRunPodClient + RunPodDispatcher + BudgetGuard
├── inbox.py                # CallbackInbox (durable JSONL)
├── callbacks.py            # CallbackProcessor (verify → domain effect)
├── gateway.py              # QuantFoundryGateway (facade + receive_callback)
├── dossier.py              # DossierStore
├── tournament.py           # Tournament.score() — the points system
├── leaderboard.py          # Leaderboard.ranked()
├── promotion.py            # PromotionGate + PromotionReviewQueue
└── outbox.py               # JobOutbox (durable JSONL queue)
```

---

## 9. TL;DR

1. Fincept enqueues a `RunPodTrainingRequest` in the `JobOutbox`.
2. `RunPodDispatcher` POSTs it to RunPod (budget-guarded).
3. The RunPod container loads the dataset, runs **walk-forward LightGBM**,
   produces real out-of-sample metrics (accuracy, logloss, Brier, Sharpe,
   drawdown, PBO, deflated Sharpe).
4. It pickles the model, hashes the bytes, builds an `ArtifactManifest`
   + `ModelDossier` (always `SHADOW_ONLY`), and **HMAC-signs** the
     callback envelope.
5. Fincept verifies the signature, validates the schema, stores the
   dossier.
6. The `Tournament` turns the dossier's OOS returns + metrics into a
   weighted `total_score` (the "points"), gated by significance and
   staleness.
7. The `Leaderboard` ranks models; the `PromotionGate` decides
   human-gated promotion. Nothing trained on RunPod ever touches a
   trading stream directly.
