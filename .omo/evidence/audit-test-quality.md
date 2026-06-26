# Test Quality & Coverage Audit — ml-dataset-evidence-spine

**Scope:** Changes from commit `7dc5fc1` to `HEAD` (20 commits, 44 files changed, +7149/-194 lines)
**Date:** 2026-06-26
**Reviewer:** Automated test-quality audit
**Verdict:** PASS (with minor advisories)

---

## Summary

This audit reviews the test quality and coverage of the ml-dataset-evidence-spine
implementation across 17 new/modified test files spanning 6 packages. All 194
collected tests in scope pass, and all 6 full package test suites pass green
(2031 total tests: 2029 passed, 2 skipped due to missing optional `onnxruntime`).

Every test file meets or exceeds the plan's acceptance-criteria test count.
Edge-case coverage is thorough — boundary conditions, failure paths, and
malformed-input handling are well represented. Test isolation is strong with
universal `tmp_path` usage. Two minor advisories are noted below (one
isolation risk, one coverage gap) but neither blocks acceptance.

---

## Test Count vs. Required Count

| Test File | Actual Count | Required | Status |
|---|---|---|---|
| `test_approved_roots.py` | 13 | >= 8 | PASS |
| `test_datasets_schemas.py` | 10 | >= 6 | PASS |
| `test_settlement_ledger.py` | 31 | >= 10 | PASS |
| `test_feature_snapshots.py` | 12 | >= 5 | PASS |
| `test_datasets_init.py` | 6 (incl. import assertion) | import assertion | PASS |
| `test_datasets_dossier.py` | 9 | >= 4 | PASS |
| `test_cv.py` (make_folds) | 14 | >= 8 | PASS |
| `test_cv.py` (derive) | 7 | >= 3 | PASS |
| `test_models_train.py` | 5 (new) | >= 2 new | PASS |
| `test_models_outcomes.py` | 11 | >= 4 | PASS |
| `test_backtest.py` | 4 (new TestApprovedRootsGate) | >= 2 new | PASS |
| `test_gbm_feature_health.py` | 9 | >= 4 | PASS |
| `test_gateway_callbacks.py` | 5 (incl. no-compat test) | no-compat test | PASS |
| `test_callback_metrics.py` | 10 | >= 4 | PASS |
| `test_gateway_runpod_loop.py` | 6 (incl. durable health) | durable health test | PASS |
| `test_worker.py` | 15 | >= 5 | PASS |
| `test_logreg_baseline.py` | 3 | >= 3 | PASS |
| `test_gbm_train.py` | 10 (incl. embargo test) | embargo test | PASS |
| **TOTAL** | **194** | — | **ALL PASS** |

---

## Full Suite Regression Results

| Package | Tests | Result |
|---|---|---|
| `libs/fincept-core/tests/` | 285 | 285 passed |
| `services/api/tests/` | 466 | 466 passed |
| `services/agents/tests/` | 141 | 141 passed |
| `services/backtester/tests/` | 198 | 198 passed |
| `services/quant_foundry/tests/` | 926 | 926 passed, 2 skipped (onnxruntime) |
| `services/settlements/tests/` | 15 | 15 passed |
| **TOTAL** | **2031** | **2029 passed, 2 skipped, 0 failed** |

---

## Findings

### F-1: ISOLATION RISK (Low) — `_can_symlink()` probe writes to CWD

**File:** `libs/fincept-core/tests/test_approved_roots.py`, lines 43-56

The `_can_symlink()` helper probes symlink capability by creating temporary
files (`_aprobed_symlink`, `_aprobed_target`) in `os.getcwd()` — the repo
working directory — rather than in `tmp_path`. The `finally` block cleans
up, but if the test process is killed between creation and cleanup, stale
files could be left in the repo root.

**Severity:** Low. The files are uniquely prefixed (`_aprobed_`) and cleaned
in a `finally` block with `contextlib.suppress(OSError)`. The practical risk
is minimal, but the pattern violates the "tests never write to production
paths" principle.

**Recommendation:** Pass `tmp_path` into `_can_symlink()` or use
`pathlib.Path(tempfile.gettempdir())` for the probe location.

---

### F-2: COVERAGE GAP (Low) — No test for concurrent/interleaved appends to JSONL stores

**Files:** `test_settlement_ledger.py`, `test_feature_snapshots.py`,
`test_callback_metrics.py`

All three append-only JSONL stores (`SettlementStore`, `FeatureSnapshotStore`,
`CallbackMetricsStore`) are tested for single-writer append + read round-trips
and malformed-line resilience, but there is no test for what happens when two
writers append to the same agent file simultaneously. While the production
code likely uses simple `open(path, "a")` which is atomic on most OSes for
small writes, an explicit test would lock this invariant.

**Severity:** Low. The stores are designed for single-process append; the
worker and publish loop are the only writers. Concurrent access is not a
documented use case.

**Recommendation:** Consider adding a test that verifies line-atomicity
(e.g., interleaved appends from two `SettlementStore` instances pointing at
the same root produce valid JSONL with no partial lines).

---

### F-3: PASS — Boundary conditions thoroughly tested

**Files:** All settlement and CV test files

Boundary conditions are tested comprehensively:
- `test_look_ahead_boundary_succeeds`: `decision_window_end_ns == now_ns` (line 187-199)
- `test_decision_window_start_equals_end_allowed`: zero-length window (line 595-602)
- `test_cost_breakdown_spread_bps_at_100_allowed`: boundary at exactly 100 bps (line 569-575)
- `test_cost_breakdown_spread_bps_over_100_rejected`: just over boundary (line 552-566)
- `test_rejects_invalid_args` parametrized: n_folds=0, train_min_bars=0, val_bars=0, purge_bars=-1, embargo_bars=-1
- `test_outcomes_rejects_out_of_range_limit`: parametrized [0, -1, 1001] — covers zero, negative, and over-max

---

### F-4: PASS — Failure paths well covered

**Files:** All test files

Failure paths are tested across all layers:
- Malformed JSONL: `test_malformed_jsonl_line_skipped` (settlement, feature_snapshots, callback_metrics, outcomes)
- Missing files: `test_read_for_agent_missing_file_returns_empty`, `test_read_missing_file_returns_empty`, `test_outcomes_missing_prediction_file_returns_empty`
- Write failures: `test_feature_health_write_failure_does_not_crash_inference` (simulates unwritable dir via file-as-dir blocker)
- Bad agent_id: `test_bad_agent_id_rejected` (settlement), `test_append_bad_agent_id_raises` (feature_snapshots), `test_feature_health_log_rejects_bad_agent_id`
- Invalid inputs: empty prediction_id, missing required fields, extra keys, bad schema hashes, negative timestamps
- Security: bad signature, ts skew, unsigned callback shape, traversal paths, absolute paths outside roots

---

### F-5: PASS — Windows clock-granularity flakiness fix is correct

**File:** `services/api/tests/test_models_outcomes.py`, lines 205-236

The `test_outcomes_since_ns_filters_predictions` test was fixed for Windows
clock granularity (~15ms). Instead of asserting a fixed count (e.g., "3
predictions after cutoff"), it computes the expected count dynamically:

```python
expected = sum(1 for p in all_preds if p.ts_recorded >= cutoff)
assert body["count"] == expected
assert body["count"] >= 1
```

This is the correct pattern — it accounts for ties at the cutoff boundary
that occur when predictions are written in a tight loop on platforms with
coarse clock resolution. No other time-dependent tests exhibit similar
flakiness risk:
- `test_callback_metrics.py` uses explicit `ts_ns` values and `time.time_ns()` only for "now" anchors, not for count assertions.
- `test_gateway_callbacks.py::test_ts_skew_rejected` uses `int(time.time()) - (MAX_TS_SKEW_SECONDS + 60)` which is deterministic relative to current time.
- `test_gateway_runpod_loop.py` uses `int(time.time())` for callback_ts which is within the skew window by construction.

---

### F-6: PASS — Test isolation is strong

**Files:** All test files

- Universal use of `tmp_path` fixture for all filesystem operations.
- `monkeypatch` used for env vars (`FINCEPT_APPROVED_DATA_ROOTS`), `chdir`, and module attribute patches — all auto-reverted by pytest.
- `test_backtest.py` uses `autouse=True` fixture `_patch_reports_root` to redirect the reports root for every test in the file.
- `test_models_train.py` and `test_models_outcomes.py` monkeypatch module-level factory functions (`_get_approved_roots`, `_get_prediction_log`, `_get_settlement_store`) to redirect at tmp dirs.
- `test_gbm_feature_health.py` uses `fakeredis.aioredis.FakeRedis` for the online store — no real Redis dependency.
- No tests write to production `data/`, `models/`, or `reports/` directories.
- The only exception is F-1 above (`_can_symlink` probe in CWD), which is low-risk and cleaned up.

---

### F-7: PASS — Test naming and organization are consistent

**Files:** All test files

- Test names follow `test_<behavior>_<condition>` or `test_<subject>_<expected_result>` patterns consistently.
- Tests are organized with clear section separators (`# --- Section Name ---`) matching the plan's todo structure.
- Class-based grouping used where logical: `TestMakeFolds`, `TestFold`, `TestDeriveWalkForwardWindow`, `TestStrategiesEndpoint`, `TestRunEndpoint`, `TestRunsListAndDetail`, `TestApprovedRootsGate`.
- Parametrized tests used effectively: `test_rejects_invalid_args[kwargs0-n_folds]`, `test_outcomes_rejects_out_of_range_limit[0]`.
- Docstrings on every test explain the intent and reference the plan todo number.

---

### F-8: PASS — Fixture quality is high

**Files:** All test files

- Reusable helper functions (`_make_record`, `_snapshot`, `_row`, `_synthetic_frame`, `_price_source`, `_async_price_source`, `_seed_predictions`, `_make_settlement`) reduce duplication.
- Fixtures are scoped appropriately: module-level `data_root`/`gate` fixtures in test_approved_roots, function-level `tmp_path`-based fixtures elsewhere.
- The `patched_training_with_roots` fixture in test_models_train.py is self-contained and documented as mirroring the `patched_training` pattern from test_training.py.
- The `approved_data_root` fixture in test_backtest.py uses `monkeypatch.setenv` rather than writing real files under `<repo>/data/` — explicitly documented as avoiding workspace pollution.
- No unnecessary fixture duplication across files; each file's fixtures are local and tailored.
- `_FakeAgent` and `_RecordingProducer` in test_gbm_feature_health.py are well-designed test doubles that expose exactly the interface the publish loop needs.

---

### F-9: PASS — Calibration tests use deterministic seeds

**File:** `libs/fincept-core/tests/test_datasets_dossier.py`, lines 199-211

The `test_calibration_ece_well_calibrated_approx_005` test uses
`random.Random(20240626)` with a fixed seed, ensuring deterministic output
across runs. The tolerance is generous (`< 0.15` for ECE, `abs=0.05` for
Brier) to absorb any remaining sampling noise. The `test_calibration_brier_skewed_048`
test uses fully deterministic synthetic data (no RNG). No flakiness risk.

---

## Verdict

**PASS**

All 17 test files meet or exceed the plan's acceptance-criteria test counts.
The full test suite (2031 tests across 6 packages) passes with zero failures.
Edge-case coverage is thorough, failure paths are well tested, test isolation
is strong, and the Windows clock-granularity flakiness fix is correct. Two
minor advisories (F-1 isolation risk, F-2 coverage gap) are noted for future
hardening but do not block acceptance.
