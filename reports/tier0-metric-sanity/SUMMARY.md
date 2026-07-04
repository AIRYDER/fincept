# Tier 0 — Metric Sanity Bounds

**Task ID:** task-mr6i5im89v-58fe0988
**Builder:** 5
**Branch:** `tier0/metric-sanity`
**Status:** complete

## Problem

The A7 canary run produced a Sharpe ratio of **769** — impossible for
any real trading strategy (real strategies rarely exceed Sharpe ~3).
The worker passed `metrics_summary` directly from the dossier
`training_metrics` to `build_callback` with **no sanity validation**,
so implausible metrics could reach promotion decisions unchecked.

## Solution

Added a `validate_metric_sanity()` function in
`services/quant_foundry/src/quant_foundry/runpod_training.py` with
conservative, env-configurable thresholds. It is wired into
`build_callback()` so that:

1. **Raw values are preserved** — never deleted or modified. The sanity
   report is embedded alongside them under
   `metrics_summary["metric_sanity"]`.
2. **Implausible critical metrics block promotion** —
   `promotion_eligible` is forced `False` when any critical metric
   (sharpe_ratio, annual_return, max_drawdown, fold_overfit_ratio) is
   flagged "implausible".
3. **Warning-level metrics are advisory** — they are recorded but do
   NOT block promotion.
4. **The callback still serializes correctly** — the sanity report is
   JSON-safe and travels inside the signed payload.

## Key files

- `services/quant_foundry/src/quant_foundry/runpod_training.py` —
  `validate_metric_sanity()`, `MetricSanityReport`, threshold constants,
  wiring in `build_callback()`.
- `runpod/quant-foundry-training/handler.py` — annotation comment at
  the `metrics_summary` construction site (L3725).
- `services/quant_foundry/tests/test_metric_sanity.py` — 18 tests.

## Acceptance criteria

| Criterion | Status |
|---|---|
| Sanity validation function with configurable thresholds | ✅ |
| Sharpe 769 flagged "implausible" with reason code | ✅ |
| Raw metric values preserved (not deleted) | ✅ |
| Promotion blocked when critical metric implausible | ✅ |
| Normal metrics pass with status "ok" | ✅ |
| Callback still serializes correctly | ✅ |
| Tests prove all of the above | ✅ (18 passed) |
| Receipt bundle written | ✅ |
