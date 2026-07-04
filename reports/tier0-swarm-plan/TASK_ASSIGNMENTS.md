# Tier 0 Swarm — Task Assignments

## File Ownership Matrix

| File | W1 (artifact) | W2 (timeout) | W3 (image) | W4 (ruff) | W5 (metric) |
|------|:---:|:---:|:---:|:---:|:---:|
| runpod/quant-foundry-training/handler.py | **OWN** (L143-174, L3370-3480) | — | — | last | **OWN** (L3570-3700) |
| runpod/quant-foundry-training/Dockerfile | — | — | **OWN** | last | — |
| runpod/quant-foundry-training/Dockerfile.minimal | — | — | **OWN** (read first) | — | — |
| runpod/quant-foundry-training/run_live_canary.py | — | **OWN** | — | last | — |
| runpod/quant-foundry-training/run_train_model.py | — | **OWN** | — | last | — |
| runpod/quant-foundry-training/run_gpu_healthcheck.py | — | **OWN** | — | last | — |
| services/quant_foundry/tests/test_artifact_writer.py | **OWN** | — | — | last | — |
| services/quant_foundry/src/quant_foundry/runpod_training.py | — | — | — | last | **OWN** |
| scripts/runpod/runpod_lifecycle.py (NEW) | — | **OWN** | — | — | — |
| ruff.toml | — | — | — | **OWN** | — |
| pyproject.toml | — | — | — | **OWN** | — |
| NEW test files | **OWN** (artifact tests) | **OWN** (lifecycle tests) | **OWN** (slim image tests) | — | **OWN** (metric sanity tests) |

**"last"** = Worker 4 runs ruff --fix after all other workers complete.

## Sequencing

### Phase 1 (parallel, single turn)
- **Scout** — codebase intelligence report
- **Worker 2** (timeout/lifecycle) — touches only RunPod scripts + new helper
- **Worker 3** (image-slimming) — touches only Dockerfile(s)

### Phase 2 (after Scout completes, parallel with still-running workers)
- **Worker 1** (artifact-durability) — touches handler.py (writer selection area)

### Phase 3 (after Worker 1 completes)
- **Worker 5** (metric-sanity) — touches handler.py (metrics/callback area)

### Phase 4 (after all code workers complete)
- **Worker 4** (ruff burn-down) — runs on all .py files, separate branch

### Phase 5 (after Worker 4)
- **Reviewer** — final QA gate

## Worker Assignments

### Worker 1 — artifact-durability-builder
- **Branch:** `tier0/durable-artifacts`
- **Owned files:** handler.py (L143-174, L3370-3480), test_artifact_writer.py, new test files
- **Goal:** Add /tmp deny gate for real jobs, validate output_prefix, wire durable storage
- **Key constraint:** Do NOT rewrite the writer stack. VolumeArtifactWriter/PresignedUploadArtifactWriter/FakeArtifactWriter already exist and work. This is policy + validation, not new abstractions.
- **Skill:** `durable-artifact` (read SKILL.md before starting)
- **Acceptance:** `pytest test_artifact_writer.py -k "artifact or writer or manifest"` (or compileall if 3.12 blocks), new tests for /tmp deny + output_prefix validation

### Worker 2 — runpod-timeout-lifecycle-builder
- **Branch:** `tier0/runpod-lifecycle-timeout`
- **Owned files:** run_live_canary.py, run_train_model.py, run_gpu_healthcheck.py, new scripts/runpod/runpod_lifecycle.py, new test files
- **Goal:** Set endpoint executionTimeout >= 1860s, extract shared lifecycle helper
- **Key constraint:** Do NOT change base image, do NOT add HEALTHCHECK, do NOT run live tests
- **Skill:** `runpod-worker-ops` (read SKILL.md before starting)
- **Acceptance:** `pytest -k "runpod or lifecycle or timeout"` (or compileall), `python -m compileall`

### Worker 3 — image-slimming-builder
- **Branch:** `tier0/image-slimming`
- **Owned files:** Dockerfile, Dockerfile.minimal (read first), new Dockerfile.slim, new test files
- **Goal:** Create slim image path without torch for lightgbm/xgboost-only training
- **Key constraint:** Do NOT switch from python:3.12-slim base. Do NOT add HEALTHCHECK. Docker NOT available locally — static validation only.
- **Skill:** `runpod-worker-ops` (read SKILL.md before starting)
- **Acceptance:** Static Dockerfile validation, `test_dockerfile_no_healthcheck.py` passes, import/startup tests via compileall

### Worker 4 — ci-ruff-burndown-builder
- **Branch:** `tier0/ruff-burndown`
- **Owned files:** ruff.toml, pyproject.toml, broad .py files (auto-fix only)
- **Goal:** Burn down 1823 ruff errors with safe auto-fixes, separate from logic changes
- **Key constraint:** Runs LAST. Do NOT mix with feature implementation. Do NOT hide real errors with broad ignores.
- **Acceptance:** `ruff check .` before/after counts, `pytest` (or compileall if 3.12 blocks)

### Worker 5 — metric-sanity-builder
- **Branch:** `tier0/metric-sanity`
- **Owned files:** handler.py (L3570-3700), runpod_training.py, new test files
- **Goal:** Add metric sanity bounds — flag Sharpe 769 as implausible, preserve raw values, block promotion
- **Key constraint:** Do NOT silently delete raw metrics. Do NOT change training behavior. Do NOT touch handler.py outside L3570-3700.
- **Acceptance:** New tests proving normal Sharpe passes, Sharpe 769 flagged implausible, raw preserved, promotion blocked, callback serializes

### Worker 6 — integration-reviewer
- **Branch:** reviews all tier0/* branches
- **Owned files:** NONE (read-only)
- **Goal:** Review all worker receipts, check for conflicts, verify non-regression rules, produce merge order
- **Acceptance:** Final receipt with NON_REGRESSION_CHECKLIST, MERGE_ORDER, BLOCKERS, OPERATOR_APPROVAL_NEEDED
