# Test Results

## New test suite: `tests/test_metric_sanity.py`

```
18 passed in 0.62s
```

### Test list

| # | Test | Verifies |
|---|------|----------|
| 1 | `test_normal_sharpe_passes_ok` | Sharpe 1.5 → status "ok", no reason codes |
| 2 | `test_sharpe_769_flagged_implausible` | Sharpe 769 → "implausible", promotion blocked |
| 3 | `test_raw_metric_value_preserved` | Raw value 769.0 preserved in flagged_metrics |
| 4 | `test_warning_sharpe_does_not_block_promotion` | Sharpe 6.0 → "warning", promotion allowed |
| 5 | `test_max_drawdown_implausible_blocks_promotion` | Drawdown -1.5 → "implausible", blocked |
| 6 | `test_annual_return_implausible_blocks_promotion` | Return 7.5 → "implausible", blocked |
| 7 | `test_fold_overfit_implausible_blocks_promotion` | PBO 6.0 → "implausible", blocked |
| 8 | `test_empty_metrics_returns_ok` | None / {} → "ok" |
| 9 | `test_non_numeric_metric_ignored` | "not-a-number" → ignored, "ok" |
| 10 | `test_negative_extreme_sharpe_implausible` | Sharpe -769 → "implausible" (abs checked) |
| 11 | `test_metric_sanity_report_serializes_json` | to_dict() JSON-serializable |
| 12 | `test_build_callback_blocks_promotion_on_implausible_sharpe` | Production + gates pass + Sharpe 769 → promotion False |
| 13 | `test_build_callback_preserves_raw_metrics` | Raw sharpe/drawdown preserved in callback |
| 14 | `test_build_callback_normal_metrics_ok_and_promotion_allowed` | Normal + production + gates → promotion True |
| 15 | `test_build_callback_warning_does_not_block_promotion` | Warning Sharpe 6.0 → promotion still True |
| 16 | `test_build_callback_serializes_canonical_json` | Full callback JSON-serializes with sanity report |
| 17 | `test_build_callback_canary_still_blocked_with_implausible` | Canary + implausible → stays blocked |
| 18 | `test_build_callback_does_not_mutate_input_metrics` | Caller's dict not mutated |

## Regression: existing tests

```
tests/test_runpod_training.py tests/test_runpod_modes.py
67 passed in 0.82s
```

No regressions. The `build_callback` change (copying metrics_summary
and embedding the sanity report) is backward-compatible — existing
callbacks now carry an additional `metric_sanity` key inside
`metrics_summary`, which does not break signature verification or
required-field validation.
