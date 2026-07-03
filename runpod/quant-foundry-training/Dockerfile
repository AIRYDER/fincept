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
# Base image: nvidia/cuda runtime (NOT pytorch/pytorch).
#
# The pytorch/pytorch:2.4.1-cuda12.4-cudnn9-runtime image caused workers to go
# unhealthy within 90 seconds on RunPod serverless ADA_24 GPUs (driver 550,
# CUDA 12.4 max). The nvidia/cuda:12.4.1-runtime-ubuntu22.04 base works —
# verified with the cuda-test endpoint (became healthy in ~10 minutes).
#
# We install Python 3 explicitly via apt since nvidia/cuda doesn't include it.
# ---------------------------------------------------------------------------
FROM nvidia/cuda:12.4.1-runtime-ubuntu22.04

# Prevent Python from buffering stdout/stderr so RunPod captures logs live.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    DEBIAN_FRONTEND=noninteractive

WORKDIR /worker

# Install Python 3 and OS dependencies:
#  - python3/pip/venv: Python runtime (not included in nvidia/cuda base).
#  - build-essential: headers needed by a few pip sdists.
#  - gosu: used by the entrypoint to drop privileges (kept for compatibility
#    but entrypoint currently execs as root — see comment below).
#  - curl: used by diagnostics.
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 \
    python3-pip \
    python3-venv \
    build-essential \
    gosu \
    curl \
    ca-certificates \
    && ln -sf /usr/bin/python3 /usr/bin/python \
    && rm -rf /var/lib/apt/lists/*

RUN cat > /worker/_strenum_compat.py <<'PY_COMPAT'
import datetime
import enum
import sys
import typing

if not hasattr(enum, "StrEnum"):
    class StrEnum(str, enum.Enum):
        pass

    enum.StrEnum = StrEnum
    sys.modules["enum"].StrEnum = StrEnum

if not hasattr(datetime, "UTC"):
    datetime.UTC = datetime.timezone.utc
    sys.modules["datetime"].UTC = datetime.timezone.utc

if not hasattr(typing, "Self"):
    try:
        from typing_extensions import Self
    except ImportError:
        Self = object

    typing.Self = Self
    sys.modules["typing"].Self = Self
PY_COMPAT

# Also install as a .pth file in site-packages so it runs at Python startup
# before any user code, guaranteeing StrEnum is available even if the handler
# import order changes.
RUN SITE_DIR=$(python -c "import site; print(site.getsitepackages()[0])") && \
    printf 'import enum, datetime, typing, sys\nif not hasattr(enum,"StrEnum"): enum.StrEnum=type("StrEnum",(str,enum.Enum),{}); sys.modules["enum"].StrEnum=enum.StrEnum\nif not hasattr(datetime,"UTC"): datetime.UTC=datetime.timezone.utc; sys.modules["datetime"].UTC=datetime.timezone.utc\nif not hasattr(typing,"Self"):\n try:\n  from typing_extensions import Self\n except ImportError:\n  Self=object\n typing.Self=Self; sys.modules["typing"].Self=Self\n' > "$SITE_DIR/_compat_shim.pth"

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

COPY runpod/shared/worker_status.py /worker/shared/worker_status.py

# Copy the handlers (layered isolation build):
#   - handler_full.py: the unmodified production handler (real code paths).
#   - handler.py: the layered dispatcher that selects a code-path layer per
#     job via QF_DIAG_LAYER env or input.diag_layer. Layer 5 (default) calls
#     the real handler. The layered handler has a clean __main__ block that
#     properly starts the RunPod SDK.
COPY runpod/quant-foundry-training/handler.py /worker/handler_full.py
COPY runpod/quant-foundry-training/handler_layered.py /worker/handler.py

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
    "runpod==1.7.13" \
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

_t0=$(date +%s)

# 1. Security preflight (fail closed on forbidden env vars).
# Skip with QF_DIAG_SKIP_PREFLIGHT=1 for diagnostic builds.
if [ "${QF_DIAG_SKIP_PREFLIGHT:-0}" != "1" ]; then
  python /worker/preflight.py
  _t1=$(date +%s)
  echo "[entrypoint] preflight done in $((_t1 - _t0))s" >&2
else
  echo "[entrypoint] preflight SKIPPED (QF_DIAG_SKIP_PREFLIGHT=1)" >&2
  _t1=$_t0
fi

# 2. Best-effort ownership fix for RunPod network volumes.
# Skip with QF_DIAG_SKIP_CHOWN=1 — chown -R on a large volume can take
# minutes and delay handler startup, which may interact with RunPod's
# worker health management.
if [ "${QF_DIAG_SKIP_CHOWN:-0}" != "1" ]; then
  for d in /runpod-volume /workspace; do
    if [ -d "$d" ]; then
      chown -R trainer:trainer "$d" 2>/dev/null || true
    fi
  done
  _t2=$(date +%s)
  echo "[entrypoint] chown done in $((_t2 - _t1))s" >&2
else
  echo "[entrypoint] chown SKIPPED (QF_DIAG_SKIP_CHOWN=1)" >&2
  _t2=$_t1
fi

# 3. Exec the handler as root (RunPod serverless workers run as root by
#    default; the gosu drop to trainer was causing the worker to go
#    unhealthy within ~40s, likely due to GPU device file permissions
#    or NVIDIA container runtime access restrictions for non-root users).
echo "[entrypoint] starting handler at $(date +%s) (total entrypoint: $((_t2 - _t0))s)" >&2
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
ENV QUANT_FOUNDRY_TRAINING_DEADLINE_SECONDS="1800" \
    QUANT_FOUNDRY_USE_REAL_TRAINER="true" \
    QUANT_FOUNDRY_TRAINING_MODE="canary" \
    QF_DIAG_SKIP_PREFLIGHT="0" \
    QF_DIAG_SKIP_CHOWN="0" \
    PYTHONPATH=/worker

# ---------------------------------------------------------------------------
# NO HEALTHCHECK — deliberately removed (layer-isolation evidence 2026-07-03).
#
# The proven-working smoke image defines no HEALTHCHECK. With a HEALTHCHECK
# at --interval=300s, Docker reports the container as "health: starting"
# until the FIRST probe fires at t=+300s. The diagnostic pod (endpoint
# zbpy7m8s8dps7k, image 412080c6) was "Exited by Runpod" at t=+230s —
# seconds after the first job arrived and BEFORE any healthcheck had ever
# run — with an immediate-return (layer 0) handler. A worker that never
# reports "healthy" gets recycled by RunPod at dispatch time, which matched
# every observed symptom (ready -> running -> unhealthy, job stuck
# IN_QUEUE). RunPod serverless does its own worker health management; a
# Docker HEALTHCHECK is not required and must not be reintroduced without
# re-running the layer-0 probe.
# ---------------------------------------------------------------------------

# RunPod's serverless loader calls handler(event). Use ENTRYPOINT (not CMD) so
# RunPod's dockerArgs field can't override it. The entrypoint runs the security
# preflight, fixes volume ownership, then execs the handler.
ENTRYPOINT ["/worker/entrypoint.sh"]
