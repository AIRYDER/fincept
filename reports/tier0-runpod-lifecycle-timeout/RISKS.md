# Risks

## 1. `executionTimeout` field name (MEDIUM)

The RunPod GraphQL `EndpointInput` field for job timeout is assumed to be
`executionTimeout`. This is based on the RunPod API documentation and common
usage. If RunPod uses a different field name (e.g., `jobTimeout`,
`timeoutSeconds`), the timeout would silently not be applied — RunPod would
ignore the unknown field and fall back to its 600s default.

**Mitigation:** The endpoint-create receipt now includes a `timeout_config`
block that records the exact value sent. A live test (with operator approval)
would confirm the field is accepted by checking the endpoint's actual
configuration via the RunPod API.

**Impact if wrong:** The original bug persists (jobs killed at 600s before
handler deadline). No new harm is introduced — the field is simply ignored.

## 2. sys.path manipulation for cross-package import (LOW)

The probe tools (`run_live_canary.py`, `run_train_model.py`,
`run_gpu_healthcheck.py`) add `scripts/` to `sys.path` to import
`runpod.runpod_lifecycle`. This works when running from the repo root but
could break if the scripts are run from a different working directory.

**Mitigation:** The `sys.path` insertion uses `Path(__file__).resolve().parents[2]`
which is anchored to the file's location, not the CWD. This is robust against
CWD changes.

## 3. No live validation of retry cleanup behavior (LOW)

The `retry_delete_endpoint` helper is unit-tested with mocks but has not been
validated against the real RunPod API's transient failure behavior.

**Mitigation:** The retry logic is a direct extraction of the existing
hand-rolled retry loop in `run_train_model.py` (L503-511), which was already
proven in the A7 live test. The extraction preserves the same 5-attempt /
10s-delay behavior.

## 4. Template name uniqueness now includes timestamp (LOW)

Previously, `run_live_canary.py` and `run_gpu_healthcheck.py` used bare
`qf-canary-<sha8>-tpl` names (no timestamp). Now all three tools use
`make_unique_name()` which appends a timestamp. This changes the template
naming pattern.

**Mitigation:** RunPod requires unique template names — the timestamp makes
collisions impossible. The old bare-name pattern was actually fragile (a
second run with the same SHA would collide). The new pattern is strictly
better.

## Next recommended task

- Get operator approval for a live canary test to validate `executionTimeout`
  is accepted by the RunPod API.
- Remove the unused `import importlib` in `run_live_canary.py` (done).
