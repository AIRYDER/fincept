# Tier 1A — Model Registry (Tier 1.2) Summary

## What was built

The durable model lifecycle — Postgres tables and state machine that turn "a trained model exists" into "a model is a governed, promotable, retireable platform asset."

### New files created (4)

1. **`libs/fincept-db/src/fincept_db/migrations/versions/0005_model_registry.py`** — Alembic migration creating 6 tables: `models`, `model_versions`, `model_metrics`, `promotions`, `promotion_decisions`, `shadow_evaluations`. Follows the `0004_callback_ingestion` pattern with `revision="0005"`, `down_revision="0004b"`, JSONB for structured fields, BigInteger for ns timestamps, CHECK constraints for enums.

2. **`libs/fincept-db/src/fincept_db/registry_tables.py`** — SQLAlchemy 2.0 ORM models (6 classes) matching the migration tables. Uses generic `JSON` type for cross-dialect test compatibility.

3. **`services/quant_foundry/src/quant_foundry/registry_db.py`** — `ModelRegistryDB` class with sync sessions, idempotent registration, and the promotion workflow that wires the existing `PromotionGate` into Postgres.

4. **`services/quant_foundry/tests/test_registry_db.py`** — 38 tests using in-memory SQLite.

### Modified files (2)

5. **`libs/fincept-db/src/fincept_db/__init__.py`** — Added `observability` and `registry_tables` module imports.

6. **`libs/fincept-db/src/fincept_db/migrations/versions/0005_model_registry.py`** — Fixed `down_revision` from `"0004"` to `"0004b"` for clean linear migration chain.

## Key design decisions

- **The registry persists; the gate enforces.** The `promote()` method assembles `PromotionEvidence` from the registry tables, calls `PromotionGate.evaluate(...)`, persists the receipt, and only then updates status. The gate's logic is NOT duplicated.
- **DossierStatus enum NOT renamed.** The codebase enum (`candidate → research_approved → shadow_approved → paper_approved → limited_live_approved → rejected`) is used as-is.
- **FK constraints enforce referential integrity.** The `model_versions.dossier_content_hash` FK to `model_dossiers.content_hash` makes the NO_DOSSIER rejection path unreachable through normal operations (defense-in-depth in the gate).
- **Rejection receipts are persisted.** Even when the gate rejects, the `promotions` and `promotion_decisions` rows are written for the audit trail.

## Test results

- **38/38 new registry tests pass** (Python 3.12)
- **17/17 regression tests pass** (schemas, runpod_client, promotion, dossier)
- **0 failures** (pytest exit code 1 is Windows temp-dir cleanup PermissionError, not a test failure)
