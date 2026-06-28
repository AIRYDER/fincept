# Quant Foundry Training Worker Container (TASK-0501)
#
# Minimal Python container for the RunPod training worker. No broker
# credentials, no Redis, no trading access. The worker only reads a
# RunPodTrainingRequest, trains a tiny baseline, and returns a signed
# callback envelope.
#
# Build (from repo root):
#   docker build -t fincept-qf-training:latest -f runpod/quant-foundry-training/Dockerfile .
#
# Run (local test):
#   echo '{"input": {"job_id": "qf:train:test:1", "dataset_manifest_ref": "ds-1", "model_family": "gbm", "search_space": {"n_estimators": [100]}, "random_seed": 42, "hardware_class": "mock-gpu"}}' | docker run --rm -i -e QUANT_FOUNDRY_CALLBACK_SECRET=secret fincept-qf-training:latest
#
# Run (with real GPU on RunPod):
#   The container is deployed as a RunPod serverless endpoint. RunPod
#   injects the GPU and calls handler(event) for each job. The only
#   required env var is QUANT_FOUNDRY_CALLBACK_SECRET (HMAC signing key).

FROM python:3.12-slim

WORKDIR /worker

# Install build dependencies for Python packages.
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Copy the quant_foundry source from the monorepo.
# NOTE: fincept_core is intentionally NOT copied. real_trainer.py imports it
# lazily behind try/except, so it is optional for the baked-in image.
COPY services/quant_foundry/src/quant_foundry/ /worker/quant_foundry/

# Copy the handler.
COPY runpod/quant-foundry-training/handler.py /worker/handler.py

# Install dependencies. `runpod` is the serverless worker SDK that
# registers the handler with RunPod's control plane and polls for jobs.
# Without it, the handler falls back to stdin mode which doesn't work
# on RunPod serverless (the platform doesn't pipe jobs via stdin).
# httpx is needed for the HTTP RunPod client (status/health checks).
# pydantic is the core schema library.
# Pin dependencies to compatible ranges for reproducible builds.
# Lower bounds ensure feature availability; upper bounds prevent
# breaking changes from unreleased major versions.
RUN pip install --no-cache-dir \
    "pydantic>=2.7,<3" \
    "httpx>=0.27,<1" \
    "runpod>=1.6,<2" \
    "lightgbm>=4.0,<5" \
    "pyarrow>=14.0,<20"

# Pin the code git SHA at build time (for reproducibility).
# Override with --build-arg GIT_SHA=$(git rev-parse HEAD)
ARG GIT_SHA=unknown
ENV QUANT_FOUNDRY_GIT_SHA=${GIT_SHA}

# No broker credentials, no Redis, no trading env vars.
# Only the callback secret is required (injected at runtime).
ENV QUANT_FOUNDRY_CALLBACK_SECRET=""
ENV QUANT_FOUNDRY_TRAINING_DEADLINE_SECONDS="1800"
# Use RealLightGBMTrainer (real LightGBM training with walk-forward validation).
ENV QUANT_FOUNDRY_USE_REAL_TRAINER="true"
ENV PYTHONPATH=/worker

# Create a non-root user for defense-in-depth. RunPod serverless workers
# don't need root — the handler only reads job input and writes callbacks.
RUN groupadd --system --gid 1001 qfworker \
    && useradd --system --uid 1001 --gid qfworker --home-dir /worker qfworker
USER qfworker

# Healthcheck: verify the Python runtime + handler module can load.
# RunPod polls this to decide whether to send jobs to the worker.
HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
  CMD python -c "import sys; sys.path.insert(0, '/worker'); import handler; print('ok')" || exit 1

# RunPod's serverless loader calls handler(event).
# Use ENTRYPOINT (not CMD) so RunPod's dockerArgs field can't override it.
ENTRYPOINT ["python", "-u", "/worker/handler.py"]
