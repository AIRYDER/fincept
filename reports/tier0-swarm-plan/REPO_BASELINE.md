# Tier 0 Swarm — Repo Baseline

**Date:** 2026-07-04
**Branch:** `fix/test-harness-optional-deps-guards`
**Swarm ID:** `11bc137965f560`

## Environment

| Tool | Version | Notes |
|------|---------|-------|
| Node | v24.15.0 | |
| Python | 3.10.6 | **Project requires >=3.12.** Local pytest will fail on 3.12-only syntax (StrEnum, datetime.UTC, typing.Self). Use `python -m compileall` and `ruff check` for static validation where pytest cannot run. |
| Docker | NOT AVAILABLE | `docker: The term 'docker' is not recognized`. Image-slimming worker must do static validation only. |
| pytest | 7.4.4 | Installed but will fail on 3.12-only imports. |
| ruff | 0.15.12 | 1823 errors detected (see CI/Ruff section). |

## Git Status

```
 M docs/runpod-fix-plan/RECEIPT_INDEX.md
 M infra/docker/api.Dockerfile
?? .dev
?? AGENTS.md
?? SESSION_HANDOFF.md
?? docs/AAA_GLM_SUPERTEAM_LOGS.zip
?? docs/IMPROVEMENT_ROADMAP_TIERED.md
?? docs/runpod-fix-plan.zip
?? handoffs/
?? kimiSuggestionFix.md
```

Branch: `fix/test-harness-optional-deps-guards` (not main).

## Proven Baseline (do not break)

- RunPod training worker boots, accepts jobs, stays healthy on `6dbec436` image.
- 6/6 canaries PASSED, A6 gpu_healthcheck PASSED, A7 train_model PASSED.
- Full training pipeline works: inline dataset → RealLightGBMTrainer → model export with sha256 + HMAC receipt.
- Training is bit-deterministic across environments (A7 live and local produced identical sha256).

## Key Files

### RunPod Training Worker
- `runpod/quant-foundry-training/Dockerfile` — production image (python:3.12-slim base, torch cu124, lightgbm/xgboost/catboost)
- `runpod/quant-foundry-training/Dockerfile.minimal` — existing minimal variant (check before creating new slim)
- `runpod/quant-foundry-training/handler.py` — worker entrypoint (~3700 lines)
- `runpod/quant-foundry-training/preflight.py` — startup security preflight
- `runpod/quant-foundry-training/run_live_canary.py` — canary probe tool
- `runpod/quant-foundry-training/run_train_model.py` — train model probe tool
- `runpod/quant-foundry-training/run_gpu_healthcheck.py` — GPU healthcheck tool

### Handler Key Locations (handler.py)
- `runpod_data_root()` — L143, returns /runpod-volume or /workspace or /tmp fallback
- `resolve_volume_path()` — L159, rewrites volume paths
- `_get_deadline_seconds()` — L1274, default "600" (Dockerfile ENV sets "1800")
- `VolumeArtifactWriter` — L969, writes to volume + re-reads + re-hashes
- `PresignedUploadArtifactWriter` — L1072, HTTP PUT to presigned URL
- `FakeArtifactWriter` — L1163, canary-only no-persistence
- Writer selection — L3370-3420 (presigned → volume → fake)
- `metrics_summary` — L3578, from `dossier_data.get("training_metrics", {})`
- `build_callback()` call — L3625, imported from `quant_foundry.runpod_training` (L121)

### Tests
- `runpod/tests/test_receipt_integrity.py` — receipt integrity guard
- `runpod/tests/test_dockerfile_no_healthcheck.py` — HEALTHCHECK guard
- `runpod/tests/healthcheck_guard.py` — HEALTHCHECK detector helper
- `services/quant_foundry/tests/test_artifact_writer.py` — 24 artifact writer tests

### Config
- `ruff.toml` — ruff config (target py312, rules: E/F/I/UP/B/SIM/RUF/ASYNC/S/T20)
- `pyproject.toml` — root project (requires-python >=3.12, pytest config, ruff B008 ignore)

### Other Dockerfiles (context, not Tier 0 targets)
- `runpod/quant-foundry-smoke/Dockerfile` — smoke test image
- `runpod/quant-foundry-inference/Dockerfile` — inference image
- `runpod/quant-foundry-cuda-test/Dockerfile*` — CUDA test variants
- `services/quant_foundry/docker/trainer-gpu-*/Dockerfile` — future GPU trainer images

## RunPod Endpoint Template (current state)

`run_live_canary.py` creates endpoints with:
- `idleTimeout: 300` (IDLE_TIMEOUT)
- `workersMin: 1`, `workersMax: 1`
- `gpuIds: "ADA_24"`, `containerDiskInGb: 20`
- **No `executionTimeout` field** — this is the Tier 0.3 gap. RunPod default is 600s, handler deadline is 1800s.

## CI/Ruff State

- 1823 ruff errors (roadmap said 1334 — has grown or count methodology differs)
- Config in `ruff.toml` + `pyproject.toml [tool.ruff.lint]`
- Rules: E, F, I, UP, B, SIM, RUF, ASYNC, S, T20
- Per-file ignores: tests/scripts/notebooks exempt from S/T20; RunPod probe scripts exempt from T20/S310/S110
- `target-version = "py312"`
