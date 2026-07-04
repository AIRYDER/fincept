# Next Recommended Tasks

## Immediate (post-merge)

1. **Build and test the slim image** — `docker build -f Dockerfile.slim` in a Docker-enabled environment. Run a live canary against it. (Operator approval needed.)

2. **Verify executionTimeout live** — Run a live canary with the lifecycle helper changes. Confirm the endpoint accepts `executionTimeout >= 1860`. (Operator approval needed.)

3. **Move the /tmp deny gate before training** — Currently fires after training (wastes GPU time). Move to before training starts in handler.py. (Follow-up task, ~30 min.)

4. **Run full pytest on Python 3.12** — Verify all 41 artifact writer tests + 18 metric sanity tests + 38 lifecycle tests + 23 slim Dockerfile tests pass in a 3.12 environment.

## Short-term (Tier 0 follow-up)

5. **Ruff burn-down pass 2** — Address the remaining 880 ruff errors (B017, T201, F841, etc.). These require manual fixes, not auto-fix.

6. **Wire presigned URL through the request schema** — The `PresignedUploadArtifactWriter` exists but the request schema doesn't pass `presigned_artifact_url` for real jobs. Add it to `RunPodTrainingRequest`.

7. **Mount a RunPod network volume in the endpoint template** — The lifecycle helper sets timeouts but doesn't mount a volume. Add `volumeInGb` and `volumeMountPath` to the template.

## Medium-term (Tier 1 — do NOT start until Tier 0 is fully merged and verified)

8. **Callback ingestion service** (Tier 1.1) — FastAPI endpoint that receives the worker's signed callback, verifies HMAC, writes dossier + artifact manifest to fincept-db.

9. **Model registry with promotion workflow** (Tier 1.2) — Tables: models, model_versions, promotions. State machine: shadow-only → candidate → production → retired.

10. **Dataset registry** (Tier 1.5) — Immutable dataset manifests, point-in-time proof, fold specs.

11. **GPU backend** (Tier 1.3) — Wire xgboost_gpu / catboost_gpu as separate model families.

12. **Optuna search** (Tier 1.4) — Hyperparameter search with per-trial recording.
