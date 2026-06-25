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
COPY services/quant_foundry/src/quant_foundry/ /worker/quant_foundry/

# Copy the handler.
COPY runpod/quant-foundry-training/handler.py /worker/handler.py

# Install dependencies. httpx is needed for the HTTP RunPod client
# (used when the handler needs to check status or health). pydantic
# is the core schema library.
RUN pip install --no-cache-dir \
    "pydantic>=2.7" \
    "httpx>=0.27"

# Pin the code git SHA at build time (for reproducibility).
# Override with --build-arg GIT_SHA=$(git rev-parse HEAD)
ARG GIT_SHA=unknown
ENV QUANT_FOUNDRY_GIT_SHA=${GIT_SHA}

# No broker credentials, no Redis, no trading env vars.
# Only the callback secret is required (injected at runtime).
ENV QUANT_FOUNDRY_CALLBACK_SECRET=""
ENV QUANT_FOUNDRY_TRAINING_DEADLINE_SECONDS="600"
ENV PYTHONPATH=/worker

# RunPod's serverless loader calls handler(event).
CMD ["python", "-u", "/worker/handler.py"]
