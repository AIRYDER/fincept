# Tier 1A Product Loop — Plan

## Mission

```
platform dispatches training job
→ RunPod worker trains
→ durable artifact is written
→ signed callback returns
→ callback is verified
→ dossier/artifact/metrics persist to fincept-db
→ model version enters registry
```

## Dependency graph

```
callback-persistence-builder (Tier 1.1) ──┐
                                          ├──→ model-registry-builder (Tier 1.2)
training-dispatcher-builder (Tier 1.0)  ──┘
                                          
observability-cost-builder (Tier 1.0)  ────→ (parallel, no deps)

product-loop-reviewer ─────────────────────→ (after all builders)
```

## Workers

### 1. callback-persistence-builder (CRITICAL PATH)
- **Tier:** 1.1
- **Skill:** `callback-ingestion`
- **Depends on:** nothing (starts immediately)
- **Owns:**
  - `libs/fincept-db/src/fincept_db/migrations/versions/0004_callback_ingestion.py`
  - `libs/fincept-db/src/fincept_db/callback_tables.py` (SQLAlchemy models)
  - `services/quant_foundry/src/quant_foundry/db_sinks.py` (DB-backed sinks)
  - `services/quant_foundry/tests/test_callback_db_sinks.py`
- **Acceptance:**
  - Migration 0004 creates: `model_dossiers`, `artifact_manifests`, `callback_receipts`, `callback_dlq`, `callback_metrics`, `shadow_predictions`
  - DB-backed sinks implement `DossierStoreSink` and `ShadowLedgerSink` protocols
  - `INSERT ... ON CONFLICT DO NOTHING` for idempotency
  - No secrets, no raw payloads, no signature bytes in DB
  - All existing callback tests still pass
  - New tests for DB sinks pass on Python 3.12

### 2. training-dispatcher-builder (PARALLEL)
- **Tier:** 1.0 (pre-req wiring)
- **Depends on:** nothing (starts immediately)
- **Owns:**
  - `services/quant_foundry/src/quant_foundry/runpod_training.py` (dispatch path)
  - `services/quant_foundry/src/quant_foundry/runpod_client.py`
  - `services/quant_foundry/tests/test_runpod_dispatch.py`
- **Acceptance:**
  - `RunPodTrainingRequest` schema includes `presigned_artifact_url` field
  - Dispatch path passes `presigned_artifact_url` through to the worker
  - `build_job_policy()` is called in the dispatch path (not just probe scripts)
  - Network volume mounting in endpoint template (`volumeInGb`, `volumeMountPath`, `networkVolumeId`)
  - No live RunPod calls in tests (mock the client)

### 3. observability-cost-builder (PARALLEL)
- **Tier:** 1.0 (platform observability)
- **Depends on:** nothing (starts immediately)
- **Owns:**
  - `libs/fincept-db/src/fincept_db/migrations/versions/0004b_observability.py`
  - `libs/fincept-db/src/fincept_db/observability.py` (models)
  - `services/quant_foundry/src/quant_foundry/cost_tracker.py`
  - `services/quant_foundry/tests/test_cost_tracker.py`
- **Acceptance:**
  - Tables: `training_jobs` (job lifecycle), `training_costs` (GPU seconds, cost per job)
  - Cost tracker records job dispatch, worker start, worker end, artifact write
  - No live RunPod calls in tests
  - Migration follows 0003 pattern

### 4. model-registry-builder (AFTER callback-persistence)
- **Tier:** 1.2
- **Depends on:** callback-persistence-builder (needs 0004 tables)
- **Owns:**
  - `libs/fincept-db/src/fincept_db/migrations/versions/0005_model_registry.py`
  - `libs/fincept-db/src/fincept_db/registry_tables.py` (SQLAlchemy models)
  - `services/quant_foundry/src/quant_foundry/registry_db.py` (DB-backed registry)
  - `services/quant_foundry/tests/test_registry_db.py`
- **Acceptance:**
  - Migration 0005 creates: `models`, `model_versions`, `model_metrics`, `promotions`, `promotion_decisions`, `shadow_evaluations`
  - `promote()` transaction enforces the hard rule (7 evidence items)
  - State machine: candidate → research_approved → shadow_approved → paper_approved → limited_live_approved → rejected
  - `promotion_eligible=false` is a hard block on promotion past candidate
  - All existing promotion tests still pass

### 5. product-loop-reviewer (AFTER ALL)
- **Depends on:** all 4 builders
- **Acceptance:**
  - Integration test: dispatch → callback → persist → registry → promote
  - Non-regression checklist passes
  - Merge order documented
  - Final receipt written

## Phase plan

- **Phase 1:** Launch Scout + Builders 1, 2, 3 in parallel (callback-persistence, training-dispatcher, observability-cost)
- **Phase 2:** When Builder 1 completes, launch Builder 4 (model-registry)
- **Phase 3:** When all builders complete, launch Reviewer
- **Phase 4:** Merge and write final receipt

## Non-regression rules (carry forward from Tier 0)

- Do not switch training base image to nvidia/cuda, runpod/base, or pytorch/pytorch
- Do not add Docker HEALTHCHECK
- Do not allow production artifacts to final-destination only in /tmp
- Do not run live RunPod jobs without operator approval
- Do not change the `CallbackProcessor` interface (add new sinks, don't edit the processor)
- Do not weaken the HMAC signature verification or skew guard
- Do not bypass the promotion gate's evidence check
