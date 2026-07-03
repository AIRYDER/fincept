# Minimal CUDA test image — isolates base image vs heavy imports.
#
# Same pytorch CUDA base as the training worker, but with a trivial handler
# that does NO heavy imports.  If this image also gets "Exited by Runpod",
# the issue is the base image / CUDA runtime.  If it works, the issue is
# the heavy module-scope imports in the training handler.
#
# Build (from repo root):
#   DOCKER_BUILDKIT=1 docker build \
#     -t fincept-qf-cuda-test \
#     -f runpod/quant-foundry-cuda-test/Dockerfile \
#     --build-arg GIT_SHA=$(git rev-parse HEAD) .

FROM pytorch/pytorch:2.4.1-cuda12.4-cudnn9-runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /worker

RUN pip install --no-cache-dir "runpod==1.7.13"

COPY runpod/quant-foundry-cuda-test/handler.py /worker/handler.py

ARG GIT_SHA=unknown
ENV QUANT_FOUNDRY_GIT_SHA=${GIT_SHA} \
    PYTHONPATH=/worker

# Direct entrypoint — no entrypoint.sh wrapper, no preflight, no chown.
# This matches the proven-working smoke image pattern exactly.
ENTRYPOINT ["python", "-u", "/worker/handler.py"]
