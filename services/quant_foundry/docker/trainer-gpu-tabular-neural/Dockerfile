# trainer-gpu-tabular-neural — PyTorch CUDA tabular neural worker (T-9.1)
#
# Image spec source of truth:
#   services/quant_foundry/src/quant_foundry/tabular_neural_runtime.py::ImageSpec
#
# This image is the GPU RunPod target for tabular neural canary / training
# jobs. It carries the quant_foundry tabular neural runtime module and a
# healthcheck that probes the CUDA device + runs a 1-epoch canary.
#
# Base: PyTorch official CUDA 12.1 / cuDNN 8 runtime image (matches
# ImageSpec.base_image). Python 3.12 is provided by the base image.

FROM pytorch/pytorch:2.1.0-cuda12.1-cudnn8-runtime

# Python version is provided by the base image (3.12). We pin pip.
RUN pip install --no-cache-dir --upgrade pip

# Copy the requirements file and install CPU/GPU-agnostic Python deps.
# torch is already provided by the base image; the requirements pin the
# remaining packages (numpy, pandas, scikit-learn, pydantic).
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

# Copy the quant_foundry tabular neural runtime module into the image.
# Only the runtime module is required for the healthcheck + canary; the
# full package is not installed to keep the image lean.
RUN mkdir -p /opt/quant_foundry
COPY src/quant_foundry/tabular_neural_runtime.py /opt/quant_foundry/tabular_neural_runtime.py

# Make the runtime importable as `quant_foundry.tabular_neural_runtime`.
ENV PYTHONPATH=/opt/quant_foundry:$PYTHONPATH

# Healthcheck: probe GPU + run a 1-epoch canary. Exits 0 on success,
# 1 on failure (GPU missing or canary raised). Matches ImageSpec.healthcheck_cmd.
HEALTHCHECK --interval=30s --timeout=30s --start-period=15s --retries=3 \
    CMD python -c "from quant_foundry.tabular_neural_runtime import TabularNeuralHealthcheck; import sys; sys.exit(0 if TabularNeuralHealthcheck().is_healthy() else 1)"

# Default entrypoint: run the healthcheck once and exit.
CMD ["python", "-c", "from quant_foundry.tabular_neural_runtime import TabularNeuralHealthcheck; import sys; sys.exit(0 if TabularNeuralHealthcheck().is_healthy() else 1)"]
