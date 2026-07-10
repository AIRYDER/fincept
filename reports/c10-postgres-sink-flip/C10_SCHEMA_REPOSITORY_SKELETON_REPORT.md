# C10 Schema Migration and Repository Skeleton Report

## Branch SHA

`feature/c10-postgres-sink-skeleton` (based on `main` at `bf9e9cce`)

## Files changed

| File | Status | Purpose |
|------|--------|---------|
| `libs/fincept-db/src/fincept_db/migrations/versions/0008_settlement_records.py` | new | Alembic migration 0008 — creates `settlement_records` table |
| `libs/fincept-db/src/fincept_db/settlement_tables.py` | new | SQLAlchemy 2.0 ORM model `SettlementRecordRow` |
| `libs/fincept-db/src/fincept_db/__init__.py` | modified | Register `settlement_tables` module in package exports |
| `libs/fincept-db/tests/test_settlement_records_schema.py` | new | Tests for migration imports, revision chain, ORM metadata, indexes, CHECK constraints |
| `services/quant_foundry/src/quant_foundry/settlement_db_sink.py` | new | `DbSettlementStore` — DB-backed settlement record repository |
| `services/quant_foundry/src/quant_foundry/c10_flags.py` | new | Feature flags for C10 Postgres sink flip (all default off) |
| `services/quant_foundry/tests/test_settlement_db_sink.py` | new | Tests for `DbSettlementStore` — insert, get, list, count, idempotency, CHECK constraints, no secrets |
| `services/quant_foundry/tests/test_c10_flags.py` | new | Tests for feature flags — defaults, combinations, truthy/falsy values |
| `.github/workflows/ci.yml` | modified | Updated Alembic downgrade/upgrade verify job to include migration 0008 |

## Migration added

**Migration 0008: `settlement_records`**

- Revision: `0008`
- Down revision: `0007` (promotion_gate_hardening)
- Create date: 2026-07-10
- Operation: `CREATE TABLE settlement_records` + 5 indexes
- Downgrade: `DROP TABLE settlement_records` (drops indexes automatically)
- Additive only — no existing table modified
- No destructive changes

## Tables added/changed

### New table: `settlement_records`

| Column | Type | Nullable | Default | Notes |
|--------|------|----------|---------|-------|
| `schema_version` | Integer | NO | 1 | |
| `settlement_id` | String(256) | NO | — | PK: `f"{prediction_id}:{cost_model_version}"` |
| `prediction_id` | String(128) | NO | — | |
| `model_id` | String(128) | NO | — | |
| `symbol` | String(32) | NO | — | |
| `ts_event` | BigInteger | NO | — | Decision time t (ns) |
| `horizon_ns` | BigInteger | NO | — | Prediction horizon |
| `status` | String(32) | NO | — | CHECK: pending_time, pending_data, settled |
| `settled_at_ns` | BigInteger | YES | — | NULL when pending |
| `realized_return_gross` | Numeric(28,12) | YES | — | |
| `realized_return_net` | Numeric(28,12) | YES | — | |
| `abnormal_return` | Numeric(28,12) | YES | — | |
| `brier` | Numeric(28,12) | YES | — | |
| `calibration_bucket` | String(16) | YES | — | CHECK: 0.0-0.2, 0.2-0.4, 0.4-0.6, 0.6-0.8, 0.8-1.0 |
| `cost_model_version` | String(16) | NO | — | CHECK: cm-v1, v1.default |
| `decision_window_start` | BigInteger | NO | — | t |
| `decision_window_end` | BigInteger | NO | — | t + horizon_ns |
| `created_at_ns` | BigInteger | NO | — | |

**Primary key:** `settlement_id`

**Unique constraint:** `uq_settlement_records_prediction_id_cost_model_version` on `(prediction_id, cost_model_version)`

**CHECK constraints:**
- `ck_settlement_records_status_domain`: status IN ('pending_time','pending_data','settled')
- `ck_settlement_records_cost_model_version_domain`: cost_model_version IN ('cm-v1','v1.default')
- `ck_settlement_records_calibration_bucket_domain`: calibration_bucket IS NULL OR in domain

**Indexes:**
- `ix_settlement_records_model_id_ts` (model_id, ts_event) — primary query pattern
- `ix_settlement_records_symbol_ts` (symbol, ts_event) — cross-model queries
- `ix_settlement_records_status` (status) — filter pending vs settled
- `ix_settlement_records_prediction_id` (prediction_id) — idempotency lookup
- `ix_settlement_records_cost_model_version` (cost_model_version) — filter by cost model

**Foreign keys:** None (soft FK to `models.model_id` — settlement can arrive before model registration)

**No existing tables changed.**

## Repository skeleton

### `DbSettlementStore` (`services/quant_foundry/src/quant_foundry/settlement_db_sink.py`)

DB-backed settlement record store. Uses sync SQLAlchemy engine (same pattern as `db_sinks.py`). All writes are idempotent via `INSERT ... ON CONFLICT (settlement_id) DO NOTHING`.

Methods implemented:

| Method | Signature | Returns | Purpose |
|--------|-----------|---------|---------|
| `write` | `(record: SettlementRecord, *, now_ns: int \| None = None)` | `bool` | Write one record. True if new, False if idempotent replay. |
| `write_batch` | `(records: Sequence[SettlementRecord], *, now_ns: int \| None = None)` | `int` | Write batch. Returns count of new rows inserted. |
| `get` | `(prediction_id: str, cost_model_version: str)` | `SettlementRecord \| None` | Read one record by (prediction_id, cost_model_version). |
| `list_for_model` | `(model_id: str, *, limit: int = 1000, status: str \| None = None)` | `list[SettlementRecord]` | List records for a model, newest-first. |
| `list_all` | `(*, limit: int = 10000, status: str \| None = None)` | `list[SettlementRecord]` | List all records, newest-first. |
| `count` | `(*, model_id: str \| None = None, status: str \| None = None)` | `int` | Count records with optional filters. |

**Not wired into production execution.** The `SettlementLedger` does not call `DbSettlementStore` yet. Feature flags control when dual-writing begins (Task 18).

## Feature flags

### `services/quant_foundry/src/quant_foundry/c10_flags.py`

| Flag | Default | Purpose |
|------|---------|---------|
| `QF_POSTGRES_SINK_ENABLED` | `0` (off) | Enable Postgres writer for settlement records |
| `QF_POSTGRES_READS_ENABLED` | `0` (off) | Flip reads to Postgres |
| `QF_DUAL_WRITE_SETTLEMENTS` | `0` (off) | Continue JSONL writes alongside Postgres |
| `QF_LEGACY_FILE_READ_FALLBACK` | `1` (on) | Force JSONL reads (emergency rollback) |

**Derived helpers:**
- `should_write_to_postgres()` → `postgres_sink_enabled()`
- `should_read_from_postgres()` → `postgres_reads_enabled() and not legacy_file_read_fallback()`
- `should_write_to_jsonl()` → `not postgres_sink_enabled() or dual_write_settlements()`

**All flags default to safe legacy mode.** No runtime behavior changes when flags are off.

## Tests added

### `libs/fincept-db/tests/test_settlement_records_schema.py` (5 tests)

| Test | What it proves |
|------|---------------|
| `test_migration_0008_imports` | Migration module imports cleanly |
| `test_migration_0008_revision_chain` | 0008.revision == "0008", down_revision == "0007" |
| `test_settlement_records_table_in_metadata` | Base.metadata includes settlement_records with all columns, PK, unique constraint |
| `test_settlement_records_indexes_in_metadata` | All 5 indexes present in metadata |
| `test_settlement_records_check_constraints_in_metadata` | All 3 CHECK constraints present in metadata |

### `services/quant_foundry/tests/test_settlement_db_sink.py` (13 tests)

| Test | What it proves |
|------|---------------|
| `test_write_and_get` | Write a record, read it back, field-by-field equality |
| `test_write_returns_true_for_new_record` | First write returns True |
| `test_idempotent_write_same_record` | Same record twice → no second row |
| `test_idempotent_write_different_cost_model` | Same prediction_id, different cost_model_version → new row |
| `test_pending_record_round_trips` | Pending records (null fields) round-trip correctly |
| `test_list_for_model` | List records for a model, newest-first, pending last |
| `test_list_for_model_filtered_by_status` | Status filter works |
| `test_list_all` | List across all models |
| `test_count` | Count with model_id/status filters |
| `test_write_batch` | Batch write with mixed new + existing |
| `test_invalid_status_rejected` | CHECK constraint rejects invalid status |
| `test_invalid_cost_model_version_rejected` | CHECK constraint rejects invalid cost_model_version |
| `test_no_secrets_in_columns` | No secret/signature/payload columns |

### `services/quant_foundry/tests/test_c10_flags.py` (24 tests)

| Test | What it proves |
|------|---------------|
| `test_postgres_sink_enabled_defaults_off` | QF_POSTGRES_SINK_ENABLED defaults to 0 |
| `test_postgres_reads_enabled_defaults_off` | QF_POSTGRES_READS_ENABLED defaults to 0 |
| `test_dual_write_settlements_defaults_off` | QF_DUAL_WRITE_SETTLEMENTS defaults to 0 |
| `test_legacy_file_read_fallback_defaults_on` | QF_LEGACY_FILE_READ_FALLBACK defaults to 1 |
| `test_should_write_to_postgres_defaults_false` | Derived helper defaults False |
| `test_should_read_from_postgres_defaults_false` | Derived helper defaults False |
| `test_should_write_to_jsonl_defaults_true` | Derived helper defaults True |
| `test_sink_on_reads_off` | Sink on, reads off → write Postgres, read JSONL, no JSONL write |
| `test_sink_on_dual_write_on` | Sink on, dual-write on → write both, read JSONL |
| `test_sink_on_reads_on_fallback_on` | Sink on, reads on, fallback on → reads from JSONL (rollback) |
| `test_sink_on_reads_on_fallback_off` | Sink on, reads on, fallback off → reads from Postgres |
| `test_full_postgres_mode` | All flags set for Phase 7 |
| `test_truthy_values` (7 parametrized) | 1, true, yes, on, TRUE, Yes enable flag |
| `test_falsy_values` (6 parametrized) | 0, false, no, off, "", random keep flag off |

**Total new tests: 42**

## Alembic verification

The CI workflow `alembic-downgrade-verify` has been updated to verify migration 0008:

1. `alembic upgrade head` — creates `settlement_records` table
2. Verify table exists with columns, indexes, CHECK constraints
3. `alembic downgrade -1` — drops `settlement_records` table
4. Verify table is removed
5. `alembic upgrade head` — restores `settlement_records` table
6. Verify table is restored

**Local verification:** Migration 0008 imports cleanly, revision chain is correct (0008 → 0007), ORM metadata includes the table with all columns, indexes, and CHECK constraints. Full Alembic upgrade/downgrade/re-upgrade requires a Postgres database (verified in CI).

## Runtime behavior change

```text
None. All C10 sink/read flags default off.
```

- `QF_POSTGRES_SINK_ENABLED=0` — no Postgres writes
- `QF_POSTGRES_READS_ENABLED=0` — no Postgres reads
- `QF_DUAL_WRITE_SETTLEMENTS=0` — no dual-write
- `QF_LEGACY_FILE_READ_FALLBACK=1` — legacy JSONL reads are the default

The `SettlementLedger` does not call `DbSettlementStore` yet. The repository methods exist and are tested but are not wired into production execution. Legacy behavior is unchanged.

## Verification results

| Check | Result |
|-------|--------|
| `ruff format --check .` | 837 files OK |
| `ruff check libs services` | All checks passed |
| `mypy libs services` | 372 files, no issues |
| `pytest libs/fincept-db/tests` | All passed |
| `pytest services/quant_foundry/tests` | All passed |
| `pytest services/api/tests` | All passed |
| `pytest services/settlements/tests` | All passed (1 pre-existing Windows temp teardown error) |
| New tests (42 total) | 42 passed |
| Full suite | 5018 passed, 230 skipped, 1 pre-existing error |

## Risks

| Risk | Severity | Mitigation |
|------|----------|------------|
| Migration 0008 fails on Postgres | low | Additive only (new table, no existing table modified). Downgrade is `DROP TABLE`. CI verifies upgrade/downgrade/re-upgrade. |
| ORM model diverges from migration | low | Both are tested against `Base.metadata` in `test_settlement_records_schema.py`. CI verifies the actual table schema against `information_schema`. |
| Feature flags accidentally enabled | low | All flags default to safe legacy mode (off/off/off/on). Tests verify defaults. Flags are env vars, not config files. |
| `DbSettlementStore` called before table exists | low | Store is not wired into production execution. Feature flags gate all usage. |
| `rowcount` behavior differs between SQLite and Postgres | low | Both dialects return 1 for new insert, 0 for ON CONFLICT DO NOTHING. Tested against SQLite; CI tests against Postgres. |
| Pre-existing Windows temp teardown error | low | Unrelated to C10 — `pytest-of-nolan\pytest-current` permission issue. Exists on main before C10 changes. |

## Safe to open PR: yes

All preconditions met. Migration 0008 is additive only. ORM model mirrors the migration. Repository skeleton is tested but not wired into production. Feature flags default to safe legacy mode. No runtime behavior changes. CI workflow updated to verify migration 0008. All tests pass (42 new, 5018 total). Ruff/mypy clean.
