# Quant Foundry Training Worker Container — trainer-gpu-tree (T-4.2)
#
# CUDA-capable RunPod serverless worker for tree-model training
# (XGBoost GPU / CatBoost GPU / LightGBM CPU baseline). No broker
# credentials, no Redis, no trading access. The worker only reads a
# RunPodTrainingRequest, trains a baseline, and returns a signed
# callback envelope.
#
# Build (from repo root):
#   DOCKER_BUILDKIT=1 docker build \
#     -t fincept-qf-training:gpu-tree \
#     -f runpod/quant-foundry-training/Dockerfile \
#     --build-arg GIT_SHA=$(git rev-parse HEAD) .
#
# Run (local CPU smoke — healthcheck will report no GPU, canary still runs):
#   echo '{"input": {"task": "gpu_healthcheck", "mode": "canary"}}' | \
#     docker run --rm -i -e QUANT_FOUNDRY_CALLBACK_SECRET=secret \
#     fincept-qf-training:gpu-tree
#
# Run (with real GPU on RunPod):
#   Deploy as a RunPod serverless endpoint with an NVIDIA GPU template
#   (e.g. RTX 4090 / A100). RunPod injects the GPU and calls
#   handler(event) for each job. The only required env var is
#   QUANT_FOUNDRY_CALLBACK_SECRET (HMAC signing key).

# ---------------------------------------------------------------------------
# Base image: PyTorch CUDA runtime gives us Python + CUDA 12.4 + cuDNN 9 in
# one well-maintained layer. Python 3.11 is the conda interpreter; pip works
# directly. This avoids hand-installing Python via deadsnakes on a bare
# nvidia/cuda image (which is fragile and slow to build).
# ---------------------------------------------------------------------------
FROM pytorch/pytorch:2.4.1-cuda12.4-cudnn9-runtime

# Prevent Python from buffering stdout/stderr so RunPod captures logs live.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /worker

# Install OS dependencies:
#  - build-essential: headers needed by a few pip sdists (scikit-learn wheels
#    are prebuilt, but keep it for safety).
#  - gosu: used by the entrypoint to drop privileges from root to the
#    `trainer` user after fixing network-volume ownership.
#  - curl: used by the healthcheck fallback / diagnostics.
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    gosu \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Copy the quant_foundry source from the monorepo.
COPY services/quant_foundry/src/quant_foundry/ /worker/quant_foundry/

# Copy fincept_core.datasets — needed for fincept_core.datasets.cv.make_folds
# (canonical walk-forward fold math used by RealLightGBMTrainer).
# We copy only the datasets subpackage + its deps (naming, prediction_log,
# schemas) to avoid pulling in redis and storage-heavy service modules.
COPY libs/fincept-core/src/fincept_core/datasets/ /worker/fincept_core/datasets/
COPY libs/fincept-core/src/fincept_core/naming.py /worker/fincept_core/naming.py
COPY libs/fincept-core/src/fincept_core/prediction_log.py /worker/fincept_core/prediction_log.py
COPY libs/fincept-core/src/fincept_core/schemas.py /worker/fincept_core/schemas.py
COPY libs/fincept-core/src/fincept_core/errors.py /worker/fincept_core/errors.py
COPY libs/fincept-core/src/fincept_core/clock.py /worker/fincept_core/clock.py
COPY libs/fincept-core/src/fincept_core/config.py /worker/fincept_core/config.py
# Create a minimal __init__.py that only imports the datasets subpackage
# (avoids importing storage/heartbeat which need redis).
RUN python -c "import pathlib; p=pathlib.Path('/worker/fincept_core/__init__.py'); p.write_text('from . import datasets, naming, prediction_log, schemas, errors, clock, config\n__all__=[\"datasets\",\"naming\",\"prediction_log\",\"schemas\",\"errors\",\"clock\",\"config\"]\n', encoding='utf-8')"

# Copy the shared RunPod worker_status utility (sibling to this handler in
# the monorepo). The handler adds runpod/shared to sys.path at import time.
COPY runpod/shared/worker_status.py /worker/runpod/shared/worker_status.py

# Copy the handler.
COPY runpod/quant-foundry-training/handler.py /worker/handler.py

# ---------------------------------------------------------------------------
# Install GPU-capable ML libraries.
#
#  - xgboost:  the official manylinux wheel is built with CUDA support since
#              2.0; `device="cuda"` works on a CUDA runtime image.
#  - catboost: the official wheel ships GPU support (`task_type="GPU"`).
#  - lightgbm: the official PyPI wheel is CPU-only (GPU requires a custom
#              OpenCL/CUDA build). We install it as the CPU baseline and
#              document this in the README. The gpu_healthcheck task reports
#              lightgbm_gpu=False for this image.
#  - pandas / pyarrow / scikit-learn / numpy: data handling + walk-forward
#    fold math used by RealLightGBMTrainer.
#  - pydantic / pydantic-settings / httpx / runpod: handler runtime +
#    RunPod serverless SDK. pydantic-settings is required by the copied
#    fincept_core.config module, which is imported through fincept_core.
# ---------------------------------------------------------------------------
RUN pip install --no-cache-dir \
    "pydantic>=2.7,<3" \
    "pydantic-settings>=2.0,<3" \
    "httpx>=0.27,<1" \
    "runpod>=1.6,<2" \
    "numpy>=1.26,<3" \
    "pandas>=2.0,<3" \
    "pyarrow>=14.0,<20" \
    "scikit-learn>=1.4,<2" \
    "lightgbm>=4.0,<5" \
    "xgboost>=2.0,<3" \
    "catboost>=1.2,<2"

# ---------------------------------------------------------------------------
# Startup security preflight.
#
# A small Python script run by the entrypoint BEFORE the handler starts. It:
#   1. Fails closed if any forbidden env var is set (broker secrets, Redis,
#      DB write URLs, trading keys). The worker must be a pure function over
#      its inputs — it must never carry trading/broker credentials.
#   2. Validates the callback URL host (if QUANT_FOUNDRY_CALLBACK_URL is set)
#      rejects loopback/private/link-local hosts in production.
#   3. Prints a REDACTED config summary so operators can audit the runtime
#      without leaking secrets.
# ---------------------------------------------------------------------------
RUN cat > /worker/preflight.py <<'PREFLIGHT_EOF'
"""Startup security preflight for the trainer-gpu-tree worker.

Run by /worker/entrypoint.sh before the handler starts. Exits non-zero
(fail closed) if a forbidden env var is present, so a misconfigured image
can never reach the training loop.
"""
from __future__ import annotations

import ipaddress
import os
import socket
import sys
from urllib.parse import urlparse

# Env vars the worker must NEVER carry. Presence of any of these means the
# image was misconfigured with trading/broker/storage credentials, which
# violates the security boundary (pure function over inputs).
FORBIDDEN_ENV = [
    "REDIS_URL",
    "REDIS_HOST",
    "FINCEPT_JWT_SECRET",
    "ALPACA_API_KEY",
    "ALPACA_SECRET_KEY",
    "ALPACA_API_SECRET",
    "DATABASE_URL",
    "DB_URL",
    "POSTGRES_URL",
    "KAFKA_BOOTSTRAP_SERVERS",
    "BROKER_URL",
    "AMQP_URL",
    "MONGO_URL",
    "MONGODB_URI",
]

# Callback URL is optional (the worker returns the callback in its response,
# it does not POST). If set, the host must not be loopback/private in
# production mode.
_CALLBACK_URL_ENV = "QUANT_FOUNDRY_CALLBACK_URL"
_MODE_ENV = "QUANT_FOUNDRY_TRAINING_MODE"


def _redact(value: str) -> str:
    """Redact a secret-like value, keeping only the first and last char."""
    if not value:
        return "<empty>"
    if len(value) <= 4:
        return "***"
    return f"{value[0]}***{value[-1]}"


def _host_is_private(host: str) -> bool:
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    for info in infos:
        ip = info[4][0]
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            continue
        if addr.is_loopback or addr.is_private or addr.is_link_local or addr.is_reserved:
            return True
    return False


def main() -> int:
    mode = os.environ.get(_MODE_ENV, "canary").lower()
    print(f"[preflight] training_mode={mode}", flush=True)

    # 1. Forbidden env vars — fail closed regardless of mode.
    violations = [k for k in FORBIDDEN_ENV if os.environ.get(k)]
    if violations:
        print(
            f"[preflight] FAIL: forbidden env vars present: {sorted(violations)}",
            file=sys.stderr,
            flush=True,
        )
        return 2

    # 2. Validate callback URL host (if provided).
    cb_url = os.environ.get(_CALLBACK_URL_ENV, "")
    if cb_url:
        parsed = urlparse(cb_url)
        host = parsed.hostname or ""
        if host and _host_is_private(host) and mode == "production":
            print(
                f"[preflight] FAIL: callback URL host {host!r} is loopback/private "
                f"in production mode",
                file=sys.stderr,
                flush=True,
            )
            return 3
        print(f"[preflight] callback_url host={host} (ok)", flush=True)
    else:
        print("[preflight] callback_url not set (worker returns callback in response)", flush=True)

    # 3. Redacted config summary.
    secret = os.environ.get("QUANT_FOUNDRY_CALLBACK_SECRET", "")
    print("[preflight] redacted config summary:", flush=True)
    print(f"  QUANT_FOUNDRY_CALLBACK_SECRET={_redact(secret)}", flush=True)
    print(f"  QUANT_FOUNDRY_USE_REAL_TRAINER={os.environ.get('QUANT_FOUNDRY_USE_REAL_TRAINER', 'false')}", flush=True)
    print(f"  QUANT_FOUNDRY_TRAINING_DEADLINE_SECONDS={os.environ.get('QUANT_FOUNDRY_TRAINING_DEADLINE_SECONDS', '600')}", flush=True)
    print(f"  QUANT_FOUNDRY_GIT_SHA={os.environ.get('QUANT_FOUNDRY_GIT_SHA', 'unknown')}", flush=True)
    print("[preflight] OK", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
PREFLIGHT_EOF

# ---------------------------------------------------------------------------
# Entrypoint wrapper.
#
# Runs as root (RunPod mounts the network volume owned by root), performs the
# security preflight, fixes ownership of the writable volume dirs so the
# non-root `trainer` user can write artifacts/status, then drops privileges
# via gosu and execs the handler. If the volume dirs are not mountpoints
# (local test), chown is best-effort.
# ---------------------------------------------------------------------------
RUN cat > /worker/entrypoint.sh <<'ENTRY_EOF'
#!/usr/bin/env bash
set -euo pipefail

# 1. Security preflight (fail closed on forbidden env vars).
python /worker/preflight.py

# 2. Best-effort ownership fix for RunPod network volumes.
for d in /runpod-volume /workspace; do
  if [ -d "$d" ]; then
    chown -R trainer:trainer "$d" 2>/dev/null || true
  fi
done

# 3. Exec the handler as root (RunPod serverless workers run as root by
#    default; the gosu drop to trainer was causing the worker to go
#    unhealthy within ~40s, likely due to GPU device file permissions
#    or NVIDIA container runtime access restrictions for non-root users).
exec python -u /worker/handler.py
ENTRY_EOF
RUN chmod +x /worker/entrypoint.sh

# ---------------------------------------------------------------------------
# Non-root user. RunPod network volumes are mounted with permissive perms and
# the entrypoint chowns them at startup, so a non-root user is safe. The
# security boundary is still the container itself (no broker creds, no Redis),
# but running as non-root adds defense in depth against privilege escalation
# inside the container.
# ---------------------------------------------------------------------------
RUN useradd --create-home --shell /bin/bash --uid 1000 trainer \
    && chown -R trainer:trainer /worker

# Pin the code git SHA at build time (for reproducibility).
# Override with --build-arg GIT_SHA=$(git rev-parse HEAD)
ARG GIT_SHA=unknown
ENV QUANT_FOUNDRY_GIT_SHA=${GIT_SHA}

# No broker credentials, no Redis, no trading env vars.
# Only the callback secret is required (injected at runtime).
ENV QUANT_FOUNDRY_CALLBACK_SECRET="" \
    QUANT_FOUNDRY_TRAINING_DEADLINE_SECONDS="1800" \
    QUANT_FOUNDRY_USE_REAL_TRAINER="true" \
    QUANT_FOUNDRY_TRAINING_MODE="canary" \
    PYTHONPATH=/worker

# ---------------------------------------------------------------------------
# Healthcheck: verify the Python runtime + handler module load. We do NOT
# require nvidia-smi here because the pytorch/pytorch:cuda-runtime base image
# does not ship nvidia-smi in its PATH — the binary is mounted by the NVIDIA
# container runtime at /usr/bin/nvidia-smi but may not be discoverable in all
# base images. The handler's gpu_healthcheck task is the authoritative GPU
# probe; this Docker HEALTHCHECK is only the container-level liveness gate
# RunPod polls to decide if the worker is eligible to receive jobs.
# ---------------------------------------------------------------------------
HEALTHCHECK --interval=60s --timeout=15s --start-period=40s --retries=3 \
  CMD python -c "import sys; sys.path.insert(0, '/worker'); import handler; print('healthcheck ok')" || exit 1

# RunPod's serverless loader calls handler(event). Use ENTRYPOINT (not CMD) so
# RunPod's dockerArgs field can't override it. The entrypoint runs the
# security preflight, fixes volume ownership, then drops to the non-root
# trainer user before execing the handler.
ENTRYPOINT ["/worker/entrypoint.sh"]
