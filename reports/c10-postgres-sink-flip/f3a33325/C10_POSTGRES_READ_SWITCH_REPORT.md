# C10 Postgres Read Switch Report

## Branch SHA

`f3a33325` (feature/c10-postgres-sink-skeleton)

## Files changed

| File | Status |
|------|--------|
| `services/quant_foundry/src/quant_foundry/c10_flags.py` | modified — added `postgres_read_switch_active()` flag helper |
| `services/quant_foundry/src/quant_foundry/read_switch.py` | new — read switch module: validation, switch coordinator, evidence |
| `services/quant_foundry/src/quant_foundry/settlement.py` | modified — `read_all()` uses Postgres-first read switch; refactored into `_read_all_from_jsonl()` and `_read_all_from_postgres()` |
| `services/quant_foundry/tests/test_c10_read_switch.py` | new — 26 read switch tests |
| `services/quant_foundry/tests/test_c10_flags.py` | modified — 3 new flag tests |
| `reports/c10-postgres-sink-flip/f3a33325/read-switch-proof/run_proof.py` | new — deterministic proof script |
| `reports/c10-postgres-sink-flip/f3a33325/read-switch-proof/*.json` | new — 6 evidence files |
| `reports/c10-postgres-sink-flip/f3a33325/read-switch-proof/summary.md` | new — proof summary |

## Feature flags

| Flag | Default | Purpose |
|------|---------|---------|
| `QF_POSTGRES_SINK_ENABLED` | `0` (off) | Enable Postgres dual-write |
| `QF_DUAL_WRITE_SETTLEMENTS` | `0` (off) | Continue JSONL writes alongside Postgres |
| `QF_POSTGRES_READ_COMPARE_ENABLED` | `0` (off) | Compare legacy reads against Postgres reads |
| `QF_POSTGRES_READS_ENABLED` | `0` (off) | Read from Postgres (Postgres-first) |
| `QF_LEGACY_FILE_READ_FALLBACK` | `1` (on) | Fall back to legacy JSONL on Postgres failure |
| `QF_DUAL_WRITE_FAIL_HARD` | `0` (off) | Re-raise dual-write/read-compare errors |

### Flag helper semantics

| Helper | True when | Meaning |
|--------|-----------|---------|
| `postgres_read_switch_active()` | `QF_POSTGRES_READS_ENABLED=1` | Postgres-first mode (with or without fallback) |
| `should_read_from_postgres()` | `QF_POSTGRES_READS_ENABLED=1` AND `QF_LEGACY_FILE_READ_FALLBACK=0` | Postgres-only mode (no fallback, Phase 7) |

## Records switched

| Record type | Method | Status |
|------------|--------|--------|
| Settlement records | `read_switch_settlements()` via `SettlementLedger.read_all()` | Implemented + tested + proofed |
| Callback receipts | Not switched (only proven via read_compare_dict in Task 19) | Not flipped (unproven) |
| Artifact receipts | Not switched (only proven via read_compare_dict in Task 19) | Not flipped (unproven) |
| Bundle/selfcheck evidence | Not switched (only proven via read_compare_dict in Task 19) | Not flipped (unproven) |

Only settlement records are flipped in this task. Other record types were proven via the generic `read_compare_dict()` path in Task 19 but are not wired into a production read switch yet. They will be flipped in a future task after their read paths are proven with settlement-level rigor.

## Flags-off behavior

- Legacy JSONL reads remain source of truth.
- No Postgres read attempted.
- Runtime behavior unchanged.
- All existing tests pass.
- `read_all()` calls `_read_all_from_jsonl()` + optional `_read_compare()`.

## Postgres-read behavior

When `QF_POSTGRES_READS_ENABLED=1`:
1. `read_all()` calls `_read_all_from_postgres()`.
2. `read_switch_settlements()` is called with `db_store` and `legacy_reader`.
3. Postgres `list_all()` is called first.
4. If Postgres returns records, they are validated.
5. If all records validate, Postgres records are returned.
6. If read-compare is also on, legacy is read for comparison, evidence is emitted.

## Fallback behavior

When `QF_POSTGRES_READS_ENABLED=1` AND `QF_LEGACY_FILE_READ_FALLBACK=1`:
- Postgres read is attempted first.
- If Postgres returns records and they validate → return Postgres records.
- If Postgres returns empty but legacy has records → return legacy records (fallback).
- If Postgres read errors → return legacy records (fallback) with warning/evidence.
- If Postgres records fail validation → return legacy records (fallback) with validation errors.

## Fallback-disabled behavior

When `QF_POSTGRES_READS_ENABLED=1` AND `QF_LEGACY_FILE_READ_FALLBACK=0`:
- Postgres read is required.
- If Postgres returns records and they validate → return Postgres records.
- If Postgres returns empty → return empty list (Postgres is source of truth).
- If Postgres read errors → raise `ReadSwitchError`.
- If Postgres records fail validation → raise `ReadSwitchError`.
- Legacy is not used silently.

## Read-compare interaction

When both `QF_POSTGRES_READS_ENABLED=1` AND `QF_POSTGRES_READ_COMPARE_ENABLED=1`:
1. Postgres is read (primary).
2. Legacy is read for comparison.
3. Postgres records are returned if valid.
4. Comparison evidence is emitted (mismatches, misses).
5. Mismatches are surfaced but do not override the Postgres result.

When `QF_POSTGRES_READS_ENABLED=0` AND `QF_POSTGRES_READ_COMPARE_ENABLED=1`:
1. Legacy is read (primary).
2. Postgres is read for comparison.
3. Legacy records are returned.
4. Comparison evidence is emitted.
5. This is the Task 19 behavior (unchanged).

## Validation rules

Postgres read results must validate before being returned:

| Check | Rule |
|-------|------|
| Required fields | `prediction_id`, `model_id`, `symbol`, `ts_event`, `horizon_ns`, `status`, `cost_model_version`, `decision_window_start`, `decision_window_end` — all non-None |
| Status domain | Must be one of `pending_time`, `pending_data`, `settled` |
| prediction_id | Non-empty string |
| model_id | Non-empty string |
| cost_model_version | Non-empty string |
| Timestamps | Non-negative integers |

Invalid records are rejected. With fallback on, validation failure falls back to legacy. With fallback off, validation failure raises `ReadSwitchError`.

## Tests added

26 new tests in `test_c10_read_switch.py`:

| Test class | Tests | Coverage |
|-----------|-------|----------|
| `TestFlagsOff` | 3 | Legacy read, no Postgres read, no db_store |
| `TestPostgresReadsEnabled` | 3 | Returns Postgres, different from legacy, multiple records |
| `TestPostgresMissingFallbackOn` | 2 | Missing Postgres, legacy values returned |
| `TestPostgresMissingFallbackOff` | 1 | Missing Postgres, empty result |
| `TestPostgresErrorFallbackOn` | 1 | Error, legacy returned |
| `TestPostgresErrorFallbackOff` | 1 | Error, ReadSwitchError raised |
| `TestPostgresInvalidRecord` | 5 | Invalid record, validation, batch validation |
| `TestReadCompareAndReads` | 2 | Returns Postgres with compare, mismatch detected |
| `TestSettlementReadSwitch` | 2 | End-to-end settle+read, idempotency via Postgres |
| `TestLegacyAPICompatibility` | 3 | Returns list, empty dir, sorted newest-first |
| `TestReadSwitchEvidence` | 3 | to_dict, with error, with validation errors |

3 new tests in `test_c10_flags.py`:
- `test_postgres_read_switch_active_defaults_false`
- `test_read_switch_active_with_fallback_on`
- `test_read_switch_active_with_fallback_off`

## Read-switch proof

**Proof path**: `reports/c10-postgres-sink-flip/f3a33325/read-switch-proof/`

**Proof run**: `uv run python reports/c10-postgres-sink-flip/f3a33325/read-switch-proof/run_proof.py`

| Proof | Outcome | Records Read | Source |
|-------|---------|-------------|--------|
| Flags off -> legacy read | PASS | 5 | jsonl |
| Postgres reads enabled | PASS | 5 | postgres |
| Postgres missing + fallback on | PASS | 5 | legacy_fallback |
| Postgres missing + fallback off | PASS | 0 | postgres_empty |
| Postgres mismatch detected | PASS | 3 | postgres |

**Overall: ALL PASS** — 5/5 proofs passed.

Evidence files:
- `flags_off_legacy_read.json` — 5 records from JSONL
- `postgres_read_enabled.json` — 5 records from Postgres (value=0.07)
- `postgres_missing_fallback_on.json` — 5 records from legacy fallback
- `postgres_missing_fallback_off.json` — 0 records (Postgres empty, no fallback)
- `postgres_mismatch_detected.json` — 3 records from Postgres (value=0.99, not 0.05)
- `summary.json` — machine-readable summary

## Runtime behavior with defaults

All flags default to off/legacy. No runtime behavior change. `read_all()` reads from JSONL. No Postgres read attempted. All existing tests pass unchanged.

## Verification results

| Check | Result |
|-------|--------|
| `ruff format --check .` | 846 files already formatted |
| `ruff check libs services` | All checks passed |
| `mypy libs services` | Success: no issues found in 375 source files |
| `pytest libs/fincept-db/tests` | All passed |
| `pytest services/quant_foundry/tests` | All passed (including 26 new read switch tests) |
| `pytest services/api/tests` | All passed |
| `pytest services/settlements/tests` | All passed (1 pre-existing Windows temp error) |
| **Total** | **5049 passed, 294 skipped, 1 pre-existing error** |

## Risks

1. **Postgres-first with fallback**: When fallback is on and Postgres returns empty, the system falls back to legacy. This means a Postgres outage is masked. Mitigation: the fallback is logged at WARNING level; monitoring should alert on fallback events.

2. **Empty Postgres vs. missing records**: An empty Postgres result (0 records) is treated as "missing" when legacy has records. This is correct for the transition period but could mask a Postgres data loss event. Mitigation: the `postgres_missing` evidence outcome is emitted; monitoring should alert on this.

3. **Validation strictness**: The validation rules are strict — any invalid Postgres record triggers fallback (or failure). This is conservative but could cause unnecessary fallbacks if Postgres has a minor data issue. Mitigation: validation errors are logged with details; the threshold can be adjusted.

4. **Read-compare + reads interaction**: When both are enabled, Postgres is returned even if it differs from legacy. The mismatch is evidence only, not a blocking condition. This is intentional — the read switch trusts Postgres as the primary source. Mitigation: mismatches are logged and counted; operators can review evidence before retiring legacy.

5. **Unproven records not flipped**: Only settlement records are flipped. Callback receipts, dossiers, and model metrics are not flipped because they were only proven via the generic `read_compare_dict()` path, not with settlement-level rigor. They will be flipped in a future task.

## Safe to open PR: yes
