# risk.Dockerfile — Fincept Risk Gate Service (TASK-0903)
#
# Production container for the Risk service. Pre-trade checks: position
# limits, kill-switch state, drawdown gates. The risk service is invoked
# by the OMS before any order is sent to a broker. NO broker credentials
# in this container — it only reads Redis state + Postgres positions.
#
# Build:
#   docker build -t fincept-risk:v1.0.0 -f infra/docker/risk.Dockerfile .

FROM python:3.12-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:0.5.7 /uv /uvx /usr/local/bin/

WORKDIR /build

COPY pyproject.toml uv.lock ./
COPY libs libs
COPY services services
COPY apps apps 2>/dev/null || true

RUN uv sync --frozen --no-dev --package risk

FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONFAULTHANDLER=1

RUN groupadd --system --gid 1001 fincept \
    && useradd --system --uid 1001 --gid fincept --home-dir /app fincept

WORKDIR /app

COPY --from=builder --chown=fincept:fincept /build/.venv /app/.venv
COPY --chown=fincept:fincept services/risk/src/risk /app/src/risk
COPY --chown=fincept:fincept libs /app/libs

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH=/app/src:/app/libs

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
  CMD python -c "import urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://localhost:8000/health',timeout=3).status==200 else 1)"

USER fincept

CMD ["uvicorn", "risk.main:app", \
     "--host", "0.0.0.0", \
     "--port", "8000", \
     "--workers", "1", \
     "--log-level", "info"]