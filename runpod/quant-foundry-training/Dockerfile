FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /worker

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Copy minimal handler with heavy imports
COPY runpod/quant-foundry-training/handler_minimal.py /worker/handler.py

RUN pip install --no-cache-dir \
    "runpod>=1.6,<2" \
    "numpy>=1.26,<3" \
    "pandas>=2.0,<3" \
    "pyarrow>=14.0,<20" \
    "scikit-learn>=1.4,<2" \
    "lightgbm>=4.0,<5" \
    "xgboost>=2.0,<3" \
    "catboost>=1.2,<2"

ARG GIT_SHA=unknown
ENV QUANT_FOUNDRY_GIT_SHA=${GIT_SHA}

ENV QUANT_FOUNDRY_CALLBACK_SECRET="" \
    QUANT_FOUNDRY_TRAINING_MODE="canary" \
    PYTHONPATH=/worker

HEALTHCHECK --interval=60s --timeout=15s --start-period=40s --retries=3 \
  CMD python -c "import sys; sys.path.insert(0, '/worker'); import handler; print('healthcheck ok')" || exit 1

ENTRYPOINT ["python", "-u", "/worker/handler.py"]
