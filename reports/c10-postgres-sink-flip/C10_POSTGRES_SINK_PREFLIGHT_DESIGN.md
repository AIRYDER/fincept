# C10 Postgres Sink Flip Preflight Design

## Main SHA

`fc99a6cc5e0ac3b9c7ab59fcc5e8710985eb24d8` (main, post-C6 merge)

## Date / Operator

2026-07-10 / Devin (Task 16)

## Preconditions

| Check | Result |
|-------|--------|
| Task 15 returned C6_MERGED_AND_GREEN | yes |
| On `main` branch | yes |
| Working tree clean (except untracked C8 reports) | yes |
| HEAD == origin/main | yes (`fc99a6cc`) |
| `ruff format --check .` | 831 files OK |
| `ruff check libs services` | all passed |
| `mypy libs services` | 369 files, no issues |
| `pytest services/quant_foundry` | 4367 passed, 230 skipped |
| `pytest services/api/tests` | 494 passed |
| `pytest services/settlements/tests` | 56 passed |

All preconditions met.

## Current Persistence Inventory

### Summary by backend

| Backend | Record types | Count |
|---------|-------------|-------|
| Postgres (already canonical) | 15 | 15 |
| JSONL (has DB equivalent, not yet flipped) | 7 | 7 |
| JSONL (no DB equivalent — needs new table) | 1 | 1 |
| JSONL (operational/audit, acceptable as-is) | 4 | 4 |
| File/artifact (appropriate) | 2 | 2 |
| SQLite (test only) | 1 | 1 |

### Detailed inventory

#### Already Postgres-canonical (no C10 work needed)

| # | Record type | Table | Writer | Reader | Promotion | Tournament | API | Worker | Risk |
|---|------------|-------|--------|--------|-----------|------------|-----|--------|------|
| 1 | Artifact manifests | `artifact_manifests` | `DbDossierStore.store()` | ORM queries | yes | no | yes | yes | low |
| 2 | Model dossiers | `model_dossiers` | `DbDossierStore.store()` | `ModelRegistryDB._assemble_evidence()` | yes | yes | yes | no | low |
| 3 | Callback receipts | `callback_receipts` | `CallbackReceiptDbStore.write()` | `CallbackReceiptDbStore.get_by_job_id()` | yes | no | yes | yes | low |
| 4 | Callback DLQ | `callback_dlq` | `CallbackDlqDbStore.write()` | `CallbackDlqDbStore.count()` | no | no | no | yes | low |
| 5 | Callback metrics | `callback_metrics` | `CallbackMetricsDbStore.record()` | `CallbackMetricsDbStore.rejection_rate()` | no | no | yes | yes | low |
| 6 | Shadow predictions | `shadow_predictions` | `DbShadowLedgerStore.store()` | ORM queries | yes | yes | no | yes | low |
| 7 | Models (identity) | `models` | `ModelRegistryDB.register_model()` | `ModelRegistryDB.get_model()` | yes | no | yes | no | low |
| 8 | Model versions | `model_versions` | `ModelRegistryDB.register_version()` | `ModelRegistryDB.get_version()` | yes | no | yes | no | low |
| 9 | Model metrics (training/tournament/sentinel/settlement/selfcheck/pit_evidence/feature_set/backend) | `model_metrics` | `ModelRegistryDB.record_metrics()` | `ModelRegistryDB._assemble_evidence()` | yes | yes | yes | yes | low |
| 10 | Promotions | `promotions` | `ModelRegistryDB.promote()` | `ModelRegistryDB.get_promotion_history()` | yes | no | yes | no | low |
| 11 | Promotion decisions | `promotion_decisions` | `ModelRegistryDB.promote()` | `ModelRegistryDB._assemble_evidence()` | yes | no | yes | no | low |
| 12 | Shadow evaluations | `shadow_evaluations` | `ModelRegistryDB.record_shadow_evaluation()` | ORM queries | yes | yes | yes | no | low |
| 13 | Dataset manifests | `dataset_manifests` | Dataset registry DB ops | ORM queries | no | no | yes | yes | low |
| 14 | Training jobs | `training_jobs` | `CostTracker.record_job_dispatch()` | ORM queries | no | no | yes | yes | low |
| 15 | Job cost events / metrics / cost summary | `job_cost_events`, `job_metrics`, `cost_summary` | `CostTracker` | ORM queries | no | no | yes | yes | low |

#### JSONL with DB equivalent — needs read-flip only

| # | Record type | JSONL location | DB table | DB writer exists | DB reader exists | Flip priority |
|---|------------|---------------|----------|-----------------|-----------------|---------------|
| 16 | Dossier registry | `dossier_registry.jsonl` | `model_dossiers` | yes (`DbDossierStore`) | yes (`ModelRegistryDB`) | high |
| 17 | Callback inbox | `inbox.jsonl` | `callback_receipts` | yes (`CallbackReceiptDbStore`) | yes (`get_by_job_id`) | high |
| 18 | Shadow ledger | `shadow_predictions.jsonl` | `shadow_predictions` | yes (`DbShadowLedgerStore`) | partial (list only) | high |
| 19 | Callback DLQ (JSONL) | `callback_dlq.jsonl` | `callback_dlq` | yes (`CallbackDlqDbStore`) | yes (`count`) | medium |
| 20 | Callback metrics (JSONL) | `callback_metrics.jsonl` | `callback_metrics` | yes (`CallbackMetricsDbStore`) | yes (`rejection_rate`) | medium |
| 21 | Dataset registry ledger | `dataset_registry.jsonl` | `dataset_manifests` | partial | partial | medium |
| 22 | Job outbox | `outbox.jsonl` | (none) | no | no | medium (see below) |

#### JSONL without DB equivalent — needs new table

| # | Record type | JSONL location | Writer | Reader | Used by | Flip priority |
|---|------------|---------------|--------|--------|---------|---------------|
| 23 | **SettlementLedger** | `<root>/<model_id>.settlements.jsonl` | `SettlementLedger._append()` | `SettlementLedger.read_all()` | promotion, tournament, API | **high** |

#### JSONL operational/audit — acceptable as-is

| # | Record type | JSONL location | Used by | Priority |
|---|------------|---------------|---------|----------|
| 24 | TrainingJobLedger | `job_ledger.jsonl` | API, worker | low (audit-only) |
| 25 | BudgetGuard | `spend_<YYYY-MM>.jsonl` | worker | low (simple counter) |
| 26 | Receipt bundles | `reports/runpod-training/<job-id>/` | promotion, worker | low (artifact) |

#### File/artifact-backed — appropriate

| # | Record type | Location | Used by | Priority |
|---|------------|----------|---------|----------|
| 27 | Bundle manifests | Inside model bundle zip | promotion, worker | low (artifact bytes) |
| 28 | Receipt bundles | `reports/runpod-training/<job-id>/` | promotion, worker | low (evidence dirs) |

## Record Classification

| Record type | Classification | Rationale |
|------------|---------------|-----------|
| Settlement records | **POSTGRES_CANONICAL** | Critical for promotion, tournament, API. Currently JSONL-only — the one new table C10 needs. |
| Promotion decisions | **POSTGRES_CANONICAL** | Already in `promotion_decisions` table. No change. |
| Dossiers | **POSTGRES_CANONICAL** | Already in `model_dossiers` table. Read-flip needed. |
| Callback receipts | **POSTGRES_CANONICAL** | Already in `callback_receipts` table. Read-flip needed. |
| Artifact manifests | **POSTGRES_CANONICAL** (metadata) | Already in `artifact_manifests` table. Artifact bytes remain in artifact store. |
| Bundle metadata | **ARTIFACT_STORE_CANONICAL** | Bundle bytes + manifest remain in zip. `bundle_sha256` verified at load time. |
| Selfcheck evidence | **POSTGRES_CANONICAL** | Already in `model_metrics` with `metric_type='selfcheck'`. No change. |
| PIT evidence refs | **POSTGRES_CANONICAL** | Already in `model_metrics` with `metric_type='pit_evidence'`. No change. |
| Feature set version | **POSTGRES_CANONICAL** | Already in `model_metrics` with `metric_type='feature_set'`. No change. |
| Tournament results | **POSTGRES_CANONICAL** | Already in `model_metrics` with `metric_type='tournament'`. No change. |
| Shadow predictions | **POSTGRES_CANONICAL** | Already in `shadow_predictions` table. Read-flip needed. |
| Shadow evaluations | **POSTGRES_CANONICAL** | Already in `shadow_evaluations` table. No change. |
| Dataset manifests | **POSTGRES_CANONICAL** | Already in `dataset_manifests` table. No change. |
| Job outbox | **FILE_CACHE_ONLY** | Operational dispatch queue. JSONL acceptable for now. Low query volume. |
| Job ledger | **FILE_CACHE_ONLY** | Audit-only append log. JSONL acceptable. |
| Budget guard | **FILE_CACHE_ONLY** | Simple monthly spend counter. JSONL acceptable. |
| Callback metrics (JSONL) | **LEGACY_COMPAT** | DB sink exists. JSONL can remain as fallback. |
| Raw reports | **REPORT_ONLY** | Report files under `reports/`. Not queried by services. |

## Existing Schema Review

### Database connection pattern

Dual-engine setup in `libs/fincept-db/src/fincept_db/engine.py`:
- **Async engine**: `postgresql+asyncpg://`, pool_size=20, for API paths
- **Sync engine**: `postgresql+psycopg://` (converted from asyncpg URL), pool_size=20, for callback processor and registry DB
- **Test mode**: `FINCEPT_DB_TEST_NULLPOOL=1` enables NullPool for both

### Existing tables (25 total, migrations 0001-0007)

| Migration | Tables |
|-----------|--------|
| 0001_initial | trades, bars, book_deltas, audit_log, strategies, universe |
| 0002_features | features |
| 0003_provider_data | provider_data |
| 0004_callback_ingestion | artifact_manifests, model_dossiers, callback_receipts, callback_dlq, callback_metrics, shadow_predictions |
| 0004b_observability | training_jobs, job_cost_events, job_metrics, cost_summary |
| 0005_model_registry | models, model_versions, model_metrics, promotions, promotion_decisions, shadow_evaluations |
| 0006_dataset_manifests | dataset_manifests |
| 0007_promotion_gate_hardening | (CHECK constraint updates only) |

### Conventions

- **Timestamps**: BigInteger nanosecond (`*_ns` suffix), no TIMESTAMPTZ
- **IDs**: String(128), no UUID
- **Enums**: CHECK constraints, no PostgreSQL ENUM types
- **Structured data**: JSONB (generic `JSON` in ORM for cross-dialect test compatibility)
- **Idempotency**: `INSERT ... ON CONFLICT (...) DO NOTHING` on all sinks
- **Security**: No secrets/signatures/raw payloads stored. `payload_ref` is a file path, `payload_hash` is SHA-256.
- **Foreign keys**: Comprehensive FK graph from `model_versions` → `models`, `model_dossiers`, `artifact_manifests`, `callback_receipts`
- **TimescaleDB**: 5 hypertables (trades, bars, book_deltas, features, provider_data) — all market data, not registry data

### What exists for C10

- `model_metrics` table already has `metric_type='settlement'` in its CHECK domain — settlement **metrics** can be stored today
- `shadow_evaluations` table stores aggregated shadow eval results with `tournament_result_id` field
- `ModelRegistryDB._assemble_evidence()` already reads from all evidence tables
- DB sinks (`db_sinks.py`) already exist for dossiers, shadow predictions, callback receipts, DLQ, metrics
- `ModelRegistryDB` already handles promotion workflow with DB-backed evidence assembly

### What's missing for C10

1. **`settlement_records` table** — the canonical settlement record (one row per settled prediction) does not exist in Postgres. This is the primary new table C10 needs.
2. **Settlement DB sink** — a `DbSettlementStore` class that writes settlement records to Postgres, mirroring `SettlementLedger._append()`.
3. **Settlement DB reader** — methods to read settlement records from Postgres, mirroring `SettlementLedger.read_all()` and `read_for_model()`.
4. **Read-flip for existing DB sinks** — the JSONL readers are still canonical for dossier registry, callback inbox, shadow ledger. The DB sinks write but reads haven't been flipped.
5. **Feature flags** — no `QF_POSTGRES_SINK_ENABLED` or `QF_POSTGRES_READS_ENABLED` flags exist yet.

## Target Schema Design

### New table: `settlement_records` (migration 0008)

One row per settled prediction. Mirrors the fields in `quant_foundry.outcomes.SettlementRecord`.

```sql
CREATE TABLE settlement_records (
    schema_version        INTEGER     NOT NULL DEFAULT 1,
    settlement_id         String(128) NOT NULL,  -- PK: f"{prediction_id}:{cost_model_version}"
    prediction_id         String(128) NOT NULL,
    model_id              String(128) NOT NULL,
    symbol                String(32)  NOT NULL,
    ts_event              BIGINT      NOT NULL,   -- prediction event time (ns)
    horizon_ns            BIGINT      NOT NULL,   -- prediction horizon
    cost_model_version    String(16)  NOT NULL,   -- 'cm-v1'
    status                String(32)  NOT NULL,   -- 'pending_time','pending_data','settled','failed'
    direction             NUMERIC(28,12) NULL,    -- from prediction
    confidence            NUMERIC(28,12) NULL,    -- from prediction
    p_up                  NUMERIC(28,12) NULL,    -- probability of up move
    realized_return_gross NUMERIC(18,12) NULL,    -- gross return (before costs)
    realized_return_net   NUMERIC(18,12) NULL,    -- net return (after costs)
    abnormal_return       NUMERIC(18,12) NULL,    -- return minus benchmark
    brier_score           NUMERIC(18,12) NULL,    -- Brier score using p_up
    calibration_bucket    String(16)  NULL,       -- '0.0-0.2', '0.2-0.4', etc.
    entry_price           NUMERIC(28,12) NULL,
    exit_price            NUMERIC(28,12) NULL,
    benchmark_entry_price NUMERIC(28,12) NULL,
    benchmark_exit_price  NUMERIC(28,12) NULL,
    borrow_cost           NUMERIC(18,12) NULL,    -- short borrow cost
    holding_days          INTEGER     NOT NULL DEFAULT 1,
    settled_at_ns         BIGINT      NULL,       -- NULL when pending
    created_at_ns         BIGINT      NOT NULL,
    -- PK
    PRIMARY KEY (settlement_id),
    -- Unique constraint: one record per (prediction_id, cost_model_version)
    -- (settlement_id already encodes this, but add for query clarity)
    UNIQUE (prediction_id, cost_model_version),
    -- Check constraints
    CHECK (status IN ('pending_time','pending_data','settled','failed')),
    CHECK (cost_model_version IN ('cm-v1','v1.default')),
    CHECK (holding_days >= 1),
    CHECK (calibration_bucket IS NULL OR calibration_bucket IN
           ('0.0-0.2','0.2-0.4','0.4-0.6','0.6-0.8','0.8-1.0')),
    -- Foreign keys (optional — settlement records may exist before model is registered)
    -- FK to models.model_id is soft: settlement can arrive before model registration
);
```

**Indexes:**
- `ix_settlement_records_model_id_ts` (model_id, ts_event) — primary query pattern
- `ix_settlement_records_symbol_ts` (symbol, ts_event) — cross-model queries
- `ix_settlement_records_status` (status) — filter pending vs settled
- `ix_settlement_records_prediction_id` (prediction_id) — idempotency lookup
- `ix_settlement_records_cost_model_version` (cost_model_version) — filter by cost model

**Downgrade:** `DROP TABLE settlement_records`

### No other new tables needed

All other record types already have Postgres tables. C10 is primarily a **read-flip** for those, not a schema addition.

### ORM model: `SettlementRecordRow`

New file: `libs/fincept-db/src/fincept_db/settlement_tables.py`

Follows the same pattern as `callback_tables.py` and `registry_tables.py`:
- SQLAlchemy 2.0 declarative style (`Mapped`, `mapped_column`)
- Registered on shared `Base` from `fincept_db.models`
- Generic `JSON` type for cross-dialect test compatibility
- BigInteger for nanosecond timestamps
- String IDs
- CHECK constraints for enum-like columns

## Migration Plan

### Phase 1: Add schema only (migration 0008)

**Scope:** Create `settlement_records` table. No code changes.

```text
migration 0008_settlement_records.py
  - CREATE TABLE settlement_records
  - CREATE INDEXES
  - down_revision = '0007'
```

**Verification:** Alembic upgrade/downgrade/re-upgrade CI job passes.

**Risk:** None — additive only, no existing table modified.

### Phase 2: Add Postgres writer behind feature flag

**Scope:** Add `DbSettlementStore` class in `services/quant_foundry/src/quant_foundry/db_sinks.py` (or new `settlement_db_sink.py`). Writes settlement records to `settlement_records` table. Uses `INSERT ... ON CONFLICT (settlement_id) DO NOTHING` for idempotency.

**Feature flag:** `QF_POSTGRES_SINK_ENABLED=1` enables the DB writer. Default: `0` (off).

**Code changes:**
- `libs/fincept-db/src/fincept_db/settlement_tables.py` — ORM model
- `services/quant_foundry/src/quant_foundry/settlement_db_sink.py` — `DbSettlementStore`
- `services/settlements/src/settlements/compat.py` — add DB writer path behind flag

**Verification:** Unit tests for `DbSettlementStore` against SQLite (test) and Postgres (CI).

### Phase 3: Dual-write Postgres + legacy JSONL

**Scope:** When `QF_POSTGRES_SINK_ENABLED=1`, write to both Postgres and JSONL. JSONL remains the read path.

**Feature flag:** `QF_DUAL_WRITE_SETTLEMENTS=1` (implied by `QF_POSTGRES_SINK_ENABLED=1`).

**Code changes:**
- `SettlementLedger.settle()` — after `_append()`, also call `DbSettlementStore.write()` if flag is on
- Error handling: DB write failure is logged but does not block the JSONL write (JSONL is still canonical)

**Verification:** Dual-write comparison tests — write N records, read from both, assert equality.

### Phase 4: Add read-compare mode

**Scope:** When `QF_POSTGRES_SINK_ENABLED=1` and `QF_POSTGRES_READS_ENABLED=0`, read from both Postgres and JSONL and compare. Log divergences but serve from JSONL.

**Feature flag:** `QF_POSTGRES_READS_ENABLED=0` (default) + `QF_POSTGRES_SINK_ENABLED=1`.

**Code changes:**
- `SettlementLedger.read_all()` — if flag is on, also read from DB and compare
- New `settlement_read_compare.py` utility

**Verification:** Read-compare tests with injected divergences.

### Phase 5: Flip reads to Postgres behind feature flag

**Scope:** When `QF_POSTGRES_READS_ENABLED=1`, reads come from Postgres. JSONL is still written (dual-write continues).

**Feature flag:** `QF_POSTGRES_READS_ENABLED=1`.

**Code changes:**
- `SettlementLedger.read_all()` — if `QF_POSTGRES_READS_ENABLED=1`, read from DB
- `SettlementLedger.read_for_model()` — same
- `services/api/src/api/routes/models.py` outcomes route — if flag is on, read from DB
- `services/settlements/src/settlements/compat.py` — adapter reads from DB

**Verification:** Settlement replay with 0 divergences. All focused tests pass with flag on.

### Phase 6: Keep rollback flag to legacy reads

**Scope:** `QF_LEGACY_FILE_READ_FALLBACK=1` forces reads from JSONL regardless of `QF_POSTGRES_READS_ENABLED`.

**Feature flag:** `QF_LEGACY_FILE_READ_FALLBACK=1`.

**Code changes:**
- Read path checks `QF_LEGACY_FILE_READ_FALLBACK` first — if set, read from JSONL

**Verification:** Rollback flag tests — set flag, verify reads come from JSONL.

### Phase 7: Retire legacy writes only after proof

**Scope:** After N days of green dual-write + read-from-DB, stop writing JSONL. This is a configuration change, not a code change.

**Feature flag:** `QF_DUAL_WRITE_SETTLEMENTS=0` (stop JSONL writes).

**Prerequisite:** At least 7 days of `QF_POSTGRES_READS_ENABLED=1` with 0 divergences in production.

**Verification:** Production monitoring confirms no JSONL reads after flag flip.

## Dual-Write Strategy

### Settlement records (the new dual-write path)

```text
SettlementLedger.settle()
  ├── _append(record)                    # JSONL write (always, unless QF_DUAL_WRITE_SETTLEMENTS=0)
  └── if QF_POSTGRES_SINK_ENABLED==1:
        DbSettlementStore.write(record)  # Postgres write (idempotent)
        # DB write failure: log + continue (JSONL is still canonical in Phase 3)
```

### Existing dual-write paths (already implemented)

The callback processor already has dual-write for:
- Dossiers: `DurableDossierStore` (JSONL) + `DbDossierStore` (Postgres)
- Shadow predictions: `DurableShadowLedgerStore` (JSONL) + `DbShadowLedgerStore` (Postgres)
- Callback receipts: `CallbackInbox` (JSONL) + `CallbackReceiptDbStore` (Postgres)
- Callback DLQ: `CallbackDLQ` (JSONL) + `CallbackDlqDbStore` (Postgres)
- Callback metrics: `CallbackMetricsStore` (JSONL) + `CallbackMetricsDbStore` (Postgres)

These are already dual-writing. C10's job is to **flip the reads** for these.

### Transaction boundaries

- Settlement write: single-row INSERT, autocommit. No cross-table transaction needed.
- Dossier + artifact manifest: already in a single session (see `DbDossierStore.store()`).
- Shadow predictions batch: already in a single session (see `DbShadowLedgerStore.store()`).
- Promotion workflow: already in a single session (see `ModelRegistryDB.promote()`).

### Idempotency

All DB sinks use `INSERT ... ON CONFLICT (...) DO NOTHING`:
- `settlement_records`: `ON CONFLICT (settlement_id) DO NOTHING`
- `model_dossiers`: `ON CONFLICT (content_hash) DO NOTHING`
- `callback_receipts`: `ON CONFLICT (callback_id) DO NOTHING`
- `shadow_predictions`: `ON CONFLICT (prediction_id) DO NOTHING`

## Read-Switch Strategy

### Settlement records

| Flag combination | Write path | Read path |
|-----------------|------------|-----------|
| `QF_POSTGRES_SINK_ENABLED=0` (default) | JSONL only | JSONL only |
| `QF_POSTGRES_SINK_ENABLED=1`, `QF_POSTGRES_READS_ENABLED=0` | JSONL + Postgres | JSONL (with read-compare) |
| `QF_POSTGRES_SINK_ENABLED=1`, `QF_POSTGRES_READS_ENABLED=1` | JSONL + Postgres | Postgres |
| `QF_POSTGRES_READS_ENABLED=1`, `QF_LEGACY_FILE_READ_FALLBACK=1` | JSONL + Postgres | JSONL (rollback) |
| `QF_DUAL_WRITE_SETTLEMENTS=0`, `QF_POSTGRES_READS_ENABLED=1` | Postgres only | Postgres |

### Existing records (dossiers, callbacks, shadow predictions)

These already have DB sinks writing. The read-flip is simpler:

| Flag | Read path |
|------|-----------|
| `QF_POSTGRES_READS_ENABLED=0` (default) | JSONL (current) |
| `QF_POSTGRES_READS_ENABLED=1` | Postgres |
| `QF_LEGACY_FILE_READ_FALLBACK=1` | JSONL (rollback) |

### API outcomes route

The outcomes route in `services/api/src/api/routes/models.py` currently reads from `SettlementLedger` (Path B) or `SettlementStore` (Path A, via `SETTLEMENTS_USE_PATH_B=0`). When `QF_POSTGRES_READS_ENABLED=1`, it should read from `settlement_records` table.

## Feature Flags

| Flag | Default | Purpose |
|------|---------|---------|
| `QF_POSTGRES_SINK_ENABLED` | `0` | Enable Postgres writer for settlement records (dual-write) |
| `QF_POSTGRES_READS_ENABLED` | `0` | Flip reads to Postgres for settlement records and existing DB sinks |
| `QF_DUAL_WRITE_SETTLEMENTS` | `1` (when sink enabled) | Continue writing JSONL while also writing Postgres. Set to `0` to retire JSONL writes. |
| `QF_LEGACY_FILE_READ_FALLBACK` | `0` | Force reads from JSONL regardless of `QF_POSTGRES_READS_ENABLED`. Emergency rollback. |

### Relationship to existing `SETTLEMENTS_USE_PATH_B`

`SETTLEMENTS_USE_PATH_B` controls Path A vs Path B settlement computation. `QF_POSTGRES_*` flags control the storage backend. They are orthogonal:

```text
SETTLEMENTS_USE_PATH_B=1, QF_POSTGRES_READS_ENABLED=0  → Path B computation, JSONL storage (current)
SETTLEMENTS_USE_PATH_B=1, QF_POSTGRES_READS_ENABLED=1  → Path B computation, Postgres storage (C10 target)
SETTLEMENTS_USE_PATH_B=0, QF_POSTGRES_READS_ENABLED=1  → Path A computation, Postgres storage (not tested, not recommended)
SETTLEMENTS_USE_PATH_B=0, QF_POSTGRES_READS_ENABLED=0  → Path A computation, JSONL storage (legacy rollback)
```

## Verification Plan

### Goals

C10 verification must prove:

1. Postgres writes match legacy JSONL writes (field-by-field equality)
2. Reads from Postgres match legacy JSONL reads
3. Promotion decisions see the same evidence from Postgres as from JSONL
4. Settlement replay still has 0 divergences after Postgres sink
5. Callback receipt lookup works from Postgres
6. Artifact/bundle hashes round-trip through Postgres
7. Alembic upgrade/downgrade/re-upgrade works for migration 0008
8. Rollback flags restore legacy behavior

### Test plan

| Test | Type | What it proves |
|------|------|---------------|
| `test_settlement_db_sink.py` | Unit (SQLite) | `DbSettlementStore.write()` inserts correct row, idempotent on replay |
| `test_settlement_db_sink_postgres.py` | Integration (Postgres) | Same as above against real Postgres |
| `test_settlement_dual_write.py` | Unit | Dual-write produces identical JSONL and Postgres records |
| `test_settlement_read_compare.py` | Unit | Read-compare detects injected divergences |
| `test_settlement_read_switch.py` | Unit | `QF_POSTGRES_READS_ENABLED=1` reads from Postgres, `=0` reads from JSONL |
| `test_settlement_rollback_flag.py` | Unit | `QF_LEGACY_FILE_READ_FALLBACK=1` forces JSONL reads |
| `test_settlement_replay_postgres.py` | Integration | Post-unification replay with `QF_POSTGRES_READS_ENABLED=1` has 0 divergences |
| `test_promotion_evidence_postgres.py` | Integration | `ModelRegistryDB._assemble_evidence()` reads settlement evidence from Postgres |
| `test_outcomes_route_postgres.py` | Integration | Outcomes API route reads from Postgres when flag is on |
| `test_alembic_0008.py` | Integration (CI) | Migration 0008 upgrade/downgrade/re-upgrade works |
| Existing `test_callback_db_sinks.py` | Unit | Existing DB sinks still pass (no regression) |
| Existing `test_settlements_poller.py` | Unit | Settlements poller works with both flag states |
| Existing `test_models_outcomes.py` | Unit | Outcomes tests pass with both flag states |
| Existing `test_shadow_tournament.py` | Unit | Tournament tests pass with both flag states |

### CI requirements

- Alembic downgrade/upgrade verify CI job must include migration 0008
- Python tests + coverage must pass with `QF_POSTGRES_SINK_ENABLED=1` and `QF_POSTGRES_READS_ENABLED=1`
- Startup safety matrix must pass with both flag states

## Rollback Plan

### Rollback from Phase 5 (reads from Postgres) to Phase 3 (reads from JSONL)

```bash
# Emergency rollback: set fallback flag
export QF_LEGACY_FILE_READ_FALLBACK=1
# Restart the API and settlements worker
# Reads now come from JSONL (which is still being written via dual-write)
```

### Rollback from Phase 3 (dual-write) to Phase 1 (JSONL only)

```bash
# Disable Postgres sink
export QF_POSTGRES_SINK_ENABLED=0
# Restart the API and settlements worker
# Writes now go to JSONL only, reads from JSONL only
```

### Rollback from Phase 1 (schema only) to pre-C10

```bash
# Downgrade migration
alembic downgrade 0007
# settlement_records table is dropped
# No code depends on the table yet
```

### Data preservation during rollback

- JSONL files are never deleted during any phase
- Postgres data is never deleted during rollback (only the table is dropped on migration downgrade)
- If rolling back from Phase 7 (Postgres only) to Phase 5, JSONL files may be stale — a backfill from Postgres to JSONL is needed

## Risks and Mitigations

| Risk | Severity | Mitigation |
|------|----------|------------|
| **Partial dual-write failure** (DB write fails, JSONL succeeds) | medium | DB write failure is logged but does not block JSONL write. JSONL remains canonical in Phase 3. Read-compare in Phase 4 detects the gap. |
| **Transaction boundary violation** (settlement + metrics not atomic) | low | Settlement records and model_metrics are separate concerns. Settlement is always written first; metrics are derived. No cross-table atomicity needed. |
| **Idempotency / duplicate records** | low | `ON CONFLICT (settlement_id) DO NOTHING` on all sinks. `settlement_id = f"{prediction_id}:{cost_model_version}"` is deterministic. |
| **Hash mismatch between file and Postgres** | medium | Read-compare mode (Phase 4) detects any field-level divergence. Settlement replay script verifies 0 divergences. |
| **Migration rollback risk** | low | Migration 0008 is additive only (new table, no existing table modified). Downgrade is `DROP TABLE`. No data loss in other tables. |
| **Read-after-write latency** | low | Sync engine writes are committed before returning. Reads from the same engine see the write immediately. No async replication lag. |
| **CI vs local DB differences** | low | All DB sinks use `_dialect_insert()` which picks SQLite or Postgres insert based on engine dialect. Tests use SQLite; CI uses Postgres. Same code path. |
| **Historical JSONL migration** | medium | C10 does NOT migrate historical JSONL to Postgres. Historical settlements remain in JSONL. Only new settlements are dual-written. A separate backfill script can be run later if needed. |
| **Operator flag confusion** | medium | Four flags (`QF_POSTGRES_SINK_ENABLED`, `QF_POSTGRES_READS_ENABLED`, `QF_DUAL_WRITE_SETTLEMENTS`, `QF_LEGACY_FILE_READ_FALLBACK`) plus existing `SETTLEMENTS_USE_PATH_B`. Document all flag combinations. Add a startup log line showing all flag states. |
| **Postgres connection pool exhaustion** | low | Sync engine has pool_size=20, max_overflow=10. Settlement writes are low-frequency (one per settled prediction). No risk of pool exhaustion. |
| **TimescaleDB hypertable for settlements** | low | Settlements are NOT time-series in the TimescaleDB sense — they're per-prediction records with a fixed horizon. A regular table with indexes is sufficient. If query volume grows, a hypertable can be added later. |

## Open Questions

1. **Should `settlement_records` have a FK to `models.model_id`?**
   - Settlement records can arrive before a model is registered (the callback may be processed before the registry entry).
   - Recommendation: soft FK (no constraint), with a periodic consistency check.

2. **Should the outcomes API route read from `settlement_records` or from `model_metrics` (metric_type='settlement')?**
   - `settlement_records` has the full per-prediction record (return, Brier, calibration).
   - `model_metrics` has aggregated settlement metrics per version.
   - Recommendation: outcomes route reads from `settlement_records` (per-prediction), promotion gate reads from `model_metrics` (aggregated).

3. **Should the dataset registry JSONL ledger be flipped in C10?**
   - The `dataset_manifests` table exists but the `DatasetRegistry` class still uses JSONL for its ledger.
   - Recommendation: defer to a separate task. C10 focuses on settlement records and the existing callback/registry sinks.

4. **Should the job outbox be migrated to Postgres?**
   - The `training_jobs` table exists but the `JobOutbox` class uses JSONL.
   - Recommendation: defer. The outbox is an operational queue, not a critical record. JSONL is acceptable.

5. **Should C10 add a `settlement_read_compare` background task?**
   - A periodic task that reads from both JSONL and Postgres and logs divergences.
   - Recommendation: yes, as part of Phase 4. Simple script that runs the existing replay comparison.

6. **Backfill strategy for historical JSONL settlements?**
   - C10 Phase 3 only dual-writes new settlements. Historical JSONL records are not in Postgres.
   - Recommendation: a `scripts/backfill_settlements_to_postgres.py` script that reads all JSONL files and inserts into `settlement_records`. Run after Phase 3 is stable. Not blocking for C10.

## Safe to Proceed to Task 17: yes

All preconditions met. Persistence inventory is complete. Record classification is clear. Existing schema is well-understood. Target schema is minimal (one new table). Dual-write and read-switch strategy is phased and reversible. Verification plan covers all goals. Rollback plan exists for every phase. Risks are documented with mitigations.
