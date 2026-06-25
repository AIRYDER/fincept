# Fincept Terminal — Consolidated Documentation Report

**Generated:** 2026-06-25
**Scope:** Synthesis of 10 docs across the Fincept Terminal repo
**Branch:** `codex/portfolio-optimizer-core`
**Sources reviewed:**
`AWS_PRODUCTION_CONTROL_PLANE.md`, `ON_DEMAND_MODULES.md`, `ROADMAP.md`,
`AGENT_SYSTEM_REPORT.md`, `NEXT_STEPS_PLAN.md`, `LIMITED_LIVE_READINESS_REVIEW.md`,
`dashboard-route-atlas.md`, `ENVIRONMENT.md`, `RAILWAY_STAGING_GUIDE.md`,
`MODULE_RUNTIME_PLAN.md`

---

## 1. Executive Summary

Fincept Terminal is a **one-operator quant research + paper-trading platform**
with a closed-loop "Quant Foundry" subsystem for candidate trading models.
The stack is **contract-proven and TDD-tested end-to-end at the Python module
+ API surface level**, but the GPU half has **never fired against a real
RunPod GPU**, **no model has been promoted**, and **live trading is disabled
by default**.

The single most important conclusion across all 10 docs is the
**NOT READY verdict** from `LIMITED_LIVE_READINESS_REVIEW.md` (TASK-1101,
commit `7027db4`): a limited paper-to-live pilot is blocked by **8 specific
blockers (B1–B8)**, all of which trace back to the fact that the GPU +
promotion + deployment halves of the loop have only ever run in mock/local
mode.

The system is well-architected for its scale: hard trust boundaries
(RunPod never sees broker creds, OMS/risk never imported by Quant Foundry),
fail-closed cost governance (BudgetGuard), three independent config gates
defaulting live mode to off, and a human-gated promotion queue. The gap is
operational, not architectural.

---

## 2. What Fincept Actually Is

| Dimension | State |
|---|---|
| **Identity** | Python-first research + paper-trading terminal for 1–5 internal quants (ROADMAP "Bet A" MVP track) |
| **Live trading** | Disabled by default. Three config gates: `QUANT_FOUNDRY_ENABLED=false`, `QUANT_FOUNDRY_MODE=local_mock`, `QUANT_FOUNDRY_ALLOW_PAPER_BRIDGE` unset |
| **GPU** | RunPod only (never AWS GPU). AWS is 3–5x more expensive for spot training |
| **Broker creds** | Not present anywhere in the tree. RunPod handlers see only the HMAC callback secret |
| **Deployment** | Local dev (current, $0) → Railway staging (~$20–30/mo, design complete) → AWS production (~$200–310/mo, **design only, not deployed**) |
| **Test signal** | 991 quant_foundry tests passing after TASK-0802; full TDD discipline across all 36 Quant Foundry modules |

---

## 3. Architecture at a Glance

```
FINCEPT (trusted, non-RunPod)
  Operator ──JWT──▶ FastAPI ──▶ QuantFoundryGateway
  Dashboard ──JWT──▶ /quant-foundry/{jobs,models,tournament,promotion,shadow}
  Gateway owns: outbox, inbox, mock dispatcher, callback processor,
                dossier registry, leaderboard, promotion queue,
                shadow ledger, budget guard
                │
                │ HMAC-signed callbacks (QUANT_FOUNDRY_CALLBACK_SECRET)
                ▼
RUNPOD (untrusted, GPU only)
  quant-foundry-training/handler.py  → RunPodTrainingHandler
  quant-foundry-inference/handler.py → ShadowInferenceEngine
  Invariant: NO broker creds, NO Redis, NO sig.predict, NO order fields
```

**Trust boundary invariant (non-negotiable):** OMS and risk services run
inside the trusted Fincept deployment and are **NEVER** deployed to RunPod
or any external compute. Verified by grep: zero `from oms`/`from risk`
imports in `quant_foundry`, zero `quant_foundry` imports in `oms`/`risk`.

---

## 4. Quant Foundry — Module Inventory (36 files)

All modules live in `services/quant_foundry/src/quant_foundry/`. Grouped by
function:

| Group | Modules | Phase |
|---|---|---|
| **Evidence loop** | settlement, outcomes, metrics, shadow_ledger, shadow_settlement, shadow_inference, dossier, registry, artifacts | 3–4 |
| **Scoring** | tournament, leaderboard, leaderboard_expanded, significance, pbo, sentinel | 4, 7 |
| **Routing** | moe_router, conformal_gate, drift_sentinel, promotion | 7, 10 |
| **Bridge** | paper_bridge (config-gated off), causal_graph (research only) | 7, 10 |
| **Infra** | budget, callbacks, dataset_manifest, feature_lake, feature_availability, feature_snapshot_export, ids, signatures, schemas, outbox, inbox, mock_dispatcher, runpod_client, runpod_training, gateway | 3, 4, 5, 9 |

**Critical files to read first:** `gateway.py` (19,450 bytes — the facade
the API talks to), `sentinel.py` (30,568 bytes — largest module, the
promotion gate), `paper_bridge.py` (config-gated off, has rollback pointer).

---

## 5. Phase / Task Map

The plan's master ordering rule: **"build the scoreboard before adding more
players."** Every phase before Phase 5 (RunPod) exists to make Phase 5 safe.

| Phase | Theme | Status | Key tasks |
|---|---|---|---|
| 0 | Safety guards, path boundaries, baseline receipt | ✅ DONE | — |
| 1 | Verification receipt, matrix tests, env docs | ~~ (TASK-0104 CI hardening optional/pending) | — |
| 2 | Dashboard readiness, module control | ✅ Complete (TASK-0201..0205) | route atlas, readiness center, on-demand modules, fetch timeouts, provider redaction |
| 3 | Quant Foundry contracts + mock connectivity | ✅ Complete | TASK-0301..0306 (ids, signatures, schemas, outbox, inbox, mock dispatcher, gateway) |
| 4 | Evidence loop foundations | ✅ Complete | TASK-0401..0406 (settlement, shadow ledger, dossier, tournament, feature lake, leakage sentinel) |
| 5 | RunPod training MVP | ✅ Code complete, **never run on real GPU** | TASK-0501..0504 |
| 6 | Shadow inference swarm | ✅ Code complete, **stub-only, never run on real GPU** | TASK-0601..0604 |
| 7 | Tournament + promotion | ✅ Code complete, **no real submissions** | TASK-0701..0704 |
| 8 | Operator experience (dashboard pages) | ✅ Complete | TASK-0801, TASK-0802 |
| 9 | Deployment + cost-optimized runtime | ⚠️ Partial: BudgetGuard wired (TASK-0901 ✅), Railway staging guide (TASK-0902 ✅), **AWS production design only (TASK-0903, not deployed)** | TASK-0901..0903 |
| 10 | Frontier performance modules | ✅ Complete | TASK-1001..1004 (MoE router, causal graph, conformal gate, drift sentinel). TASK-1005 (Alpha Genome Lab) not started |
| 11 | Limited live readiness review | ✅ Complete → **verdict: NOT READY** | TASK-1101 |

**Agent roster:** 6 GLM-5.2 builder agents (Builders 1–6) executed the
Quant Foundry work between 2026-06-22 and 2026-06-23. Builder 3 was the
largest contributor (90 KB work log, drove Phases 5/6/7/10). Builder 6
completed the final wave (causal graph, dashboard pages, readiness review,
shadow health). All builder logs in `docs/AAA_GLM_SUPERTEAM_LOGS/`.

---

## 6. The 8 Blockers (B1–B8) — Why Live Mode Is NOT READY

From `LIMITED_LIVE_READINESS_REVIEW.md`. Every PARTIAL or NOT MET gate is a
blocker:

| # | Blocker | Gate blocked | Root cause |
|---|---|---|---|
| **B1** | No promoted model family | #8, #13 (operational) | `PromotionGate.evaluate()` has never processed a real submission |
| **B2** | Shadow inference is stub-only | #9 | `ShadowLedgerStub` + `DossierStub` replace real storage; no real RunPod shadow GPU has produced a settled prediction |
| **B3** | Paper bridge never enabled with a real model | #10 | `QUANT_FOUNDRY_ALLOW_PAPER_BRIDGE` unset; bridge refuses every publish |
| **B4** | No production deployment environment | #14 | TASK-0903 is design-only. No ECS Fargate, no Secrets Manager, no CloudWatch, no Railway staging deployed |
| **B5** | No broker credentials configured | (pilot prereq) | No paper-broker or live-broker account, no broker API key in tree |
| **B6** | Real RunPod GPU has never run | #5, #6, #7, #9 | Phase 5/6 shipped container MVPs + dispatch client, all tested with mock GPU only |
| **B7** | Leakage/overfit sentinel un-runnable | #8 | `sentinel.py` runs only on registered dossiers; no real dossier exists |
| **B8** | Settled history is empty | #5, #7, #9 | `shadow_settlement.py` + `settlement.py` are correct but have no inputs |

**Pattern:** B6 (no real GPU run) cascades into B2, B7, B8, which cascade
into B1, B3. B4 and B5 are independent deployment/credential gaps. The
critical path to unblock is: **real RunPod GPU run → real shadow history →
real dossier → sentinel pass → human-gated promotion → paper bridge
enablement → deployed environment with broker creds.**

---

## 7. Hard Gate Checklist (14 gates from NEXT_STEPS_PLAN.md:2196–2214)

| # | Gate | Verdict |
|---|---|---|
| 1 | Runtime safety guards enforced | ✅ MET |
| 2 | Backtest path handling locked down | ✅ MET |
| 3 | Verification receipts exist | ✅ MET (6 receipts in `reports/verification/`) |
| 4 | Quant Foundry contract-tested | ✅ MET (991 tests passing) |
| 5 | Settlement ledger reliable | ⚠️ PARTIAL (built + tested, no real inputs) |
| 6 | Dossier registry reliable | ⚠️ PARTIAL (built + tested, `DossierStub` in use) |
| 7 | Tournament scoring reliable | ⚠️ PARTIAL (fixture-backed only) |
| 8 | Leakage/overfit sentinel green on promoted family | ❌ NOT MET (no promoted family) |
| 9 | Shadow inference has enough settled history | ❌ NOT MET (stub-only) |
| 10 | Paper bridge has run safely | ❌ NOT MET (never enabled) |
| 11 | Rollback pointer exists | ✅ MET (`paper_bridge.py:105`, created at line 299) |
| 12 | OMS and risk unchanged and authoritative | ✅ MET (structurally isolated, grep-verified) |
| 13 | Human approval workflow working | ✅ MET (code) / ❌ NOT MET (operational — no model ever submitted) |
| 14 | Deployment env has secure secrets + monitoring | ❌ NOT MET (design only) |
| 14b | Live provider/broker creds never available to RunPod | ✅ MET (only HMAC callback secret in `runpod/`) |

**Summary: 7 MET, 3 PARTIAL, 5 NOT MET.** Gates #1–4 (the safety
foundation) are solid. Gates #5 onward are blocked by the B1–B8 cascade.

---

## 8. Safety Invariants (Verified, Non-Negotiable)

These are proven by grep/code inspection in the readiness review and hold
regardless of the operational blockers:

1. **Live mode defaults to off.** Three independent config gates:
   `QUANT_FOUNDRY_ENABLED=false`, `QUANT_FOUNDRY_MODE=local_mock`,
   `QUANT_FOUNDRY_ALLOW_PAPER_BRIDGE` unset. Disabling is a config flip,
   not a code change.
2. **Rollback pointer.** `paper_bridge.py:297–316` creates a
   `RollbackPointer` recording the prior model pointer before publishing.
3. **BudgetGuard fail-closed.** `budget.py:107` blocks any non-zero spend
   when the kill switch is set; monthly ceiling enforced before any GPU job
   is dispatched. Wired into `QuantFoundryGateway.from_env()`.
4. **OMS/risk authority preserved.** Zero coupling between `quant_foundry`
   and `oms`/`risk` in either direction (grep-verified).
5. **RunPod never sees broker creds.** Only `QUANT_FOUNDRY_CALLBACK_SECRET`
   (HMAC) is consumed by RunPod handlers. Verified across `runpod/`.
6. **Human approval required.** `PromotionGate.evaluate()` enforces 4
   fail-closed checks: dossier present, tournament evidence sufficient,
   settlement evidence sufficient, sentinel receipt passes. No
   auto-promote path.
7. **Schema-level order-field rejection.** Shadow predictions cannot
   contain `quantity`, `order side`, `broker account`, `order type`,
   `time in force`, or `notional size` (enforced by `extra="forbid"` +
   explicit tests).
8. **Artifact import URI allowlist.** Only `file://` and `s3://` accepted;
   `http(s)://`, `ftp://` rejected. Path traversal rejected. SHA-256 hash
   verification required. AWS credentials never stored in the artifact
   module (delegated to caller-injected `s3_reader`).

---

## 9. Deployment Strategy (Three Tiers)

| Tier | Platform | Cost | Status | Purpose |
|---|---|---|---|---|
| **Local dev** | Local Docker | $0 | ✅ Current | All development, mock GPU |
| **Staging** | Railway | ~$20–30/mo | ⚠️ Guide complete (`RAILWAY_STAGING_GUIDE.md`), **not confirmed deployed** | Route smoke tests, operator demos, mock QF loop |
| **Production** | AWS (ECS Fargate + RDS + ElastiCache + S3 + ALB/WAF) | ~$200–310/mo always-on + RunPod GPU on-demand | ⚠️ **Design only** (`AWS_PRODUCTION_CONTROL_PLANE.md`), not deployed | Control plane; RunPod stays for GPUs |

**Key deployment invariants:**
- Railway is **test/staging ONLY** — never GPU, never broker-adjacent OMS,
  never serious artifact storage (1GB Postgres cap).
- AWS production keeps OMS/risk in private subnets; broker creds in Secrets
  Manager, accessible only to the OMS task execution role.
- RunPod workers write artifacts to S3 via pre-signed URLs (time-limited,
  scoped to a single object key). No direct DB/Redis access.
- Migration path: Local → Railway staging (Phase A) → AWS production
  (Phase B) → AWS + RunPod (Phase C, shadow-only first).

**AWS component selection rationale:**
- ECS Fargate over EC2/EKS (serverless containers, right-sized for one
  operator) and over Lambda (15-min timeout disqualifies long-running
  orchestrator/OMS/risk).
- RDS Postgres + TimescaleDB extension over self-managed (one-operator shop
  can't run 24/7 DB on-call). Open question: TimescaleDB compression on RDS
  vs Aurora (Aurora doesn't support TimescaleDB natively).
- Valkey over Redis on ElastiCache (OSS fork, avoids licensing concerns).
- Single NAT Gateway for cost; single-region (us-east-1), multi-AZ.

---

## 10. Cost Governance

| Category | Local | AWS Production |
|---|---|---|
| Always-on shell (dashboard, API, Redis, PG, orchestrator, OMS, risk, ingestion) | $0 | ~$125–155/mo |
| On-demand modules (8 modules, 2h/day avg) | $0 | ~$57–115/mo |
| GPU (RunPod, on-demand) | $0 | $0.50–5.00/job |
| LLM API (sentiment agents) | $0 (no key) | $20–40/mo |
| **Total (no GPU)** | **$0** | **~$200–310/mo** |
| **Total (with GPU)** | **$0** | **~$250–400/mo** |

**BudgetGuard mechanics:**
- `QUANT_FOUNDRY_MONTHLY_BUDGET_CENTS` (default 0 = no paid jobs until
  explicitly set).
- `QUANT_FOUNDRY_BUDGET_KILL_SWITCH=true` blocks ALL paid jobs (emergency
  stop).
- Durable JSONL spend tracking per calendar month (`spend_<YYYY-MM>.jsonl`);
  survives restart.
- Zero-cost jobs always allowed (local dev never blocked).
- 20 tests in `test_budget.py` covering guard behavior, kill switch,
  durability, edge cases.

---

## 11. On-Demand Module Control (TASK-0203)

8 optional modules, each with idle timeout, cost class, and one-instance
enforcement. Start/stop via allowlisted PowerShell scripts (no arbitrary
shell execution from user input).

| Module | Cost | Idle timeout | Notes |
|---|---|---|---|
| `openbb` | medium | 30 min | Research terminal |
| `market_data` | medium | 60 min | ingestor + features |
| `news_learning` | medium | 45 min | information_enricher + news_outcome_labeler |
| `jobs` | low | 20 min | Background jobs worker |
| `gbm_predictor` | low | 30 min | — |
| `news_alpha_predictor` | low | 30 min | — |
| `sentiment` | high | 15 min | LLM API cost |
| `regime` | low | 60 min | — |

**Security invariants:** allowlisted module IDs only, auth required,
local-only launches, secrets redacted via `_redact_output`, duplicate
starts return `already_running` without spawning. Every action records a
receipt in Redis list `module:receipts` (500 entries, 7-day TTL).

---

## 12. Dashboard Route Atlas (TASK-0201)

25 routes mapped. **18 live, 2 hybrid, 1 mock, 2 demo, 2 redirect.**

**High-priority conversion targets (mock/hybrid → live):**
1. **`/watchlist`** — entirely mock (`mockPriceWalk`), HIGH risk if mistaken
   for live. Replace with `/markets/bars` or `/markets/quotes` API + WebSocket.
2. **`/symbol/[symbol]`** — hybrid, 3 MockBadge instances (metadata + chart
   fixtures are mock; positions/predictions are live). Replace mock
   metadata with `/symbols/{symbol}/metadata` API.
3. **`/portfolio-builder`** — hybrid, market data source unclear
   (`marketDataService.ts` can be live or mock). Ensure live API in
   production; add MockBadge when using mock.

**Convention:** Every consumer of `mock-data.ts` must display a
`<MockBadge>`. Live panels must be backed by a real API endpoint.

---

## 13. Cross-Cutting Quant Rigor Requirements

Four disciplines enforced across every phase (from NEXT_STEPS_PLAN.md):

1. **Point-in-time correctness (no look-ahead, no survivorship).** As-of
   (backward) joins only; label horizons start strictly after feature
   cutoff; reconstruct tradable universe as-of each date; fit transforms
   on training fold only. Enforced by TASK-0405 + TASK-0406.
2. **Multiple-testing and overfit control.** Purged k-fold + embargo
   (López de Prado); Deflated Sharpe Ratio; PBO via CSCV; stationary/block
   bootstrap (never IID t-test). Enforced by TASK-0404 + TASK-0406.
3. **Reproducibility and determinism.** Every artifact pins: dataset
   snapshot hash, feature/label schema hash, code git SHA, lockfile hash,
   container image digest, random seed(s), hardware class. Enforced by
   TASK-0403 + TASK-0501.
4. **Cost governance with a hard ceiling.** Global monthly budget + kill
   switch above per-sweep/per-job budgets; spot capacity with
   checkpoint/resume; track cost-per-validated-edge. Enforced by
   TASK-0502 + TASK-0901.

---

## 14. ROADMAP Reality Check

The ROADMAP doc is candid about the gap between the original BLUEPRINT.md
and reality:

- The blueprint describes a Bloomberg + HFT + quant platform — a
  **5–10 year, 50–100 engineer, $50M–$200M effort**. It claimed 14 months
  with 12–15 engineers.
- **Recommendation: Bet A (MVP Track)** — Python research + paper-trading
  for 1–5 internal quants, 4–6 engineers, 6–9 months to internal beta.
  Drops FPGA, kernel bypass, Qt6, sub-100μs, FIX certification, regulatory
  reporting.
- Sub-100μs tick-to-trade is achievable only with co-lo + FPGA + 3+ years;
  drop to sub-10ms for MVP.
- "Bloomberg replacement at zero recurring cost" is false — data licensing
  alone costs $500k–$5M/year for comparable coverage.
- Multi-agent AI with hierarchical coordination is cutting-edge research,
  brittle in production. Start with 1 agent, add second only after first
  proves Sharpe improvement in shadow.
- Budget envelope: ~$1.6M–$2M Year 1 (7 engineers + cloud + data + tooling).
  If <$500k: execute Bet C (research platform only, 3-person, 12 months).

**Decision gates are strict:** no phase proceeds without meeting its gate
(e.g., Gate 4→5 requires shadow model beats baseline at p<0.05 over ≥4
weeks + risk committee approval).

---

## 15. Recommended Next Actions (Critical Path to Unblocking Live Mode)

Per the readiness review's "Required Operator Decision" section, in order:

1. **Phase 5 — Real RunPod training.** Stand up a real RunPod training
   container, dispatch a real training job from `runpod_client.py` against
   a real GPU, import the resulting artifact via TASK-0503 path. (Unblocks
   B6, B7, B8.)
2. **Phase 6 — Real RunPod shadow inference.** Stand up a real RunPod
   inference container, dispatch a real shadow inference run, observe
   settled predictions landing in `ShadowLedger` (real, not stub).
   (Unblocks B2, B8.)
3. **Phase 7 — Build settled shadow history + sentinel + promotion.**
   Populate the tournament leaderboard, run the leakage/overfit sentinel,
   submit a model to `PromotionReviewQueue.submit()` with a real dossier +
   tournament result + sentinel receipt. A human must call `approve()`.
   (Unblocks B1, B7.)
4. **Phase 7 (cont.) — Enable paper bridge.** Set
   `QUANT_FOUNDRY_ALLOW_PAPER_BRIDGE=true` against the promoted model.
   (Unblocks B3.)
5. **Deployment (TASK-0902/0903).** Deploy the AWS production control plane
   per the existing design (`4cce0c9`), wire Secrets Manager for the
   callback secret, stand up CloudWatch alarms on BudgetGuard. Configure a
   paper-broker sandbox account. (Unblocks B4, B5.)

**Optional but recommended before scaling training:**
- TASK-0104 (CI hardening) — pin GitHub Actions to commit SHAs, add receipt
  runner + matrix tests + gitleaks as required CI checks.
- TASK-1005 (Alpha Genome Lab) — automate feature/model recipe generation
  with trial budgets and early-kill of underperforming sweeps.

---

## 16. Key File References

| Purpose | Path |
|---|---|
| Quant Foundry facade (read first) | `services/quant_foundry/src/quant_foundry/gateway.py` |
| Promotion gate (4 fail-closed checks) | `services/quant_foundry/src/quant_foundry/promotion.py` |
| Paper bridge (config-gated off + rollback pointer) | `services/quant_foundry/src/quant_foundry/paper_bridge.py` |
| Budget guard (fail-closed GPU spend) | `services/quant_foundry/src/quant_foundry/budget.py` |
| Leakage/overfit sentinel (largest module) | `services/quant_foundry/src/quant_foundry/sentinel.py` |
| Cross-boundary schemas (`extra="forbid"`) | `services/quant_foundry/src/quant_foundry/schemas.py` |
| HMAC callback signatures | `services/quant_foundry/src/quant_foundry/signatures.py` |
| RunPod training handler | `runpod/quant-foundry-training/handler.py` |
| RunPod inference handler | `runpod/quant-foundry-inference/handler.py` |
| Module control API | `services/api/src/api/routes/modules.py` |
| Runtime safety guard | `libs/fincept-core/src/fincept_core/config.py` |
| Redis stream names | `libs/fincept-bus/src/fincept_bus/streams.py` |
| All builder work logs | `docs/AAA_GLM_SUPERTEAM_LOGS/BUILDER{1..6}.md` |
| Readiness verdict (NOT READY + B1–B8) | `docs/LIMITED_LIVE_READINESS_REVIEW.md` |
| Master task plan (2291 lines) | `docs/NEXT_STEPS_PLAN.md` |

---

## 17. Open Questions (from AWS design doc)

1. **TimescaleDB on RDS vs. Aurora:** RDS supports the extension but with
   compression limitations in some versions; Aurora doesn't support
   TimescaleDB natively. Recommendation: start with RDS + TimescaleDB,
   evaluate compression after 6 months of production data.
2. **Valkey vs. Redis on ElastiCache:** Recommendation: Valkey (OSS fork,
   avoids licensing concerns, API-compatible).
3. **ECS Service Connect vs. internal ALB:** Recommendation: internal ALB
   for OMS/risk boundary (path-based routing); Service Connect for simpler
   service-to-service calls.

---

## 18. Bottom Line

The Fincept Terminal is **architecturally sound and safety-engineered** but
**operationally unproven past the mock/local boundary**. The codebase has
a complete, TDD-tested Quant Foundry loop (contracts → settlement →
dossier → tournament → sentinel → promotion → paper bridge) that has
never executed against a real GPU, never promoted a model, and never been
deployed to a production environment.

**To move toward live mode, the operator must execute the 5-step critical
path in §15 in strict order.** No amount of additional contract work or
dashboard polish substitutes for actually running a real RunPod GPU job
and accumulating real settled shadow history. The system is ready to do
this safely — the invariants, guards, and gates are in place — but it has
not yet done it.
