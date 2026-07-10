# Changed Files — Model Registry (Tier 1.2)

## New files
- `libs/fincept-db/src/fincept_db/migrations/versions/0005_model_registry.py` — Alembic migration (6 tables)
- `libs/fincept-db/src/fincept_db/registry_tables.py` — SQLAlchemy 2.0 ORM models (6 classes)
- `services/quant_foundry/src/quant_foundry/registry_db.py` — ModelRegistryDB with promotion workflow
- `services/quant_foundry/tests/test_registry_db.py` — 38 tests

## Modified files
- `libs/fincept-db/src/fincept_db/__init__.py` — Added observability + registry_tables imports
- `libs/fincept-db/src/fincept_db/migrations/versions/0005_model_registry.py` — Fixed down_revision to "0004b"

## Tables created (migration 0005)
1. `models` — top-level model identity (PK: model_id)
2. `model_versions` — one row per training run (PK: version_id, FKs to models, model_dossiers, artifact_manifests, callback_receipts)
3. `model_metrics` — validation metrics (PK: metric_id, FK to model_versions)
4. `promotions` — one row per promotion attempt (PK: promotion_id, FK to model_versions)
5. `promotion_decisions` — immutable PromotionReceipt (PK: decision_id, FK to promotions)
6. `shadow_evaluations` — aggregated shadow evaluation (PK: evaluation_id, FK to model_versions)
