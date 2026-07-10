# Risks

## 1. Deny gate fires after training (not before)

**Risk:** The deny gate is placed at the start of the writer selection block
(~L3484), which is AFTER training has completed (~L3283). This means GPU
time is spent training a model that is then refused persistence.

**Why:** The owned line range for this task is L3370-3480 (writer selection).
The pre-training area (L3237-3283) is outside the owned range. Moving the
gate before training requires touching lines owned by another worker.

**Impact:** Wasted GPU time for misconfigured real jobs (training runs, then
artifact is refused). The artifact is NOT persisted to /tmp (the gate
prevents that), so the core safety property holds.

**Mitigation:** A future task should move the deny gate to immediately after
`output_prefix = resolve_volume_path(output_prefix)` (L3237) and before
`write_status(req.job_id, "started")` (L3241). This requires coordinating
with the worker that owns the L3237-3283 range.

## 2. Python 3.10 test environment

**Risk:** The test file cannot be imported on Python 3.10 (system default)
because `quant_foundry.schemas` imports `StrEnum` (Python 3.11+). This is a
pre-existing issue, not caused by this change.

**Impact:** `pytest` on Python 3.10 errors on all 41 tests (including the
original 24). Tests pass on Python 3.12.

**Mitigation:** `compileall` passes on Python 3.10. Tests verified on
Python 3.12 (`py -3.12 -m pytest`). The CI/CD pipeline should use Python
3.11+.

## 3. resolve_volume_path rewrites /runpod-volume to /tmp on test systems

**Risk:** On a system without `/runpod-volume` or `/workspace` mounted,
`resolve_volume_path("/runpod-volume/foo")` rewrites to `/tmp/foo` (because
`runpod_data_root()` returns `/tmp`). The deny gate then sees `/tmp/foo`
and rejects it for non-canary jobs.

**Impact:** In a test environment without a mounted volume, a non-canary job
with `output_prefix="/runpod-volume/foo"` would be rejected by the deny gate
even though the operator intended a volume path. This is correct behavior
(no volume mounted = no durable destination), but may be surprising.

**Mitigation:** This is the intended fail-closed behavior. On a real RunPod
worker with a volume mounted, `runpod_data_root()` returns the volume path
and the rewrite succeeds.

## 4. No presigned URL generation on the worker

**Risk:** The worker does not generate presigned URLs (it has no S3
credentials). The caller (platform service) must generate and pass
`presigned_artifact_url` in the request.

**Impact:** If the caller forgets to pass a presigned URL and no volume is
mounted, the deny gate fires for real jobs. This is correct but requires
the caller to be aware of the contract.

**Mitigation:** The error message explicitly says "pass presigned_artifact_url"
to guide the operator.

## 5. Manifest verifier does not re-download https:// artifacts by default

**Risk:** The manifest verifier fetches `https://` artifacts via HTTP GET.
For presigned URLs that have expired, the fetch will fail with an HTTP 403.

**Impact:** Verification of expired presigned URLs fails (exit code 1 or 2).
This is correct — an expired URL means the artifact is not retrievable.

**Mitigation:** The verifier reports the HTTP status clearly. Operators
should verify manifests before presigned URLs expire.
