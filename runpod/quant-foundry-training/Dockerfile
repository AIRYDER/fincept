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
# EVIDENCE (2026-07-03, controlled A/B tests on RunPod serverless ADA_24):
#   - python:3.12-slim + trivial handler: jobs picked up, COMPLETED in ~15s
#   - python:3.12-slim + pip torch CUDA + trivial handler: COMPLETED in ~15s
#   - nvidia/cuda:12.4.1 + trivial handler: worker "ready" but NO job pickup
#     (jobs stayed IN_QUEUE for 7+ minutes while worker showed ready=1,idle=1)
#   - pytorch/pytorch:2.4.1-cuda12.4: unhealthy in 90s
#   - runpod/base:1.0.2-cuda1281: unhealthy in 90s
#
# The nvidia/cuda and pytorch/pytorch base images break the runpod SDK's
# job dispatch mechanism (the HTTP webhook that delivers jobs to ready
# workers). The python:3.12-slim base works perfectly.
#
# Previous attempt (8c45c484) used python:3.12-slim but was missing
# libgomp1 (OpenMP runtime), causing xgboost/lightgbm/sklearn imports to
# crash. This version adds libgomp1 and other system libraries.
#
# PyTorch with CUDA 12.4 runtime is installed via pip from
# download.pytorch.org/whl/cu124 — the wheel includes CUDA runtime
# libraries (~2GB) so no CUDA base image is needed.
#
# Python 3.12 includes enum.StrEnum, datetime.UTC, and typing.Self
# natively, so the Python 3.10 compat shims are not needed.
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
#  - libgomp1: OpenMP runtime, required by xgboost, lightgbm, scikit-learn.
#    Without this, importing these libraries crashes with
#    "libgomp.so.1: cannot open shared object file" and the worker goes
#    unhealthy during handler import.
#  - libglib2.0-0: GLib, needed by some ML library shared objects.
#  - build-essential: headers needed by a few pip sdists.
#  - curl: used by diagnostics.
#  - ca-certificates: TLS root certs for pip and runpod SDK HTTP calls.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgomp1 \
    libglib2.0-0 \
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

# Test E: sentinel handler copied to /worker/handler.py to isolate whether
# the RunPod SDK job loop works inside the training image shape independent
# of the production handler's heavy imports. See
# docs/runpod-fix-plan/02-single-variable-tests.md Test E.
# The production handler remains available as handler_full.py for reference.
COPY runpod/quant-foundry-training/handler.py /worker/handler_full.py
COPY runpod/quant-foundry-training/handler_sentinel.py /worker/handler.py

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
ENV QUANT_FOUNDRY_TRAINING_DEADLINE_SECONDS="1800" \
    QUANT_FOUNDRY_USE_REAL_TRAINER="true" \
    QUANT_FOUNDRY_TRAINING_MODE="canary" \
    QF_DIAG_SKIP_PREFLIGHT="0" \
    PYTHONPATH=/worker

# ---------------------------------------------------------------------------
# NO HEALTHCHECK — deliberately removed (layer-isolation evidence 2026-07-03).
#
# The proven-working smoke image defines no HEALTHCHECK. RunPod serverless
# does its own worker health management; a Docker HEALTHCHECK is not
# required and must not be reintroduced without re-running the layer-0 probe.
# ---------------------------------------------------------------------------

# RunPod's serverless loader calls handler(event). Use ENTRYPOINT (not CMD) so
# RunPod's dockerArgs field can't override it. Direct python entrypoint matches
# the proven-working smoke and slim+CUDA patterns — no entrypoint.sh wrapper.
ENTRYPOINT ["python", "-u", "/worker/handler.py"]
