# Tier 1A Product Loop — Final Receipt

## Swarm
- **Workspace:** `.devin/swarms/12c0ccd05eef4c`
- **Goal:** Tier 1A Product Loop: callback persistence, model registry, training dispatcher, observability/cost, product loop review
- **Branch:** `tier1a/product-loop`
- **Commit:** `9e8862cf` — tier1a: product loop — callback persistence, model registry, dispatcher, observability

## Fleet (5 agents)
| Agent | Role | Status | Work |
|-------|------|--------|------|
| Orchestrator | coordinator | active | Coordinated 3 phases |
| Scout 1 | scout | done | Codebase intelligence report |
| Builder 1 | builder | done | Callback persistence + Model registry |
| Builder 2 | builder | done | Training dispatcher + Observability |
| Reviewer 1 | reviewer | done | Integration review — APPROVED |

## Work items completed (4)

### 1. Callback Persistence (Tier 1.1)
- **6 tables:** `artifact_manifests`, `model_dossiers`, `callback_receipts`, `callback_dlq`, `callback_metrics`, `shadow_predictions`
- **5 DB-backed sinks:** `DbDossierStore`, `DbShadowLedgerStore`, `CallbackReceiptDbStore`, `CallbackDlqDbStore`, `CallbackMetricsDbStore`
- **31 tests** — all pass
- **Migration:** `0004_callback_ingestion.py`

### 2. Training Dispatcher Wiring
- **`runpod_policy.py`:** per-request execution timeout policy, endpoint template builder, network volume support
- **`runpod_client.py`:** dispatch now sends `{"input": ..., "policy": build_job_policy()}`
- **`schemas.py`:** added `presigned_artifact_url` field to `RunPodTrainingRequest`
- **18 tests** — all pass

### 3. Model Registry (Tier 1.2)
- **6 tables:** `models`, `model_versions`, `model_metrics`, `promotions`, `promotion_decisions`, `shadow_evaluations`
- **`ModelRegistryDB`:** register models/versions, record metrics, record shadow evaluations, promote via `PromotionGate`
- **Promotion workflow:** registry persists evidence → gate enforces → receipt persisted → status updated only on approval
- **38 tests** — all pass
- **Migration:** `0005_model_registry.py`

### 4. Observability & Cost Tracking
- **4 tables:** `training_jobs`, `job_cost_events`, `job_metrics`, `cost_summary`
- **`CostTracker`:** job lifecycle tracking, cost events, GPU cost estimation, period rollups
- **GPU rates:** RTX_4090 $0.40/hr, A100_80GB $1.10/hr, A100_40GB $0.80/hr, L4 $0.25/hr
- **44 tests** — all pass
- **Migration:** `0004b_observability.py`

## Migration chain
```
0003_provider_data → 0004_callback_ingestion → 0004b_observability → 0005_model_registry
```
Linear, no branches. All cross-migration FK references valid.

## Test results
- **272 tests pass** across 12 test files (220 Tier 1A + 52 existing callback)
- **0 failures** (pytest exit code 1 is Windows temp-dir cleanup PermissionError)
- Python 3.12.9

## Security invariants verified
- No secrets, HMAC signatures, or raw payloads in any DB column
- `request_payload_ref` is a file path, not the payload
- `callback_receipts` stores `signature_valid: bool` + `payload_hash`, never signature bytes
- `shadow_predictions.authority` has CHECK forcing `'shadow-only'`
- `promotion_decisions.waivers` is a JSON list of `{issue_code, waived_by, reason}`, never secrets

## Promotion gate integration
- Registry's `promote()` delegates to `PromotionGate.evaluate()` — no logic duplicated
- Evidence assembled from DB tables, receipt always persisted (even on rejection)
- Status only updates on approval
- `DossierStatus` enum NOT renamed

## Files changed
- **41 files** (16 new source/test files, 25 receipt/report files)
- **7,956 insertions**, 4 deletions

## Next recommended tasks
1. Wire `CostTracker.record_job_dispatch()` into the RunPod dispatch path
2. Wire `CostTracker.update_job_status()` into the callback processor
3. Wire `ModelRegistryDB.promote()` into the API route
4. Add `dataset_manifests` table (Tier 1.5)
5. Add `tournament_results` table for `shadow_evaluations.tournament_result_id` FK
6. Make GPU cost rates configurable via environment variables
7. Add cost dashboard endpoint to the API
