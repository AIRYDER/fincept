# C10 Dual-Write Implementation Report

## Branch SHA

`feature/c10-postgres-sink-skeleton` at `b0b47e2b` (Task 17 commit)

## Files changed

| File | Status | Purpose |
|------|--------|---------|
| `services/quant_foundry/src/quant_foundry/settlement.py` | modified | Added `db_store` parameter to `SettlementLedger` + `_dual_write()` method |
| `services/quant_foundry/src/quant_foundry/gateway.py` | modified | Inject `DbSettlementStore` into `SettlementLedger` when DB engine available; flag-aware callback receipt dual-write |
| `services/quant_foundry/src/quant_foundry/dual_write.py` | new | Dual-write coordinator for callback receipts, dossiers, model metrics |
| `services/quant_foundry/tests/test_c10_dual_write.py` | new | 22 tests for dual-write behavior |
| `reports/c10-postgres-sink-flip/dual-write-proof/run_proof.py` | new | Deterministic dual-write comparison proof script |
| `reports/c10-postgres-sink-flip/dual-write-proof/proof_output.txt` | new | Proof output (0 divergences) |

## Feature flags

| Flag | Default | Purpose |
|------|---------|---------|
| `QF_POSTGRES_SINK_ENABLED` | `0` (off) | Enable Postgres writer for settlement records + callback receipts |
| `QF_DUAL_WRITE_SETTLEMENTS` | `0` (off) | Continue JSONL writes alongside Postgres writes |
| `QF_POSTGRES_READS_ENABLED` | `0` (off) | Flip reads to Postgres (NOT used in Task 18) |
| `QF_LEGACY_FILE_READ_FALLBACK` | `1` (on) | Force JSONL reads (emergency rollback) |
| `QF_DUAL_WRITE_FAIL_HARD` | `0` (off) | Re-raise DB write failures (test/verification mode) |

All flags default to safe legacy mode. No runtime behavior changes when flags are off.

## Records dual-written

| Record type | Dual-write method | Flag | Status |
|------------|-------------------|------|--------|
| Settlement records | `SettlementLedger._dual_write()` | `QF_POSTGRES_SINK_ENABLED` | Implemented + tested |
| Callback receipts | `dual_write_callback_receipt()` | `QF_POSTGRES_SINK_ENABLED` | Implemented + tested |
| Dossier records | `dual_write_dossier()` | `QF_POSTGRES_SINK_ENABLED` | Implemented + tested |
| Model metrics (selfcheck, PIT, feature_set) | `dual_write_model_metric()` | `QF_POSTGRES_SINK_ENABLED` | Implemented + tested |
| Artifact manifests | Via `DbDossierStore.store()` (existing) | `sink_backend="db"` | Pre-existing (not C10-controlled) |
| Shadow predictions | Via `DbShadowLedgerStore` (existing) | `sink_backend="db"` | Pre-existing (not C10-controlled) |

## Error handling policy

### Default (production mode)

- **Legacy write succeeds, Postgres write fails**: DB write failure is logged at ERROR level. The legacy write (which already succeeded) remains canonical. The record is not lost.
- **Postgres write succeeds, legacy write fails**: The legacy write failure propagates (it happens first). The Postgres write is not attempted.
- **Duplicate write with same hash**: Idempotent via `ON CONFLICT DO NOTHING`. No error, no duplicate row.
- **Duplicate write with different hash**: Different `settlement_id` (or `callback_id`, etc.) → new row. No error.
- **Database unavailable**: DB write failure is logged. Legacy write is preserved. System continues operating.
- **Transaction failure**: Same as database unavailable — logged, legacy write preserved.
- **Schema mismatch**: DB write failure (e.g., CHECK constraint violation) is logged. Legacy write is preserved.

### Fail-hard mode (`QF_DUAL_WRITE_FAIL_HARD=1`)

- All DB write failures re-raise the exception.
- Used in test/verification mode to catch dual-write mismatches.
- Never silently drop mismatches.

### Key principle

**Never silently drop mismatches.** All DB write failures are logged at ERROR level. In fail-hard mode, they are re-raised.

## Idempotency behavior

All dual-write paths use `INSERT ... ON CONFLICT (key) DO NOTHING`:

| Record type | Conflict key | Behavior on replay |
|------------|-------------|-------------------|
| Settlement records | `settlement_id` (`f"{prediction_id}:{cost_model_version}"`) | No-op, no duplicate row |
| Callback receipts | `callback_id` | No-op, no duplicate row |
| Dossier records | `content_hash` | No-op, no duplicate row |
| Model metrics | `metric_id` | No-op, no duplicate row |

**Replay test proof**: 10 records written, then all 10 re-settled with identical inputs. JSONL count remained 10, Postgres count remained 10. Zero duplicates.

## Hash mismatch behavior

- **Same prediction_id + same cost_model_version**: Same `settlement_id` → idempotent no-op.
- **Same prediction_id + different cost_model_version**: Different `settlement_id` → new row (history preserved).
- **Different prediction_id**: Different `settlement_id` → new row.
- **Hash mismatch is never silently ignored**: If a DB write fails due to a constraint violation, the error is logged at ERROR level. In fail-hard mode, the error is re-raised.

## Tests added

### `services/quant_foundry/tests/test_c10_dual_write.py` (22 tests)

| Test class | Test | What it proves |
|-----------|------|---------------|
| `TestFlagsOff` | `test_settlement_no_db_write_when_flag_off` | No Postgres write when flag is off |
| | `test_should_write_to_postgres_false` | `should_write_to_postgres()` is False |
| | `test_should_read_from_postgres_false` | `should_read_from_postgres()` is False |
| | `test_legacy_read_unchanged` | JSONL reads work when flag is off |
| | `test_postgres_reads_disabled` | `postgres_reads_enabled()` is False |
| `TestFlagsOn` | `test_settlement_dual_write` | Both JSONL and Postgres writes happen |
| | `test_settlement_dual_write_via_settle` | Full `settle()` call dual-writes |
| | `test_callback_receipt_dual_write` | `dual_write_callback_receipt()` writes to DB |
| | `test_dossier_dual_write` | `dual_write_dossier()` writes to DB |
| | `test_model_metric_dual_write` | `dual_write_model_metric()` writes to DB |
| `TestIdempotency` | `test_settlement_idempotent_replay` | Same record twice → no duplicate |
| | `test_settlement_different_cost_model_new_row` | Different cost_model → new row |
| `TestHashMismatch` | `test_different_records_different_rows` | Different records → different rows |
| `TestDatabaseUnavailable` | `test_db_failure_does_not_block_legacy_write` | DB failure logged, JSONL preserved |
| | `test_db_failure_fail_hard_re_raises` | Fail-hard mode re-raises |
| | `test_callback_receipt_db_failure_logged` | Callback receipt DB failure logged |
| | `test_callback_receipt_db_failure_fail_hard` | Callback receipt fail-hard re-raises |
| | `test_no_db_store_no_error` | No db_store → no-op, no error |
| `TestLegacyReadUnchanged` | `test_read_all_from_jsonl_with_flag_off` | JSONL reads work with flag off |
| | `test_read_all_from_jsonl_with_flag_on` | JSONL reads still work with flag on |
| `TestDualWriteComparison` | `test_settlement_jsonl_equals_postgres` | Field-by-field equality |
| | `test_settlement_replay_zero_divergences` | 10 records, 0 divergences |

## Dual-write proof

**Proof script**: `reports/c10-postgres-sink-flip/dual-write-proof/run_proof.py`
**Proof output**: `reports/c10-postgres-sink-flip/dual-write-proof/proof_output.txt`

### Results

```text
=== C10 Dual-Write Comparison Proof ===

Records written: 10
JSONL records read: 10
Postgres records read: 10

Total divergences: 0

After replay: JSONL=10, Postgres=10
Idempotency: PASS (no duplicates after replay)

Read path: JSONL (QF_POSTGRES_READS_ENABLED=0, QF_LEGACY_FILE_READ_FALLBACK=1)

=== PROOF SUMMARY ===
Records written: 10
JSONL records: 10
Postgres records: 10
Divergences: 0
Idempotency: PASS
Read path: JSONL (legacy)

VERDICT: PASS — 0 divergences, dual-write is correct
```

### What the proof verifies

1. **Legacy record hash == Postgres canonical record hash**: 10 records written via dual-write, all 10 match field-by-field between JSONL and Postgres.
2. **Settlement replay remains 0 divergences**: 10 records re-settled with identical inputs → 0 duplicates, 0 divergences.
3. **Promotion evidence lookup remains unchanged**: Reads still come from JSONL (`QF_POSTGRES_READS_ENABLED=0`), so promotion evidence assembly is unchanged.
4. **Callbacks/artifacts/bundles round-trip through Postgres repositories**: `dual_write_callback_receipt()`, `dual_write_dossier()`, and `dual_write_model_metric()` all write to Postgres when the flag is on (verified in unit tests with mock stores).

## Runtime behavior with flags off

```text
None. All C10 sink/read flags default off.
```

- `QF_POSTGRES_SINK_ENABLED=0` → no Postgres writes attempted
- `QF_POSTGRES_READS_ENABLED=0` → no Postgres reads
- `QF_DUAL_WRITE_SETTLEMENTS=0` → no dual-write
- `QF_LEGACY_FILE_READ_FALLBACK=1` → legacy JSONL reads are the default
- `SettlementLedger` with `db_store=None` (no DB engine) → no DB write attempted
- `SettlementLedger` with `db_store` set but flag off → `should_write_to_postgres()` returns False → no DB write
- Callback receipt mirroring: existing pre-C10 behavior (silent suppress for CostTracker FK) remains
- All existing tests pass unchanged

## Runtime behavior with flags on

When `QF_POSTGRES_SINK_ENABLED=1` and `QF_DUAL_WRITE_SETTLEMENTS=1`:

1. **Settlement records**: `SettlementLedger.settle()` writes to JSONL first (via `_append()`), then to Postgres (via `_dual_write()`). JSONL write remains canonical. Postgres write is idempotent.
2. **Callback receipts**: `dual_write_callback_receipt()` writes to Postgres with proper error handling (logged at ERROR, optionally fail-hard).
3. **Dossier records**: `dual_write_dossier()` writes to Postgres via `DbDossierStore.store()`.
4. **Model metrics**: `dual_write_model_metric()` writes to Postgres via `ModelRegistryDB.record_metrics()`.
5. **Reads**: Still from JSONL (`QF_POSTGRES_READS_ENABLED=0`). Postgres reads are not flipped.

## Verification results

| Check | Result |
|-------|--------|
| `ruff format --check .` | 839 files OK |
| `ruff check libs services` | All checks passed |
| `mypy libs services` | 373 files, no issues |
| `pytest libs/fincept-db/tests` | All passed |
| `pytest services/quant_foundry/tests` | All passed |
| `pytest services/api/tests` | All passed |
| `pytest services/settlements/tests` | All passed (1 pre-existing Windows temp teardown error) |
| New dual-write tests (22) | 22 passed |
| Full suite | 5040 passed, 230 skipped, 1 pre-existing error |
| Dual-write proof | 0 divergences, idempotency PASS |

## Risks

| Risk | Severity | Mitigation |
|------|----------|------------|
| DB write failure in production | low | Logged at ERROR level. JSONL write (canonical) already succeeded. Record is not lost. |
| DB write failure in test mode | low | Fail-hard mode (`QF_DUAL_WRITE_FAIL_HARD=1`) re-raises. |
| Double-write when both C10 and CostTracker paths active | low | Both paths write idempotently (ON CONFLICT DO NOTHING). No duplicate rows. |
| SettlementLedger constructed without db_store | low | `_dual_write()` checks `db_store is None` first → no-op. |
| Feature flags accidentally enabled | low | All flags default to safe legacy mode (off/off/off/on). Tests verify defaults. |
| Pre-existing Windows temp teardown error | low | Unrelated to C10 — `pytest-of-nolan\pytest-current` permission issue. Exists on main before C10 changes. |

## Safe to open PR: yes

All preconditions met. Settlement dual-write implemented behind feature flag. Callback receipt dual-write implemented with proper error handling. Dossier and model metric dual-write coordinators implemented. 22 new tests pass. Dual-write proof shows 0 divergences. All flags default to safe legacy mode. No runtime behavior changes when flags are off. Ruff/mypy clean. Full suite passes (5040 passed, no regressions).
