# Minimal CUDA test image — isolates base image vs heavy imports.
#
# Uses RunPod's own CUDA base image which is designed for their serverless
# runtime. The pytorch/pytorch Docker Hub image was causing workers to go
# unhealthy within 1-2 minutes of initialization.
#
# Build (from repo root):
#   DOCKER_BUILDKIT=1 docker build \
#     -t fincept-qf-cuda-test \
#     -f runpod/quant-foundry-cuda-test/Dockerfile \
#     --build-arg GIT_SHA=$(git rev-parse HEAD) .

FROM runpod/base:1.0.2-cuda1280-ubuntu2204

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /worker

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-pip python3-venv && \
    ln -sf /usr/bin/python3 /usr/bin/python && \
    rm -rf /var/lib/apt/lists/*

RUN pip3 install --no-cache-dir "runpod==1.7.13"

COPY runpod/quant-foundry-cuda-test/handler.py /worker/handler.py

ARG GIT_SHA=unknown
ENV QUANT_FOUNDRY_GIT_SHA=${GIT_SHA} \
    PYTHONPATH=/worker

# Direct entrypoint — no entrypoint.sh wrapper, no preflight, no chown.
# This matches the proven-working smoke image pattern exactly.
ENTRYPOINT ["python", "-u", "/worker/handler.py"]
