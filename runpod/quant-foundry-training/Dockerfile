# Quant Foundry Training Worker Container (TASK-0501)
#
# Minimal Python container for the RunPod training worker. No broker
# credentials, no Redis, no trading access. The worker only reads a
# RunPodTrainingRequest, trains a tiny baseline, and returns a signed
# callback envelope.
#
# Build:
#   docker build -t fincept-qf-training:local runpod/quant-foundry-training
#
# Run (local test):
#   echo '{"input": {"job_id": "qf:train:test:1", "dataset_manifest_ref": "ds-1", "model_family": "gbm", "search_space": {"n_estimators": [100]}, "random_seed": 42, "hardware_class": "mock-gpu"}}' | docker run --rm -i -e QUANT_FOUNDRY_CALLBACK_SECRET=secret fincept-qf-training:local

FROM python:3.12-slim

WORKDIR /worker

# Install only the quant_foundry package (no broker/Redis/trading deps).
# In a real build, this would copy the built wheel from the monorepo.
# For MVP, we copy the source and install with uv or pip.
COPY handler.py /worker/handler.py
COPY quant_foundry/ /worker/quant_foundry/

RUN pip install --no-cache-dir pydantic>=2.7

# No broker credentials, no Redis, no trading env vars.
# Only the callback secret is required (injected at runtime).
ENV QUANT_FOUNDRY_CALLBACK_SECRET=""
ENV QUANT_FOUNDRY_TRAINING_DEADLINE_SECONDS="600"

# RunPod's serverless loader calls handler(event).
CMD ["python", "-u", "/worker/handler.py"]
