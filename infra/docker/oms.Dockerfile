# oms.Dockerfile — Fincept Order Management Service (TASK-0903)
#
# Production container for the Order Management Service. The OMS holds
# BROKER CREDENTIALS in Secrets Manager and is the ONLY service that
# issues real orders to Alpaca / IBKR / etc. It MUST stay inside the
# trusted Fincept deployment — NEVER on RunPod or any external compute.
#
# Build:
#   docker build -t fincept-oms:v1.0.0 -f infra/docker/oms.Dockerfile .
#
# OMS task definitions are reserved in infra/aws/ecs.tf but NOT deployed
# in this MVP (Railway staging is the source of truth for v1). This
# Dockerfile is ready when that boundary moves.

FROM python:3.12-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:0.5.7 /uv /uvx /usr/local/bin/

WORKDIR /build

COPY pyproject.toml uv.lock ./
COPY libs libs
COPY services services

RUN uv sync --frozen --no-dev --package oms

FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONFAULTHANDLER=1

# OMS holds broker credentials — extra hardening: drop all capabilities,
# read-only root filesystem, no new privileges. The ECS task role still
# grants Secrets Manager + S3 access; nothing else in the container needs
# to write to the FS.
RUN groupadd --system --gid 1001 fincept \
    && useradd --system --uid 1001 --gid fincept --home-dir /app fincept

WORKDIR /app

COPY --from=builder --chown=fincept:fincept /build/.venv /app/.venv
COPY --chown=fincept:fincept services/oms/src/oms /app/src/oms
COPY --chown=fincept:fincept libs /app/libs

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH=/app/src:/app/libs

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
  CMD python -c "import urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://localhost:8000/health',timeout=3).status==200 else 1)"

USER fincept

CMD ["uvicorn", "oms.main:app", \
     "--host", "0.0.0.0", \
     "--port", "8000", \
     "--workers", "1", \
     "--log-level", "info"]