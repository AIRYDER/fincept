# Tier 1A — Callback Persistence (Track A)

## What was built

Persisted callback ingestion results (dossier, artifact manifest, callback
receipt, DLQ, metrics, shadow predictions) into fincept-db (Postgres) via a
new Alembic migration (`0004`) and DB-backed sink implementations.

## Deliverables

1. **Migration `0004_callback_ingestion.py`** — 6 new tables:
   `artifact_manifests`, `model_dossiers`, `callback_receipts`,
   `callback_dlq`, `callback_metrics`, `shadow_predictions`. Follows the
   `0003_provider_data.py` pattern exactly. JSONB for structured fields,
   BigInteger for ns timestamps, CHECK constraints for enums, UNIQUE
   indexes/PKs for idempotency.

2. **SQLAlchemy 2.0 ORM models** (`callback_tables.py`) — 6 ORM model
   classes matching the migration tables. Uses generic `JSON` type (works
   on both SQLite for tests and Postgres for production) while the
   migration uses `JSONB` for Postgres.

3. **Sync engine** (`engine.py`) — Added `get_sync_engine()`,
   `get_sync_sessionmaker()`, `sync_session_scope()`, and
   `reset_sync_engine()` alongside the existing async engine. Converts
   `postgresql+asyncpg://` to `postgresql+psycopg://` for the sync path.

4. **DB-backed sinks** (`db_sinks.py`) — 5 sink classes implementing the
   existing protocols:
   - `DbDossierStore` → `DossierStoreSink` protocol
   - `DbShadowLedgerStore` → `ShadowLedgerSink` protocol
   - `CallbackReceiptDbStore` — writes InboxRecord to callback_receipts
   - `CallbackDlqDbStore` — writes DLQRecord to callback_dlq
   - `CallbackMetricsDbStore` — writes metrics events to callback_metrics

   All sinks use `INSERT ... ON CONFLICT (key) DO NOTHING` for idempotency.
   The CallbackProcessor interface was NOT changed — the sinks are drop-in
   replacements via protocol implementation.

5. **Tests** (`test_callback_db_sinks.py`) — 31 tests covering:
   - Idempotent insert (same key → no-op)
   - Tamper detection (same job_id + different payload_hash → error)
   - All sink protocols work with CallbackProcessor
   - No secrets/signatures/raw payloads in DB
   - shadow_predictions CHECK constraint rejects non-shadow authority
   - Bad signature → no DB write (fail-closed)
   - Replayed callback → exactly one DB row

## Key design decisions

- **Sync engine, not async**: The CallbackProcessor is sync. Making it
  async would ripple through the whole gateway. Instead, the DB-backed
  sinks use a sync SQLAlchemy engine. Cost: a second connection pool.
- **JSON (generic) in ORM models, JSONB in migration**: The ORM models use
  `sqlalchemy.JSON` so `Base.metadata.create_all` works on SQLite for
  tests. The migration uses `JSONB` for Postgres production. The migration
  is the source of truth for the DB schema.
- **`content_hash` as PK for model_dossiers**: The schema reference
  specifies `UNIQUE (content_hash)` as the idempotency key. Making it the
  PK satisfies both the UNIQUE requirement and the ORM's need for a PK.
- **Engine injection in sinks**: Each sink accepts an optional `engine`
  parameter in its constructor. Tests inject a SQLite engine; production
  uses `get_sync_engine()` (lazy-init).

## Acceptance gates

- ✅ Migration 0004 creates all 6 tables with correct columns, constraints,
  and indexes
- ✅ DB-backed sinks implement the existing protocols (DossierStoreSink,
  ShadowLedgerSink)
- ✅ INSERT ON CONFLICT DO NOTHING for idempotency
- ✅ All existing callback tests still pass (110 passed)
- ✅ New DB sink tests pass on Python 3.12 (31 passed)
- ✅ No secrets, signatures, or raw payloads in any DB column
- ✅ CallbackProcessor interface unchanged
- ✅ HMAC signature verification unchanged
