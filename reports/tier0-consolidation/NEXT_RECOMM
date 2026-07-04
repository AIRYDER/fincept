# Next Recommended Tasks

## Immediate (post-merge, requires operator approval)

1. **Build the slim image** — `docker build -f Dockerfile.slim` in a Docker-enabled environment. Run a live canary against it. (Operator approval needed for cloud spend.)

2. **Verify executionTimeout live** — Run a live canary with the lifecycle helper changes. Confirm the per-request `policy.executionTimeout` (1860000 ms) is accepted and enforced. (Operator approval needed.)

3. **Run full pytest on Python 3.12** — Run the complete test suite in a Python 3.12 environment with all dependencies installed. The consolidation only ran targeted test files.

## Short-term (Tier 0 follow-up)

4. **Ruff burn-down pass 2** — Address the remaining 357 ruff errors. Start with T201 (60), F841 (37), E402 (31). See `RUFF_DEBT.md` for the full breakdown.

5. **Wire presigned URL through the request schema** — The `PresignedUploadArtifactWriter` exists but the request schema doesn't pass `presigned_artifact_url` for real jobs. Add it to `RunPodTrainingRequest`.

6. **Mount a RunPod network volume in the endpoint template** — The lifecycle helper sets timeouts but doesn't mount a volume. Add `volumeInGb` and `volumeMountPath` to the template. Also add `networkVolumeId` to `build_endpoint_input()`.

7. **Re-apply UP017 fix** — The `datetime.timezone.utc` → `datetime.UTC` fix was reverted for Python 3.10 compat. Re-apply once 3.10 support is dropped (the Dockerfile already uses 3.12).

## Medium-term (Tier 1 — do NOT start until Tier 0 is fully merged and verified)

8. **Callback ingestion service** (Tier 1.1) — FastAPI endpoint that receives the worker's signed callback, verifies HMAC, writes dossier + artifact manifest to fincept-db.

9. **Model registry with promotion workflow** (Tier 1.2) — Tables: models, model_versions, promotions. State machine: shadow-only → candidate → production → retired.

10. **Dataset registry** (Tier 1.5) — Immutable dataset manifests, point-in-time proof, fold specs.

11. **GPU backend** (Tier 1.3) — Wire xgboost_gpu / catboost_gpu as separate model families.

12. **Optuna search** (Tier 1.4) — Hyperparameter search with per-trial recording.

## Do NOT start yet

- CPCV / PBO / Deflated Sharpe / champion-challenger (Tier 2+)
- These require callback ingestion and model registry to exist first.
