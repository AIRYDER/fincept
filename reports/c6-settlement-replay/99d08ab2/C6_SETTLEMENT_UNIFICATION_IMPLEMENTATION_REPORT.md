# C6 Settlement Unification Implementation Report

## Branch SHA

`99d08ab2` (base) → implementation on `feature/c6-settlement-unification`

## Files changed

### New files

| File | Purpose |
|------|---------|
| `services/settlements/src/settlements/compat.py` | PathACompatAdapter — compatibility wrapper over Path B |
| `services/settlements/tests/test_compat.py` | 34 tests for the compat adapter |
| `scripts/c6_post_unification_replay.py` | Post-unification replay verification script |
| `reports/c6-settlement-replay/99d08ab2/post-unification/post_unification_replay.json` | Replay results |
| `reports/c6-settlement-replay/99d08ab2/post-unification/post_unification_divergences.json` | Divergence report (empty) |
| `reports/c6-settlement-replay/99d08ab2/post-unification/post_unification_normalized.csv` | Normalized CSV |
| `reports/c6-settlement-replay/99d08ab2/post-unification/fixtures.json` | Fixtures used |

### Modified files

| File | Change |
|------|--------|
| `services/settlements/src/settlements/__init__.py` | Export compat adapter symbols |
| `services/api/src/api/settlements_poller.py` | Rewire poller to use Path B via adapter with `SETTLEMENTS_USE_PATH_B` feature flag |
| `services/api/src/api/routes/models.py` | Add `_get_settlement_ledger`, `_use_path_b_settlement`; update `get_outcomes` to read from Path B when flag is on |
| `libs/fincept-core/src/fincept_core/datasets/__init__.py` | Update `build_evidence_receipt` to accept both Path A and Path B records; add `abnormal_return`, `calibration_bucket`, `cost_model_version`, `cost_fee_bps`, `cost_spread_bps`, `cost_slippage_bps` fields |
| `services/api/tests/test_models_outcomes.py` | Update `patched_stores` fixture to also patch `_get_settlement_ledger`; add `_seed_settlement_both_stores` helper; update 2 tests to seed both stores |

## Canonical ledger implementation

**Path B — `quant_foundry.SettlementLedger`** is the sole settlement math source.

The `PathACompatAdapter` in `services/settlements/src/settlements/compat.py`:

1. Accepts `PredictionRow` inputs (agent_id keyed, no `p_up`)
2. Maps `agent_id` → `model_id` via `default_agent_to_model_id` (replaces `.` with `-`)
3. Derives `p_up` from `direction` + `confidence` via `derive_p_up_from_confidence`
4. Builds a `PredictionInput` dict for Path B
5. Delegates to `SettlementLedger.settle()` with `cm-v1` cost model
6. Translates the Path B `SettlementRecord` back to Path A shape via `path_b_to_path_a_record`
7. Optionally writes to the legacy `SettlementStore` for backward-compatible reads

No duplicate settlement math — Path B is the sole computation engine.

## Path A compatibility wrapper

The `PathACompatAdapter` class provides:

- `settle_prediction(pred, prices, benchmark_prices, now_ns, holding_days)` — settle a single prediction
- `settle_due_predictions(predictions_dir, now_ns, market_data_source, ...)` — sync batch settlement
- `settle_due_predictions_async(predictions_dir, now_ns, market_data_source, ...)` — async batch settlement

The adapter also provides:
- `default_agent_to_model_id(agent_id)` — default mapping (replace `.` with `-`)
- `default_model_to_agent_id(model_id)` — reverse mapping
- `derive_p_up_from_confidence(direction, confidence)` — p_up derivation
- `path_b_to_path_a_record(b_record, agent_id, model_name, cost_model)` — record translation

The legacy `settlements.worker.tick` and `tick_sync` functions are preserved for rollback (`SETTLEMENTS_USE_PATH_B=0`).

## API compatibility

The `/models/{name}/outcomes` route continues to return the expected response shape. When `SETTLEMENTS_USE_PATH_B=1` (default):

1. The route reads from `SettlementLedger` (Path B) filtered by `model_id` (mapped from `agent_id`)
2. `build_evidence_receipt` handles both Path A and Path B record shapes
3. Existing fields are preserved: `prediction_id`, `agent_id`, `model_name`, `ts_event`, `horizon_ns`, `symbol`, `direction`, `confidence`, `settlement_status`, `realized_return_gross`, `realized_return_net`, `settled_at_ns`, `brier_component`
4. New fields added (non-breaking, null when unavailable): `abnormal_return`, `calibration_bucket`, `cost_model_version`, `cost_fee_bps`, `cost_spread_bps`, `cost_slippage_bps`

All 13 existing outcomes tests pass.

## Identity mapping

- `model_id` is canonical
- `agent_id` is a legacy compatibility input
- `default_agent_to_model_id(agent_id)` replaces `.` with `-` (e.g. `gbm_predictor.v1` → `gbm_predictor-v1`)
- Mapping is deterministic and configurable (custom mapping function can be passed to the adapter)
- Missing mapping (empty string) raises `ValueError` — fails clearly, does not silently invent a model_id

## Cost model

- Canonical cost model: `cm-v1` (fee 10 bps, spread 5 bps, slippage 3 bps, borrow 25 bps/day)
- `cost_model_version` is stored on every settled record
- Cost model fields (`cost_fee_bps`, `cost_spread_bps`, `cost_slippage_bps`) are included in the API response via `build_evidence_receipt`
- Borrow cost applies only to short positions (`direction < 0`)
- Cost model versioning: re-settling with a different cost model version appends a new record (tested)

## Store location

- Canonical store: `data/quant-foundry/settlements/<model_id>.settlements.jsonl` (Path B)
- Legacy store: `data/settlements/<agent_id>.jsonl` (Path A) — still written to for backward-compatible reads
- The adapter writes to both stores during the migration period
- `SETTLEMENTS_USE_PATH_B=0` falls back to reading from the legacy store

## Output schema

The canonical output includes all required fields:

| Field | Source | Status |
|-------|--------|--------|
| `status` | `SettlementRecord.status` | Present |
| `realized_return_gross` | Path B `realized_return` (direction-aware) | Present |
| `realized_return_net` | Path B `apply_costs` (cm-v1) | Present |
| `abnormal_return` | Path B `abnormal_return` | Present |
| `brier` | Path B `brier_score` (uses `p_up`) | Present |
| `calibration_bucket` | Path B `calibration_bucket` | Present |
| `settled_at_ns` | `SettlementRecord.settled_at_ns` | Present |
| `cost_model_version` | `"cm-v1"` | Present |
| `cost_fee_bps` | `CostModel.fee_bps` (10.0) | Present |
| `cost_spread_bps` | `CostModel.spread_bps` (5.0) | Present |
| `cost_slippage_bps` | `CostModel.slippage_bps` (3.0) | Present |
| `borrow_bps_per_day` | `CostModel.borrow_bps_per_day` (25.0) | Supported (applied to shorts) |
| `decision_window_start_ns` | `ts_event` | Present |
| `decision_window_end_ns` | `ts_event + horizon_ns` | Present |
| `model_id` | Canonical key | Present (Path B) |
| `legacy_agent_id` | Preserved via adapter | Present (Path A compat) |
| `metadata` | Adapter adds `legacy_agent_id` | Present |

## Tests added/updated

### New tests: `services/settlements/tests/test_compat.py` (34 tests)

| # | Test | Class | Covers |
|---|------|-------|--------|
| 1 | `test_default_agent_to_model_id_replaces_dots` | TestIdentityMapping | agent_id → model_id mapping |
| 2 | `test_default_agent_to_model_id_no_dots` | TestIdentityMapping | No-op mapping |
| 3 | `test_default_agent_to_model_id_empty_raises` | TestIdentityMapping | Empty mapping fails |
| 4 | `test_default_model_to_agent_id_replaces_dashes` | TestIdentityMapping | Reverse mapping |
| 5 | `test_default_model_to_agent_id_empty_raises` | TestIdentityMapping | Empty reverse mapping fails |
| 6 | `test_custom_mapping_function` | TestIdentityMapping | Custom mapping |
| 7 | `test_long_uses_confidence` | TestDerivePUp | p_up for longs |
| 8 | `test_short_uses_one_minus_confidence` | TestDerivePUp | p_up for shorts |
| 9 | `test_flat_uses_half` | TestDerivePUp | p_up for flat |
| 10 | `test_long_clamped_to_1` | TestDerivePUp | p_up clamping |
| 11 | `test_short_clamped_to_0` | TestDerivePUp | p_up clamping |
| 12 | `test_winning_long` | TestSettlementScenarios | Long, price +5% |
| 13 | `test_losing_long` | TestSettlementScenarios | Long, price -5% |
| 14 | `test_winning_short` | TestSettlementScenarios | Short, price -5% → POSITIVE gross/net |
| 15 | `test_losing_short` | TestSettlementScenarios | Short, price +5% → NEGATIVE gross/net |
| 16 | `test_flat` | TestSettlementScenarios | No movement → net = -costs |
| 17 | `test_missing_prices` | TestSettlementScenarios | No prices → pending_data |
| 18 | `test_partial_prices_entry_only` | TestSettlementScenarios | Entry only → pending_data |
| 19 | `test_high_confidence_win` | TestSettlementScenarios | High confidence +10% |
| 20 | `test_brier_uses_p_up_not_direction` | TestBrierScore | Brier uses p_up, not (direction+1)/2 |
| 21 | `test_brier_changes_with_confidence` | TestBrierScore | Brier changes with confidence |
| 22 | `test_brier_short_uses_one_minus_confidence` | TestBrierScore | Brier for shorts |
| 23 | `test_abnormal_return_populated` | TestAbnormalReturnAndCalibration | abnormal_return = realized - benchmark |
| 24 | `test_abnormal_return_none_when_no_benchmark` | TestAbnormalReturnAndCalibration | None when no benchmark |
| 25 | `test_calibration_bucket_populated` | TestAbnormalReturnAndCalibration | calibration_bucket from confidence |
| 26 | `test_calibration_bucket_low_confidence` | TestAbnormalReturnAndCalibration | Low confidence bucket |
| 27 | `test_cm_v1_cost_model_emitted` | TestCostModel | cm-v1 cost model |
| 28 | `test_borrow_cost_for_shorts` | TestCostModel | Borrow cost for shorts |
| 29 | `test_no_borrow_cost_for_longs` | TestCostModel | No borrow for longs |
| 30 | `test_cost_model_versioning` | TestCostModel | Different cost model → two records |
| 31 | `test_path_b_to_path_a_record` | TestRecordTranslation | Record translation |
| 32 | `test_settle_same_prediction_twice_is_idempotent` | TestIdempotency | Idempotency |
| 33 | `test_settle_due_predictions_sync` | TestBatchSettlement | Sync batch settlement |
| 34 | `test_empty_mapping_raises` | TestMissingMapping | Missing mapping fails clearly |

### Updated tests: `services/api/tests/test_models_outcomes.py`

- `patched_stores` fixture updated to also patch `_get_settlement_ledger` with a tmp-path `SettlementLedger`
- `_make_path_b_settlement` helper added to create Path B records
- `_seed_settlement_both_stores` helper added to seed both stores
- `test_outcomes_with_settlements` updated to seed both stores
- `test_outcomes_malformed_settlement_line_skipped` updated to seed both stores and test Path B malformed line handling

### Existing tests verified (no changes needed)

- `services/settlements/tests/test_worker.py` — 22 tests (Path A math, still pass for rollback)
- `services/quant_foundry/tests/test_settlement_provider.py` — 9 tests (Path B provider)
- `services/quant_foundry/tests/test_shadow_tournament.py` — 96 tests (tournament)
- `services/quant_foundry/tests/test_auto_tournament.py` — 9 tests (auto tournament)
- `services/quant_foundry/tests/test_champion_challenger.py` — 20 tests (champion/challenger)
- `libs/fincept-core/tests/` — 35 settlement/evidence tests

## Replay verification

Post-unification replay results saved to:
`reports/c6-settlement-replay/99d08ab2/post-unification/`

### Result: 0 divergences (ALL MATCH)

| Metric | Value |
|--------|-------|
| Total fixtures | 8 |
| Total comparisons | 80 |
| Total divergences | 0 |
| MATCH | 80 |
| SEMANTIC_DIFFERENCE | 0 |
| MISSING_FIELD | 0 |
| EXPECTED_MODE_DIFFERENCE | 0 |
| ROUNDING_ONLY | 0 |
| BUG_LIKELY | 0 |

### Key verification points

| Fixture | Gross | Net | Brier | Abnormal | Calibration | Status |
|---------|-------|-----|-------|----------|-------------|--------|
| winning_long | 0.05 | 0.0482 | 0.09 | 0.045 | 0.6-0.8 | settled |
| losing_long | -0.05 | -0.0518 | 0.36 | -0.055 | 0.4-0.6 | settled |
| winning_short | 0.05 | 0.0457 | 0.4225 | 0.045 | 0.6-0.8 | settled |
| losing_short | -0.05 | -0.0543 | 0.16 | -0.055 | 0.4-0.6 | settled |
| flat | 0.0 | -0.0018 | 0.25 | 0.0 | 0.4-0.6 | settled |
| missing_prices | None | None | None | None | None | pending_data |
| partial_prices | None | None | None | None | None | pending_data |
| high_confidence_win | 0.10 | 0.0982 | 0.01 | 0.075 | 0.8-1.0 | settled |

### Must-pass expectations verified

- **winning short has positive gross/net return after costs**: gross=0.05, net=0.0457 ✓
- **losing short has negative gross/net return after costs**: gross=-0.05, net=-0.0543 ✓
- **Brier score changes with p_up confidence**: 0.09 (conf 0.7) vs 0.01 (conf 0.9) ✓
- **Path A wrapper and Path B produce matching normalized outputs**: 0 divergences ✓

## Remaining divergences

**None.** The post-unification replay shows 0 divergences between the Path A compat wrapper and the Path B canonical ledger.

## Verification results

| Check | Result |
|-------|--------|
| `ruff format --check .` | 836 files OK |
| `ruff check libs services` | All passed |
| `mypy services/quant_foundry/src` | 162 files, no issues |
| `mypy services/settlements/src` | 4 files, no issues |
| `mypy libs/fincept-core/src` | 27 files, no issues |
| `pytest services/settlements/tests` | 56 passed (22 existing + 34 new) |
| `pytest services/api/tests/test_models_outcomes.py` | 13 passed |
| `pytest services/quant_foundry/tests` | 4367 passed, 230 skipped |
| `pytest libs/fincept-core/tests -k "settlement or evidence"` | 35 passed |
| Post-unification replay | 0 divergences (ALL MATCH) |

## Risks

| Risk | Level | Mitigation |
|------|-------|------------|
| `SETTLEMENTS_USE_PATH_B` defaults to 1 (behavior change) | Medium | Feature flag allows instant rollback to `0` |
| Cost model change (v1.default → cm-v1) affects displayed returns | Medium | Old records preserved in legacy store; new records carry `cm-v1` version |
| agent_id → model_id mapping may be wrong for non-standard agents | Low | Configurable mapping function; default is conservative |
| Benchmark price fetch adds latency to poller | Low | Best-effort — `abnormal_return` is None if benchmark unavailable |
| Borrow cost makes shorts more expensive | Low | Intentional — cm-v1 is more realistic |
| Legacy `worker.py` math still reachable via `SETTLEMENTS_USE_PATH_B=0` | Low | Deprecated; will be removed in Phase 6 |

## Safe to open PR: yes

All stop conditions are clear:
- Path A is safely wrapped over Path B ✓
- Legacy API compatibility preserved (13 tests pass) ✓
- agent_id/model_id mapping is deterministic and fails clearly on empty ✓
- Short PnL is direction-aware (winning short = positive, losing short = negative) ✓
- Brier uses p_up from confidence, not (direction+1)/2 ✓
- abnormal_return is populated when benchmark is available ✓
- calibration_bucket is populated for all settled records ✓
- Replay shows 0 semantic differences ✓
- ruff/mypy/pytest all pass ✓
- No C7 promotion gate rules changed ✓
