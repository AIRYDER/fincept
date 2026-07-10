# trainer-gpu-sequence — PyTorch CUDA sequence worker (T-10.5)
#
# Image spec source of truth:
#   services/quant_foundry/src/quant_foundry/sequence_runtime.py::SequenceImageSpec
#
# This image is the GPU RunPod target for sequence (RNN/Transformer) training
# jobs. It carries the quant_foundry sequence runtime module and a healthcheck
# that probes the CUDA device + loads a tiny sequence tensor + runs a tiny
# forward pass. Supports mixed precision (float16/bfloat16) and checkpoint
# resume.
#
# Base: PyTorch official CUDA 12.1 / cuDNN 8 runtime image (matches
# SequenceImageSpec.base_image). Python 3.12 is provided by the base image.

FROM pytorch/pytorch:2.1.0-cuda12.1-cudnn8-runtime

# Python version is provided by the base image (3.12). We pin pip.
RUN pip install --no-cache-dir --upgrade pip

# Copy the requirements file and install CPU/GPU-agnostic Python deps.
# torch is already provided by the base image; the requirements pin the
# remaining packages (numpy, pandas, scikit-learn, pydantic, einops).
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

# Copy the quant_foundry sequence runtime module into the image.
# Only the runtime module is required for the healthcheck + sequence probes;
# the full package is not installed to keep the image lean.
RUN mkdir -p /opt/quant_foundry
COPY src/quant_foundry/sequence_runtime.py /opt/quant_foundry/sequence_runtime.py
COPY src/quant_foundry/tabular_neural_runtime.py /opt/quant_foundry/tabular_neural_runtime.py

# Make the runtime importable as `quant_foundry.sequence_runtime`.
ENV PYTHONPATH=/opt/quant_foundry:$PYTHONPATH

# Healthcheck: probe GPU + load a tiny sequence tensor + run a tiny forward
# pass. Exits 0 on success, 1 on failure (GPU missing or probe raised).
# Matches SequenceImageSpec.healthcheck_cmd.
HEALTHCHECK --interval=30s --timeout=60s --start-period=15s --retries=3 \
    CMD python -c "from quant_foundry.sequence_runtime import SequenceHealthcheck; import sys; sys.exit(0 if SequenceHealthcheck().is_healthy() else 1)"

# Default entrypoint: run the healthcheck once and exit.
CMD ["python", "-c", "from quant_foundry.sequence_runtime import SequenceHealthcheck; import sys; sys.exit(0 if SequenceHealthcheck().is_healthy() else 1)"]
