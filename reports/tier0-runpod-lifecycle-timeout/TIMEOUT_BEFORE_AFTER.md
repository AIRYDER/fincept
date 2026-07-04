# Timeout Configuration: Before vs After

## The Problem

The handler enforces `QUANT_FOUNDRY_TRAINING_DEADLINE_SECONDS=1800` (30 min).
RunPod's **default** endpoint job timeout is **600s** (10 min). A real
training job taking 20 minutes would be `TIMED_OUT` by RunPod *before* the
handler's signed failure envelope fires — the platform loses the signed
receipt.

The endpoint template creation code in `run_live_canary.py` set
`idleTimeout=300` but did **NOT** set `executionTimeout`, silently inheriting
RunPod's 600s default.

## Before

| Field | Value | Source |
|-------|-------|--------|
| `idleTimeout` | 300 | `run_live_canary.py` `IDLE_TIMEOUT` constant |
| `executionTimeout` | **NOT SET** (inherits RunPod default 600s) | missing from `create_endpoint()` input |
| Handler deadline | 1800s | `QUANT_FOUNDRY_TRAINING_DEADLINE_SECONDS` |

**Gap:** RunPod kills the job at 600s. Handler deadline is 1800s. A job
running >10 min is killed by RunPod before the handler can emit its signed
failure envelope.

## After

| Field | Value | Source |
|-------|-------|--------|
| `idleTimeout` | 300 | `DEFAULT_IDLE_TIMEOUT_S` in `runpod_lifecycle.py` |
| `executionTimeout` | **1860** (1800 + 60s slack) | `compute_execution_timeout()` in `runpod_lifecycle.py` |
| Handler deadline | 1800s | `QUANT_FOUNDRY_TRAINING_DEADLINE_SECONDS` |
| Min enforced | 1860s | `MIN_EXECUTION_TIMEOUT_S` constant + `validate_execution_timeout()` |

**Fix:** `executionTimeout=1860` is now explicitly set in every endpoint
creation call via `build_endpoint_input()`. The `validate_execution_timeout()`
function raises `ValueError` if anyone tries to set it below 1860, making the
hard rule impossible to violate silently.

## How it works

```python
from runpod.runpod_lifecycle import compute_execution_timeout, validate_execution_timeout

# Default: 1800 + 60 = 1860
EXECUTION_TIMEOUT = compute_execution_timeout()  # -> 1860

# Custom: 3600 + 120 = 3720
EXECUTION_TIMEOUT = compute_execution_timeout(deadline_s=3600, slack_s=120)  # -> 3720

# Floor enforced: max(100+10, 1860) = 1860
EXECUTION_TIMEOUT = compute_execution_timeout(deadline_s=100, slack_s=10)  # -> 1860

# Validation: raises ValueError
validate_execution_timeout(600)  # ValueError: below the minimum 1860s
```

## Receipt evidence

Every endpoint-create receipt now includes a `timeout_config` block:

```json
{
  "executionTimeout": 1860,
  "idleTimeout": 300,
  "handler_deadline_s": 1800,
  "slack_s": 60,
  "meets_min_requirement": true,
  "min_required_execution_timeout": 1860,
  "note": "executionTimeout >= handler deadline + 60s slack so the handler's signed failure envelope fires before RunPod times the job out."
}
```
