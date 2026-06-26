# api.Dockerfile — Fincept API service (TASK-0903)
#
# Production container for the FastAPI HTTP + WebSocket service that fronts
# the trading bus. Read-only views over Redis + Postgres + S3, plus
# kill-switch + control endpoints. NO broker credentials, NO write access
# to trading streams (only the orchestrator does that).
#
# Build:
#   docker build -t fincept-api:v1.0.0 -f infra/docker/api.Dockerfile .
#
# Local run (paper mode, no AWS):
#   docker run --rm -p 8010:8000 \
#     -e ENVIRONMENT=local \
#     -e REDIS_URL=redis://host.docker.internal:6379/0 \
#     fincept-api:local
#
# ECS task injects all credentials via Secrets Manager (see infra/aws/ecs.tf).

# ---- Stage 1: dependency resolution (cached layer) ------------------------
FROM python:3.12-slim AS builder

# Install uv (fast Python package manager). Pinned via SHA256 in CI.
COPY --from=ghcr.io/astral-sh/uv:0.5.7 /uv /uvx /usr/local/bin/

WORKDIR /build

# Copy the entire monorepo so workspace members resolve. This layer is
# cached as long as uv.lock + pyproject.toml do not change.
COPY pyproject.toml uv.lock ./
COPY libs libs
COPY services services

# Sync only the api service and its dependencies. Frozen = reproducible.
# --no-dev strips mypy/pytest/etc. out of the production image.
RUN uv sync --frozen --no-dev --package api

# ---- Stage 2: minimal runtime --------------------------------------------
FROM python:3.12-slim AS runtime

# Don't write .pyc files (we're read-only anyway); don't buffer stdout/stderr.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONFAULTHANDLER=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Create a non-root user for the runtime. ECS exec / IAM role assumption
# still works the same way; running as non-root is a defense-in-depth
# baseline that limits blast radius of any RCE.
RUN groupadd --system --gid 1001 fincept \
    && useradd --system --uid 1001 --gid fincept --home-dir /app fincept

WORKDIR /app

# Copy the resolved virtual environment from the builder stage. This is the
# only layer that depends on python source, so code changes rebuild fast.
COPY --from=builder --chown=fincept:fincept /build/.venv /app/.venv

# Copy only the api package source (small, changes frequently).
COPY --chown=fincept:fincept services/api/src/api /app/src/api
COPY --chown=fincept:fincept libs /app/libs

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH=/app/src:/app/libs

# Port the FastAPI service listens on (matches infra/aws/variables.tf
# var.api_container_port).
EXPOSE 8000

# Healthcheck: a curl/python 2xx against /health. ECS also does an HTTP
# healthcheck on the same path via the target group; this is a fast in-
# container signal for orchestrators that look at container health.
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
  CMD python -c "import urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://localhost:8000/health',timeout=3).status==200 else 1)"

USER fincept

# Single-process uvicorn; ECS handles replica count + autoscaling.
# --proxy-headers honours X-Forwarded-For from the ALB.
# --workers=1 keeps the container simple; scale via desired_count instead.
CMD ["uvicorn", "api.main:app", \
     "--host", "0.0.0.0", \
     "--port", "8000", \
     "--proxy-headers", \
     "--workers", "1", \
     "--log-level", "info"]