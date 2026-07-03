# trainer-gpu-foundation-ts — Foundation TS GPU worker (T-11.4)
#
# Image spec source of truth:
#   services/quant_foundry/src/quant_foundry/foundation_runtime.py::FoundationImageSpec
#
# This image is the GPU RunPod target for foundation time-series forecasting
# jobs. It carries the quant_foundry foundation runtime module and a
# healthcheck that probes the CUDA device + the weight cache directory +
# runs a tiny synthetic forecast. Supports pinned-weight, offline inference
# (no network downloads) and batch probabilistic forecasts.
#
# Base: PyTorch official CUDA 12.1 / cuDNN 8 runtime image (matches
# FoundationImageSpec.base_image). Python 3.12 is provided by the base image.

FROM pytorch/pytorch:2.1.0-cuda12.1-cudnn8-runtime

# Python version is provided by the base image (3.12). We pin pip.
RUN pip install --no-cache-dir --upgrade pip

# Copy the requirements file and install CPU/GPU-agnostic Python deps.
# torch is already provided by the base image; the requirements pin the
# remaining packages (numpy, pandas, pydantic, transformers,
# huggingface_hub).
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

# Copy the quant_foundry foundation runtime module (+ its dependency
# tabular_neural_runtime, which provides check_gpu / GPUStatus) into the
# image. Only the runtime modules are required for the healthcheck +
# forecast probes; the full package is not installed to keep the image
# lean.
RUN mkdir -p /opt/quant_foundry
COPY src/quant_foundry/foundation_runtime.py /opt/quant_foundry/foundation_runtime.py
COPY src/quant_foundry/tabular_neural_runtime.py /opt/quant_foundry/tabular_neural_runtime.py

# Make the runtime importable as `quant_foundry.foundation_runtime`.
ENV PYTHONPATH=/opt/quant_foundry:$PYTHONPATH

# Create the weight cache directory (FoundationImageSpec.weight_cache_dir).
# Weights are mounted/baked at build time; the directory exists so the
# healthcheck's weight-cache probe passes even before any weights are
# added.
RUN mkdir -p /opt/foundation_weights
ENV FOUNDATION_WEIGHT_CACHE_DIR=/opt/foundation_weights

# Enforce offline mode: disable HuggingFace Hub network access by default.
# The foundation runtime loads weights from the local cache dir only.
ENV HF_HUB_OFFLINE=1
ENV TRANSFORMERS_OFFLINE=1

# Healthcheck: probe GPU + weight cache + tiny forecast. Exits 0 on
# success, 1 on failure (GPU missing, cache inaccessible, or forecast
# raised). Matches FoundationImageSpec.healthcheck_cmd.
HEALTHCHECK --interval=30s --timeout=60s --start-period=15s --retries=3 \
    CMD python -c "from quant_foundry.foundation_runtime import FoundationHealthcheck; import sys; sys.exit(0 if FoundationHealthcheck().is_healthy() else 1)"

# Default entrypoint: run the healthcheck once and exit.
CMD ["python", "-c", "from quant_foundry.foundation_runtime import FoundationHealthcheck; import sys; sys.exit(0 if FoundationHealthcheck().is_healthy() else 1)"]
