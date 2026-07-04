# Changed Files — Observability & Cost Tracking

## New files
- `libs/fincept-db/src/fincept_db/migrations/versions/0004b_observability.py` — Alembic migration (4 tables)
- `libs/fincept-db/src/fincept_db/observability.py` — SQLAlchemy 2.0 ORM models (4 classes)
- `services/quant_foundry/src/quant_foundry/cost_tracker.py` — CostTracker with GPU cost rates
- `services/quant_foundry/tests/test_cost_tracker.py` — 44 tests

## Modified files
- `libs/fincept-db/src/fincept_db/__init__.py` — Added observability import
- `services/quant_foundry/.venv/Lib/site-packages/fincept_db.pth` — Path file for fincept-db package

## Tables created (migration 0004b)
1. `training_jobs` — one row per dispatched RunPod job (PK: job_id, FK to callback_receipts)
2. `job_cost_events` — cost events (PK: event_id, FK to training_jobs)
3. `job_metrics` — operational metrics (PK: metric_id, FK to training_jobs)
4. `cost_summary` — period rollup (PK: summary_id, UNIQUE model_family + period_start_ns)
