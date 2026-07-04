# Tier 0 Swarm — Risk Register

## Critical Risks

### R1: Python 3.10 vs 3.12 Mismatch
- **Risk:** Local Python is 3.10.6, project requires >=3.12. Most pytest runs will fail on import.
- **Impact:** Workers cannot run tests to verify changes. Must rely on `compileall` + `ruff check`.
- **Mitigation:** Workers use static validation. Document test failures as "Python version mismatch, not code error". The integration reviewer notes this in the final receipt.
- **Owner:** All workers.

### R2: Docker Not Available
- **Risk:** `docker` command not found. Cannot build images locally.
- **Impact:** Image-slimming worker cannot verify the slim image builds. HEALTHCHECK guard cannot run via Docker build.
- **Mitigation:** Image-slimming worker does static Dockerfile validation (parse, dependency analysis, size estimation). Documents why Docker build isn't possible.
- **Owner:** Worker 3 (image-slimming).

### R3: handler.py File Ownership Conflict
- **Risk:** Worker 1 (artifact-durability) and Worker 5 (metric-sanity) both modify `handler.py`.
- **Impact:** Merge conflicts, overwritten changes.
- **Mitigation:** **Sequence them.** Worker 1 runs first (Phase 2), Worker 5 runs after Worker 1 completes (Phase 3). Strict line-range ownership: Worker 1 owns L143-174 + L3370-3480, Worker 5 owns L3570-3700.
- **Owner:** Orchestrator.

### R4: Ruff Auto-Fix Conflicts
- **Risk:** Worker 4 (ruff burn-down) runs `ruff --fix` on all .py files, which could overwrite changes made by other workers.
- **Impact:** Lost work, merge conflicts.
- **Mitigation:** **Worker 4 runs LAST** (Phase 4), after all code workers complete. It runs on a separate branch. The integration reviewer merges it after verifying no logic changes.
- **Owner:** Orchestrator.

### R5: Breaking the Working RunPod Dispatch Path
- **Risk:** Changes to handler.py, Dockerfile, or RunPod scripts could break the proven `6dbec436` dispatch path.
- **Impact:** Worker goes unhealthy, jobs stuck IN_QUEUE, lost canary capability.
- **Mitigation:** Hard rules from `runpod-worker-ops` skill: no base image switch, no HEALTHCHECK, no import-time side effects, ENTRYPOINT not CMD. Workers run `test_dockerfile_no_healthcheck.py` and `test_receipt_integrity.py` as regression guards.
- **Owner:** All workers touching RunPod files.

### R6: Live RunPod Spend
- **Risk:** A worker accidentally creates a live RunPod endpoint, incurring GPU charges.
- **Impact:** Unwanted cloud spend.
- **Mitigation:** No worker runs live probes without operator approval. The orchestrator prompt explicitly forbids it. All RunPod scripts require `RUNPOD_API_KEY` env var — if not set, they fail safely.
- **Owner:** All workers.

### R7: Ruff Error Count Discrepancy
- **Risk:** Roadmap says 1334 errors, actual count is 1823. May indicate config drift or new errors since the roadmap was written.
- **Impact:** Worker 4's baseline may not match expectations.
- **Mitigation:** Worker 4 captures the actual current count as the baseline. The receipt documents the discrepancy.
- **Owner:** Worker 4.

## Non-Critical Risks

### R8: Existing Dockerfile.minimal
- **Risk:** `runpod/quant-foundry-training/Dockerfile.minimal` already exists. Worker 3 might duplicate it.
- **Mitigation:** Worker 3 reads `Dockerfile.minimal` first. If it already implements the slim path, Worker 3 documents it and focuses on validation/tests instead of creating a new file.
- **Owner:** Worker 3.

### R9: Uncommitted Changes on Current Branch
- **Risk:** `RECEIPT_INDEX.md` and `api.Dockerfile` have uncommitted modifications. Workers might accidentally commit these.
- **Mitigation:** Workers create their own branches (tier0/*). The orchestrator notes the pre-existing uncommitted changes in the integration receipt.
- **Owner:** Orchestrator.
