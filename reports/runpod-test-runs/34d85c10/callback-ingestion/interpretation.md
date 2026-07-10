# Callback Ingestion Proof — Live Run (Tier 1.1)

**Date**: 2026-07-06
**SHA**: `34d85c10a52cf59f8bd30d4d8ab7474b2cc53f9e`
**Image**: `ghcr.io/airyder/fincept/quant-foundry-training:34d85c10a52cf59f8bd30d4d8ab7474b2cc53f9e`
**GPU**: ADA_24 (RTX 4090)
**Verdict**: **PASS**

## What was proved

The full product loop — from live RunPod dispatch through signed callback
ingestion into a database to model version registration — works end-to-end.

```
dispatch → RunPod worker trains LightGBM → signed callback
  → gateway polls RunPod → verifies HMAC signature
  → ingests into SQLite (callback_receipts, model_dossiers,
     artifact_manifests, training_jobs)
  → registers model_version from dossier
```

## Evidence

| Table                | Rows | Key value                                                              |
|----------------------|------|------------------------------------------------------------------------|
| callback_receipts    | 1    | status=**processed**, callback_id=`cb:qf:cbproof:34d85c10:1783388382`  |
| model_dossiers       | 1    | model_id=`model:qf:cbproof:34d85c10:1783388382`, status=candidate      |
| artifact_manifests   | 1    | sha256=`af9a7ddc34f9c686...`, artifact_id=`artifact:af9a7ddc34f9c686`  |
| training_jobs        | 1    | status=**completed**, callback_receipt_id linked                        |
| model_versions       | 1    | version_id=`version:cbproof:34d85c10:001`, status=**candidate**        |

## Flow detail

1. **Endpoint created**: template `x9qy47srnd` + endpoint `2e4exkin3mnkj6`
2. **Worker readiness**: ~25s for GPU worker to become ready
3. **Job dispatched**: `qf:cbproof:34d85c10:1783388382` via `gateway.create_job()`
4. **Polling**: First poll = `IN_PROGRESS` (still running). Second poll =
   `COMPLETED` → gateway extracted signed callback fields from RunPod output,
   verified HMAC signature, ingested into DB.
5. **DB verification**: All 5 tables populated correctly. Training job
   status = `completed` with `callback_receipt_id` linked.
6. **Model version registered**: `ModelRegistryDB.register_version()` created
   a `candidate` version linked to the dossier content hash + artifact ID.
7. **Cleanup**: Endpoint + template deleted.

## Issues found and fixed during this proof

1. **Corrupted files**: Earlier edits corrupted `runpod_lifecycle.py`,
   `run_live_canary.py`, and `test_runpod_lifecycle.py`. Restored from HEAD.
2. **pytest import conflict**: Root `runpod/` directory (namespace package)
   shadowed `scripts/runpod/` (regular package). Fixed with `conftest.py`
   that force-imports the correct package + `pythonpath = ["scripts"]` in
   `pyproject.toml`.
3. **Schema validation failure**: Payload included extra fields
   (`gpu_type`, `gpu_count`, `execution_timeout_ms`, `container_image`)
   not in `RunPodTrainingRequest` schema (`extra="forbid"`). Removed.
4. **Stale inbox**: Previous run's inbox file caused `payload_hash_mismatch`
   security error. Fixed by using timestamped job_id + cleaning stale state.

## Receipts

- `endpoint-create-redacted.json` — endpoint + template IDs (redacted)
- `health-before.json` — worker health before dispatch
- `poll-receipt.json` — final poll receipt from gateway
- `verification.json` — full DB verification results
- `cleanup.json` — endpoint + template deletion confirmation

## Next steps

- **Tier 1.1 complete**: Callback ingestion is proven live.
- **Postgres migration**: Move from in-memory SQLite to `fincept-db` Postgres
  by setting `FINCEPT_DB_URL` + `QUANT_FOUNDRY_SINK_BACKEND=db`.
- **Tier 1.2**: Model registry — the `register_version()` call already works;
  wire it into the promotion gate for production promotion.
