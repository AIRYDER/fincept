FROM python:3.12-slim

WORKDIR /app

# Install dependencies.
COPY services/quant_foundry/pyproject.toml services/quant_foundry/uv.lock* ./services/quant_foundry/
RUN pip install uv && cd services/quant_foundry && uv sync --frozen --no-dev

# Copy the quant_foundry package.
COPY services/quant_foundry/src ./services/quant_foundry/src

# Copy the handler.
COPY runpod/quant-foundry-inference/handler.py ./handler.py

# Set the entry point.
ENV QUANT_FOUNDRY_MODE=runpod_shadow
ENV PYTHONPATH=/app/services/quant_foundry/src

ENTRYPOINT ["python", "handler.py"]
