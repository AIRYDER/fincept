# Changed Files

## 1. runpod/quant-foundry-training/handler.py

### Helper functions (after `resolve_volume_path`, ~L186-296)

- **`_is_under_tmp(path_str: str) -> bool`** — Detects whether a path string
  resolves under `/tmp`. Handles `file://` URIs and normalizes backslashes
  for cross-platform testing.

- **`_validate_output_prefix_durable(*, output_prefix, presigned_artifact_url, training_mode) -> str | None`**
  — Returns an error message if the artifact destination is not durable for
  real jobs, or `None` if acceptable. Rules:
  - Canary jobs may use `/tmp`.
  - Non-canary jobs must resolve to `/runpod-volume/`, `/workspace/`,
    `s3://`, `https://`, or `file://` to a mounted volume.
  - No destination at all for a real job → fail closed.

### Deny gate (start of writer selection block, ~L3484-3510)

Inserted a deny gate AFTER `model_bytes = typed_artifact.model_bytes` and
BEFORE the writer selection comment. The gate:
1. Resolves `training_mode` from `req.extra_constraints`.
2. Calls `_validate_output_prefix_durable`.
3. If the validation returns an error, writes a `failed` status and returns
   a `_build_signed_failure` envelope with `error_code="artifact_destination_not_durable"`.

**Note:** The deny gate fires at the start of the writer selection block
(within owned range L3370-3480), which is after training has completed. This
is a constraint of the line ownership boundaries — the gate prevents the
artifact from being persisted to `/tmp` but does not prevent the GPU time
from being spent. A future task should move the gate before training starts
(requires touching the L3237-3283 range, which is owned by another worker).

## 2. services/quant_foundry/tests/test_artifact_writer.py

Added 17 new tests (appended after `test_handler_volume_writer_persists_artifact`):

### Unit tests for `_is_under_tmp`:
- `test_is_under_tmp_detects_tmp_paths` — detects `/tmp`, `/tmp/foo`, `file:///tmp/foo`

### Unit tests for `_validate_output_prefix_durable`:
- `test_validate_output_prefix_denies_tmp_for_real_jobs` — production mode, `/tmp` → error
- `test_validate_output_prefix_denies_tmp_for_research_mode` — research mode, `/tmp` → error
- `test_validate_output_prefix_allows_tmp_for_canary` — canary mode, `/tmp` → None
- `test_validate_output_prefix_rejects_invalid_prefix` — `/var/tmp/` → error
- `test_validate_output_prefix_accepts_runpod_volume` — `/runpod-volume/` → None
- `test_validate_output_prefix_accepts_workspace` — `/workspace/` → None
- `test_validate_output_prefix_accepts_presigned_url` — `https://` → None
- `test_validate_output_prefix_accepts_s3_uri` — `s3://` → None
- `test_validate_output_prefix_rejects_file_uri_to_tmp` — `file:///tmp/` → error
- `test_validate_output_prefix_accepts_file_uri_to_volume` — `file:///runpod-volume/` → None
- `test_validate_output_prefix_rejects_no_destination_for_real_job` — no prefix, no URL → error
- `test_validate_output_prefix_rejects_file_uri_to_non_volume` — `file:///opt/` → error

### Handler integration tests:
- `test_handler_denies_tmp_for_real_jobs` — production + `/tmp` → signed failure
- `test_handler_allows_tmp_for_canary_jobs` — canary + tmp_path → success
- `test_handler_denies_invalid_prefix_for_real_jobs` — production + `/var/tmp/` → signed failure
- `test_handler_denies_no_destination_for_real_jobs` — production, no prefix → signed failure

## 3. scripts/verify_artifact_manifest.py (NEW)

Standalone manifest verifier (242 lines). Usage:
```
QUANT_FOUNDRY_CALLBACK_SECRET=secret python scripts/verify_artifact_manifest.py path/to/artifact_manifest.json
```

Features:
- Loads manifest JSON from file or stdin (`-`).
- Fetches artifact by URI (`file://` or `https://`).
- Re-hashes with SHA-256, compares to manifest's declared sha256.
- Verifies HMAC write receipt against `QUANT_FOUNDRY_CALLBACK_SECRET` (constant-time).
- Exit codes: 0=verified, 1=verification failed, 2=operational error.
- Uses `url2pathname` for cross-platform `file://` URI handling.
