# Lifecycle Helper API: `scripts/runpod/runpod_lifecycle.py`

## Constants

| Constant | Value | Description |
|----------|-------|-------------|
| `MIN_EXECUTION_TIMEOUT_S` | 1860 | Hard floor for `executionTimeout` (deadline 1800 + 60s slack) |
| `DEFAULT_DEADLINE_S` | 1800 | Handler deadline (`QUANT_FOUNDRY_TRAINING_DEADLINE_SECONDS`) |
| `DEFAULT_SLACK_S` | 60 | Slack added to deadline |
| `DEFAULT_IDLE_TIMEOUT_S` | 300 | Default idle timeout (worker warm time) |

## Functions

### `compute_execution_timeout(deadline_s=1800, slack_s=60) -> int`

Returns `max(deadline_s + slack_s, MIN_EXECUTION_TIMEOUT_S)`. Always >= 1860.

```python
compute_execution_timeout()  # 1860
compute_execution_timeout(deadline_s=3600, slack_s=120)  # 3720
compute_execution_timeout(deadline_s=100, slack_s=10)  # 1860 (floor enforced)
```

### `validate_execution_timeout(timeout_s: int) -> int`

Raises `ValueError` if `timeout_s < 1860`. Returns `timeout_s` unchanged when valid.

```python
validate_execution_timeout(1860)  # 1860
validate_execution_timeout(600)   # ValueError
```

### `make_unique_name(prefix, sha, *, suffix="", timestamp=None, sha_len=8) -> str`

Generates a unique RunPod resource name: `<prefix>-<sha[:sha_len]>[-<suffix>]-<timestamp>`.

```python
make_unique_name("qf-canary", "abcdef1234567890", timestamp=1719900000)
# "qf-canary-abcdef12-1719900000"

make_unique_name("qf-a7train", "abcdef1234567890", suffix="tpl", timestamp=1719900000)
# "qf-a7train-abcdef12-tpl-1719900000"
```

### `build_template_input(config: TemplateConfig) -> dict`

Builds the `SaveTemplateInput` dict for the RunPod GraphQL mutation.

### `build_endpoint_input(config: EndpointConfig) -> dict`

Builds the `EndpointInput` dict for the RunPod GraphQL mutation. Always
includes `executionTimeout` (validated >= 1860). Raises `ValueError` if the
configured timeout is below the minimum.

### `retry_delete_endpoint(endpoint_id, delete_fn, *, max_attempts=5, delay_s=10.0, sleeper=time.sleep, logger=None) -> bool`

Retries `delete_fn(endpoint_id)` up to `max_attempts` times with `delay_s`
between attempts. Returns `True` on success, `False` if all attempts fail.
Never raises.

### `safe_scale_to_zero(endpoint_id, scale_fn, *, logger=None) -> bool`

Calls `scale_fn(endpoint_id, 0, 0)`. Returns `True` on success, `False` on
failure. Never raises.

### `format_timeout_receipt(execution_timeout, idle_timeout=300, deadline_s=1800) -> dict`

Builds a receipt-friendly dict recording the timeout configuration for audit.

```python
format_timeout_receipt(1860, idle_timeout=300)
# {
#   "executionTimeout": 1860,
#   "idleTimeout": 300,
#   "handler_deadline_s": 1800,
#   "slack_s": 60,
#   "meets_min_requirement": True,
#   "min_required_execution_timeout": 1860,
#   "note": "executionTimeout >= handler deadline + 60s slack ..."
# }
```

## Dataclasses

### `TemplateConfig`

| Field | Type | Default |
|-------|------|---------|
| `name` | str | required |
| `image_name` | str | required |
| `env_vars` | Sequence[dict] | required |
| `registry_auth_id` | str | required |
| `container_disk_gb` | int | 20 |
| `volume_in_gb` | int | 0 |
| `docker_args` | str | "" |
| `is_serverless` | bool | True |

### `EndpointConfig`

| Field | Type | Default |
|-------|------|---------|
| `name` | str | required |
| `template_id` | str | required |
| `gpu_ids` | str | "ADA_24" |
| `workers_min` | int | 1 |
| `workers_max` | int | 1 |
| `idle_timeout` | int | 300 |
| `execution_timeout` | int \| None | None (-> `compute_execution_timeout()`) |
| `scaler_type` | str | "QUEUE_DELAY" |
| `scaler_value` | int | 4 |
| `container_disk_gb` | int | 20 |

## Usage from probe tools

```python
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPTS_DIR = str(_REPO_ROOT / "scripts")
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from runpod.runpod_lifecycle import (
    EXECUTION_TIMEOUT,  # imported from run_live_canary, not here
    make_unique_name,
    retry_delete_endpoint,
    safe_scale_to_zero,
    format_timeout_receipt,
)
```
