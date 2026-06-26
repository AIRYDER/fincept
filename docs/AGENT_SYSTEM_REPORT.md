# Fincept Quant Foundry — Deep System Report

> **Audience:** anyone joining the codebase — engineers, auditors, downstream
> agent trainers, operator handoff. This document is the single canonical
> "what is this thing" reference.
>
> **Scope:** Quant Foundry (Phases 3-10) — the candidate-model lifecycle from
> data manifest through shadow inference through human-gated promotion, plus
> every agent that contributed and where their context lives.
>
> **Posture (2026-06-23):** Stack is contract-proven and TDD-tested end-to-end
> at the Python module + API surface level. **No real RunPod GPU has run.**
> **No model has been promoted.** **Live trading is disabled by default.**
> See `docs/LIMITED_LIVE_READINESS_REVIEW.md` (commit `7027db4`) for the
> formal NOT-READY verdict and the 8 specific blockers (B1-B8).
>
> **Branch:** `codex/portfolio-optimizer-core`
> **Last plan completed:** `quant-foundry-remaining-tasks` (14/14 ✅,
> 6h 28m 25s total elapsed)

---

## Table of Contents

1. [System overview](#1-system-overview)
2. [Agent roster — who did what, where their logs live](#2-agent-roster)
3. [Phase map — when each module shipped](#3-phase-map)
4. [Module inventory — what each file does](#4-module-inventory)
5. [Module connection map — how features wire together](#5-module-connection-map)
6. [RunPod side — the two containers](#6-runpod-side)
7. [Non-RunPod hosting — where everything plugs back in](#7-non-runpod-hosting)
8. [Hard invariants — what is forbidden, where it is enforced](#8-hard-invariants)
9. [Where to start / reference index](#9-where-to-start--reference-index)

---

## 1. System overview

**The Quant Foundry is a closed loop for candidate trading models.** It
ingests data, trains candidate models on RunPod GPUs, scores them in shadow
mode (no trading authority), routes them through a human-gated promotion
queue, and (only after a real RunPod run + a real human review) could one
day feed a model pointer into the paper-to-live bridge. **At the time of
this report, that loop is built top-to-bottom but the GPU half has never
fired.**

```
┌────────────────────────────────────────────────────────────────────────────┐
│                         FINCEPT (non-RunPod side)                          │
│                                                                            │
│  Operator  ──JWT──▶  FastAPI routes ──▶  gateway (QuantFoundryGateway)    │
│                                  │                                       │
│  Dashboard  ──JWT──▶  /quant-foundry/{jobs,models,tournament,              │
│                                 promotion,shadow}                         │
│                                                                            │
│  gateway owns:                                                            │
│   ├─ JobOutbox (JSONL) ─┐                                                │
│   ├─ CallbackInbox      │                                                │
│   ├─ MockDispatcher ────┤                                                │
│   ├─ CallbackProcessor ─┤                                                │
│   ├─ DossierRegistry    ├─ additive lazy-init (TASK-0802)                  │
│   ├─ ExpandedLeaderboard                                                  │
│   ├─ PromotionReviewQueue                                                 │
│   └─ ShadowLedger (real, durable JSONL) — TASK-0604                       │
│                                                                            │
│  Quant Foundry modules (36 files):                                       │
│   evidence: settlement · outcomes · metrics · shadow_ledger ·              │
│             shadow_settlement · shadow_inference · dossier ·               │
│             registry · artifacts                                          │
│   scoring:   tournament · leaderboard · leaderboard_expanded ·             │
│             significance · pbo · sentinel · retirement                     │
│   routing:   moe_router · conformal_gate · drift_sentinel ·                │
│             promotion                                                     │
│   bridge:    paper_bridge · causal_graph (research only)                   │
│   infra:     budget · callbacks · dataset_manifest · feature_lake ·       │
│             feature_availability · feature_snapshot_export ·               │
│             ids · signatures · schemas · outbox · inbox ·                  │
│             mock_dispatcher · runpod_client · runpod_training ·            │
│             gateway                                                       │
│                                                                            │
└────────────────────────────────────────────────────────────────────────────┘
                                    │
                                    │  HMAC-signed callbacks
                                    │  (QUANT_FOUNDRY_CALLBACK_SECRET)
                                    ▼
┌────────────────────────────────────────────────────────────────────────────┐
│                           RUNPOD (container side)                            │
│                                                                            │
│  runpod/quant-foundry-training/                                           │
│   ├─ handler.py ──▶ RunPodTrainingHandler (services-side class)             │
│   └─ Returns signed RunPodCallbackEnvelope                                 │
│                                                                            │
│  runpod/quant-foundry-inference/                                          │
│   ├─ handler.py ──▶ ShadowInferenceEngine (services-side class)            │
│   └─ Returns signed callback + ShadowPrediction batch                      │
│                                                                            │
│  Invariant: NO broker creds, NO Redis, NO sig.predict, NO order fields.    │
│  Only `QUANT_FOUNDRY_CALLBACK_SECRET` (HMAC) is consumed.                  │
│                                                                            │
└────────────────────────────────────────────────────────────────────────────┘
```

**Where the Fincept side and RunPod side meet:** the only crossing is the
HMAC-signed callback (`POST /quant-foundry/callbacks/runpod`). RunPod
cannot write to anything on the Fincept side except this endpoint, and the
Fincept side is the only authority that decides what a RunPod callback
means (verify HMAC, schema-validate, write to inboxes).

---

## 2. Agent roster

Six GLM-5.2 builder agents worked on the Quant Foundry between 2026-06-22
and 2026-06-23, plus one handoff author (Builder 6's predecessor, see
`docs/AAA_GLM_SUPERTEAM_LOGS/HANDOFF_REMAINDER_GLM52.md` — the file
**itself** is the handoff prompt, written by the previous builder session).

| Agent | Track / phase | Work log | Sessions used | Commits |
|---|---|---|---|---|
| **Builder 1 (GLM-5.2)** | evidence-loop foundations (Phase 4): settlement ledger, metrics, outcomes | `docs/AAA_GLM_SUPERTEAM_LOGS/BUILDER1_GLM.md` (37 KB) | adopted TASK-0302 (yielded), TASK-0401, TASK-0402 (yielded to Builder 3), TASK-0306 (yielded) | `855f01b` (settlement.py + outcomes.py + metrics.py) |
| **Builder 2 (GLM-5.2)** | durability layer + gateway (Phase 3): outbox, inbox, mock dispatcher, callbacks, gateway | `docs/AAA_GLM_SUPERTEAM_LOGS/BUILDER2.md` (38 KB) | adopted TASK-0303, TASK-0304, TASK-0305, TASK-0306 | `48c0c27` (outbox + inbox), mock_dispatcher + callbacks (commit in BUILDER2.md), gateway (commit in BUILDER2.md) |
| **Builder 3 (GLM-5.2)** | dossier registry, tournament scoring, RunPod containers, Phases 5-7 + Phase 10 (part) | `docs/AAA_GLM_SUPERTEAM_LOGS/BUILDER3.md` (90 KB — the largest) | adopted TASK-0402 (after Builder 1 yielded), TASK-0403, TASK-0404, then drove all of Phases 5/6/7/10 (partial) | `de56c38` (dossier), `fd3f115` (tournament), `caeb468` (TASK-0504 first baseline), `b3fc4e1` (TASK-0502 dispatch client), `ae893a6` (TASK-0503 artifact import), `df326d4` (TASK-0601 RunPod inference MVP), `1a91a82` (TASK-0602 feature snapshot export), `0aa4aef` (TASK-0603 shadow settlement), `0831e2c` (TASK-0701 expanded leaderboard), `60f9e61` (TASK-0702 promotion queue), `ffe9ce7` (TASK-0703 retirement/edge-decay), `e95c51f` (TASK-0704 paper bridge), `a88e8c2` (TASK-1001 MoE router), `e272b6e` (TASK-1003 conformal gate), `22700a7` (TASK-1004 drift sentinel) |
| **Builder 4 (GLM-5.2)** | feature lake + shadow ledger (Phase 4) | `docs/AAA_GLM_SUPERTEAM_LOGS/BUILDER4.md` (15 KB) | adopted TASK-0405, TASK-0402 (after Builder 1 yielded) | `7f704bd` (feature_lake + dataset_manifest + feature_availability), shadow_ledger (commit in BUILDER4.md) |
| **Builder 5 (GLM-5.2)** | operator experience (on-demand module control) | `docs/AAA_GLM_SUPERTEAM_LOGS/BUILDER5.md` (7 KB) | adopted TASK-0203 | (commit in BUILDER5.md; see `libs/fincept-core` and `services/api/src/api/routes/modules.py`) |
| **Builder 6 (GLM-5.2)** | this plan — remaining tasks, Phase 10 causal graph, dashboard pages, shadow health, readiness review | `docs/AAA_GLM_SUPERTEAM_LOGS/BUILDER6.md` (14 KB) | adopted TASK-1002 (causal graph), TASK-0802 (dashboard pages), TASK-1101 (readiness review), TASK-0604 (shadow health) | `808e7ab` (causal_graph) + `19afc5b` (docs), `8f3a589` (dashboard pages) + `eed6612` (docs), `7027db4` (readiness report) + `25db6ee` (docs), `4233e64` (shadow health) + `751d212` (docs) |

**Where to find agent context:**

| Resource | Path |
|---|---|
| All builder logs | `docs/AAA_GLM_SUPERTEAM_LOGS/BUILDER{1..6}.md` |
| Inter-agent messages | `docs/AAA_GLM_SUPERTEAM_LOGS/AGENT_TO_AGENT_MESSAGING/` |
| Handoff that kicked off Builder 6's plan | `docs/AAA_GLM_SUPERTEAM_LOGS/HANDOFF_REMAINDER_GLM52.md` |
| Orchestrator session state (sessions used per task) | `.omo/boulder.json` |
| This plan | `.omo/plans/quant-foundry-remaining-tasks.md` |
| Plan-internal learnings (this run) | `.omo/notepads/quant-foundry-remaining-tasks/{learnings,issues,problems,decisions}.md` |
| Source of truth for task scope | `docs/NEXT_STEPS_PLAN.md` (all 4 docs commits `19afc5b`/`eed6612`/`25db6ee`/`751d212` add ownership + completion blockquotes) |
| Readiness verdict (NOT READY + 8 blockers) | `docs/LIMITED_LIVE_READINESS_REVIEW.md` (commit `7027db4`) |

**Session ID conventions** (from `.omo/boulder.json`):
- `opencode:ses_10d...` — orchestrator Atlas session for the current plan
- `opencode:ses_10d889c6cfferdBL9qGOpTWcxU` — Builder 6 (writing category) — TASK-1101
- `opencode:ses_10d8874d3ffeRzjfy09RdAvOP0` — Builder 6 (deep category) — TASK-1002
- `opencode:ses_10d4dec5dffeQBFSljPyCHUV1j` — Builder 6 (deep category) — TASK-0604
- `opencode:ses_10d89cccaffeebuOay9zDq6hd5` — Builder 6 (deep category) — TASK-0802 endpoints
- `opencode:ses_10d302dffffePKfLc7DbM19Bnn` — Final-wave F1 oracle (plan compliance)
- `opencode:ses_10d3010baffeLDUIvFyVEX1fJe` — Final-wave F2 oracle (code quality)
- `opencode:ses_10d302a95ffevsJdSnK7ZZqsQt` — Final-wave F3 oracle (real manual QA)
- `opencode:ses_10d3015c1ffekR1l4D5afoaH5t` — Final-wave F4 oracle (scope fidelity)

**Builders 1-5 are NOT in the boulder** because they completed before the
orchestrator (Atlas) picked up the remaining-tasks plan. Their evidence
lives only in their `.md` work logs and in `git log --all` commits they
authored (e.g. `855f01b` for Builder 1, `48c0c27` for Builder 2, etc.).

---

## 3. Phase map

The plan's numbering is by **task order** (lower = earlier dependency),
not by phase. Here is the actual phase map (from `docs/NEXT_STEPS_PLAN.md`):

| Phase | Theme | Tasks (in chronological order) | Builder | Key commit SHAs |
|---|---|---|---|---|
| **Phase 3** | Durability layer | TASK-0301..0306 (ids, signatures, schemas, outbox, inbox, mock dispatcher + callbacks, gateway) | Builder 1, 2, 3 | `48c0c27` (0304), 0305-0306 in BUILDER2.md |
| **Phase 4** | Evidence loop foundations | TASK-0401 (settlement), TASK-0402 (shadow ledger), TASK-0403 (dossier), TASK-0404 (tournament), TASK-0405 (feature lake), TASK-0406 (leakage sentinel) | Builder 1, 3, 4 | `855f01b` (0401), shadow_ledger in BUILDER4.md, `de56c38` (0403), `fd3f115` (0404), `7f704bd` (0405), `d864b94` (0406 per BUILDER3.md) |
| **Phase 5** | RunPod training | TASK-0501 (container MVP), TASK-0502 (dispatch client), TASK-0503 (artifact import from S3), TASK-0504 (first real baseline) | Builder 3 | `b3fc4e1` (0502), `ae893a6` (0503), `caeb468` (0504) |
| **Phase 6** | RunPod shadow inference | TASK-0601 (container MVP), TASK-0602 (feature snapshot export), TASK-0603 (shadow settlement) | Builder 3 | `df326d4` (0601), `1a91a82` (0602), `0aa4aef` (0603) |
| **Phase 7** | Tournament & promotion | TASK-0701 (expanded leaderboard), TASK-0702 (promotion queue + human gate), TASK-0703 (retirement / edge-decay), TASK-0704 (paper bridge) | Builder 3 | `0831e2c` (0701), `60f9e61` (0702), `ffe9ce7` (0703), `e95c51f` (0704) |
| **Phase 8** | Operator experience | TASK-0801 (QF overview dashboard) | Builder 3 | (commit in BUILDER3.md) |
| **Phase 9** | Deployment & cost-optimized runtime | TASK-0901 (BudgetGuard), TASK-0902 (design only), TASK-0903 (design only) | Builder 3 | `6256cdf` (0901) |
| **Phase 10** | Research + gating | TASK-1001 (MoE router), TASK-1002 (causal graph), TASK-1003 (conformal gate), TASK-1004 (drift sentinel) | Builder 3, 6 | `a88e8c2` (1001), `808e7ab` (1002), `e272b6e` (1003), `22700a7` (1004) |
| **Phase post-10** | This plan (remaining tasks) | TASK-0604 (shadow health), TASK-0802 (4 dashboard pages), TASK-1101 (readiness review) | Builder 6 | `4233e64` (0604), `8f3a589` (0802), `7027db4` (1101) |

---

## 4. Module inventory

All modules live in `services/quant_foundry/src/quant_foundry/`. File size in bytes; **bold** = critical to read first.

### Evidence loop (Phases 3-4)

| Module | LOC | What it does | Connects to |
|---|---|---|---|
| **`gateway.py`** | 19,450 | **Read this first.** The facade the API talks to. Owns outbox, inbox, mock dispatcher, callback processor, dossier registry, leaderboard, promotion queue, shadow ledger, budget guard. Reads env vars: `QUANT_FOUNDRY_ENABLED`, `QUANT_FOUNDRY_MODE`, `QUANT_FOUNDRY_SHADOW_ONLY`, `QUANT_FOUNDRY_CALLBACK_SECRET`, `QUANT_FOUNDRY_BASE_DIR`. Default `enabled=false`. | everything below |
| `outbox.py` | 11,316 | Durable JSONL outbox for jobs. `JobOutbox.enqueue`, `update_status`, `get`, `list`. Idempotent on `(job_id, payload_hash)`. Rejects same job_id + different hash as security event. | gateway, mock_dispatcher, runpod_client |
| `inbox.py` | 9,082 | Durable JSONL inbox for callbacks. `CallbackInbox.receive` (idempotent on `(job_id, payload_hash)`), `mark_processed`, `get_by_job_id`. Signature verification is the gate (not enforced in inbox itself). | gateway, callbacks |
| `signatures.py` | 2,940 | `sign_callback`, `verify_callback` — HMAC-SHA256 with `QUANT_FOUNDRY_CALLBACK_SECRET`. | gateway, mock_dispatcher, callbacks, runpod handlers |
| `ids.py` | 1,930 | `hash_payload`, `make_idempotency_key` — deterministic hashing. | signatures, outbox, inbox, dossier, registry, mock_dispatcher |
| `schemas.py` | 7,473 | Cross-boundary Pydantic models: `RunPodTrainingRequest`, `RunPodInferenceRequest`, `RunPodCallbackEnvelope`, `ModelDossier`, `ArtifactManifest`, `ShadowPrediction`, `Authority`, `JobType`. All `extra="forbid"`, frozen. | every module that crosses the RunPod boundary |
| `settlement.py` | 11,950 | `SettlementLedger` (filesystem JSONL, idempotent on `(prediction_id, cost_model_version)`). `pending_time` vs `pending_data` vs `settled` states. Post-decision window enforced. | outcomes, metrics, dossier, tournament |
| `outcomes.py` | 4,611 | `SettlementRecord`, `SettlementStatus`, `CostModel` (frozen, versioned). Carries gross+net, decision window, tournament fields. | settlement, tournament |
| `metrics.py` | 5,581 | Pure math: `realized_return` (look-ahead guarded), `brier_score`, `calibration_bucket`, `abnormal_return`, `apply_costs` (fee+spread+slippage+borrow). | settlement, sentinel |
| `shadow_ledger.py` | 13,371 | Durable JSONL shadow prediction storage. `store_batch` (idempotent, tamper-check via `compute_batch_hash`). `ShadowLedgerRecord` carries `authority=shadow-only` (structural invariant). | gateway (real ledger), shadow_inference, shadow_settlement |
| `shadow_settlement.py` | 13,260 | Settles shadow predictions against realized outcomes. `store_batch`, `settle_prediction`, `settle_batch`. | shadow_ledger, settlement, dossier |
| `shadow_inference.py` | 8,707 | `ShadowInferenceEngine` (called by RunPod inference handler). `FeatureSnapshot`, `InferenceDisabledError`. Disabled unless `QUANT_FOUNDRY_MODE=runpod_shadow`. | runpod inference handler, shadow_ledger, callbacks |
| `dossier.py` | 8,969 | `DossierRecord`, `DossierStatus` (CANDIDATE / SHADOW_APPROVED / PROMOTED / RETIRED). Carries artifact manifest, dataset manifest refs, training metrics, evidence refs. | registry, mock_dispatcher (training complete), promotion |
| `registry.py` | 6,956 | `DossierRegistry` (durable). `register`, `list`, `get`, `get_by_hash`. | dossier, gateway, tournament |
| `artifacts.py` | 15,900 | `ArtifactManifest` (frozen, full reproducibility set: feature_schema_hash, label_schema_hash, code_git_sha, lockfile_hash, container_image_digest, random_seed, hardware_class). | dossier, runpod_training |
| `tournament.py` | 24,610 | `ScoringInput` schema, `TournamentResult`. Stationary/block bootstrap p-value (not IID t-test), deflated Sharpe ratio. | leaderboard, dossier, settlement, sentinel |
| `leaderboard.py` | 3,006 | Basic `Leaderboard` ranking. | tournament |
| **`leaderboard_expanded.py`** | 13,051 | `ExpandedLeaderboard.ranked()`, `stale_models()`, `decayed_models()`, `explain()`. Used by API `/tournament/leaderboard`. | tournament, retirement, sentinel |
| `significance.py` | 11,365 | Stationary/block bootstrap, deflated Sharpe ratio math. | tournament |
| `pbo.py` | 9,007 | Probability of backtest overfitting. | sentinel |
| `sentinel.py` | 30,568 | **Largest module.** Leakage/overfit sentinel. Used for promotion gate. | settlement, metrics, pbo, promotion |
| `dataset_manifest.py` | 7,066 | `FeatureLakeManifest`, `PurgedFoldSpec`, `FoldBoundary`. Embargo ≥ max label horizon. As-of universe (no survivorship bias). | feature_lake, runpod_training |
| `feature_lake.py` | 13,818 | `FeatureLakeBuilder` (as-of joins only, PIT proof, leak rejection, export receipt). | dataset_manifest, feature_availability |
| `feature_availability.py` | 2,948 | Per-feature availability report. | feature_lake, shadow_inference |
| `feature_snapshot_export.py` | 9,913 | Live feature snapshot export for shadow inference. | feature_lake, runpod inference handler |
| `promotion.py` | 11,719 | `PromotionReviewQueue`, `PromotionGate.evaluate()` (4 fail-closed checks: dossier + settlement evidence + sentinel pass + human review note). | dossier, settlement, sentinel, paper_bridge |
| `retirement.py` | 7,495 | Retirement / edge-decay flags. | leaderboard_expanded |
| `paper_bridge.py` | 11,835 | **Reads `QUANT_FOUNDRY_ALLOW_PAPER_BRIDGE` env var (refuses unless explicitly enabled).** `RollbackPointer` model + creation step before publish. Refuses with explicit reason string when disabled. | promotion (output of gate), would be live OMS (currently disabled) |
| `moe_router.py` | 11,784 | Mixture-of-experts model router. | conformal_gate, drift_sentinel, promotion, paper_bridge |
| `conformal_gate.py` | 10,020 | Conformal prediction risk gate. | moe_router |
| `drift_sentinel.py` | 10,247 | Adversarial drift sentinel (`DriftSentinel.evaluate()`, `check_drift()`). | moe_router, shadow_inference |
| `causal_graph.py` | 5,242 | **Research only.** `CausalNode` (SYMBOL/SECTOR/EVENT/REGIME/OUTCOME), `CausalEdge` (LEADS/LAGS/CORRELATES/CAUSES/INFLUENCES), `CausalGraph`, `CausalGraphBuilder`, `extract_features`. **No live data, no `sig.predict`, no order fields.** | research (not connected to live loop) |
| `budget.py` | 12,197 | `BudgetGuard` (fail-closed). Per-job budget + monthly ceiling. `kill_switch`. Wired into gateway. | gateway |
| `callbacks.py` | 11,800 | `CallbackProcessor`, `ShadowLedgerStub` (in-process), `DossierStub` (in-process). Fail-closed on bad signature. | gateway, mock_dispatcher, shadow_ledger |
| `mock_dispatcher.py` | 11,460 | `MockDispatcher` — simulates RunPod in `local_mock` mode. Drives outbox transitions, signs callbacks. | gateway, outbox, inbox, signatures, runpod_client |
| `runpod_client.py` | 14,004 | Real RunPod dispatch client (HTTP, async). Used by `runpod_training.py` and `runpod_inference.py`. | runpod_training, runpod_inference |
| `runpod_training.py` | 10,417 | `RunPodTrainingHandler` (called by `runpod/quant-foundry-training/handler.py`). Parses `RunPodTrainingRequest`, trains, returns signed callback. | artifacts, dossier, mock_dispatcher (same contract) |
| `__init__.py` | 1,210 | Package init. | — |

### Test suite (per-module, 26 new tests in this plan)

`services/quant_foundry/tests/`:
- `test_causal_graph.py` — 12 TDD tests (this plan, commit `808e7ab`)
- `test_dossiers.py` (in `services/api/tests/`) — 8 TDD tests (commit `8f3a589`)
- `test_quant_foundry_shadow.py` (in `services/api/tests/`) — 6 TDD tests (commit `4233e64`)
- Plus all Builder 1-3 tests (582+ tests passing after TASK-1002; 997+ after this plan)

---

## 5. Module connection map

### Job lifecycle (training)

```
Outbox.enqueue(job_id, request_payload, payload_hash)
  └─ MockDispatcher.dispatch(job_id, request_payload)
       ├─ verify payload hash matches outbox (tamper check)
       ├─ parse RunPodTrainingRequest
       ├─ outbox: DISPATCHING → DISPATCHED → RUNNING
       ├─ [mock mode] OR [real RunPod via runpod_client → runpod_training]
       │     └─ RunPodTrainingHandler (in services-side, called from runpod/quant-foundry-training/handler.py)
       │          ├─ train tiny baseline (LightGBM in TASK-0504)
       │          ├─ write ArtifactManifest (full reproducibility pins)
       │          └─ write ModelDossier
       ├─ build RunPodCallbackEnvelope, durably write to <base_dir>/payloads/<job_id>.json
       ├─ signatures.sign_callback(payload) → HMAC
       ├─ Inbox.receive(job_id, payload, signature, signature_valid=True)
       └─ CallbackProcessor.process(job_id)
            ├─ verify signature via signatures.verify_callback (fail-closed)
            ├─ schema-validate RunPodCallbackEnvelope (Pydantic extra='forbid')
            ├─ cross-job replay guard (envelope.job_id must match)
            ├─ apply domain effect: DossierStub.store(dossier)
            ├─ Inbox.mark_processed(PROCESSED)
            └─ outbox: VALIDATING → COMPLETED
```

**Real RunPod path** (currently never run): outbox → `runpod_client` → RunPod API → RunPod container `handler.py` → `RunPodTrainingHandler` (services-side) → `POST /quant-foundry/callbacks/runpod` (HMAC) → Inbox → Processor → Dossier.

### Shadow inference lifecycle

```
Outbox.enqueue(inference job)
  └─ MockDispatcher.dispatch
       └─ ShadowInferenceEngine.run(request, snapshot, model_id) [mock OR real RunPod]
            └─ ShadowLedgerStub.store(predictions) [mock] OR ShadowLedger.store_batch (real)
                 └─ shadow_settlement.settle_prediction (against realized outcomes)
                      └─ settlement.SettlementLedger (with cost model)

Each prediction:
  - authority: SHADOW_ONLY (structural)
  - latency_ms: recorded
  - feature_availability: recorded
  - direction, confidence, expected_return, p_up: recorded
  - NO order fields (quantity, side, broker, order_type) — rejected at store time
  - NO write to sig.predict or any trading stream
```

### Promotion gate

```
PromotionGate.evaluate(candidate) [promotion.py]:
  1. dossier present + complete?         (dossier.py / registry.py)
  2. settlement evidence net-positive?   (settlement.py / outcomes.py)
  3. sentinel pass?                      (sentinel.py / pbo.py / metrics.py)
  4. human review note present?          (operator input)

  AND all 4 → enqueue in PromotionReviewQueue
  AND any 1 missing → blocked (blocking_issues list)

PromotionReviewQueue.pending() → /quant-foundry/promotion/queue (read-only)
Operator (human) → review packet → approve / reject
  (NO POST endpoint yet; this plan's TASK-0802 page shows a confirmation
   dialog but the server-side action is a future task per BUILDER6.md)
```

### Paper bridge (disabled)

```
paper_bridge.publish(model_pointer):
  if not QUANT_FOUNDRY_ALLOW_PAPER_BRIDGE:
    return RefusedError("set QUANT_FOUNDRY_ALLOW_PAPER_BRIDGE=true explicitly")
  
  write RollbackPointer (paper_bridge.py:105)
  return publish receipt

  (would feed live OMS — currently disabled by env var)
```

### MoE routing (research-stage)

```
moe_router.route(features):
  conformal_gate.gate(prediction) → confidence check
  drift_sentinel.check(features) → drift check
  if both pass → return selected model
  if either fails → abstaining prediction
  (all decisions are shadow-only; no order writes)
```

### Causal graph (research only — no live connection)

```
CausalGraphBuilder.add_node / .add_edge
  └─ build() → CausalGraph (frozen)
       └─ extract_features(graph, node_id) → dict[str, float]
            (degree centrality, weighted degree, avg neighbor strength, lag stats)
       └─ explain_analogs(graph, node_id) → text
```

---

## 6. RunPod side

Two containers, both file-disjoint, both use the same callback contract.

### Training container

| File | Purpose |
|---|---|
| `runpod/quant-foundry-training/handler.py` | RunPod serverless entrypoint. Reads `event["input"]`, validates `RunPodTrainingRequest`, instantiates `RunPodTrainingHandler` (from `services/quant_foundry/src/quant_foundry/runpod_training.py`), returns `RunPodCallbackEnvelope` + HMAC signature. |
| `runpod/quant-foundry-training/Dockerfile` | Container build. |
| `runpod/quant-foundry-training/README.md` | Operator doc; security boundary callout. |

**Env vars (training container):**
- `QUANT_FOUNDRY_CALLBACK_SECRET` (HMAC; required in prod)
- `QUANT_FOUNDRY_TRAINING_DEADLINE_SECONDS` (default 600)

**Input contract:** `event["input"]` is a dict matching `RunPodTrainingRequest` (schema_version, job_id, dataset_manifest_ref, model_family, search_space, random_seed, hardware_class, extra_constraints).

**Output contract (success):** `{job_id, callback_payload, callback_signature, callback_ts, artifact_id, dossier_id}`.

**Output contract (failure):** `{job_id, error_code, error_summary}`.

**Security invariants** (proven by F4 grep, all read-only denial statements):
- NO `ALPACA_API_KEY` / no broker creds
- NO `FINCEPT_JWT_SECRET`
- NO Redis URL
- NO trading access
- `authority=SHADOW_ONLY` always

### Inference container

| File | Purpose |
|---|---|
| `runpod/quant-foundry-inference/handler.py` | RunPod entrypoint. Reads `event["input"]`, parses `RunPodInferenceRequest` + `FeatureSnapshot`, instantiates `ShadowInferenceEngine`. Returns signed callback + `ShadowPrediction` batch. |
| `runpod/quant-foundry-inference/Dockerfile` | Container build. |
| `runpod/quant-foundry-inference/README.md` | Operator doc. |

**Env vars (inference container):**
- `QUANT_FOUNDRY_MODE` (must be `runpod_shadow` to enable)
- `PYTHONPATH` (default `/app/services/quant_foundry/src`)

**Disabled-by-default:** when `QUANT_FOUNDRY_MODE != "runpod_shadow"`, the engine raises `InferenceDisabledError` — no predictions produced. Fail-safe.

### How RunPod reaches the Fincept side

```
RunPod container → RunPod callback URL
  (configured in RunPod dashboard; points to Fincept host)

POST {FINCEPT_HOST}/quant-foundry/callbacks/runpod
  Headers:
    X-QF-Job-Id: <job_id>
    X-QF-Timestamp: <unix_seconds>
    X-QF-Signature: <hmac_sha256(secret, timestamp + job_id + body)>
  Body:
    <RunPodCallbackEnvelope JSON>

Fincept side (services/api/src/api/routes/quant_foundry.py:249):
  - verify HMAC via signatures.verify_callback
  - 401 on missing/bad signature
  - 400 on bad timestamp
  - 404 on unknown job_id
  - 200 on success
```

**No other crossing.** RunPod cannot write to any other Fincept surface, and RunPod has no access to OMS, risk, broker creds, or `sig.predict`.

---

## 7. Non-RunPod hosting

This is where everything plugs back in. Three layers: **gateway** (Python facade) → **API** (FastAPI routes) → **dashboard** (Next.js pages).

### Layer 1: Gateway (the brain)

`services/quant_foundry/src/quant_foundry/gateway.py` (19,450 bytes, **read this first**)

The single facade the API talks to. Wires every module together. Owns the in-process state. Reads env vars (NOT `fincept_core.Settings`) to stay file-disjoint:

| Env var | Default | Effect |
|---|---|---|
| `QUANT_FOUNDRY_ENABLED` | `"false"` | When `"false"`, all operator endpoints return safe disabled state, NO jobs are created or processed. **This is the master kill switch.** |
| `QUANT_FOUNDRY_MODE` | `"local_mock"` | `local_mock` runs the full loop synchronously on `create_job` (mock dispatcher). `runpod_shadow` enables real RunPod. |
| `QUANT_FOUNDRY_SHADOW_ONLY` | `"true"` | Structural shadow-only enforcement. |
| `QUANT_FOUNDRY_CALLBACK_SECRET` | `""` | HMAC secret for RunPod callbacks. Empty in local_mock (no real callbacks). |
| `QUANT_FOUNDRY_BASE_DIR` | `"reports/quant-foundry"` | Where the JSONL durability lives (`outbox.jsonl`, `inbox.jsonl`, `payloads/`, `budget/`, etc.). |

**Additive lazy-init** (TASK-0802/0604, this plan): the gateway now lazily constructs the real (durable) `DossierRegistry`, `ExpandedLeaderboard`, `PromotionReviewQueue`, and `ShadowLedger` from `base_dir` — not just the in-process stubs. This is the upgrade path from mock to real.

**Key public methods** (used by the API):

| Method | Returns | Notes |
|---|---|---|
| `health()` | `{enabled, mode, shadow_only, job_count}` | 503 if disabled. |
| `heartbeats()` | `list[dict]` | empty in `local_mock` mode. |
| `create_job(...)` | receipt | 402 on budget exceeded, 429 on kill switch. |
| `list_jobs(status=)` | `list[dict]` | |
| `get_job(job_id)` | `dict \| None` | |
| `list_dossiers(status=)` | `list[dict]` | **NEW** (TASK-0802) — delegates to real `DossierRegistry`. |
| `get_dossier(model_id)` | `dict \| None` | **NEW** (TASK-0802). |
| `tournament_leaderboard()` | `list[dict]` | **NEW** (TASK-0802) — delegates to real `ExpandedLeaderboard.ranked()`. |
| `pending_promotions()` | `list[dict]` | **NEW** (TASK-0802) — delegates to real `PromotionReviewQueue.pending()`. |
| `completed_promotions()` | `list[dict]` | **NEW** (TASK-0802) — delegates to real `PromotionReviewQueue.completed()`. |
| `shadow_health()` | `dict` | **NEW** (TASK-0604) — delegates to real `ShadowLedger.list()`. |
| `receive_callback(job_id, payload, signature, ts)` | receipt | HMAC-verified; fail-closed on bad sig. |

### Layer 2: API (FastAPI routes)

`services/api/src/api/routes/quant_foundry.py` (10,239 bytes; 12 routes)

| Route | Auth | Purpose |
|---|---|---|
| `POST /quant-foundry/jobs` | bearer (`require_user`) | Create a job. 402 on budget exceeded, 429 on kill switch. |
| `GET /quant-foundry/jobs` | bearer | List jobs, optional `?status=` filter. |
| `GET /quant-foundry/jobs/{job_id}` | bearer | Job detail. 404 if unknown. |
| `GET /quant-foundry/dossiers` | bearer | **TASK-0802.** List dossiers, optional `?status=` filter. 400 on invalid filter. |
| `GET /quant-foundry/dossiers/{model_id}` | bearer | **TASK-0802.** 404 if unknown. |
| `GET /quant-foundry/tournament/leaderboard` | bearer | **TASK-0802.** Ranked leaderboard. |
| `GET /quant-foundry/promotion/queue` | bearer | **TASK-0802.** Pending review queue. |
| `GET /quant-foundry/promotion/completed` | bearer | **TASK-0802.** Completed receipts. |
| `GET /quant-foundry/shadow/health` | bearer | **TASK-0604.** Aggregate shadow health. 503 if gateway absent. |
| `GET /quant-foundry/health` | bearer | Health state. |
| `GET /quant-foundry/heartbeats` | bearer | Worker heartbeats (empty in local_mock). |
| `POST /quant-foundry/callbacks/runpod` | **HMAC** (NOT bearer) | Receive signed callback. 401/400/404/200. |

**Disabled-state contract:** every read endpoint returns safe empty/disabled response when gateway absent or `enabled=false`. The route is registered unconditionally (so 404 doesn't hide the surface) — `_get_gateway` returns `None`, and each endpoint either:
- calls `_require_gateway` which raises 503, OR
- returns a safe default (e.g. `[]` or nulls)

### Layer 3: Dashboard (Next.js pages)

`apps/dashboard/src/app/quant-foundry/`

| Page | LOC | Shows |
|---|---|---|
| `page.tsx` (overview) | 19,791 | TASK-0801 (Builder 3). Multiple cards: job outbox summary, dossier summary, tournament summary, promotion summary, shadow health summary. Additive nav links to 4 sub-pages (TASK-0802) + 1 sub-page (TASK-0604). |
| `jobs/page.tsx` | 6,185 | TASK-0802. Table of jobs with status filter. |
| `models/page.tsx` | 4,760 | TASK-0802. List of dossiers with artifact hash, status, evidence completeness. |
| `tournament/page.tsx` | 5,208 | TASK-0802. Ranked leaderboard. |
| `promotion/page.tsx` | 8,471 | TASK-0802. Review queue (pending + completed). Read-only; confirmation dialog for approve/reject (no POST wired — server-side promotion is a future task). |
| `shadow/page.tsx` | 9,958 | TASK-0604. Aggregate shadow health: enabled, models_running, latency p50/p95, feature availability, circuit breaker state, prediction/settled counts. |

**Frontend types & API client** (`apps/dashboard/src/lib/`):
- `types.ts` — 7 QF interfaces (additive across this plan): `QuantFoundryJob`, `QuantFoundryDossier`, `QuantFoundryTournamentEntry`, `QuantFoundryPromotionQueueEntry`, `QuantFoundryPromotionReceipt`, `QuantFoundryShadowHealth`, ...
- `api.ts` — 9 client methods: `quantFoundryHealth`, `quantFoundryJobs`, `quantFoundryDossiers`, `quantFoundryDossier`, `quantFoundryTournamentLeaderboard`, `quantFoundryPromotionQueue`, `quantFoundryPromotionCompleted`, `quantFoundryShadowHealth`, ...

**Common page pattern** (every page):
```tsx
const token = useAuth((s) => s.token);
const query = useQuery({
  queryKey: ["quant-foundry", ...],
  queryFn: () => api.quantFoundryXxx(token, ...),
  enabled: !!token,
  refetchInterval: 30_000,
  staleTime: 10_000,
  retry: false,
});
const disabled = query.error instanceof UnavailableError && query.error.status === 503;

return (
  <AppShell>
    <PageHeader .../>
    <Card>
      {disabled ? <EmptyState "Quant Foundry is disabled" />
        : query.isLoading ? <EmptyState "Loading" />
        : query.error ? <EmptyState "Error" />
        : data.length === 0 ? <EmptyState "No data" />
        : <Table ... />}
    </Card>
  </AppShell>
);
```

---

## 8. Hard invariants

These are the rules enforced across the entire stack. Every builder logs them in their completion log; F4 confirmed all of them on the audit window.

| Invariant | Where enforced | Failure mode |
|---|---|---|
| `QUANT_FOUNDRY_ENABLED` defaults to `"false"` | `gateway.py:100` | A flip requires explicit operator action. |
| `QUANT_FOUNDRY_ALLOW_PAPER_BRIDGE` unset by default | `paper_bridge.py:244-249` | Paper bridge refuses with explicit reason string. |
| No `sig.predict` writes | `shadow_ledger.py` (structural: no `sig.predict` reference); F4 grep | Test: structural source scan + no forbidden attributes. |
| No `oms`/`risk` imports in quant_foundry | F4 grep returns zero matches | Structural isolation. |
| `QUANT_FOUNDRY_CALLBACK_SECRET` (HMAC) is the ONLY secret on the RunPod side | `runpod/quant-foundry-{training,inference}/README.md` + F4 grep | No broker creds, no Redis, no JWT secret. |
| `authority=SHADOW_ONLY` on every prediction | `shadow_ledger.py` rejects non-shadow; `callbacks.py` asserts | Structural: `ORDER_LIKE_FIELDS` frozenset (quantity/side/broker/order_type) rejected. |
| GPU spend fails closed | `budget.py` (BudgetGuard + kill_switch) → wired into `gateway.py` (TASK-0901) | 402/429 on budget exceeded. |
| Operator endpoints require bearer JWT | `api.auth.require_user` in every GET | 401 on missing. |
| Callback endpoint uses HMAC (NOT bearer) | `services/api/src/api/routes/quant_foundry.py:249` | 401 on missing/bad signature. |
| Promotion requires human review | `promotion.py` `PromotionGate.evaluate()` (4 fail-closed checks) | No auto-promote path. |
| `test_news.py` failures are NOT in scope | (deliberate — separate news track) | 9 known pre-existing failures, untouched. |
| File-disjoint zones (parallel builders) | Each `BUILDER*.md` log + `.gitignore` + `git show --stat` | Stage only owned files; `git add -p` for shared plan file. |
| Live mode is not flipped in this plan | F4 grep: `QUANT_FOUNDRY_ENABLED.*true` only in pre-existing commit `3ec6c06` | TASK-1101 report explicitly states NOT READY. |

**Where NOT to look for trading authority:** quant_foundry never imports `oms` or `risk`. Verified by F4 grep — zero matches. The OMS and risk services are unchanged by any builder in the Quant Foundry track.

---

## 9. Where to start / reference index

### If you are an engineer joining the codebase

1. **Read `gateway.py`** (19 KB) — the facade. Everything else is a building block.
2. **Read `docs/LIMITED_LIVE_READINESS_REVIEW.md`** (274 lines) — what's missing for live.
3. **Read `docs/AAA_GLM_SUPERTEAM_LOGS/BUILDER3.md`** (90 KB) — the largest, most complete builder log. Shows how Phases 5/6/7 were built end-to-end.
4. **Read `apps/dashboard/src/app/quant-foundry/page.tsx`** (overview, 615 lines) — see the operator surface.
5. **Skim the handoff file** `docs/AAA_GLM_SUPERTEAM_LOGS/HANDOFF_REMAINDER_GLM52.md` — shows what was "remaining" at the start of this plan.

### If you are an operator

1. Set `QUANT_FOUNDRY_ENABLED=true` to enable the gateway (default is off).
2. Set `QUANT_FOUNDRY_MODE=local_mock` for synchronous mock, or `runpod_shadow` for real RunPod shadow inference.
3. Set `QUANT_FOUNDRY_CALLBACK_SECRET=<secret>` to receive RunPod callbacks.
4. Set `QUANT_FOUNDRY_ALLOW_PAPER_BRIDGE=true` (and only if B1-B8 from the readiness review are resolved) to enable the paper bridge.
5. Check `GET /quant-foundry/health` for current state.
6. Check `GET /quant-foundry/shadow/health` for shadow inference health.
7. Check `GET /quant-foundry/promotion/queue` for pending review items.

### If you are a security auditor

Start with `docs/LIMITED_LIVE_READINESS_REVIEW.md` (8 grep evidence commands with output, 14 hard gates with MET/PARTIAL/NOT MET). Then run F4's invariant checks yourself:

```bash
# No sig.predict writes
git grep "sig.predict" services/quant_foundry/src/quant_foundry/{dossier,leaderboard_expanded,promotion,paper_bridge,gateway,causal_graph}.py

# No oms/risk imports
git grep "^from oms\|^import oms\|^from risk\|^import risk" services/quant_foundry/src/quant_foundry/

# No broker creds in RunPod
Get-ChildItem runpod -Recurse | Select-String "broker|alpaca|credential|api_key"

# Live mode default
git grep "QUANT_FOUNDRY_ENABLED" services/quant_foundry/src/quant_foundry/gateway.py
```

### Reference index — file paths

| What | Where |
|---|---|
| Plan file | `.omo/plans/quant-foundry-remaining-tasks.md` |
| This run's notepad | `.omo/notepads/quant-foundry-remaining-tasks/{learnings,issues,problems,decisions}.md` |
| Builder logs | `docs/AAA_GLM_SUPERTEAM_LOGS/BUILDER{1..6}.md` + `BUILDER1_GLM.md` |
| Handoff | `docs/AAA_GLM_SUPERTEAM_LOGS/HANDOFF_REMAINDER_GLM52.md` |
| Plan source of truth | `docs/NEXT_STEPS_PLAN.md` (4 ownership blockquotes added: TASK-1002, TASK-0802, TASK-1101, TASK-0604) |
| Readiness verdict | `docs/LIMITED_LIVE_READINESS_REVIEW.md` (commit `7027db4`) |
| Builder 6's work log | `docs/AAA_GLM_SUPERTEAM_LOGS/BUILDER6.md` (13 KB) |
| Orchestrator state | `.omo/boulder.json` |
| Quant Foundry modules | `services/quant_foundry/src/quant_foundry/*.py` (36 files) |
| Quant Foundry tests | `services/quant_foundry/tests/*.py` + `services/api/tests/test_quant_foundry_{dossiers,shadow}.py` |
| API routes | `services/api/src/api/routes/quant_foundry.py` (12 routes) |
| Dashboard pages | `apps/dashboard/src/app/quant-foundry/{page,jobs,models,tournament,promotion,shadow}/page.tsx` |
| Dashboard types | `apps/dashboard/src/lib/types.ts` (7 QF interfaces added) |
| Dashboard API client | `apps/dashboard/src/lib/api.ts` (9 client methods added) |
| RunPod training | `runpod/quant-foundry-training/{handler.py,Dockerfile,README.md}` |
| RunPod inference | `runpod/quant-foundry-inference/{handler.py,Dockerfile,README.md}` |
| RunPod services-side classes | `services/quant_foundry/src/quant_foundry/{runpod_training,shadow_inference,runpod_client}.py` |
| Gateway facade | `services/quant_foundry/src/quant_foundry/gateway.py` (19 KB — start here) |
| Auth | `services/api/src/api/auth.py` (`require_user` dependency) |
| Signatures | `services/quant_foundry/src/quant_foundry/signatures.py` (HMAC-SHA256) |

### Reference index — task → commit

| Task | Commit | Author | What |
|---|---|---|---|
| TASK-1002 | `808e7ab` | Builder 6 | `causal_graph.py` + tests (research only) |
| TASK-1002 docs | `19afc5b` | Builder 6 | BUILDER6.md + NEXT_STEPS_PLAN ownership marker |
| TASK-0802 | `8f3a589` | Builder 6 | 5 read-only routes + gateway lazy-init + 4 dashboard pages + 8 TDD tests |
| TASK-0802 docs | `eed6612` | Builder 6 | BUILDER6.md + NEXT_STEPS_PLAN ownership marker |
| TASK-1101 | `7027db4` | Builder 6 | `docs/LIMITED_LIVE_READINESS_REVIEW.md` (274 lines, NOT READY) |
| TASK-1101 docs | `25db6ee` | Builder 6 | BUILDER6.md + NEXT_STEPS_PLAN ownership marker |
| TASK-0604 | `4233e64` | Builder 6 | `GET /shadow/health` + gateway `shadow_health()` + 6 TDD tests + shadow page |
| TASK-0604 docs | `751d212` | Builder 6 | BUILDER6.md + NEXT_STEPS_PLAN ownership marker |
| TASK-0901 | `6256cdf` | Builder 3 | BudgetGuard wired into gateway, fail-closed on GPU spend |
| TASK-1004 | `22700a7` | Builder 3 | Adversarial drift sentinel |
| TASK-1003 | `e272b6e` | Builder 3 | Conformal prediction risk gate |
| TASK-1001 | `a88e8c2` | Builder 3 | Mixture-of-experts model router |
| TASK-0704 | `e95c51f` | Builder 3 | Paper-only model pointer bridge (rollback pointer, config-gated off) |
| TASK-0703 | `ffe9ce7` | Builder 3 | Retirement / edge-decay flags |
| TASK-0702 | `60f9e61` | Builder 3 | Promotion review queue (human-gated) |
| TASK-0701 | `0831e2c` | Builder 3 | Expanded tournament leaderboard |
| TASK-0603 | `0aa4aef` | Builder 3 | Store and settle shadow predictions |
| TASK-0602 | `1a91a82` | Builder 3 | Live feature snapshot export |
| TASK-0601 | `df326d4` | Builder 3 | RunPod shadow inference container MVP |
| TASK-0504 | `caeb468` | Builder 3 | First real baseline model family (LightGBM) |
| TASK-0503 | `ae893a6` | Builder 3 | Artifact import from object storage (S3) |
| TASK-0502 | `b3fc4e1` | Builder 3 | RunPod job dispatch client |
| TASK-0406 | `d864b94` | Builder 3 | Leakage / overfit sentinel |
| TASK-0405 | `7f704bd` | Builder 4 | Feature lake builder MVP (PIT proof, embargo, as-of universe) |
| TASK-0404 | `fd3f115` | Builder 3 | Tournament scoring skeleton (deflated Sharpe, bootstrap) |
| TASK-0403 | `de56c38` | Builder 3 | Dossier registry |
| TASK-0402 | (commit in BUILDER4.md) | Builder 4 | Shadow prediction ledger storage (real durable JSONL) |
| TASK-0401 | `855f01b` | Builder 1 | Prediction settlement ledger (cost model, post-decision window) |
| TASK-0306 | (commit in BUILDER2.md) | Builder 2 | Gateway facade + environment config |
| TASK-0305 | (commit in BUILDER2.md) | Builder 2 | Mock dispatcher + callback processor + ShadowLedgerStub + DossierStub |
| TASK-0304 | `48c0c27` | Builder 2 | Durable local job outbox + callback inbox |
| TASK-0303 | (TASK-0303 in BUILDER2.md) | Builder 2 | ids + signatures (HMAC) |
| TASK-0302 | (TASK-0302 in BUILDER1.md) | Builder 1 | Schemas (RunPodTrainingRequest, RunPodInferenceRequest, RunPodCallbackEnvelope, Authority, etc.) |
| TASK-0301 | (TASK-0301 in BUILDER1.md) | Builder 1 | Schemas baseline |
| TASK-0203 | (commit in BUILDER5.md) | Builder 5 | On-demand module control (modules.py, ModuleRegistry, start/stop endpoints) |
| TASK-0801 | (commit in BUILDER3.md) | Builder 3 | Quant Foundry overview dashboard page |

---

## Final status (2026-06-23)

- **8 atomic commits** in this plan on `codex/portfolio-optimizer-core` (4 feat + 4 docs).
- **36 Quant Foundry source modules** (Phases 3-10) — all contract-proven via TDD, ruff + mypy clean.
- **997 backend tests** pass (full pytest run; the 9 known pre-existing `test_news.py` failures left untouched per plan).
- **6 dashboard pages** (overview + 5 sub-pages) — tsc clean in TASK scope; 17 pre-existing tsc errors in unrelated files left untouched.
- **4 final-wave reviewers** all APPROVED (F1 plan compliance, F2 code quality, F3 manual QA, F4 scope fidelity).
- **Readiness verdict: NOT READY for limited paper-to-live pilot.** 8 specific blockers (B1-B8) enumerated in `docs/LIMITED_LIVE_READINESS_REVIEW.md`. The default `QUANT_FOUNDRY_ENABLED=false` and unset `QUANT_FOUNDRY_ALLOW_PAPER_BRIDGE` keep the system in the safe, disabled state.
- **Zero hard-invariants violations** across the audit window (no `sig.predict` writes, no flag flips, no broker creds in RunPod, no `oms`/`risk` imports, no `test_news.py` modifications, no out-of-scope files staged).

The Quant Foundry is the most thoroughly-built section of the Fincept codebase. The work is complete. Next move is the operator's call (push, PR, new plan).

---

*This report was assembled from `.omo/boulder.json`, the 6 builder logs in `docs/AAA_GLM_SUPERTEAM_LOGS/`, the notepad at `.omo/notepads/quant-foundry-remaining-tasks/`, the readiness review at `docs/LIMITED_LIVE_READINESS_REVIEW.md`, and direct inspection of the 36 source modules in `services/quant_foundry/src/quant_foundry/`, the 2 RunPod containers under `runpod/`, the 12 API routes in `services/api/src/api/routes/quant_foundry.py`, and the 6 dashboard pages under `apps/dashboard/src/app/quant-foundry/`.*
