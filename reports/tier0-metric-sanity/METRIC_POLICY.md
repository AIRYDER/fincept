# Metric Sanity Policy

## Overview

Implausible training metrics (e.g. the A7 canary Sharpe of 769) must
not silently reach promotion decisions. The worker flags, annotates,
and blocks promotion for implausible metrics while **preserving the
raw values** for forensic analysis.

## Thresholds (conservative defaults)

All thresholds are overridable via environment variables.

| Metric | Canonical key(s) | Warning | Implausible | Env var |
|--------|------------------|---------|-------------|---------|
| Sharpe ratio | `sharpe_ratio`, `sharpe`, `deflated_sharpe` | \|x\| > 5.0 | \|x\| > 10.0 | `QF_METRIC_SANITY_SHARPE_IMPLAUSIBLE`, `QF_METRIC_SANITY_SHARPE_WARNING` |
| Annual return | `annual_return`, `annualized_return`, `cagr` | — | \|x\| > 5.0 (500%) | `QF_METRIC_SANITY_ANNUAL_RETURN_IMPLAUSIBLE` |
| Max drawdown | `max_drawdown`, `maximum_drawdown` | — | \|x\| > 1.0 (100%) | `QF_METRIC_SANITY_MAX_DRAWDOWN_IMPLAUSIBLE` |
| Fold overfit ratio | `pbo`, `fold_overfit_ratio`, `overfit_ratio` | — | x > 5.0 | `QF_METRIC_SANITY_FOLD_OVERFIT_IMPLAUSIBLE` |

### Rationale

- **Sharpe > 10**: real strategies rarely exceed Sharpe 3. A value of
  769 (the A7 canary) is a clear bug (likely annualization or
  sample-size error). Warning at 5.0 catches suspicious-but-plausible
  cases without blocking.
- **Annual return > 500%**: sustained 500%+ annual returns are not
  credible for a real strategy.
- **Max drawdown > 100%**: mathematically impossible for a
  long-only unleveraged strategy; indicates a sign/scale error.
- **Fold overfit ratio > 5.0**: the fold overfit ratio is bounded
  [0, 1] by construction; >5 indicates a computation bug.

## Status levels

| Status | Meaning | Promotion effect |
|--------|---------|------------------|
| `ok` | All metrics within bounds | No effect |
| `warning` | At least one metric suspicious (above warning, below implausible) | Advisory only — does NOT block |
| `implausible` | At least one CRITICAL metric above implausible threshold | **Blocks promotion** (`promotion_eligible=False`) |

## Critical metrics

A metric is "critical" if its implausible status blocks promotion:

- `sharpe_ratio`
- `annual_return`
- `max_drawdown`
- `fold_overfit_ratio`

## Raw value preservation

**Raw metric values are NEVER deleted or modified.** The sanity report
is an annotation layer embedded under
`metrics_summary["metric_sanity"]` alongside the untouched raw values.
This preserves forensic traceability — an operator can see both the
raw (possibly buggy) value and the sanity verdict.

## Report shape

```json
{
  "metric_sanity": {
    "status": "implausible",
    "reason_codes": ["sharpe_ratio_implausible:769.0"],
    "promotion_allowed": false,
    "flagged_metrics": {
      "sharpe_ratio": {
        "raw_value": 769.0,
        "status": "implausible",
        "reason_code": "sharpe_ratio_implausible:769.0"
      }
    }
  }
}
```

## Enforcement point

The sanity check runs **inside `build_callback()`** (in
`runpod_training.py`), BEFORE the callback is signed. This guarantees
every signed callback carries the sanity verdict, regardless of which
handler path constructed the `metrics_summary`. The handler in
`handler.py` passes `metrics_summary` through unchanged; the
validation is centralized to avoid duplication and concurrent-edit
conflicts.
