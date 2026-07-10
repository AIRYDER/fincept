# Changed Files

## New files

| File | Purpose |
| --- | --- |
| `libs/fincept-db/src/fincept_db/migrations/versions/0004_callback_ingestion.py` | Alembic migration creating 6 callback ingestion tables |
| `libs/fincept-db/src/fincept_db/callback_tables.py` | SQLAlchemy 2.0 ORM models for the 6 callback tables |
| `services/quant_foundry/src/quant_foundry/db_sinks.py` | DB-backed sink implementations (5 sink classes) |
| `services/quant_foundry/tests/test_callback_db_sinks.py` | 31 tests for DB sinks (SQLite in-memory) |
| `reports/tier1a-callback-persistence/SUMMARY.md` | This receipt |
| `reports/tier1a-callback-persistence/CHANGED_FILES.md` | This file |
| `reports/tier1a-callback-persistence/COMMANDS.md` | Commands run |
| `reports/tier1a-callback-persistence/TEST_RESULTS.md` | Test output |
| `reports/tier1a-callback-persistence/RISKS.md` | Risks and mitigations |

## Modified files

| File | Change |
| --- | --- |
| `libs/fincept-db/src/fincept_db/engine.py` | Added `get_sync_engine()`, `get_sync_sessionmaker()`, `sync_session_scope()`, `reset_sync_engine()`, `_async_url_to_sync()` |
| `libs/fincept-db/src/fincept_db/__init__.py` | Registered `callback_tables` module in imports + `__all__` |

## Files NOT changed (by design)

| File | Reason |
| --- | --- |
| `services/quant_foundry/src/quant_foundry/callbacks.py` | CallbackProcessor interface unchanged (hard rule) |
| `services/quant_foundry/src/quant_foundry/signatures.py` | HMAC verification unchanged (hard rule) |
| `services/quant_foundry/src/quant_foundry/inbox.py` | JSONL inbox unchanged (existing path still works) |
| `services/quant_foundry/src/quant_foundry/callback_dlq.py` | JSONL DLQ unchanged (existing path still works) |
| `services/quant_foundry/src/quant_foundry/callback_metrics.py` | JSONL metrics unchanged (existing path still works) |
| `services/quant_foundry/src/quant_foundry/registry.py` | JSONL registry unchanged (existing path still works) |
| `services/quant_foundry/src/quant_foundry/gateway_callback.py` | Gateway mixin unchanged |
