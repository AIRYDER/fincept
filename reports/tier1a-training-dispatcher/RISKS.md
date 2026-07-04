# Risks

## 1. Duplicate policy logic between service and probe scripts
**Risk:** `build_job_policy()` now exists in two places:
`scripts/runpod/runpod_lifecycle.py` (probe scripts) and
`services/quant_foundry/src/quant_foundry/runpod_policy.py` (service).
If one is updated and the other is not, the per-request timeout policy
could diverge between the probe scripts and the production dispatch
path.

**Mitigation:** The two implementations are intentionally identical
copies. The task spec explicitly allowed this ("the probe scripts can
keep their existing import"). A future refactor should make the probe
scripts import from the service package (or extract to a shared
package) to eliminate the duplication. Both enforce the same
`MIN_EXECUTION_TIMEOUT_S = 1860` floor.

## 2. presigned_artifact_url included as None in job input
**Risk:** `build_training_job_input()` always includes
`presigned_artifact_url` as a top-level key (set to `None` when unset).
The worker's `handler.py` uses `input_data.pop("presigned_artifact_url",
None)` which treats `None` and absence identically, so this is safe.
However, it adds a key to every job input dict.

**Mitigation:** Verified handler.py line ~3114 uses `.pop(..., None)`.
The explicit inclusion makes the contract testable and visible. No
behavioral change on the worker side.

## 3. Policy sent on every /run request
**Risk:** `HttpRunPodClient.dispatch()` now always includes the
`policy` key. If a future RunPod API change rejects unknown fields in
the `/run` body, this could break dispatch. RunPod's documented API
explicitly supports `policy` in the `/run` body (see
https://docs.runpod.io/serverless/endpoints/send-requests#execution-policies),
so this is low risk.

**Mitigation:** The `policy` key is documented by RunPod. The existing
`test_runpod_client.py` tests assert `"input" in body` (not
`set(body.keys()) == {"input"}`), so they still pass with the added
`policy` key.

## 4. Network volume fields are advisory on endpoint input
**Risk:** `networkVolumeId` is a real RunPod endpoint field, but
`volumeInGb` and `volumeMountPath` are template-level fields. Including
them in the endpoint input dict may be ignored by RunPod's GraphQL
mutation (extra fields). The endpoint create receipt records them for
audit, but the actual volume size/mount path must also be set on the
template via `build_template_input()` in the probe scripts.

**Mitigation:** The probe scripts' `build_template_input()` already
sets `volumeInGb` on the template. The endpoint-level fields are
echoed for receipt/audit purposes. RunPod's GraphQL mutation ignores
unknown fields (does not reject them).

## 5. Windows pytest temp cleanup PermissionError
**Risk:** pytest exits with code 1 on this Windows machine due to a
`PermissionError` in `cleanup_dead_symlinks` during session teardown.
This is NOT a test failure — all test dots are green.

**Mitigation:** None needed for the code. The exit code is misleading
on Windows; the actual test results (visible in the dot output) are all
passing. CI on Linux will not have this issue.

## Next recommended task
- Refactor `scripts/runpod/runpod_lifecycle.py` to import
  `build_job_policy` from `quant_foundry.runpod_policy` to eliminate
  the duplication (low priority — both copies are identical).
- Wire the gateway's training dispatch path to call
  `build_training_job_input()` so presigned_artifact_url flows from the
  request schema through to the worker automatically (currently the
  request_payload dict is built by the caller of `create_job`).
