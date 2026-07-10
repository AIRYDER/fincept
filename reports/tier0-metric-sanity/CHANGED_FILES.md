# Changed Files

## 1. `services/quant_foundry/src/quant_foundry/runpod_training.py`

**Lines added:** ~217 (new section) + ~13 (build_callback wiring)

### New: metric sanity constants (after imports, ~L77)
- `METRIC_SANITY_SHARPE_IMPLAUSIBLE` (default 10.0, env
  `QF_METRIC_SANITY_SHARPE_IMPLAUSIBLE`)
- `METRIC_SANITY_SHARPE_WARNING` (default 5.0, env
  `QF_METRIC_SANITY_SHARPE_WARNING`)
- `METRIC_SANITY_ANNUAL_RETURN_IMPLAUSIBLE` (default 5.0 = 500%, env
  `QF_METRIC_SANITY_ANNUAL_RETURN_IMPLAUSIBLE`)
- `METRIC_SANITY_MAX_DRAWDOWN_IMPLAUSIBLE` (default 1.0 = 100%, env
  `QF_METRIC_SANITY_MAX_DRAWDOWN_IMPLAUSIBLE`)
- `METRIC_SANITY_FOLD_OVERFIT_IMPLAUSIBLE` (default 5.0, env
  `QF_METRIC_SANITY_FOLD_OVERFIT_IMPLAUSIBLE`)
- `_METRIC_ALIASES` — known key aliases per canonical metric
- `_CRITICAL_METRICS` — metrics whose "implausible" blocks promotion

### New: `MetricSanityReport` dataclass
- `status`: "ok" | "warning" | "implausible"
- `reason_codes`: tuple[str, ...]
- `promotion_allowed`: bool
- `flagged_metrics`: dict with `raw_value`, `status`, `reason_code`
- `to_dict()` — JSON-safe serialization

### New: `validate_metric_sanity(metrics_summary)` function
- Checks sharpe_ratio (abs), annual_return (abs), max_drawdown (abs),
  fold_overfit_ratio against thresholds.
- Preserves raw values; returns annotation-only report.
- `promotion_allowed=False` only when a CRITICAL metric is implausible.

### Modified: `build_callback()`
- Now copies `metrics_summary` (no caller mutation).
- Calls `validate_metric_sanity()` and embeds the report under
  `metrics["metric_sanity"]`.
- Forces `promotion_eligible=False` when
  `sanity_report.promotion_allowed` is False (hard floor, fail-closed).

## 2. `runpod/quant-foundry-training/handler.py`

**Lines changed:** ~L3725 (metrics_summary construction)

- Added annotation comment documenting that `build_callback` applies
  the sanity bounds (no behavioural change in handler — the validation
  is centralized in `build_callback` to avoid duplicating logic and to
  respect Builder 3's concurrent edits to handler.py).

## 3. `services/quant_foundry/tests/test_metric_sanity.py` (NEW)

**18 tests** covering:
- Normal Sharpe (1.5) → "ok"
- Sharpe 769 → "implausible" + reason code
- Raw value preserved
- Promotion blocked on implausible
- Warning (Sharpe 6.0) → "warning", promotion NOT blocked
- Max drawdown / annual return / fold overfit implausible cases
- Empty/None metrics → "ok"
- Non-numeric metric ignored (no crash)
- Negative extreme Sharpe implausible
- Report JSON-serializable
- build_callback integration (promotion blocked, raw preserved,
  warning doesn't block, canonical JSON serializes, canary stays
  blocked, input dict not mutated)
