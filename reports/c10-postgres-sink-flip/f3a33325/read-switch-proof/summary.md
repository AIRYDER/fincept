# C10 Postgres Read Switch Proof — Summary

## Branch SHA

`f3a33325` (feature/c10-postgres-sink-skeleton)

## Proof Run

```
uv run python reports/c10-postgres-sink-flip/f3a33325/read-switch-proof/run_proof.py
```

## Results

| Proof | Outcome | Records Read | Source |
|-------|---------|-------------|--------|
| Flags off -> legacy read | PASS | 5 | jsonl |
| Postgres reads enabled | PASS | 5 | postgres |
| Postgres missing + fallback on | PASS | 5 | legacy_fallback |
| Postgres missing + fallback off | PASS | 0 | postgres_empty |
| Postgres mismatch detected | PASS | 3 | postgres |

## Overall

**ALL PASS** — 5/5 proofs passed.

## Key Invariants Verified

1. **Flags off**: Legacy JSONL reads are source of truth. No Postgres read.
2. **Postgres reads enabled**: Postgres records are returned (value=0.07, not legacy value).
3. **Postgres missing + fallback on**: Legacy records returned when Postgres is empty.
4. **Postgres missing + fallback off**: Empty result (Postgres is source of truth, no silent legacy).
5. **Postgres mismatch**: Postgres value (0.99) returned, not legacy (0.05). Mismatch is visible via read-compare evidence.

## Evidence Files

- `flags_off_legacy_read.json` — 5 records from JSONL
- `postgres_read_enabled.json` — 5 records from Postgres (value=0.07)
- `postgres_missing_fallback_on.json` — 5 records from legacy fallback
- `postgres_missing_fallback_off.json` — 0 records (Postgres empty, no fallback)
- `postgres_mismatch_detected.json` — 3 records from Postgres (value=0.99, not 0.05)
- `summary.json` — machine-readable summary
