# trainer-gpu-rl — PyTorch CUDA reinforcement-learning worker (T-13.5)
#
# Image spec source of truth:
#   services/quant_foundry/src/quant_foundry/rl_runtime.py::RLImageSpec
#
# This image is the GPU RunPod target for reinforcement-learning training
# jobs. It carries the quant_foundry RL runtime module and a healthcheck
# that probes the CUDA device + runs a simulator canary (reset + step) +
# round-trips a rollout artifact. Supports policy checkpoint resume and a
# rollout cache directory.
#
# Base: PyTorch official CUDA 12.1 / cuDNN 8 runtime image (matches
# RLImageSpec.base_image). Python 3.12 is provided by the base image.

FROM pytorch/pytorch:2.1.0-cuda12.1-cudnn8-runtime

# Python version is provided by the base image (3.12). We pin pip.
RUN pip install --no-cache-dir --upgrade pip

# Copy the requirements file and install CPU/GPU-agnostic Python deps.
# torch is already provided by the base image; the requirements pin the
# remaining packages (numpy, pandas, pydantic, gymnasium, stable-baselines3).
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

# Copy the quant_foundry RL runtime module into the image.
# Only the runtime modules required for the healthcheck + simulator probes
# are copied; the full package is not installed to keep the image lean.
RUN mkdir -p /opt/quant_foundry
COPY src/quant_foundry/rl_runtime.py /opt/quant_foundry/rl_runtime.py
COPY src/quant_foundry/tabular_neural_runtime.py /opt/quant_foundry/tabular_neural_runtime.py

# Make the runtime importable as `quant_foundry.rl_runtime`.
ENV PYTHONPATH=/opt/quant_foundry:$PYTHONPATH

# Rollout cache directory (matches RLImageSpec.rollout_cache_dir).
RUN mkdir -p /opt/rollout_cache
ENV ROLLOUT_CACHE_DIR=/opt/rollout_cache

# Healthcheck: probe GPU + run a simulator canary + round-trip a rollout
# artifact. Exits 0 on success, 1 on failure (GPU missing or probe raised).
# Matches RLImageSpec.healthcheck_cmd.
HEALTHCHECK --interval=30s --timeout=60s --start-period=15s --retries=3 \
    CMD python -c "from quant_foundry.rl_runtime import RLHealthcheck; import sys; sys.exit(0 if RLHealthcheck().is_healthy() else 1)"

# Default entrypoint: run the healthcheck once and exit.
CMD ["python", "-c", "from quant_foundry.rl_runtime import RLHealthcheck; import sys; sys.exit(0 if RLHealthcheck().is_healthy() else 1)"]
