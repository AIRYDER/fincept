# Quant Foundry Training Worker — trainer-gpu-tree (T-4.2)

This is the GPU RunPod serverless worker for the Quant Foundry. It runs in a
CUDA-capable container on RunPod's GPU infrastructure (or locally for
testing). It receives a `RunPodTrainingRequest`, trains a tree-model
baseline (XGBoost GPU / CatBoost GPU / LightGBM CPU baseline), writes an
`ArtifactManifest` + `ModelDossier`, and returns a signed
`RunPodCallbackEnvelope` for the dispatcher to ingest.

## Image: `trainer-gpu-tree`

The Dockerfile builds the **`fincept-qf-training:gpu-tree`** image:

- **Base:** `pytorch/pytorch:2.4.1-cuda12.4-cudnn9-runtime` (Python 3.11 +
  CUDA 12.4 + cuDNN 9). A real CUDA runtime base — not `python:slim`.
- **GPU libraries:**
  - **XGBoost GPU** — `xgboost>=2.0` (official wheel ships CUDA support;
    use `device="cuda"`).
  - **CatBoost GPU** — `catboost>=1.2` (official wheel ships GPU support;
    use `task_type="GPU"`).
  - **LightGBM** — `lightgbm>=4.0` installed as a **CPU baseline only**.
    The official PyPI wheel is CPU-only (GPU requires a custom OpenCL/CUDA
    build that is not reliably reproducible from a Dockerfile). The
    `gpu_healthcheck` task reports `lightgbm_gpu=False` for this image.
    RealLightGBMTrainer therefore runs on CPU; GPU tree training is served
    by XGBoost/CatBoost.
- **Data libs:** `pandas`, `pyarrow`, `scikit-learn`, `numpy`.
- **Runtime:** `pydantic`, `httpx`, `runpod` (serverless SDK).

## Security boundary (non-negotiable)

- **No broker credentials.** The container has no `FINCEPT_JWT_SECRET`,
  no `ALPACA_API_KEY`, no Redis URL, no stream producer. The worker is a
  pure function over its inputs.
- **Startup security preflight.** The entrypoint runs `/worker/preflight.py`
  before the handler starts. It **fails closed** (exit 2) if any forbidden
  env var is present: `REDIS_URL`, `REDIS_HOST`, `FINCEPT_JWT_SECRET`,
  `ALPACA_API_KEY`, `ALPACA_SECRET_KEY`, `ALPACA_API_SECRET`,
  `DATABASE_URL`, `DB_URL`, `POSTGRES_URL`, `KAFKA_BOOTSTRAP_SERVERS`,
  `BROKER_URL`, `AMQP_URL`, `MONGO_URL`, `MONGODB_URI`. It also validates
  the callback URL host (rejects loopback/private hosts in `production`
  mode) and prints a **redacted** config summary.
- **No trading access.** The worker cannot emit signals, orders, or
  predictions to any trading stream. Its output is a *signed callback*
  that the Fincept-side dispatcher verifies before processing.
- **Shadow-only authority.** The `ModelDossier` always carries
  `authority=SHADOW_ONLY`. Promotion to live is a separate, human-gated
  decision (TASK-0702).
- **Signed callbacks.** The callback is HMAC-signed with
  `QUANT_FOUNDRY_CALLBACK_SECRET`. The dispatcher verifies the signature
  before processing (fail-closed on bad signature).
- **Non-root user.** The image creates a `trainer` user (uid 1000). The
  entrypoint runs as root only long enough to (a) run the security
  preflight and (b) chown the RunPod network volume (`/runpod-volume`,
  `/workspace`) so the non-root user can write artifacts/status, then
  drops privileges via `gosu` and execs the handler as `trainer`.

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
    "hardware_class": "gpu-a100",
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

Build from the **repo root** (the Dockerfile `COPY`s paths relative to the
repo root). Use BuildKit for the heredoc-based preflight/entrypoint scripts.

```powershell
# Linux/macOS
DOCKER_BUILDKIT=1 docker build \
  -t fincept-qf-training:gpu-tree \
  -f runpod/quant-foundry-training/Dockerfile \
  --build-arg GIT_SHA=$(git rev-parse HEAD) .

# PowerShell
$env:DOCKER_BUILDKIT=1
docker build -t fincept-qf-training:gpu-tree `
  -f runpod/quant-foundry-training/Dockerfile `
  --build-arg GIT_SHA=$(git rev-parse HEAD) .
```

> **Note:** The build context must be the repo root because the Dockerfile
> copies `services/quant_foundry/src/`, `libs/fincept-core/src/`, and
> `runpod/shared/`.

## GPU requirements

- **NVIDIA GPU** with CUDA 12.4+ compatible driver (RunPod templates
  provide this). Tested targets: RTX 4090, A100, L40S.
- The Docker daemon host must have the NVIDIA Container Toolkit installed
  (`nvidia-docker2`) so `--gpus all` / RunPod's GPU injection works.
- The container `HEALTHCHECK` runs `nvidia-smi`; on a host without a GPU
  the healthcheck fails (exit 1) so a misrouted CPU deployment is surfaced.

## Environment variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `QUANT_FOUNDRY_CALLBACK_SECRET` | yes (prod) | `dev-callback-secret-DO-NOT-USE-IN-PROD` | HMAC secret for signing callbacks |
| `QUANT_FOUNDRY_TRAINING_DEADLINE_SECONDS` | no | `1800` | Max wall-clock seconds for training |
| `QUANT_FOUNDRY_USE_REAL_TRAINER` | no | `true` | `true` → `RealLightGBMTrainer` (real LightGBM, walk-forward CV). `false` → `LocalTrainer` (deterministic stub). |
| `QUANT_FOUNDRY_TRAINING_MODE` | no | `canary` | `canary` \| `research` \| `production`. Drives preflight callback-URL strictness and `gpu_healthcheck` fail-closed behavior. |
| `QUANT_FOUNDRY_CALLBACK_URL` | no | _unset_ | Optional callback POST target. If set in `production` mode, the host must not be loopback/private. |
| `QUANT_FOUNDRY_GIT_SHA` | no | `unknown` | Pinned at build time via `--build-arg GIT_SHA`. |

**Forbidden env vars** (preflight fails closed if any is set):
`REDIS_URL`, `REDIS_HOST`, `FINCEPT_JWT_SECRET`, `ALPACA_API_KEY`,
`ALPACA_SECRET_KEY`, `ALPACA_API_SECRET`, `DATABASE_URL`, `DB_URL`,
`POSTGRES_URL`, `KAFKA_BOOTSTRAP_SERVERS`, `BROKER_URL`, `AMQP_URL`,
`MONGO_URL`, `MONGODB_URI`.

## RunPod deployment configuration

1. **Create a serverless endpoint** in the RunPod dashboard.
2. **Image:** `fincept-qf-training:gpu-tree` (push to your registry, e.g.
   `ghcr.io/<org>/fincept-qf-training:gpu-tree`).
3. **GPU template:** select an NVIDIA GPU (RTX 4090 / A100 / L40S).
4. **Container disk:** ≥ 20 GB (CUDA image + model artifacts).
5. **Network volume** (optional, for artifact persistence): mount at
   `/runpod-volume`. The entrypoint chowns it to the `trainer` user at
   startup so non-root writes work.
6. **Env vars:** set `QUANT_FOUNDRY_CALLBACK_SECRET` (use a RunPod secret).
   Do **not** set any forbidden env var — the preflight will reject startup.
7. **Handler path:** RunPod's serverless loader calls `handler(event)` in
   `/worker/handler.py` (the entrypoint execs it as the `trainer` user).

### RunPod template (JSON, serverless)

```json
{
  "image": "ghcr.io/<org>/fincept-qf-training:gpu-tree",
  "gpu_type": "NVIDIA RTX 4090",
  "container_disk_gb": 20,
  "env": {
    "QUANT_FOUNDRY_CALLBACK_SECRET": "<secret>",
    "QUANT_FOUNDRY_USE_REAL_TRAINER": "true",
    "QUANT_FOUNDRY_TRAINING_MODE": "production"
  },
  "volumes": [{"path": "/runpod-volume", "name": "qf-artifacts"}]
}
```

## GPU healthcheck usage

The handler exposes a `gpu_healthcheck` task (T-4.1) that probes the worker's
GPU runtime and returns signed metadata. It runs `nvidia-smi`, records CUDA
version, driver version, GPU model, GPU memory, and library GPU flags
(`lightgbm_gpu`, `xgboost_gpu`, `catboost_gpu`).

### Run the GPU healthcheck

```bash
echo '{"input": {"task": "gpu_healthcheck", "mode": "canary"}}' | \
  docker run --rm -i --gpus all \
  -e QUANT_FOUNDRY_CALLBACK_SECRET=secret \
  fincept-qf-training:gpu-tree
```

Modes:
- **`production`** — fails closed if `gpu_capable=false` (a production run
  MUST execute on a GPU worker).
- **`canary`** — may report GPU absence but marks `promotion_eligible=false`.
- **`research`** — permissive; reports the GPU state without failing.

The Docker `HEALTHCHECK` (container-level liveness) additionally runs
`nvidia-smi` + imports the handler on every poll interval, so a host
without a GPU is surfaced as an unhealthy container.

## Canary training instructions

A small tree-training canary proves the full GPU training loop end-to-end.
Dispatch a minimal training job (small search space, short deadline) and
verify the signed callback + artifact are returned.

### 1. CPU smoke (no GPU — canary mode, real trainer)

```bash
echo '{"input": {"job_id": "qf:canary:tree:1", "dataset_manifest_ref": "ds-1", "model_family": "gbm", "search_space": {"n_estimators": [50]}, "random_seed": 42, "hardware_class": "mock-gpu", "extra_constraints": {"training_mode": "canary"}}}' | \
  docker run --rm -i \
  -e QUANT_FOUNDRY_CALLBACK_SECRET=secret \
  -e QUANT_FOUNDRY_USE_REAL_TRAINER=true \
  fincept-qf-training:gpu-tree
```

### 2. GPU canary (real GPU — XGBoost/CatBoost GPU path)

```bash
echo '{"input": {"job_id": "qf:canary:tree:gpu:1", "dataset_manifest_ref": "ds-1", "model_family": "gbm", "search_space": {"n_estimators": [100]}, "random_seed": 42, "hardware_class": "gpu-rtx4090", "extra_constraints": {"training_mode": "canary"}}}' | \
  docker run --rm -i --gpus all \
  -e QUANT_FOUNDRY_CALLBACK_SECRET=secret \
  -e QUANT_FOUNDRY_USE_REAL_TRAINER=true \
  fincept-qf-training:gpu-tree
```

**Acceptance:** the response contains `callback_payload`, `callback_signature`,
`artifact_id`, and `dossier_id` with no `error_code`. On a RunPod GPU the
`gpu_healthcheck` field in the callback reports `gpu_capable=true`,
`xgboost_gpu=true`, `catboost_gpu=true`, `lightgbm_gpu=false` (CPU baseline).

## Tests

The handler contract is tested locally (no Docker needed):

```powershell
uv run pytest services/quant_foundry/tests -q -k runpod_training
```

## Reproducibility pins

The `ArtifactManifest` pins the full reproducibility set:
- `feature_schema_hash` / `label_schema_hash` — derived from the request
- `code_git_sha` — pinned at container build time (`--build-arg GIT_SHA`)
- `lockfile_hash` — pinned at container build time
- `container_image_digest` — set at build time
- `random_seed` — from the request
- `hardware_class` — from the request

Re-running the same request on the same hardware class must produce the
same `artifact_id` / `sha256`. Any known nondeterminism source is
recorded, not hidden.

## LightGBM GPU note

LightGBM GPU support requires a custom build against OpenCL/CUDA and is
**not** available in the official PyPI wheel. This image installs LightGBM
as a **CPU baseline only**. GPU tree training is served by XGBoost
(`device="cuda"`) and CatBoost (`task_type="GPU"`), both of which ship
GPU-enabled official wheels. If a GPU LightGBM build becomes reliably
reproducible, add a custom-build stage to the Dockerfile and flip the
`lightgbm_gpu` flag expectation in the healthcheck.
