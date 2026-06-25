# Next Five Tasks — Post-Settlement/Tournament/Promotion/Paper-Bridge

**Date:** 2026-06-25
**Author:** Devin (GLM-5.2)
**Branch:** `codex/portfolio-optimizer-core`
**Current state:** 675 quant_foundry tests + 103 API tests passing, 0 TypeScript errors, 14 commits on this branch.

---

## Where We Are

All code for Phases 0–10 of the BIG_PLAN is implemented except TASK-1005 (Alpha Genome Lab). The RunPod loop is live-proven (real training + inference on real GPUs). Settlement, tournament, promotion, and paper bridge are wired end-to-end with 89+ new tests. The readiness review verdict is **NOT READY** — the remaining gaps are operational, not code.

### What's done

| Phase | Status |
|---|---|
| Phase 0: Freeze, inventory, stabilize | ✅ Complete |
| Phase 1: Verification, CI, release safety | ✅ Complete |
| Phase 2: Dashboard and operator workflow | ✅ Complete |
| Phase 3: Quant Foundry contracts and mock connectivity | ✅ Complete |
| Phase 4: Evidence loop foundations | ✅ Complete (feature lake, shadow ledger, dossier registry, tournament, sentinel) |
| Phase 5: RunPod research foundry MVP | ✅ Code complete + live-proven |
| Phase 6: Shadow inference swarm MVP | ✅ Code complete + live-proven |
| Phase 7: Tournament governor and promotion | ✅ Complete + wired (Tracks A/B/C) |
| Phase 8: Quant Foundry dashboard | ✅ Complete + promotion buttons wired |
| Phase 9: Deployment and cost-optimized runtime | ✅ Railway staging complete, AWS design complete |
| Phase 10: Frontier performance modules | ✅ MoE router, causal graph, conformal gate, drift sentinel done; Alpha Genome Lab NOT STARTED |
| Phase 11: Limited live readiness | ✅ Review complete, verdict NOT READY |

### What's not done

The remaining work is **operational execution** plus one greenfield module. The code is ready; the system has never been run against real market data for an extended period, no real model has been promoted, and the production deployment environment doesn't exist yet.

---

## The Next Five Tasks

### Task 1: Train First Real Baseline Model Family (TASK-0504 — operational)

**BIG_PLAN reference:** TASK-0504, Order 30, Phase 5
**Status:** Code complete, never run against real data

#### What exists

- `services/quant_foundry/src/quant_foundry/runpod_training.py` (271 lines) — `RunPodTrainingHandler` class
- `runpod/quant-foundry-training/handler.py` — RunPod handler that receives training requests
- RunPod training endpoint `8vol1uc9l75jgs` is live and proven (training job completed successfully)
- `services/quant_foundry/src/quant_foundry/feature_lake.py` — produces dataset manifests
- `services/quant_foundry/src/quant_foundry/artifacts.py` — imports trained artifacts with hash verification

#### What needs to be done

1. **Build a real dataset manifest** using `FeatureLakeBuilder` against actual market data from `fincept_db.bars` (not fixtures). The manifest must include:
   - Point-in-time proof fields
   - Feature schema hash + label schema hash
   - Train/validation/test windows with purged-fold boundaries
   - Row count and checksum
   - Feature availability report

2. **Dispatch a real training job** to RunPod endpoint `8vol1uc9l75jgs` with:
   - The real dataset manifest URI
   - A simple model config (LightGBM or CatBoost baseline — NOT a transformer)
   - Walk-forward validation enabled
   - Budget limits enforced via BudgetGuard

3. **Verify the trained artifact imports** through `artifacts.py`:
   - Hash verification passes
   - Content type validated
   - Artifact stored in quarantine/staging path
   - Dossier candidate record created in `DossierRegistry`

4. **Verify the dossier includes**:
   - Dataset manifest hash
   - Feature schema
   - Walk-forward validation results
   - Calibration report
   - Feature importance report
   - Economic metrics
   - Training cost and duration

5. **Keep the model at `candidate` or `research_approved` status** — do NOT promote yet.

#### Acceptance criteria

- [ ] One real trained artifact imports successfully
- [ ] Dossier includes dataset and feature schema
- [ ] Model cannot influence predictions or orders (Authority.SHADOW_ONLY enforced)
- [ ] Costs and duration are recorded
- [ ] Training job completes on real RunPod GPU

#### Dependencies

- `fincept_db.bars` must have real market data (check if this exists or needs to be populated)
- RunPod training endpoint must be running (confirmed: `8vol1uc9l75jgs` is live)
- BudgetGuard must have budget allocated

#### Estimated effort

2-4 hours of agent work (build manifest, dispatch job, verify import). The training job itself may take 30-60 minutes on the GPU depending on dataset size.

#### Risk

Medium. Training environment issues are common. The RunPod handler may need updates if the dataset format has changed since the live proof.

---

### Task 2: 30-Day Shadow Inference Run (operational — the long pole)

**BIG_PLAN reference:** Phase 6 operational completion
**Status:** Code complete, never run continuously against real market data

#### What exists

- `services/quant_foundry/src/quant_foundry/shadow_inference.py` — shadow inference dispatch
- `services/quant_foundry/src/quant_foundry/callbacks.py` — callback ingestion
- `services/quant_foundry/src/quant_foundry/shadow_ledger.py` — durable `ShadowLedger`
- `services/quant_foundry/src/quant_foundry/settlement_sweep.py` — periodic settlement
- `services/quant_foundry/src/quant_foundry/market_data_adapter.py` — fetches bar prices
- RunPod inference endpoint `36mz2q30jdyvru` is live and proven
- Gateway has `run_settlement_sweep()` wired to API poll task

#### What needs to be done

1. **Configure the gateway for continuous shadow inference**:
   - `QUANT_FOUNDRY_ENABLED=true`
   - `QUANT_FOUNDRY_MODE=shadow` (NOT paper, NOT live)
   - `QUANT_FOUNDRY_RUNPOD_INFERENCE_ENDPOINT=36mz2q30jdyvru`
   - `QUANT_FOUNDRY_CALLBACK_SECRET=<real secret>`
   - Settlement sweep interval configured (e.g., every 5 minutes during market hours)

2. **Deploy the gateway** somewhere stable (Railway staging or local always-on):
   - API must be reachable for RunPod callbacks
   - `ShadowLedger` must persist to durable storage
   - `SettlementLedger` must persist to durable storage
   - `DossierRegistry` must persist to durable storage

3. **Dispatch shadow predictions** for the trained baseline model (from Task 1) against real market data:
   - Daily predictions for a defined symbol universe
   - Feature snapshots exported via `feature_snapshot_export.py`
   - Predictions stored in `ShadowLedger`

4. **Settle predictions** via the settlement sweep worker:
   - Market data adapter fetches real bar prices from `fincept_db.bars`
   - Settlement records created in `SettlementLedger`
   - Settlement lag tracked in `shadow_health()`

5. **Run tournament sweeps** periodically:
   - Tournament sweep worker scores models from settlement records
   - Leaderboard updates with real rankings
   - Calibration and decay indicators tracked

6. **Monitor for 30 days**:
   - Daily health checks via `GET /quant-foundry/shadow/health`
   - Weekly tournament status via `GET /quant-foundry/tournament/status`
   - Track `settled_count`, `settlement_lag_seconds`, prediction accuracy
   - Verify no secrets leak in any output

#### Acceptance criteria

- [ ] 30+ days of continuous shadow predictions stored in `ShadowLedger`
- [ ] Settlement records accumulated in `SettlementLedger` for all expired predictions
- [ ] Tournament leaderboard shows real model performance with deflated Sharpe, calibration, decay
- [ ] No model can influence orders (Authority.SHADOW_ONLY enforced throughout)
- [ ] No secrets in any log, receipt, or dashboard response
- [ ] Gateway health endpoint shows green throughout (allowing for expected cold starts)

#### Dependencies

- Task 1 (trained baseline model) must be complete
- `fincept_db.bars` must have continuously updating real market data
- Stable deployment environment (Railway or always-on local)
- RunPod inference endpoint must remain running

#### Estimated effort

30 days of wall-clock time. ~2-4 hours of agent work to set up the continuous run configuration and deploy. Then monitoring.

#### Risk

Low code risk (everything is tested). Medium operational risk (deployment stability, data feed gaps, RunPod endpoint availability).

---

### Task 3: Alpha Genome Lab (TASK-1005 — greenfield code)

**BIG_PLAN reference:** TASK-1005, Order 48, Phase 10
**Status:** NOT STARTED — `alpha_genome.py` does not exist

#### What exists

- All dependencies are implemented: feature lake, RunPod training, shadow inference, tournament scoring, sentinel, promotion gate
- The tournament gate enforces that no model can bypass evidence requirements
- BudgetGuard enforces cost limits

#### What needs to be done

1. **Create `services/quant_foundry/src/quant_foundry/alpha_genome.py`**:
   - `Recipe` — a versioned config representing a feature/model recipe
   - `RecipeMutation` — mutates features and model settings within allowlisted ranges
   - `AlphaGenomeLab` — orchestrates recipe generation, training dispatch, and evidence collection
   - `TrialBudget` — enforces per-sweep and per-recipe cost limits
   - `EarlyStopper` — kills underperforming sweeps early based on intermediate tournament scores

2. **Implement recipe representation**:
   - Recipe is a versioned config with: feature set, model type, hyperparameters, training window, validation window
   - Recipe hash for reproducibility
   - Recipe lineage (parent recipe, mutation applied)

3. **Implement mutation engine**:
   - Allowlisted feature mutations (add, remove, transform)
   - Allowlisted model hyperparameter mutations (within bounded ranges)
   - No mutation can bypass the feature lake's point-in-time proof
   - No mutation can bypass the sentinel's leakage checks

4. **Implement trial budget enforcement**:
   - Per-recipe cost limit (GPU minutes + storage)
   - Per-sweep cost limit (total recipes in a sweep)
   - Global kill switch via BudgetGuard
   - Budget exhaustion stops new trials, doesn't kill running ones

5. **Implement early stopping**:
   - After N settled predictions, check tournament score
   - If score is below threshold vs. parent recipe, kill the recipe
   - If score is above threshold, continue to full evaluation
   - Early-killed recipes get a `KILLED_EARLY` status with reason

6. **Implement evidence-backed registration**:
   - Only recipes that pass the sentinel, tournament, and settlement gates get registered as dossier candidates
   - Failed recipes are discarded with a discard receipt
   - No recipe can bypass tournament gates — all go through the same `PromotionGate.evaluate()` path

7. **Create `services/quant_foundry/tests/test_alpha_genome.py`**:
   - Recipe reproducibility test
   - Mutation within allowlist test
   - Mutation outside allowlist rejected test
   - Trial budget enforcement test
   - Early stopping test
   - Evidence-backed registration test
   - No bypass of tournament gates test
   - No secrets in recipe or receipts test

8. **Wire into gateway** (if needed):
   - `AlphaGenomeLab` may need a gateway method to start/stop sweeps
   - Dashboard page for viewing recipe sweeps (optional, can be deferred)

#### Acceptance criteria

- [ ] Generated recipes are reproducible (same recipe hash → same training result)
- [ ] Bad candidates are discarded with receipts
- [ ] No recipe can bypass tournament gates
- [ ] Trial budgets enforced — no overspend
- [ ] Early stopping works — underperforming recipes killed before full evaluation
- [ ] All tests pass

#### Dependencies

- Task 1 (trained baseline model) — needed as the parent recipe for mutations
- Feature lake, RunPod training, shadow inference, tournament, sentinel — all implemented

#### Estimated effort

4-8 hours of agent work. This is the largest greenfield code task remaining.

#### Risk

Very high overfitting and cost risk (per BIG_PLAN). The mutation engine must be carefully bounded. Start with a small allowlist and conservative budgets.

#### Rollback

Disable search and keep manually defined model families. The `AlphaGenomeLab` is an opt-in module.

---

### Task 4: Deploy AWS Production Control Plane (TASK-0903 — operational)

**BIG_PLAN reference:** TASK-0903, Order 43, Phase 9
**Status:** Design complete (`docs/AWS_PRODUCTION_CONTROL_PLANE.md`, 383 lines), NOT DEPLOYED

#### What exists

- `docs/AWS_PRODUCTION_CONTROL_PLANE.md` — comprehensive architecture design:
  - ECS Fargate for API, dashboard, orchestrator, OMS, risk
  - S3 for receipts, dossiers, artifacts, settlements
  - ECR for container images
  - Secrets Manager for credentials
  - CloudWatch for monitoring
  - VPC with private subnets
  - ElastiCache (Redis/Valkey)
  - RDS Postgres with TimescaleDB
  - ALB + WAF
- `railway.json` — Railway staging config (for test/staging only)
- Cost estimate: ~$210-260/mo always-on + $0.5-2/hour GPU

#### What needs to be done

1. **Create infrastructure-as-code** (Terraform or CloudFormation):
   - VPC with public/private subnets across 2 AZs
   - ECS Fargate cluster
   - ECR repositories for API, dashboard, orchestrator
   - S3 buckets: `fincept-receipts`, `fincept-dossiers`, `fincept-artifacts`, `fincept-settlements` (with versioning + object lock)
   - Secrets Manager: callback secret, JWT secret, RunPod API key, database credentials
   - RDS Postgres with TimescaleDB extension
   - ElastiCache Redis/Valkey
   - ALB with WAF rules
   - CloudWatch log groups + alarms (BudgetGuard, settlement lag, shadow health)

2. **Build and push container images**:
   - API image → ECR
   - Dashboard image → ECR
   - Orchestrator image → ECR (if separate)

3. **Create ECS task definitions**:
   - API task: FastAPI with health checks, env vars from Secrets Manager
   - Dashboard task: Next.js with health checks
   - Orchestrator task: settlement sweep + tournament sweep poll tasks
   - Resource limits: CPU, memory
   - Network mode: awsvpc
   - Execution role: pull from ECR, read from Secrets Manager
   - Task role: S3 access, CloudWatch logs, Secrets Manager read

4. **Configure ALB + WAF**:
   - HTTPS listener with ACM certificate
   - Path-based routing: `/api/*` → API service, `/*` → dashboard service
   - WAF rules: rate limiting, SQL injection protection, XSS protection
   - Health check paths configured

5. **Deploy and verify**:
   - ECS services running with minimum healthy percent 100%
   - Health endpoints return 200
   - Secrets injected from Secrets Manager (not env vars in task definition)
   - CloudWatch alarms firing on BudgetGuard threshold
   - S3 buckets have versioning + object lock enabled
   - No broker credentials in any container env

6. **Create deployment verification receipt**:
   - `reports/verification/aws-production-deployment-<date>.md`
   - Document: endpoints, health checks, alarm ARNs, secret ARNs
   - Verify: no secrets in task definitions, no secrets in logs

#### Acceptance criteria

- [ ] ECS Fargate tasks running for API, dashboard, orchestrator
- [ ] Secrets Manager provides all credentials (no plaintext env vars)
- [ ] CloudWatch alarms configured for BudgetGuard, settlement lag, shadow health
- [ ] S3 buckets have versioning + object lock
- [ ] WAF rules active on ALB
- [ ] No broker credentials in any container
- [ ] Health endpoints return 200
- [ ] Deployment verification receipt written

#### Dependencies

- AWS account with appropriate IAM permissions
- ECR repositories created
- Domain name + ACM certificate (or use Railway staging domain temporarily)
- This can be done in parallel with Tasks 2 and 3

#### Estimated effort

4-8 hours of agent work for IaC + deployment. Requires AWS CLI credentials and an AWS account.

#### Risk

Medium. IaC errors are common. Cost risk if services are left running. Start with minimum task counts and auto-scaling disabled.

#### Rollback

Destroy Terraform stack or delete CloudFormation stack. Keep Railway staging as fallback.

---

### Task 5: First Real Promotion + Paper Bridge Enablement (operational)

**BIG_PLAN reference:** Phase 7 + Phase 11 operational completion
**Status:** Code complete, never executed with real evidence

#### What exists

- `services/quant_foundry/src/quant_foundry/promotion.py` — `PromotionGate`, `PromotionReviewQueue`, `PromotionEvidence`, `PromotionRequest`
- `services/quant_foundry/src/quant_foundry/sentinel.py` — `LeakageSentinel` with negative controls, PBO, purged-fold verifier
- `services/quant_foundry/src/quant_foundry/paper_bridge.py` — `PaperBridge` with 7-step validation, circuit breaker, rollback pointer
- Gateway: `submit_promotion()`, `process_promotion()` (approve/reject)
- API: `POST /quant-foundry/promotion/submit`, `/approve`, `/reject`
- Dashboard: promotion submit form + approve/reject buttons with confirmation dialogs
- 27 integration tests + 14-step proof script all passing

#### What needs to be done

1. **Run the leakage/overfit sentinel** against the trained baseline model (from Task 1) after 30 days of settled shadow history (from Task 2):
   - `LeakageSentinel.run()` with `SentinelInput` containing:
     - Negative control battery results (shuffled labels, time-reversed features, future-leak injection)
     - Purged-fold verification data
     - PBO estimate
     - Train/live gap data (in-sample vs. settled live calibration)
     - Feature stability data across folds
   - Verify `SentinelReceipt.passed == True`
   - If sentinel fails, do NOT promote — investigate and retrain

2. **Build the promotion evidence packet**:
   - `PromotionEvidence` with:
     - `dossier` — the real dossier from Task 1
     - `tournament_result` — real tournament result from 30 days of settled history
     - `sentinel_receipt` — the sentinel receipt from step 1
     - `blocking_issues` — empty list (or resolved issues with notes)

3. **Submit the promotion request** via the dashboard or API:
   - `POST /quant-foundry/promotion/submit` with `model_id`, `target_level=paper_approved`, `review_note`
   - Verify the request appears in the pending queue

4. **Human review and approval**:
   - Review the evidence packet in the dashboard
   - Verify: dossier is complete, tournament score is above threshold, sentinel passed, no blocking issues
   - `POST /quant-foundry/promotion/approve` with `model_id` and `review_note`
   - Verify `PromotionReceipt.status == APPROVED`
   - Verify dossier status updated to `PAPER_APPROVED`

5. **Enable the paper bridge**:
   - Set `QUANT_FOUNDRY_ALLOW_PAPER_BRIDGE=true`
   - Set `QUANT_FOUNDRY_MODE=paper`
   - Verify `PaperBridge.publish()` succeeds for the promoted model
   - Verify `BridgeReceipt.status == PUBLISHED`
   - Verify rollback pointer created
   - Verify `PaperPrediction` has no order/OMS fields

6. **Run paper bridge for 30 days**:
   - Monitor `BridgeReceipt` outputs
   - Verify circuit breaker doesn't trip (no 5 consecutive failures)
   - Verify rollback pointer exists for every publish
   - Track paper prediction accuracy vs. shadow predictions
   - No orders executed, no risk state changed

#### Acceptance criteria

- [ ] Leakage/overfit sentinel passes on the trained model
- [ ] Promotion evidence packet is complete (dossier + tournament + sentinel)
- [ ] Human reviews and approves the promotion
- [ ] `PromotionReceipt.status == APPROVED`
- [ ] Dossier status updated to `PAPER_APPROVED`
- [ ] Paper bridge publishes successfully with `QUANT_FOUNDRY_ALLOW_PAPER_BRIDGE=true`
- [ ] `BridgeReceipt.status == PUBLISHED`
- [ ] Rollback pointer created for every publish
- [ ] No order/OMS fields in `PaperPrediction`
- [ ] No secrets in any receipt, log, or dashboard response
- [ ] 30 days of paper bridge operation with no circuit breaker trips

#### Dependencies

- Task 1 (trained baseline model)
- Task 2 (30 days of settled shadow history)
- Sentinel must pass (if it fails, retrain or adjust)
- Human operator must review and approve

#### Estimated effort

2-4 hours of agent work to run the sentinel, build the evidence packet, and submit the promotion. The human review is a manual decision. Then 30 days of paper bridge operation.

#### Risk

High. This is the first time the system transitions from shadow to paper. The sentinel may fail, requiring retraining. The paper bridge may encounter edge cases not covered by tests.

#### Rollback

- `unset QUANT_FOUNDRY_ALLOW_PAPER_BRIDGE` — paper bridge refuses all publishes
- `unset QUANT_FOUNDRY_ENABLED` — gateway disables entirely
- Reject the promotion request — model stays at `candidate` or `shadow_approved`
- Rollback pointer reverts the model pointer to prior state

---

## What Comes After These Five Tasks

Once all five tasks are complete, the system has:
- A real trained model family
- 30+ days of settled shadow history
- A real tournament leaderboard with real rankings
- The first model promoted through the real gate with real evidence
- 30+ days of paper bridge operation
- A deployed production control plane on AWS
- An automated recipe generation system (Alpha Genome Lab)

### Phase 12: Limited Live Pilot

After the five tasks, the remaining steps to actual limited live trading are:

1. **Broker sandbox configuration** — Set up a paper-broker account (Alpaca paper or similar) with credentials stored only in AWS Secrets Manager. Never exposed to RunPod. OMS reads credentials, places paper orders.

2. **Position size limits and kill switch for live trading** — Define maximum position sizes, maximum daily loss, maximum number of concurrent positions. Wire a live-trading kill switch that halts all order placement (separate from the BudgetGuard kill switch which halts GPU spend).

3. **Live trading authorization gate** — A new gate beyond `paper_approved`: `live_approved`. Requires:
   - 30+ days of paper bridge operation with positive or neutral P&L
   - No circuit breaker trips during paper operation
   - Human review of paper trading results
   - Explicit operator authorization via a new `POST /quant-foundry/promotion/approve` with `target_level=live_approved`
   - This gate does NOT exist yet — it would need to be added to `DossierStatus` enum and `PromotionGate.evaluate()`

4. **Re-run the limited live readiness review** — Update `docs/LIMITED_LIVE_READINESS_REVIEW.md` with:
   - All 14 gates should now be MET
   - All 8 blockers should now be RESOLVED
   - Verdict should change from NOT READY to "READY for limited paper-to-live pilot with exact caps"
   - Document the exact caps: position size, daily loss, concurrent positions, symbols allowed

5. **Limited live pilot launch** — Set `QUANT_FOUNDRY_MODE=live` against the `live_approved` model with:
   - Position size limits enforced by OMS
   - Daily loss limit enforced by risk service
   - Kill switch armed
   - CloudWatch alarms on all limits
   - Human monitoring for the first 72 hours
   - Daily review of trading results for the first 30 days

### Phase 13: Continuous Improvement

After the limited live pilot is stable:

1. **Alpha Genome Lab sweeps** — Run automated recipe generation to find better model families. All recipes go through the same evidence loop.

2. **Model rotation** — As new models prove themselves in shadow → tournament → promotion → paper → live, retire underperforming models via `RetirementFlagger`.

3. **Capacity expansion** — Add more symbols, more strategies, more RunPod endpoints. Scale GPU spend via BudgetGuard.

4. **Full live trading** — Remove the "limited" caps after 90+ days of stable limited live operation. Requires another readiness review.

---

## Task Dependency Graph

```
Task 1: Train Real Baseline Model
  │
  ├──→ Task 2: 30-Day Shadow Inference Run (needs trained model)
  │      │
  │      └──→ Task 5: First Real Promotion + Paper Bridge (needs 30 days of history)
  │             │
  │             └──→ Phase 12: Limited Live Pilot (needs 30 days of paper bridge)
  │
  ├──→ Task 3: Alpha Genome Lab (needs trained model as parent recipe)
  │
  └──→ Task 4: Deploy AWS Production Control Plane (can run in parallel)
```

**Critical path:** Task 1 → Task 2 → Task 5 → Phase 12
**Parallel tracks:** Task 3 (Alpha Genome Lab) and Task 4 (AWS deployment) can run alongside the critical path.

---

## Recommended Execution Order

1. **Start Task 4 (AWS deployment) immediately** — it's parallel to everything else and needed for production stability
2. **Start Task 1 (train real baseline)** — this is the prerequisite for Tasks 2, 3, and 5
3. **Start Task 3 (Alpha Genome Lab) after Task 1** — greenfield code, can be built while Task 2 runs
4. **Start Task 2 (30-day shadow run) after Task 1** — the long pole, 30 days of wall-clock time
5. **Start Task 5 (first promotion + paper bridge) after Task 2** — needs 30 days of settled history

Tasks 1, 3, and 4 can all be worked on by parallel agents. Task 2 is mostly waiting. Task 5 requires Task 2 to complete.

---

## Current Blocker Resolution Status

| Blocker | Status after these 5 tasks |
|---|---|
| B1 — No promoted model family | **RESOLVED** by Task 5 |
| B2 — Shadow inference is stub-only | **RESOLVED** (already done — live RunPod inference proven) |
| B3 — Paper bridge never enabled | **RESOLVED** by Task 5 |
| B4 — No production deployment | **RESOLVED** by Task 4 |
| B5 — No broker credentials | Still OPEN — resolved in Phase 12 |
| B6 — Real RunPod GPU never run | **RESOLVED** (already done — training + inference on real GPUs) |
| B7 — Sentinel un-runnable | **RESOLVED** by Task 5 (sentinel runs on real model) |
| B8 — Settled history is empty | **RESOLVED** by Task 2 (30 days of settled history) |

After these 5 tasks: **7 of 8 blockers resolved.** Only B5 (broker credentials) remains, which is a Phase 12 task.
