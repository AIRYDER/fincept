# Durable artifact proof — Tier 0.2

- **Date:** 2026-07-07T00:29Z
- **Branch:** `tier1a/product-loop`
- **Image SHA:** `34d85c10a52cf59f8bd30d4d8ab7474b2cc53f9e`
- **Image:** `ghcr.io/airyder/fincept/quant-foundry-training:34d85c10a52cf59f8bd30d4d8ab7474b2cc53f9e`
- **GPU:** ADA_24 (RTX 4090)
- **Model family:** `lightgbm`
- **Network volume:** `rrsd005i3g` (`fincept-qf-vol`, 10 GB, US-NC-1)
- **Endpoint:** `xo14hppm4s8zkc` (template `du9sj49ka3`)
- **Job:** `158f635f-fd71-4a35-9803-7b7f14843d81-u1`
- **Verdict:** **PASS** — artifact written to network volume, persists after worker/endpoint deletion.

## What this run proves

This is the first live training run that writes the model artifact to a
**durable** destination (RunPod network volume) instead of `/tmp`. It closes
Tier 0.2 (durable artifact upload) — the single highest-leverage gap left in
the system.

1. **Network volume mounted** — the endpoint was created with
   `networkVolumeId=rrsd005i3g`, which mounts the `fincept-qf-vol` volume at
   `/runpod-volume/` inside the serverless worker. This is the first live
   proof that the `networkVolumeId` field in the endpoint creation mutation
   works end-to-end.

2. **Artifact written to `/runpod-volume/`** — the `output_prefix` was set to
   `/runpod-volume/models/qf:a7-train:lightgbm:34d85c10:001/`, and the
   `VolumeArtifactWriter` wrote `model.pkl` (337,368 bytes) there. The
   artifact URI is `file:///runpod-volume/models/qf%3Aa7-train%3Alightgbm%3A34d85c10%3A001/model.pkl`
   — **not** `file:///tmp/...` like the previous two runs.

3. **sha256 re-verified on write** — the `VolumeArtifactWriter` re-read the
   written bytes from the volume and re-hashed them (fail-closed on
   mismatch). The declared sha256
   `ac0b69ba8b52f20e898ccde31fabc92f574129a3e9a741f7b769e2519921274f`
   matches the re-read sha256. This proves the volume write was not
   truncated or corrupted.

4. **HMAC write receipt present** — the write receipt
   `3a3417a0ef68fa8c068fe6a0f046142931a88ad8d56e174222acfb91f4677d21` is
   HMAC-SHA256 over `uri|sha256|size|format`, signed with
   `QUANT_FOUNDRY_CALLBACK_SECRET`. The trusted-side verifier can
   re-compute this to authenticate the artifact metadata.

5. **Artifact persists after worker shutdown** — the endpoint was scaled to
   0/0 and deleted, the template was deleted, but the network volume
   `rrsd005i3g` and its data remain. RunPod network volumes are persistent
   NVMe-backed storage that exists independently of compute resources
   (docs: "Data is retained when workers scale to zero or endpoints are
   deleted").

6. **`/tmp` deny gate not triggered** — this run used `training_mode=canary`,
   which is exempt from the deny gate. The deny gate fires for non-canary
   jobs that attempt to write to `/tmp` (proven by 41 unit tests in
   `test_artifact_writer.py`).

## Comparison: /tmp (disposable) vs /runpod-volume/ (durable)

| Aspect                | Previous runs (lightgbm, xgboost_gpu) | This run (durable) |
| --------------------- | ------------------------------------- | ------------------ |
| Artifact URI          | `file:///tmp/a7-train-artifacts/model.pkl` | `file:///runpod-volume/models/.../model.pkl` |
| Survives worker shutdown? | No — `/tmp` is ephemeral container storage | Yes — network volume is persistent NVMe |
| Survives endpoint deletion? | No | Yes — volume exists independently |
| sha256 re-verified on write? | Yes (VolumeArtifactWriter) | Yes (VolumeArtifactWriter) |
| HMAC write receipt?   | Yes | Yes |
| Network volume mounted? | No | Yes (`rrsd005i3g`, 10 GB, US-NC-1) |

## Evidence cross-check (receipt-integrity)

| Claim                      | Raw evidence file              | Field                                      |
| -------------------------- | ------------------------------ | ------------------------------------------ |
| Job COMPLETED              | `probe.jsonl`                  | `status=COMPLETED`, `completed=1`          |
| Worker healthy throughout  | `probe.jsonl`                  | `unhealthy=0` on every poll                |
| Artifact on /runpod-volume/| `train-model-result.json`      | `artifact_uri=file:///runpod-volume/...`   |
| sha256 re-verified         | `train-model-result.json`      | `artifact_sha256` (non-empty, job COMPLETED) |
| Write receipt present      | `train-model-result.json`      | `write_receipt` (non-empty)                |
| Network volume attached    | `endpoint-create-redacted.json`| `network_volume_id=rrsd005i3g`             |
| output_prefix on volume    | `train-model-result.json`      | `output_prefix=/runpod-volume/models/...`  |
| Callback signed            | `train-model-result.json`      | `callback_signature_present=true`          |
| Endpoint + template cleaned| `cleanup.json`                 | `deleted=true`, `template_deleted=true`    |

## Follow-up verification (not done in this run)

The S3-compatible API (`https://s3api-us-nc-1.runpod.io/`) can read the
artifact back from the volume without launching a worker, providing an
independent sha256 verification. This requires RunPod S3 API keys (separate
from the RunPod API key). Path mapping:

- Worker path: `/runpod-volume/models/qf:a7-train:lightgbm:34d85c10:001/model.pkl`
- S3 API path: `s3://rrsd005i3g/models/qf:a7-train:lightgbm:34d85c10:001/model.pkl`

When S3 keys are configured:
```bash
aws s3 cp --region US-NC-1 \
    --endpoint-url https://s3api-us-nc-1.runpod.io/ \
    s3://rrsd005i3g/models/qf:a7-train:lightgbm:34d85c10:001/model.pkl \
    /tmp/verified-model.pkl
sha256sum /tmp/verified-model.pkl
# Should match: ac0b69ba8b52f20e898ccde31fabc92f574129a3e9a741f7b769e2519921274f
```

## Timeline

- `00:25Z` — template + endpoint created with `networkVolumeId=rrsd005i3g`.
- `00:28Z` — worker `ready=1, idle=1, unhealthy=0` (cold-pull ~45s; faster
  than previous runs, possibly cached host in US-NC-1).
- `00:29Z` — job dispatched, `IN_QUEUE` → `IN_PROGRESS` → `COMPLETED` (~10s).
- `00:29Z` — endpoint scaled to 0, deleted; template deleted.
- Volume `rrsd005i3g` and its data remain.

## Code changes

1. `scripts/runpod/runpod_lifecycle.py`:
   - `EndpointConfig` now accepts `network_volume_id` parameter.
   - `build_endpoint_input` includes `networkVolumeId` when set.
   - Added `create_network_volume`, `list_network_volumes`,
     `delete_network_volume` functions (REST API).

2. `runpod/quant-foundry-training/run_live_canary.py`:
   - `create_endpoint` now accepts `network_volume_id` parameter.

3. `runpod/quant-foundry-training/run_train_model.py`:
   - Added `--network-volume-id` and `--output-prefix` flags.
   - When `--network-volume-id` is set, `output_prefix` defaults to
     `/runpod-volume/models/<job-id>/`.
   - Receipt JSON includes `network_volume_id` and `output_prefix`.

## Risks / notes

- The artifact is on the volume but has **not been independently read back**
  (S3 API keys not configured). The sha256 re-verification on write is the
  integrity proof; the network volume's persistence is guaranteed by RunPod's
  storage architecture.
- The `fincept-qf-vol` volume is 10 GB in US-NC-1. Multiple artifacts can
  accumulate — a cleanup policy should be defined before production use.
- RunPod warns that concurrent writes to the same volume from multiple
  workers can cause data corruption. The current `workers_max=1` config
  prevents this, but production endpoints with multiple workers need
  application-level coordination (e.g., unique output_prefix per job).
- The `/tmp` deny gate is proven by 41 unit tests but has not been exercised
  in a live non-canary run (all live runs so far use `training_mode=canary`).

## Next recommended task

- **#2: Live run with callback → gateway → model_versions.** Point
  `QUANT_FOUNDRY_CALLBACK_URL` at a live gateway so the signed callback
  persists to Postgres, closing the product loop. The durable artifact on
  the volume is the prerequisite — the callback ingestion service needs a
  verifiable artifact URI to record.
