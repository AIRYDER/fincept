# Data Integrity & Schema Audit — ml-dataset-evidence-spine

**Scope:** Changes from commit `7dc5fc1` to `HEAD`
**Reviewer:** Data Integrity & Schema Reviewer (read-only)
**Date:** 2025-01-24
**Working dir:** `C:/Users/nolan/CascadeProjects/fincept-terminal`

---

## Summary

Audited the six focus areas: PredictionRow schema preservation, settlement
store idempotency, settlement worker correctness, feature snapshot store,
dossier/calibration helpers, and CV utility faithfulness.

**1 DATA INTEGRITY BUG** found in `SettlementStore._find` (first-match
semantics break terminal-row protection when a non-terminal row precedes a
terminal one). The settlement worker mitigates this at its own layer, but
the store's public contract is violated. **3 CORRECTNESS CONCERNS** (minor,
edge-case-prone). Everything else **PASS**.

| # | Area | Verdict |
|---|------|---------|
| 1 | PredictionRow schema preservation | PASS |
| 2 | Settlement store idempotency | **DATA INTEGRITY BUG** |
| 3 | Settlement worker correctness | PASS (with concerns) |
| 4 | Feature snapshot store | PASS |
| 5 | Dossier + calibration helpers | PASS (with concern) |
| 6 | CV utility correctness | PASS |

---

## Findings

### F1 — DATA INTEGRITY BUG: `SettlementStore._find` returns first match, breaking terminal-row idempotency after a pending row

**File:** `libs/fincept-core/src/fincept_core/datasets/settlement.py:363-391`
**Severity:** DATA INTEGRITY BUG

`SettlementStore._find` scans the agent ledger and returns the **first**
record matching `(prediction_id, cost_model_version)`:

```python
def _find(self, agent_id, prediction_id, cost_model_version):
    ...
    for line in f:
        ...
        if (rec.prediction_id == prediction_id
                and rec.cost_model_version == cost_model_version):
            return rec          # <-- returns FIRST match
    return None
```

`SettlementStore.append` (line 283-292) uses `_find` to enforce the
terminal-row rule:

```python
existing = self._find(record.agent_id, record.prediction_id, record.cost_model_version)
if existing is not None and existing.status in ("settled", "failed"):
    raise SettlementError("duplicate", ...)
```

**The bug:** When a non-terminal row (`pending_data` or `pending_time`)
precedes a terminal row (`settled` or `failed`) for the same
`(prediction_id, cost_model_version)`, `_find` returns the non-terminal
first match. The terminal check `existing.status in ("settled", "failed")`
evaluates against the **pending** row, not the **settled** row, so the
guard passes and a **duplicate settled row is appended**.

**Reproduction sequence (store-level, no worker involved):**

1. `append(pending_data, cost_model_version="v1.default")` → allowed (no prior)
2. `append(settled, cost_model_version="v1.default")` → `_find` returns the
   `pending_data` row (first match); not terminal → **allowed** (correct:
   pending → settled transition)
3. `append(settled, cost_model_version="v1.default")` → `_find` returns the
   `pending_data` row (still the first match); not terminal → **ALLOWED —
   BUG**: a second settled row is appended despite a settled row already
   existing for this key.

**Why the tests miss it:** `test_settled_is_terminal_rewrite_raises_duplicate`
(line 492-504) only tests the case where the settled row is the **first and
only** row. `test_pending_time_to_settled_allowed_when_window_elapsed`
(line 419-441) tests step 1→2 but never attempts step 2→3. No test covers
the pending→settled→settled sequence.

**Mitigation at the worker layer:** `settlements.worker._existing_status`
(line 96-117) iterates **all** records and keeps the **last** match
(`latest = rec` on every match), so the worker correctly sees `status=
"settled"` after the transition and skips re-settlement. The bug is
therefore not exploitable through the worker's `tick`/`tick_sync` path, but
the store's own public contract ("settled rows are terminal — cannot be
overwritten") is violated for any direct caller of `SettlementStore.append`.

**Recommended fix:** `_find` should return the **last** matching record
(continue scanning after the first match), or `append` should scan all
matches and reject if **any** is terminal.

---

### F2 — PASS: PredictionRow schema preservation

**Files reviewed:**
- `git diff 7dc5fc1..HEAD -- libs/fincept-core/src/fincept_core/prediction_log.py` → **0 lines changed** (empty diff)
- `libs/fincept-core/src/fincept_core/prediction_log.py:86-139` — `PredictionRow` fields unchanged

**Verification:**
- `PredictionRow` fields: `id, agent_id, model_name, ts_recorded, ts_event,
  horizon_ns, symbol, direction, confidence` — **no settlement fields
  added**.
- No `schema_version` field exists on `PredictionRow` (it is a plain
  `@dataclass(frozen=True)`, not a Pydantic model). The plan's guardrail
  that "`PredictionRow.schema_version` stays at 1" is vacuously satisfied
  by absence — there is no field to mutate. The intent (no settlement
  fields leak into the prediction log) is met.
- The settlement side-store carries its own `settlement_schema_version: int
  = 1` (`settlement.py:144`), correctly isolated from the prediction log.

**Verdict:** PASS — schema preservation guardrail upheld.

---

### F3 — PASS (with concerns): Settlement worker correctness

**File:** `services/settlements/src/settlements/worker.py`

**Calculations verified:**
- `realized_return_gross = (close_t2 / close_t1) - 1.0` (line 128) — **CORRECT**
- `realized_return_net = realized_return_gross - (5 + 3) / 10000.0` (line 129-130) — **CORRECT**: 5 bps fee + 3 bps spread = 8 bps = 0.0008
- `brier_component = (prob_up - actual_up) ** 2` (line 135) — **CORRECT**
- `prob_up = (pred.direction + 1.0) / 2.0` clamped via `max(0.0, min(1.0, prob_up))` (line 132-133) — **CORRECT**
- `actual_up = 1 if realized_return_gross > 0 else 0` (line 134) — correct binary label
- **No peek at `decision_window_start_ns` for PnL**: the worker uses `close_t1` at `ts_event` and `close_t2` at `ts_event + horizon_ns` (line 217-218). `decision_window_start_ns` is set to `pred.ts_event` (line 144) but never read back for PnL. **CORRECT**
- **Idempotency on rerun**: `_existing_status` returns the **last** matching status; `if prior == "settled": continue` (line 211-213) skips already-settled predictions. **CORRECT**
- **pending_data → settled transition** (Todo 11): if `prior == "pending_data"` and data still missing, skip (no duplicate pending row, line 221-224); if data now available, append `settled` (line 226-229). **CORRECT**

**Concern F3a — CORRECTNESS CONCERN: `close_t2 == 0` not guarded**
Line 220: `if close_t1 is None or close_t2 is None or close_t1 == 0:`
guards `close_t1 == 0` (prevents division-by-zero) but **not** `close_t2 ==
0`. A zero exit price yields `realized_return_gross = -1.0` (a 100% loss),
which is mathematically valid but almost certainly bad data. A real close
of 0 should arguably be treated as `pending_data` rather than producing a
spurious total-loss settlement.

**Concern F3b — CORRECTNESS CONCERN: `actual_up` boundary at zero return**
Line 134: `actual_up = 1 if realized_return_gross > 0 else 0`. A gross
return of exactly `0.0` is classified as "down" (`actual_up = 0`). This is
a convention choice (strict positivity = up); it is internally consistent
but worth documenting since a flat return being labeled "down" can
surprisingly inflate the Brier component for a model that predicted
`prob_up = 0.5`.

**Verdict:** PASS — all required calculations correct. Two minor edge-case
concerns noted.

---

### F4 — PASS: Feature snapshot store

**File:** `libs/fincept-core/src/fincept_core/datasets/feature_snapshot.py`

**JSONL shape** (line 95-101): each line is
`{"prediction_id": str, "snapshot": <FeatureSnapshot dict>}` where the
snapshot payload is `FeatureSnapshot.model_dump()`. The `prediction_id` is
a sidecar field (not embedded in `FeatureSnapshot` itself, by design —
line 88-92). **CORRECT**.

**`append_if_missing` idempotency** (line 186-219): keyed by
`prediction_id`; lazily loads the seen-set from disk via `_load_seen`
(line 281-306); invalidates the cache on `append` (line 184,
`self._seen.pop(agent_id, None)`). Returns `True` if appended, `False` if
already present. **CORRECT**.

**Malformed-line tolerance** (line 104-120): `_decode_line` returns `None`
on `json.JSONDecodeError`, `KeyError`, `ValueError`, `TypeError`, or a
non-string `prediction_id`. All read paths (`read_for_symbol` line 260-265,
`_load_seen` line 299-301) skip `None` returns. **CORRECT**.

**Look-ahead guard** lives on the schema, not the store:
`FeatureSnapshot._no_lookahead` (`schemas.py:224-231`) rejects any
`FeatureRow` with `ts > decision_time_ns`. The store explicitly documents
it does not re-check (line 136-141). **CORRECT** layering.

**Verdict:** PASS.

---

### F5 — PASS (with concern): Dossier + calibration helpers

**File:** `libs/fincept-core/src/fincept_core/datasets/dossier.py`

**ECE calculation** (line 164): `ece += (count / total) * abs(mean_pred -
mean_actual)` — weighted sum of per-bucket `|mean_pred - mean_actual|`
weighted by bucket fraction `count / total`. **CORRECT** standard ECE.

**Brier score** (line 166-169): `statistics.fmean((p - lab) ** 2 for p, lab
in zip(..., strict=True))` — mean of squared differences between prediction
and label. **CORRECT**.

**Empty input handling** (line 119-120): `if total == 0: return {"buckets":
[], "ece": 0.0, "brier": 0.0}` — no exception. **CORRECT**.

**Length mismatch** (line 112-116): `if len(val_predictions) !=
len(val_labels): raise ValueError(...)`. **CORRECT**. Uses
`zip(..., strict=True)` throughout (line 136, 145, 168) so a length drift
inside the bucket loops would also raise rather than silently truncate.

**Bucket boundaries** (line 127-147): final bucket is closed `[lo, hi]`
(`lo <= p <= hi`); earlier buckets are half-open `[lo, hi)` (`lo <= p <
hi`). **CORRECT** — matches the documented algorithm.

**Concern F5a — CORRECTNESS CONCERN: empty-input short-circuits before
`n_buckets` validation**
Line 119-120 returns the empty result **before** the `n_buckets < 1` check
(line 122-123). So `build_calibration_sidecar(val_predictions=[],
val_labels=[], n_buckets=0)` returns `{"buckets": [], "ece": 0.0, "brier":
0.0}` instead of raising `ValueError`. This is a benign edge case (empty
input with degenerate bucket count), but the validation order is
fragile — a caller passing `n_buckets=0` with non-empty lists would get
the `ValueError`, while the same invalid `n_buckets` with empty lists is
silently accepted.

**Verdict:** PASS — ECE, Brier, empty handling, and length-mismatch all
correct. One minor validation-ordering concern.

---

### F6 — PASS: CV utility correctness

**File:** `libs/fincept-core/src/fincept_core/datasets/cv.py`

**`make_folds`** (line 71-135): compared verbatim against the original at
`7dc5fc1:services/backtester/src/backtester/walk_forward.py`. The fold
math, validation guards, `required` formula
(`train_min_bars + n_folds * (purge_bars + val_bars) + (n_folds - 1) *
embargo_bars`), the `val_end > n_bars` internal-error check, and the
expanding-window step (`train_end = val_end + embargo_bars`) are
**identical**. The only difference is `Fold` is now a Pydantic
`BaseModel(frozen=True, extra="forbid")` instead of a
`@dataclass(frozen=True)` — this does not affect the index math. **CORRECT
port**.

**`derive_walk_forward_window`** (line 186-228): compared verbatim against
the original at
`7dc5fc1:services/quant_foundry/src/quant_foundry/training_manifest.py:347-389`.
The window layout (`test_end = as_of_ts`, `test_start = test_end -
test_window_ns`, `val_end = test_start - label_horizon_ns`, etc.), the
`label_horizon_ns <= 0` and window-positivity guards, and the
`train_start < 0` check are **identical**. `WalkForwardWindow` is now
Pydantic instead of dataclass — no logic impact. **CORRECT port**.

**`fold_iter_to_dicts`** (line 138-144): trivial serializer using
`f.model_dump()` — **CORRECT**.

**Verdict:** PASS — both algorithms are faithful verbatim ports.

---

## Verdict

The evidence-spine implementation is **substantially correct**. The
schema-preservation guardrail is upheld (PredictionRow untouched,
settlement side-store carries its own `settlement_schema_version=1`). The
worker calculations (gross/net return, Brier component, prob_up mapping)
are all correct. The CV utilities are verbatim ports. The dossier ECE/Brier
math is correct.

**One actionable data integrity bug** (F1) should be fixed before the store
is relied upon by any caller other than the settlement worker:
`SettlementStore._find` must return the **last** match (or `append` must
scan for **any** terminal match) to uphold the "settled/failed rows are
terminal" contract when a pending row precedes a terminal row for the same
`(prediction_id, cost_model_version)` key. The settlement worker is not
affected because it uses its own `_existing_status` (last-match) helper,
but the store's public API is broken and the gap is not covered by tests.

The three correctness concerns (F3a, F3b, F5a) are minor edge-case issues
that do not corrupt data under normal operation but should be documented or
hardened.
