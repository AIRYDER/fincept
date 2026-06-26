# orchestrator.Dockerfile — Fincept Orchestrator (TASK-0903)
#
# Production container for the orchestrator service. Consumes Predictions
# from the bus, builds per-symbol consensus, emits Decisions + OrderIntents.
# The orchestrator is the ONLY service that emits sig.predict, ord.orders,
# ord.decisions. OMS subscribes downstream and never touches them.
#
# Build:
#   docker build -t fincept-orchestrator:v1.0.0 -f infra/docker/orchestrator.Dockerfile .
#
# Healthcheck hits the orchestrator's own /health (a Redis-stream depth
# check, defined in services/orchestrator/src/orchestrator/main.py).

FROM python:3.12-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:0.5.7 /uv /uvx /usr/local/bin/

WORKDIR /build

COPY pyproject.toml uv.lock ./
COPY libs libs
COPY services services
COPY apps apps 2>/dev/null || true

RUN uv sync --frozen --no-dev --package orchestrator

FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONFAULTHANDLER=1

RUN groupadd --system --gid 1001 fincept \
    && useradd --system --uid 1001 --gid fincept --home-dir /app fincept

WORKDIR /app

COPY --from=builder --chown=fincept:fincept /build/.venv /app/.venv
COPY --chown=fincept:fincept services/orchestrator/src/orchestrator /app/src/orchestrator
COPY --chown=fincept:fincept libs /app/libs

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH=/app/src:/app/libs

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
  CMD python -c "import urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://localhost:8000/health',timeout=3).status==200 else 1)"

USER fincept

# The orchestrator runs as a long-lived stream consumer; uvicorn serves
# its /health endpoint so the ECS target group can detect liveness.
CMD ["uvicorn", "orchestrator.main:app", \
     "--host", "0.0.0.0", \
     "--port", "8000", \
     "--workers", "1", \
     "--log-level", "info"]