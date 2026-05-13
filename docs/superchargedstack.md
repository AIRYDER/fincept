# Supercharged Stack

This document replaces older aspirational stack notes with the current practical stack.

## Current stack

- **Python:** 3.12, uv workspace.
- **Services:** FastAPI API plus async Python workers/services.
- **Message bus/cache:** Redis Streams and Redis keys.
- **Storage direction:** Postgres/Timescale for durable market data and relational state; Parquet artifacts for captures/features/training where appropriate.
- **Frontend:** Next.js 14 App Router, React 18, Tailwind, Radix UI primitives, TanStack Query, Zustand, Recharts, Framer Motion.
- **Models:** LightGBM microstructure baseline with walk-forward CV support.
- **Research/data tools:** Exa, OpenBB, local model artifacts, datasource registry.
- **Brokerage direction:** Alpaca paper first, live only after gate approval.

## What makes it powerful

- Redis Streams provide a typed event spine without Kafka-scale operations.
- Python keeps research and production close enough for rapid iteration.
- The dashboard exposes operator control without making UI components own business logic.
- Feature-gated agents allow the system to degrade cleanly when API keys or models are missing.
- Filesystem-backed strategy configs make operator intent inspectable and recoverable after Redis loss.

## What is intentionally excluded

- Sub-100 microsecond HFT architecture.
- FPGA, kernel bypass, SmartNIC acceleration.
- Native Qt/Bloomberg clone.
- Uncontrolled live trading.
