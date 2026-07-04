# Before / After Payload Example

## BEFORE (no sanity validation)

A canary run producing Sharpe 769 passed the metric straight through
to the signed callback with no sanity check. Promotion eligibility
was controlled only by mode/gates.

```json
{
  "metrics_summary": {
    "sharpe_ratio": 769.0,
    "max_drawdown": -0.1,
    "win_rate": 0.52
  },
  "promotion_eligible": true
}
```

**Problem:** Sharpe 769 is impossible. A promotion decision seeing
`promotion_eligible: true` could promote a broken/buggy model.

## AFTER (with metric sanity bounds)

The raw value is **preserved**. A `metric_sanity` annotation is
embedded alongside it, and `promotion_eligible` is forced `false`.

```json
{
  "metrics_summary": {
    "sharpe_ratio": 769.0,
    "max_drawdown": -0.1,
    "win_rate": 0.52,
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
  },
  "promotion_eligible": false
}
```

**Result:**
- Raw `sharpe_ratio: 769.0` is still present (forensic traceability).
- `metric_sanity.status` is `"implausible"`.
- `promotion_eligible` is `false` — promotion blocked.

## Normal case (Sharpe 1.5)

```json
{
  "metrics_summary": {
    "sharpe_ratio": 1.5,
    "max_drawdown": -0.15,
    "metric_sanity": {
      "status": "ok",
      "reason_codes": [],
      "promotion_allowed": true,
      "flagged_metrics": {}
    }
  },
  "promotion_eligible": true
}
```

## Warning case (Sharpe 6.0)

```json
{
  "metrics_summary": {
    "sharpe_ratio": 6.0,
    "metric_sanity": {
      "status": "warning",
      "reason_codes": ["sharpe_ratio_warning:6.0"],
      "promotion_allowed": true,
      "flagged_metrics": {
        "sharpe_ratio": {
          "raw_value": 6.0,
          "status": "warning",
          "reason_code": "sharpe_ratio_warning:6.0"
        }
      }
    }
  },
  "promotion_eligible": true
}
```

Warning is advisory — promotion is NOT blocked.
