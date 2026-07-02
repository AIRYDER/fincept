# trainer-gpu-event-text — Event Text GPU worker (T-12.5)
#
# Image spec source of truth:
#   services/quant_foundry/src/quant_foundry/event_text_runtime.py::EventTextImageSpec
#
# This image is the GPU RunPod target for event-text encoding jobs. It
# carries the quant_foundry event text runtime module and a healthcheck
# that probes the CUDA device + the embedding cache directory + runs a
# tiny synthetic encoding test. Supports pinned-weight, offline inference
# (no network downloads) and batch text encoding via sentence-transformers.
#
# Base: PyTorch official CUDA 12.1 / cuDNN 8 runtime image (matches
# EventTextImageSpec.base_image). Python 3.12 is provided by the base image.

FROM pytorch/pytorch:2.1.0-cuda12.1-cudnn8-runtime

# Python version is provided by the base image (3.12). We pin pip.
RUN pip install --no-cache-dir --upgrade pip

# Copy the requirements file and install CPU/GPU-agnostic Python deps.
# torch is already provided by the base image; the requirements pin the
# remaining packages (transformers, numpy, pandas, pydantic,
# huggingface_hub, sentence-transformers).
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

# Copy the quant_foundry event text runtime module (+ its dependency
# tabular_neural_runtime, which provides check_gpu / GPUStatus) into the
# image. Only the runtime modules are required for the healthcheck +
# encoding probes; the full package is not installed to keep the image
# lean.
RUN mkdir -p /opt/quant_foundry
COPY src/quant_foundry/event_text_runtime.py /opt/quant_foundry/event_text_runtime.py
COPY src/quant_foundry/tabular_neural_runtime.py /opt/quant_foundry/tabular_neural_runtime.py

# Make the runtime importable as `quant_foundry.event_text_runtime`.
ENV PYTHONPATH=/opt/quant_foundry:$PYTHONPATH

# Create the embedding cache directory (EventTextImageSpec.embedding_cache_dir)
# and the weight cache directory (EventTextImageSpec.weight_cache_dir).
# Weights/embeddings are mounted/baked at build time; the directories
# exist so the healthcheck's cache probes pass even before any data is
# added.
RUN mkdir -p /opt/embedding_cache /opt/foundation_weights
ENV EVENT_TEXT_EMBEDDING_CACHE_DIR=/opt/embedding_cache
ENV EVENT_TEXT_WEIGHT_CACHE_DIR=/opt/foundation_weights

# Enforce offline mode: disable HuggingFace Hub network access by default.
# The event text runtime loads weights from the local cache dir only.
ENV HF_HUB_OFFLINE=1
ENV TRANSFORMERS_OFFLINE=1

# Healthcheck: probe GPU + embedding cache + tiny encoding test. Exits 0
# on success, 1 on failure (GPU missing, cache inaccessible, or encoding
# raised). Matches EventTextImageSpec.healthcheck_cmd.
HEALTHCHECK --interval=30s --timeout=60s --start-period=15s --retries=3 \
    CMD python -c "from quant_foundry.event_text_runtime import EventTextHealthcheck; import sys; sys.exit(0 if EventTextHealthcheck().is_healthy() else 1)"

# Default entrypoint: run the healthcheck once and exit.
CMD ["python", "-c", "from quant_foundry.event_text_runtime import EventTextHealthcheck; import sys; sys.exit(0 if EventTextHealthcheck().is_healthy() else 1)"]
