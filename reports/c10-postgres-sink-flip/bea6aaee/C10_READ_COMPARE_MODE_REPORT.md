# C10 Read-Compare Mode Report

## Branch SHA

`bea6aaee` (feature/c10-postgres-sink-skeleton)

## Files changed

| File | Status |
|------|--------|
| `services/quant_foundry/src/quant_foundry/c10_flags.py` | modified — added `QF_POSTGRES_READ_COMPARE_ENABLED` and `QF_DUAL_WRITE_FAIL_HARD` flags, `postgres_read_compare_enabled()`, `dual_write_fail_hard()`, `should_read_compare()` |
| `services/quant_foundry/src/quant_foundry/read_compare.py` | new — read-compare module: normalization, comparison, evidence, counters, settlement + dict coordinators |
| `services/quant_foundry/src/quant_foundry/settlement.py` | modified — `read_all()` calls `_read_compare()` after collecting legacy records; added `_read_compare()` method |
| `services/quant_foundry/tests/test_c10_read_compare.py` | new — 44 read-compare tests |
| `services/quant_foundry/tests/test_c10_flags.py` | modified — added tests for new flags |
| `reports/c10-postgres-sink-flip/bea6aaee/read-compare-proof/run_proof.py` | new — deterministic proof script |
| `reports/c10-postgres-sink-flip/bea6aaee/read-compare-proof/proof_output.txt` | new — proof output |
| `reports/c10-postgres-sink-flip/bea6aaee/read-compare-proof/read_compare_match.json` | new — match evidence |
| `reports/c10-postgres-sink-flip/bea6aaee/read-compare-proof/read_compare_missing_postgres.json` | new — miss evidence |
| `reports/c10-postgres-sink-flip/bea6aaee/read-compare-proof/read_compare_mismatch.json` | new — mismatch evidence |
| `reports/c10-postgres-sink-flip/bea6aaee/read-compare-proof/read_compare_error.json` | new — error evidence |
| `reports/c10-postgres-sink-flip/bea6aaee/read-compare-proof/summary.json` | new — machine-readable summary |
| `reports/c10-postgres-sink-flip/bea6aaee/read-compare-proof/summary.md` | new — human-readable summary |

## Feature flags

| Flag | Default | Purpose |
|------|---------|---------|
| `QF_POSTGRES_SINK_ENABLED` | `0` (off) | Enable Postgres dual-write |
| `QF_DUAL_WRITE_SETTLEMENTS` | `0` (off) | Continue JSONL writes alongside Postgres |
| `QF_POSTGRES_READ_COMPARE_ENABLED` | `0` (off) | Compare legacy reads against Postgres reads |
| `QF_POSTGRES_READS_ENABLED` | `0` (off) | Serve reads from Postgres (not enabled yet) |
| `QF_LEGACY_FILE_READ_FALLBACK` | `1` (on) | Emergency rollback to JSONL reads |
| `QF_DUAL_WRITE_FAIL_HARD` | `0` (off) | Re-raise dual-write/read-compare errors |

## Records compared

| Record type | Method | Status |
|------------|--------|--------|
| Settlement records | `read_compare_settlement()` / `read_compare_settlement_batch()` | Implemented + tested + proofed |
| Callback receipts | `read_compare_dict()` | Implemented + tested (generic dict path) |
| Dossier records | `read_compare_dict()` | Implemented + tested (generic dict path) |
| Model metrics | `read_compare_dict()` | Implemented + tested (generic dict path) |

Settlement records use the dedicated `read_compare_settlement()` coordinator
which calls `DbSettlementStore.get(prediction_id, cost_model_version)` and
compares via `compare_settlement_records()`. Other record types use the
generic `read_compare_dict()` coordinator which accepts any dict-shaped
record and a callable DB getter.

## Normalization rules

| Field type | Rule |
|-----------|------|
| Field names | As-is (dataclass field names) |
| Timestamps | `int()` — BigInteger ns, no transformation |
| Float precision | Rounded to 12 decimal places |
| Cost model fields | String, stripped |
| Status strings | Stripped, compared as string values |
| model_id / legacy_agent_id | No mapping needed (same field) |
| artifact_uri formatting | N/A for settlement records |
| sha256 casing | N/A for settlement records |
| Optional/null fields | Preserved as `None` |
| Metadata ordering | N/A (no metadata dict on SettlementRecord) |

Normalization does NOT hide meaningful differences:
- Different `realized_return_gross` values (beyond 12 decimal places) are detected.
- Different `status` values are detected.
- Different `cost_model_version` values produce different record keys.
- Missing fields (present in one, absent in other) are detected.

## Match behavior

When `QF_POSTGRES_READ_COMPARE_ENABLED=1`:
1. Legacy record is read from JSONL (via `read_all()`).
2. Postgres record is read via `db_store.get(prediction_id, cost_model_version)`.
3. Both records are normalized via `normalize_settlement_record()`.
4. SHA-256 hashes are computed for both normalized dicts.
5. If hashes match → `outcome="match"`, counter incremented, debug log.
6. Legacy record is returned to caller.

## Missing Postgres behavior

When legacy exists but Postgres is missing:
1. Legacy record is read from JSONL.
2. Postgres `get()` returns `None`.
3. Evidence: `outcome="read_compare_miss"`, `postgres_hash=None`, `legacy_hash` computed.
4. Counter `misses` incremented, WARNING log.
5. Legacy record is returned to caller.
6. No silent pass — the miss is counted and logged.

## Mismatch behavior

When both exist but differ:
1. Legacy record is read from JSONL.
2. Postgres record is read.
3. Both are normalized and hashed.
4. Hashes differ → `outcome="read_compare_mismatch"`.
5. Field-level diff is computed: `field_diffs` dict with `{field: (legacy_value, postgres_value)}`.
6. Both `legacy_hash` and `postgres_hash` are included.
7. Counter `mismatches` incremented, ERROR log with diff field names.
8. Legacy record is returned to caller — Postgres data is NOT returned.

## Postgres read error behavior

When Postgres read errors:
1. Legacy record is read from JSONL (succeeds).
2. Postgres `get()` raises an exception.
3. Evidence: `outcome="read_compare_error"`, `error_class` and `error_message` included.
4. Counter `errors` incremented, ERROR log.
5. Legacy record is returned to caller.
6. In fail-hard mode (`QF_DUAL_WRITE_FAIL_HARD=1`), the exception is re-raised.
7. No secrets are exposed — only the error class name and safe message string.

## Tests added

44 new tests in `test_c10_read_compare.py`:

| Test class | Tests | Coverage |
|-----------|-------|----------|
| `TestFlagsOff` | 4 | No Postgres read when flag off, counters zero |
| `TestReadCompareMatch` | 3 | Match evidence, batch match, direct match |
| `TestReadCompareMissing` | 3 | Missing Postgres, direct miss, legacy returned |
| `TestReadCompareMismatch` | 4 | Field mismatch, hash mismatch, legacy returned, counters |
| `TestReadCompareError` | 3 | Error evidence, legacy returned, fail-hard re-raise |
| `TestLegacyAlwaysReturned` | 4 | Legacy returned on match/miss/mismatch/error |
| `TestNoPostgresResultReturned` | 2 | Reads flag off, JSONL not Postgres |
| `TestStructuredEvidence` | 4 | to_dict, to_json, miss, error |
| `TestSettlementReplayZeroDivergences` | 1 | 10 records, 0 divergences |
| `TestNormalization` | 4 | Normalize, float precision, status, null fields |
| `TestDictReadCompare` | 6 | Dict match/mismatch/missing/error/float/fail-hard |
| `TestBatchReadCompare` | 1 | Mixed outcomes in batch |

6 new tests in `test_c10_flags.py`:
- `test_postgres_read_compare_enabled_defaults_off`
- `test_dual_write_fail_hard_defaults_off`
- `test_should_read_compare_defaults_false`
- `test_read_compare_on_reads_off`
- `test_read_compare_on_reads_on`
- (updated `clean_env` fixture to include new flags)

## Read-compare proof

**Proof path**: `reports/c10-postgres-sink-flip/bea6aaee/read-compare-proof/`

**Proof run**: `uv run python reports/c10-postgres-sink-flip/bea6aaee/read-compare-proof/run_proof.py`

| Proof | Outcome | Matches | Misses | Mismatches | Errors | Legacy Returned |
|-------|---------|---------|--------|------------|--------|-----------------|
| Match | PASS | 10 | 0 | 0 | 0 | Yes |
| Missing Postgres | PASS | 0 | 5 | 0 | 0 | Yes |
| Mismatch | PASS | 0 | 0 | 3 | 0 | Yes (0.05, not 0.99) |
| Error | PASS | 0 | 0 | 0 | 3 | Yes |

**Overall: ALL PASS** — 4/4 proofs passed.

Evidence files:
- `read_compare_match.json` — 10 records, all match
- `read_compare_missing_postgres.json` — 5 legacy records, Postgres empty, all misses detected
- `read_compare_mismatch.json` — 3 records with differing `realized_return_gross`, all mismatches detected, legacy (0.05) returned not Postgres (0.99)
- `read_compare_error.json` — 3 records with simulated DB error, all errors detected, legacy returned
- `summary.json` — machine-readable summary

## Runtime behavior with flags off

No runtime behavior change. No Postgres read attempted. Legacy JSONL reads remain canonical. All existing tests pass unchanged. Counters remain at zero.

## Runtime behavior with read-compare on

- `read_all()` reads from JSONL (legacy canonical) first.
- After collecting all legacy records, `_read_compare()` is called.
- For each legacy record, the Postgres record with the same key is read.
- Both records are normalized and compared via SHA-256 hash.
- Evidence is emitted (match/miss/mismatch/error) and counters updated.
- The legacy record is always returned to the caller.
- No Postgres data is returned while `QF_POSTGRES_READS_ENABLED=0`.
- If `QF_POSTGRES_READS_ENABLED=1` (reads flipped), read-compare is a no-op (moot).

## Verification results

| Check | Result |
|-------|--------|
| `ruff format --check .` | 843 files already formatted |
| `ruff check libs services` | All checks passed |
| `mypy libs services` | Success: no issues found in 374 source files |
| `pytest libs/fincept-db/tests` | All passed |
| `pytest services/quant_foundry/tests` | All passed (including 44 new read-compare tests) |
| `pytest services/api/tests` | All passed |
| `pytest services/settlements/tests` | All passed (1 pre-existing Windows temp error) |
| **Total** | **5020 passed, 294 skipped, 1 pre-existing error** |

## Risks

1. **Performance**: Read-compare doubles the read load (JSONL + Postgres). This is acceptable for a transitional verification mode but should not be left on indefinitely in production. Mitigation: flag is off by default; can be toggled per-environment.

2. **Log volume**: Mismatches and errors are logged at ERROR/WARNING level. In a large-scale deployment with many mismatches, this could produce significant log volume. Mitigation: counters provide aggregate metrics; log level can be tuned.

3. **Normalization edge cases**: Float rounding to 12 decimal places may mask very small precision differences. This is intentional — Decimal-to-float conversion in the DB sink introduces sub-12-place noise that should not be treated as a mismatch. Mitigation: the threshold is conservative; any meaningful difference in returns will exceed 12 decimal places.

4. **Dict-based read-compare**: The generic `read_compare_dict()` path assumes the DB getter returns a dict or an object with `to_dict()` or `asdict()`. If a DB store returns an unexpected type, the comparison will fall through to `dict(pg_record)` which may fail. Mitigation: tested with dict, MagicMock, and dataclass inputs.

## Safe to open PR: yes

## Recommended next step

Proceed to Task 20 — C10 Postgres read switch behind feature flag.
