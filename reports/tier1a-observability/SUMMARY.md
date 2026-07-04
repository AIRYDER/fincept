# Tier 1A — Observability & Cost Tracking Summary

## What was built

Observability and cost tracking for the RunPod training pipeline — 4 Postgres tables and a `CostTracker` class that records every training job's lifecycle, cost events, operational metrics, and period rollups.

### New files created (4)

1. **`libs/fincept-db/src/fincept_db/migrations/versions/0004b_observability.py`** — Alembic migration creating 4 tables: `training_jobs`, `job_cost_events`, `job_metrics`, `cost_summary`. Revision `0004b`, `down_revision="0004"`.

2. **`libs/fincept-db/src/fincept_db/observability.py`** — SQLAlchemy 2.0 ORM models (4 classes). Uses generic `JSON` type for cross-dialect compatibility. NOTE: `metadata` column renamed to `extra_metadata` Python attribute (SQLAlchemy reserved name).

3. **`services/quant_foundry/src/quant_foundry/cost_tracker.py`** — `CostTracker` class with sync sessions, GPU cost rate table, job lifecycle tracking, and period cost rollups.

4. **`services/quant_foundry/tests/test_cost_tracker.py`** — 44 tests using in-memory SQLite.

### Fixes applied during recovery
- Renamed `metadata` Python attribute to `extra_metadata` (SQLAlchemy reserved name) in `observability.py`, `cost_tracker.py`, and `test_cost_tracker.py`
- Added missing `select` import in `test_cost_tracker.py`
- Added `observability` module to `libs/fincept-db/src/fincept_db/__init__.py`
- Added `fincept_db.pth` to venv site-packages for `fincept-db` package resolution

## Key design decisions

- **`metadata` is reserved by SQLAlchemy.** The DB column is named `metadata` (per the migration), but the Python ORM attribute is `extra_metadata` with `mapped_column("metadata", JSON, nullable=True)`.
- **GPU cost rates are built-in defaults.** RTX_4090: $0.40/hr, A100_80GB: $1.10/hr, A100_40GB: $0.80/hr, L4: $0.25/hr, default: $0.50/hr. Overridable via constructor.
- **`request_payload_ref` is a file path, not the payload.** No secrets, signatures, or raw payloads in any DB column.
- **Period cost rollup uses upsert.** `compute_period_cost()` aggregates costs and upserts into `cost_summary` with `UNIQUE (model_family, period_start_ns)`.

## Test results

- **44/44 new cost tracker tests pass** (Python 3.12)
- **17/17 regression tests pass** (schemas, runpod_client, promotion, dossier)
- **0 failures** (pytest exit code 1 is Windows temp-dir cleanup PermissionError)
