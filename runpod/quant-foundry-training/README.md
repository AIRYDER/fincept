# Quant Foundry Training Worker (TASK-0501)

This is the first RunPod worker for the Quant Foundry. It runs in a
container on RunPod's GPU infrastructure (or locally for testing). It
receives a `RunPodTrainingRequest`, trains a tiny baseline model, writes
an `ArtifactManifest` + `ModelDossier`, and returns a signed
`RunPodCallbackEnvelope` for the dispatcher to ingest.

## Security boundary (non-negotiable)

- **No broker credentials.** The container has no `FINCEPT_JWT_SECRET`,
  no `ALPACA_API_KEY`, no Redis URL, no stream producer. The worker is a
  pure function over its inputs.
- **No trading access.** The worker cannot emit signals, orders, or
  predictions to any trading stream. Its output is a *signed callback*
  that the Fincept-side dispatcher verifies before processing.
- **Shadow-only authority.** The `ModelDossier` always carries
  `authority=SHADOW_ONLY`. Promotion to live is a separate, human-gated
  decision (TASK-0702).
- **Signed callbacks.** The callback is HMAC-signed with
  `QUANT_FOUNDRY_CALLBACK_SECRET`. The dispatcher verifies the signature
  before processing (fail-closed on bad signature).

## Contract

The handler uses the **same** schemas, signatures, and callback envelope
as the mock dispatcher (TASK-0305). Flipping from mock to RunPod is a
dispatcher-only change — the outbox/inbox/signature contract stays
identical.

### Input (RunPod event)

```json
{
  "input": {
    "schema_version": 1,
    "job_id": "qf:train:gbm:h1:1",
    "dataset_manifest_ref": "ds-manifest-1",
    "model_family": "gbm",
    "search_space": {"n_estimators": [100, 200]},
    "random_seed": 42,
    "hardware_class": "mock-gpu",
    "extra_constraints": {}
  }
}
```

### Output (success)

```json
{
  "job_id": "qf:train:gbm:h1:1",
  "callback_payload": "<JSON-encoded RunPodCallbackEnvelope>",
  "callback_signature": "<HMAC signature>",
  "callback_ts": 1719000000,
  "artifact_id": "artifact:abc123def4567890",
  "dossier_id": "model:qf:train:gbm:h1:1"
}
```

### Output (failure)

```json
{
  "job_id": "qf:train:gbm:h1:1",
  "error_code": "timeout",
  "error_summary": "training deadline breached (deadline_seconds=600)"
}
```

## Build

```powershell
docker build -t fincept-qf-training:local runpod/quant-foundry-training
```

## Run (local test)

```powershell
echo '{"input": {"job_id": "qf:train:test:1", "dataset_manifest_ref": "ds-1", "model_family": "gbm", "search_space": {"n_estimators": [100]}, "random_seed": 42, "hardware_class": "mock-gpu"}}' | docker run --rm -i -e QUANT_FOUNDRY_CALLBACK_SECRET=secret fincept-qf-training:local
```

## Environment variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `QUANT_FOUNDRY_CALLBACK_SECRET` | yes (prod) | `dev-callback-secret-DO-NOT-USE-IN-PROD` | HMAC secret for signing callbacks |
| `QUANT_FOUNDRY_TRAINING_DEADLINE_SECONDS` | no | `600` | Max wall-clock seconds for training |
| `QUANT_FOUNDRY_USE_REAL_TRAINER` | no | `false` | Set to `true` to use `RealLightGBMTrainer` (real LightGBM with walk-forward validation). `false` uses `LocalTrainer` (deterministic stub). |

## Tests

The handler contract is tested locally (no Docker needed):

```powershell
uv run pytest services/quant_foundry/tests -q -k runpod_training
```

## Reproducibility pins

The `ArtifactManifest` pins the full reproducibility set:
- `feature_schema_hash` / `label_schema_hash` — derived from the request
- `code_git_sha` — pinned at container build time
- `lockfile_hash` — pinned at container build time
- `container_image_digest` — set at build time
- `random_seed` — from the request
- `hardware_class` — from the request

Re-running the same request on the same hardware class must produce the
same `artifact_id` / `sha256`. Any known nondeterminism source is
recorded, not hidden.
