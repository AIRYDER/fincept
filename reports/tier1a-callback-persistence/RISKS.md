# Risks

## 1. JSON vs JSONB in ORM models

**Risk**: The ORM models use `sqlalchemy.JSON` (generic) while the migration
uses `JSONB` (Postgres-specific). On Postgres, `JSON` maps to `json` (not
`jsonb`), which lacks GIN indexing and some operators.

**Mitigation**: The migration is the source of truth for the production DB
schema — it creates `JSONB` columns. The ORM models are only used for
`Base.metadata.create_all` in tests (SQLite). In production, the tables are
created by `alembic upgrade head`, not by the ORM. If JSONB-specific queries
(GIN indexing, `@>` operator) are needed later, the ORM models can be
updated to use `JSONB().with_variant(JSON, 'sqlite')` for cross-dialect
compatibility.

## 2. Sync engine connection pool

**Risk**: Adding `get_sync_engine()` creates a second connection pool (one
async, one sync). Under high load this doubles the Postgres connection
count.

**Mitigation**: This is the documented trade-off (see
`references/fincept-db-schema.md`, option 1). The sync pool is only used by
the callback writer path (low volume — one write per callback). If profiling
shows the connection count is too high, the sync pool size can be reduced or
the path can be moved to `BackgroundTasks` with the async engine (option 2).

## 3. psycopg not installed in dev environment

**Risk**: `get_sync_engine()` converts the asyncpg URL to
`postgresql+psycopg://`, but `psycopg` is not installed in the dev
environment. Calling `get_sync_engine()` in dev without psycopg will fail
at engine creation time.

**Mitigation**: Tests inject a SQLite engine directly via the sink
constructors (`DbDossierStore(engine=sqlite_engine)`), so `get_sync_engine()`
is never called in tests. In production, `psycopg` must be installed
(`pip install psycopg[binary]`). The `fincept-db` pyproject.toml should be
updated to include `psycopg[binary]` as a dependency in a follow-up.

## 4. No gateway wiring yet

**Risk**: The DB-backed sinks are implemented and tested but not wired into
the gateway constructor. The gateway still uses the JSONL sinks by default.
An env var (`QUANT_FOUNDRY_DOSSIER_SINK=postgres`) is needed to switch to
the DB sinks in production.

**Mitigation**: This is by design — the task specifies "Do NOT change the
CallbackProcessor interface" and the sinks are drop-in replacements. The
gateway wiring is a separate task (the skill's build sequence step 4). The
JSONL path remains the default so existing tests do not break.

## 5. SQLite vs Postgres CHECK constraint behavior

**Risk**: The `test_db_check_constraint_rejects_non_shadow` test verifies
the CHECK constraint on `shadow_predictions.authority` using SQLite. SQLite
and Postgres may have slightly different CHECK constraint evaluation
semantics (e.g. case sensitivity, type coercion).

**Mitigation**: The CHECK constraint is `authority = 'shadow-only'` (exact
string match). Both SQLite and Postgres evaluate this identically for
string literals. The Python-side guard (`sp.authority != Authority.SHADOW_ONLY`)
is the primary defense; the DB CHECK is defense in depth.

## 6. Migration not run against live Postgres

**Risk**: The migration was validated syntactically and the ORM models were
validated against SQLite, but `alembic upgrade head` was not run against a
live Postgres instance (none available in the dev environment).

**Mitigation**: The migration follows the `0003_provider_data.py` pattern
exactly (same `sa.Column`, `sa.CheckConstraint`, `op.create_index` style).
The `fincept-db` tests skip cleanly when Postgres is not available. Running
`alembic upgrade head` against a local Postgres is the next validation step
before production deployment.
