# trainer-gpu-graph — PyTorch Geometric CUDA graph worker (T-12.6)
#
# Image spec source of truth:
#   services/quant_foundry/src/quant_foundry/graph_runtime.py::GraphImageSpec
#
# This image is the GPU RunPod target for graph neural network (GNN) training
# jobs. It carries the quant_foundry graph runtime module and a healthcheck
# that probes the CUDA device + loads a tiny synthetic graph snapshot + runs
# a tiny GNN forward pass. Supports mixed precision (float16/bfloat16) and
# PyTorch Geometric tensor conventions.
#
# Base: PyTorch official CUDA 12.1 / cuDNN 8 runtime image (matches
# GraphImageSpec.base_image). Python 3.12 is provided by the base image.

FROM pytorch/pytorch:2.1.0-cuda12.1-cudnn8-runtime

# Python version is provided by the base image (3.12). We pin pip.
RUN pip install --no-cache-dir --upgrade pip

# Copy the requirements file and install CPU/GPU-agnostic Python deps.
# torch is already provided by the base image; the requirements pin the
# remaining packages (torch-geometric, numpy, pandas, pydantic, scipy).
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

# Copy the quant_foundry graph runtime module into the image.
# Only the runtime module (and its tabular_neural_runtime dependency for
# check_gpu) is required for the healthcheck + graph probes; the full
# package is not installed to keep the image lean.
RUN mkdir -p /opt/quant_foundry
COPY src/quant_foundry/graph_runtime.py /opt/quant_foundry/graph_runtime.py
COPY src/quant_foundry/tabular_neural_runtime.py /opt/quant_foundry/tabular_neural_runtime.py

# Make the runtime importable as `quant_foundry.graph_runtime`.
ENV PYTHONPATH=/opt/quant_foundry:$PYTHONPATH

# Graph cache directory (matches GraphImageSpec.graph_cache_dir).
# Snapshots loaded/saved by GraphSnapshotLoader land here by default.
RUN mkdir -p /opt/graph_cache
ENV GRAPH_CACHE_DIR=/opt/graph_cache

# Healthcheck: probe GPU + load a tiny synthetic graph snapshot + run a tiny
# GNN forward pass. Exits 0 on success, 1 on failure (GPU missing or probe
# raised). Matches GraphImageSpec.healthcheck_cmd.
HEALTHCHECK --interval=30s --timeout=60s --start-period=15s --retries=3 \
    CMD python -c "from quant_foundry.graph_runtime import GraphHealthcheck; import sys; sys.exit(0 if GraphHealthcheck().is_healthy() else 1)"

# Default entrypoint: run the healthcheck once and exit.
CMD ["python", "-c", "from quant_foundry.graph_runtime import GraphHealthcheck; import sys; sys.exit(0 if GraphHealthcheck().is_healthy() else 1)"]
