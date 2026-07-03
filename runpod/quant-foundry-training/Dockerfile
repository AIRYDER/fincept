# Quant Foundry Training Worker Container — trainer-gpu-tree (T-4.2)
#
# CUDA-capable RunPod serverless worker for tree-model training
# (XGBoost GPU / CatBoost GPU / LightGBM CPU baseline). No broker
# credentials, no Redis, no trading access. The worker only reads a
# RunPodTrainingRequest, trains a baseline, and returns a signed
# callback envelope.
#
# Build (from repo root):
#   DOCKER_BUILDKIT=1 docker build \
#     -t fincept-qf-training:gpu-tree \
#     -f runpod/quant-foundry-training/Dockerfile \
#     --build-arg GIT_SHA=$(git rev-parse HEAD) .
#
# Run (local CPU smoke — healthcheck will report no GPU, canary still runs):
#   echo '{"input": {"task": "gpu_healthcheck", "mode": "canary"}}' | \
#     docker run --rm -i -e QUANT_FOUNDRY_CALLBACK_SECRET=secret \
#     fincept-qf-training:gpu-tree
#
# Run (with real GPU on RunPod):
#   Deploy as a RunPod serverless endpoint with an NVIDIA GPU template
#   (e.g. RTX 4090 / A100). RunPod injects the GPU and calls
#   handler(event) for each job. The only required env var is
#   QUANT_FOUNDRY_CALLBACK_SECRET (HMAC signing key).

# ---------------------------------------------------------------------------
# Base image: python:3.12-slim (NOT nvidia/cuda or pytorch/pytorch).
#
# EVIDENCE (2026-07-03):
#   - python:3.12-slim + runpod SDK: jobs picked up and completed in ~15s.
#   - nvidia/cuda:12.4.1-runtime-ubuntu22.04 + runpod SDK: worker shows
#     "ready" but NEVER picks up jobs (jobs stay IN_QUEUE forever).
#   - pytorch/pytorch:2.4.1-cuda12.4-cudnn9-runtime: worker goes unhealthy
#     within 90 seconds on ADA_24 GPUs.
#   - runpod/base:1.0.2-cuda1281-ubuntu2204: same unhealthy symptom.
#
# The nvidia/cuda and pytorch/pytorch base images break the runpod SDK's
# job dispatch mechanism (the HTTP webhook that delivers jobs to ready
# workers). The python:3.12-slim base works perfectly. We install PyTorch
# with CUDA runtime libraries via pip from download.pytorch.org, which
# brings the CUDA runtime into the container without a CUDA base image.
#
# Python 3.12 includes enum.StrEnum, datetime.UTC, and typing.Self
# natively, so the Python 3.10 compat shims are no longer needed.
# ---------------------------------------------------------------------------
FROM python:3.12-slim

# Prevent Python from buffering stdout/stderr so RunPod captures logs live.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    DEBIAN_FRONTEND=noninteractive

WORKDIR /worker

# Install OS dependencies:
#  - build-essential: headers needed by a few pip sdists.
#  - curl: used by diagnostics.
#  - ca-certificates: TLS root certs for pip and runpod SDK HTTP calls.
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Copy the quant_foundry source from the monorepo.
COPY services/quant_foundry/src/quant_foundry/ /worker/quant_foundry/

# Copy fincept_core.datasets — needed for fincept_core.datasets.cv.make_folds
# (canonical walk-forward fold math used by RealLightGBMTrainer).
# We copy only the datasets subpackage + its deps (naming, prediction_log,
# schemas) to avoid pulling in redis and storage-heavy service modules.
COPY libs/fincept-core/src/fincept_core/datasets/ /worker/fincept_core/datasets/
COPY libs/fincept-core/src/fincept_core/naming.py /worker/fincept_core/naming.py
COPY libs/fincept-core/src/fincept_core/prediction_log.py /worker/fincept_core/prediction_log.py
COPY libs/fincept-core/src/fincept_core/schemas.py /worker/fincept_core/schemas.py
COPY libs/fincept-core/src/fincept_core/errors.py /worker/fincept_core/errors.py
COPY libs/fincept-core/src/fincept_core/clock.py /worker/fincept_core/clock.py
COPY libs/fincept-core/src/fincept_core/config.py /worker/fincept_core/config.py
# Create a minimal __init__.py that only imports the datasets subpackage
# (avoids importing storage/heartbeat which need redis).
RUN python -c "import pathlib; p=pathlib.Path('/worker/fincept_core/__init__.py'); p.write_text('from . import datasets, naming, prediction_log, schemas, errors, clock, config\n__all__=[\"datasets\",\"naming\",\"prediction_log\",\"schemas\",\"errors\",\"clock\",\"config\"]\n', encoding='utf-8')"

COPY runpod/shared/worker_status.py /worker/shared/worker_status.py

# Copy the production handler directly (no layered wrapper).
# The layered handler was a diagnostic tool for isolating crash causes;
# now that the root cause (nvidia/cuda base image) is identified and fixed,
# the real handler is used directly.
COPY runpod/quant-foundry-training/handler.py /worker/handler.py

# ---------------------------------------------------------------------------
# Install GPU-capable ML libraries.
#
#  - torch: PyTorch with CUDA 12.4 runtime libraries from pytorch.org.
#           The wheel includes the CUDA runtime (~2GB) so no CUDA base
#           image is needed. Installed first (separate layer) so the
#           large download is cached independently.
#  - xgboost:  the official manylinux wheel is built with CUDA support since
#              2.0; `device="cuda"` works with the torch-bundled CUDA runtime.
#  - catboost: the official wheel ships GPU support (`task_type="GPU"`).
#  - lightgbm: the official PyPI wheel is CPU-only (GPU requires a custom
#              OpenCL/CUDA build). We install it as the CPU baseline.
#  - pandas / pyarrow / scikit-learn / numpy: data handling + walk-forward
#    fold math used by RealLightGBMTrainer.
#  - pydantic / pydantic-settings / httpx / runpod: handler runtime +
#    RunPod serverless SDK. pydantic-settings is required by the copied
#    fincept_core.config module, which is imported through fincept_core.
# ---------------------------------------------------------------------------
RUN pip install --no-cache-dir "torch==2.4.1" \
    --index-url https://download.pytorch.org/whl/cu124

RUN pip install --no-cache-dir \
    "pydantic>=2.7,<3" \
    "pydantic-settings>=2.0,<3" \
    "httpx>=0.27,<1" \
    "runpod==1.7.13" \
    "numpy>=1.26,<3" \
    "pandas>=2.0,<3" \
    "pyarrow>=14.0,<20" \
    "scikit-learn>=1.4,<2" \
    "lightgbm>=4.0,<5" \
    "xgboost>=2.0,<3" \
    "catboost>=1.2,<2"

# ---------------------------------------------------------------------------
# Startup security preflight.
#
# A small Python script that runs BEFORE the handler starts. It:
#   1. Fails closed if any forbidden env var is set (broker secrets, Redis,
#      DB write URLs, trading keys). The worker must be a pure function over
#      its inputs — it must never carry trading/broker credentials.
#   2. Validates the callback URL host (if QUANT_FOUNDRY_CALLBACK_URL is set)
#      rejects loopback/private/link-local hosts in production.
#   3. Prints a REDACTED config summary so operators can audit the runtime
#      without leaking secrets.
#
# The preflight is run by the handler's __main__ block before
# runpod.serverless.start(). It can be skipped with QF_DIAG_SKIP_PREFLIGHT=1.
# ---------------------------------------------------------------------------
COPY runpod/quant-foundry-training/preflight.py /worker/preflight.py

# ---------------------------------------------------------------------------
# Non-root user. RunPod network volumes are mounted with permissive perms,
# so a non-root user is safe. The security boundary is still the container
# itself (no broker creds, no Redis), but running as non-root adds defense
# in depth against privilege escalation inside the container.
# ---------------------------------------------------------------------------
RUN useradd --create-home --shell /bin/bash --uid 1000 trainer \
    && chown -R trainer:trainer /worker

# Pin the code git SHA at build time (for reproducibility).
ARG GIT_SHA=unknown
ENV QUANT_FOUNDRY_GIT_SHA=${GIT_SHA}

# No broker credentials, no Redis, no trading env vars.
# Only the callback secret is required (injected at runtime).
ENV QUANT_FOUNDRY_CALLBACK_SECRET="" \
    QUANT_FOUNDRY_TRAINING_DEADLINE_SECONDS="1800" \
    QUANT_FOUNDRY_USE_REAL_TRAINER="true" \
    QUANT_FOUNDRY_TRAINING_MODE="canary" \
    QF_DIAG_SKIP_PREFLIGHT="0" \
    PYTHONPATH=/worker

# ---------------------------------------------------------------------------
# NO HEALTHCHECK — deliberately removed (layer-isolation evidence 2026-07-03).
#
# The proven-working smoke image defines no HEALTHCHECK. With a HEALTHCHECK
# at --interval=300s, Docker reports the container as "health: starting"
# until the FIRST probe fires at t=+300s. RunPod serverless does its own
# worker health management; a Docker HEALTHCHECK is not required and must
# not be reintroduced without re-running the layer-0 probe.
# ---------------------------------------------------------------------------

# RunPod's serverless loader calls handler(event). Use ENTRYPOINT (not CMD) so
# RunPod's dockerArgs field can't override it. Direct python entrypoint matches
# the proven-working smoke and slim+CUDA patterns — no entrypoint.sh wrapper.
ENTRYPOINT ["python", "-u", "/worker/handler.py"]
