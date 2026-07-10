# Artifact Flow — BEFORE (the gap)

## Problem

The A7 live `train_model` job ran the full pipeline (dataset load →
RealLightGBMTrainer walk-forward → final fit → pickle export with sha256
re-verification + HMAC write receipt), produced a 337 KB model with a valid
signed callback — and then the model **died with the worker** because
`output_prefix` defaulted to `/tmp/a7-train-artifacts`.

## Flow (BEFORE)

```
Request arrives
  ↓
output_prefix = input_data.pop("output_prefix", None)  # e.g. "/tmp/a7-train-artifacts"
  ↓
output_prefix = resolve_volume_path(output_prefix)     # /tmp stays /tmp (no volume)
  ↓
Training runs (GPU time spent)
  ↓
Writer selection:
  presigned_artifact_url? → PresignedUploadArtifactWriter
  output_prefix?          → VolumeArtifactWriter  ← writes to /tmp
  neither?                → FakeArtifactWriter
  ↓
VolumeArtifactWriter writes model.pkl to /tmp/a7-train-artifacts/
  - Re-reads bytes, re-hashes (sha256 verified ✓)
  - Writes callback_envelope.json, artifact_manifest.json, dossier.json
  - Returns file:///tmp/a7-train-artifacts/model.pkl
  ↓
Signed callback sent to dispatcher with artifact_uri = file:///tmp/...
  ↓
Worker shuts down (serverless container reclaimed)
  ↓
/tmp is ephemeral → model.pkl GONE
  ↓
Signed receipt points at file:///tmp/a7-train-artifacts/model.pkl → NOTHING THERE
```

## Gap

1. `output_prefix` was NOT validated — any path was accepted, including `/tmp`.
2. `/tmp` was NOT denied as a final destination for real jobs — it was the
   silent default when no volume was mounted.
3. No deny gate existed between resolving `output_prefix` and writing the
   artifact.
4. No standalone manifest verifier existed to independently re-check
   artifacts after the fact.
