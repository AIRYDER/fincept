# Quant Foundry Shadow Inference Container (TASK-0601)
#
# RunPod container for shadow-only model inference. This container runs
# candidate model predictions and returns shadow-only prediction batches
# that are settled against realized outcomes but never reach sig.predict.
#
# Build (from repo root):
#   docker build -t fincept-qf-inference:latest -f runpod/quant-foundry-inference/Dockerfile .
#
# Run (local test):
#   echo '{"input": {"request": {"job_id": "job-1", "artifact_ref": "file:///model.pkl", "symbols": ["AAPL"], "horizons_ns": [3600000000000]}, "snapshot": {"symbols": ["AAPL"], "features": {"AAPL": [0.1, 0.2]}, "availability": {"AAPL": true}, "ts_event": 1000, "freshness_ns": 500}, "model_id": "m1"}}' | docker run --rm -i -e QUANT_FOUNDRY_MODE=runpod_shadow fincept-qf-inference:latest
#
# Run (with real GPU on RunPod):
#   Deploy as a RunPod serverless endpoint. Set QUANT_FOUNDRY_MODE=runpod_shadow
#   to enable inference. The handler reads JSON from stdin (or the RunPod
#   event) and returns the callback envelope + predictions.

FROM python:3.12-slim

WORKDIR /app

# Install build dependencies.
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Copy the quant_foundry source from the monorepo.
COPY services/quant_foundry/src/quant_foundry/ /app/quant_foundry/

# Copy the handler.
COPY runpod/quant-foundry-inference/handler.py /app/handler.py

# Install dependencies. `runpod` is the serverless worker SDK that
# registers the handler with RunPod's control plane and polls for jobs.
RUN pip install --no-cache-dir \
    "pydantic>=2.7" \
    "httpx>=0.27" \
    "runpod>=1.6" \
    "onnxruntime>=1.17" \
    "lightgbm>=4.3" \
    "numpy>=1.26"

# Pin the code git SHA at build time (for reproducibility).
ARG GIT_SHA=unknown
ENV QUANT_FOUNDRY_GIT_SHA=${GIT_SHA}

# Set the entry point.
ENV QUANT_FOUNDRY_MODE=runpod_shadow
# Set QUANT_FOUNDRY_USE_REAL_INFERENCE=true to use RealInferenceEngine
# (loads ONNX/LightGBM model artifacts and runs real predictions).
# Default is false (ShadowInferenceEngine stub) for backward-compatible
# contract proofs.
ENV QUANT_FOUNDRY_USE_REAL_INFERENCE="false"
ENV PYTHONPATH=/app

ENTRYPOINT ["python", "-u", "/app/handler.py"]
