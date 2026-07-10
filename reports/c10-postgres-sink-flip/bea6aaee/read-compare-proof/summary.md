# C10 Read-Compare Proof — Summary

## Branch SHA

`bea6aaee` (feature/c10-postgres-sink-skeleton)

## Proof Run

```
uv run python reports/c10-postgres-sink-flip/bea6aaee/read-compare-proof/run_proof.py
```

## Results

| Proof | Outcome | Matches | Misses | Mismatches | Errors | Legacy Returned |
|-------|---------|---------|--------|------------|--------|-----------------|
| Match | PASS | 10 | 0 | 0 | 0 | Yes |
| Missing Postgres | PASS | 0 | 5 | 0 | 0 | Yes |
| Mismatch | PASS | 0 | 0 | 3 | 0 | Yes (0.05, not 0.99) |
| Error | PASS | 0 | 0 | 0 | 3 | Yes |

## Overall

**ALL PASS** — 4/4 proofs passed.

## Evidence Files

- `read_compare_match.json` — 10 records, all match
- `read_compare_missing_postgres.json` — 5 legacy records, Postgres empty, all misses detected
- `read_compare_mismatch.json` — 3 records with differing `realized_return_gross`, all mismatches detected, legacy (0.05) returned not Postgres (0.99)
- `read_compare_error.json` — 3 records with simulated DB error, all errors detected, legacy returned
- `summary.json` — machine-readable summary

## Key Invariants Verified

1. **Legacy always returned**: In all 4 proofs, the legacy record was returned to the caller.
2. **No Postgres data returned**: In the mismatch proof, Postgres value (0.99) was never returned; legacy value (0.05) was always returned.
3. **No silent mismatch**: All mismatches, misses, and errors were counted and logged.
4. **Field-level diff**: Mismatch evidence includes the differing field name (`realized_return_gross`).
5. **Hash comparison**: Both legacy and Postgres hashes are included in mismatch evidence.
6. **Error class/message**: Error evidence includes `RuntimeError` and the safe error message.
