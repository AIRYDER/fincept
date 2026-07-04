# Artifact Flow — AFTER (deny gate + validation)

## What changed

A deny gate now sits between `output_prefix` resolution and writer
selection. For non-canary jobs, it rejects `/tmp` and any non-durable
destination with a signed failure envelope. For canary jobs, `/tmp` is
still allowed (FakeArtifactWriter is canary-only by design).

## Flow (AFTER)

```
Request arrives
  ↓
output_prefix = input_data.pop("output_prefix", None)
presigned_artifact_url = input_data.pop("presigned_artifact_url", None)
  ↓
output_prefix = resolve_volume_path(output_prefix)
  ↓
Training runs (GPU time spent)
  ↓
model_bytes = typed_artifact.model_bytes
  ↓
┌─── DURABLE ARTIFACT DENY GATE (NEW) ───────────────────────────┐
│                                                                  │
│  raw_mode = req.extra_constraints.get("training_mode") or "canary" │
│                                                                  │
│  error = _validate_output_prefix_durable(                       │
│      output_prefix=output_prefix,                               │
│      presigned_artifact_url=presigned_artifact_url,             │
│      training_mode=raw_mode,                                    │
│  )                                                              │
│                                                                  │
│  if error is not None:                                          │
│      write_status(job_id, "failed",                             │
│          error_code="artifact_destination_not_durable")         │
│      return _build_signed_failure(                              │
│          error_code="artifact_destination_not_durable",         │
│          error_message=error,                                   │
│          mode=raw_mode,                                         │
│          context={job_id, stage, output_prefix, ...})           │
│      ↑ FAIL CLOSED — no artifact written, no receipt to /tmp    │
│                                                                  │
└──────────────────────────────────────────────────────────────────┘
  ↓ (gate passed)
Writer selection:
  presigned_artifact_url? → PresignedUploadArtifactWriter (prod)
  output_prefix?          → VolumeArtifactWriter (volume path)
  neither?                → FakeArtifactWriter (canary only)
  ↓
Artifact written to durable destination
  ↓
Signed callback with artifact_uri → durable location
  ↓
Worker shuts down → artifact SURVIVES
  ↓
Trusted side runs: python scripts/verify_artifact_manifest.py manifest.json
  - Fetches artifact by URI
  - Re-hashes SHA-256, compares to manifest
  - Verifies HMAC write receipt
  - Exit 0 = verified, Exit 1 = tampered, Exit 2 = operational error
```

## Validation rules (hard rules from durable-artifact skill)

| training_mode | output_prefix | presigned_url | Result |
|---------------|---------------|---------------|--------|
| canary | /tmp/foo | — | ✅ Allowed (canary-only) |
| canary | (none) | — | ✅ FakeArtifactWriter |
| production | /tmp/foo | — | ❌ Deny gate fires |
| production | /runpod-volume/foo | — | ✅ VolumeArtifactWriter |
| production | /workspace/foo | — | ✅ VolumeArtifactWriter |
| production | — | https://s3... | ✅ PresignedUploadArtifactWriter |
| production | — | — | ❌ Deny gate fires (no durable dest) |
| production | /var/tmp/foo | — | ❌ Deny gate fires (not a volume) |
| production | file:///tmp/foo | — | ❌ Deny gate fires |
| production | file:///runpod-volume/foo | — | ✅ VolumeArtifactWriter |
| research | /tmp/foo | — | ❌ Deny gate fires |
| research | /runpod-volume/foo | — | ✅ VolumeArtifactWriter |

## Error message format

The deny gate returns a signed failure envelope with:
- `error_code`: `"artifact_destination_not_durable"`
- `error_message`: names the missing durable destination, e.g.:
  > "output_prefix '/tmp/a7-train-artifacts' resolves under /tmp —
  > refusing to persist artifact to /tmp for a real job (artifact would
  > die with the worker). Set output_prefix to a /runpod-volume/ or
  > /workspace/ path, or pass presigned_artifact_url."
