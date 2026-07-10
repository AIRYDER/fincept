FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /worker

RUN pip install --no-cache-dir "runpod==1.7.13"

COPY runpod/quant-foundry-smoke/handler.py /worker/handler.py

ARG GIT_SHA=unknown
ENV QUANT_FOUNDRY_GIT_SHA=${GIT_SHA} \
    PYTHONPATH=/worker

ENTRYPOINT ["python", "-u", "/worker/handler.py"]
