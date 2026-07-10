# Tier 0.2 — Durable Artifact Upload Policy

**Task ID:** task-mr6i5c4b-65b96801
**Agent:** Builder 3
**Branch:** tier0/durable-artifacts
**Status:** Review-ready

## What was done

Added a **durable artifact deny gate** and **output_prefix validation** to the
RunPod training worker (`handler.py`), plus a standalone manifest verifier
script. This closes the gap that caused the A7 model to die with the worker:
`output_prefix` defaulted to `/tmp/a7-train-artifacts`, the signed receipt
pointed at a URI that no longer existed, and the artifact was lost.

## Changes (3 files)

1. **`runpod/quant-foundry-training/handler.py`** — Added two helper functions
   (`_is_under_tmp`, `_validate_output_prefix_durable`) after `resolve_volume_path`,
   and a deny gate at the start of the writer selection block. The gate:
   - Denies `/tmp` as a final destination for non-canary jobs (fail-closed
     with a signed failure envelope).
   - Validates `output_prefix` resolves to `/runpod-volume/`, `/workspace/`,
     `s3://`, `https://`, or `file://` to a mounted volume.
   - Allows `/tmp` for canary jobs (FakeArtifactWriter is canary-only).
   - Names the missing durable destination in the error message.

2. **`services/quant_foundry/tests/test_artifact_writer.py`** — Added 17 new
   tests (41 total, all passing): unit tests for `_is_under_tmp` and
   `_validate_output_prefix_durable`, plus handler integration tests for the
   deny gate firing on `/tmp` for real jobs, allowing `/tmp` for canary, and
   rejecting invalid prefixes.

3. **`scripts/verify_artifact_manifest.py`** — New standalone manifest verifier
   that loads a manifest JSON, fetches the artifact by URI, re-hashes with
   SHA-256, compares to the manifest's sha256, and verifies the HMAC write
   receipt against `QUANT_FOUNDRY_CALLBACK_SECRET`.

## What was NOT changed

- No writer stack rewritten (VolumeArtifactWriter, PresignedUploadArtifactWriter,
  FakeArtifactWriter, ArtifactWriteResult all unchanged).
- No model training behavior or deterministic seed behavior changed.
- No handler.py lines touched outside owned ranges (L143-174 area for helpers,
  L3370-3480 area for deny gate).
- No base image switch, no HEALTHCHECK.
- No live/paid RunPod tests run.

## Acceptance criteria

| Criterion | Status |
|-----------|--------|
| /tmp deny gate for non-canary jobs | ✅ |
| output_prefix validation (volume + presigned, rejects /tmp for real) | ✅ |
| New tests for deny gate and validation | ✅ (17 new) |
| scripts/verify_artifact_manifest.py exists and is functional | ✅ |
| Existing 24 tests still pass | ✅ (41/41 pass) |
| No writer stack rewritten | ✅ |
| No training behavior changed | ✅ |
| Receipt bundle written | ✅ |
