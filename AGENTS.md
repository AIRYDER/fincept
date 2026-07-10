# Fincept / Quant Foundry Agent Rules

The project running end-to-end is non-negotiable.

Before implementing any roadmap item, identify its tier and dependency chain.

Current highest-priority path:

1. Tier 0 security and worker stability.
2. Durable artifacts.
3. Callback ingestion.
4. Model registry.
5. Dataset registry.
6. GPU backend and Optuna.
7. Advanced quant validation.
8. Determinism/provenance/agentic ops mesh.

Do not start Tier 2+ work before callback ingestion and model registry exist unless explicitly assigned.

Every task must produce receipts:
- commands run
- files changed
- tests run
- test outputs
- risks
- next recommended task

Never claim completion without executable evidence.
