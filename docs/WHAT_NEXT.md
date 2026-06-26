# Fincept Terminal — What Next

**Generated:** 2026-06-26
**Branch:** `codex/portfolio-optimizer-core`
**Last commit:** `802a46e docs(evidence): add comprehensive session context document`
**Test suite:** 2054 tests, 2052 passed, 2 skipped (onnxruntime), 0 failed

---

## 1. Where the System Stands Today

### 1.1 What is built and working

The Fincept Terminal is a **complete paper-trading + quant research platform** with a
fully wired ML evidence spine. The system has 13 services, 5 shared libraries, a
Next.js dashboard with 20+ pages, 92 API routes, and a 2054-test suite that passes
clean.

**The core trading spine is production-wired:**
- Market data ingestor → Redis Streams → feature pipeline → agents → orchestrator
  → OMS (paper or Alpaca) → portfolio → API → dashboard
- All services communicate via Redis Streams (`fincept-bus`)
- TimescaleDB stores bars/trades/features; Redis holds hot state

**The Quant Foundry ML layer is contract-complete (Phases 3–10):**
- Job outbox/inbox with HMAC-signed callbacks
- Settlement ledger, shadow ledger, dossier registry, tournament scoring
- Leakage/overfit sentinel, conformal risk gate, drift sentinel, MoE router
- Causal market memory graph
- RunPod training + inference containers (with real LightGBM/ONNX engines)
- Promotion gate with human approval workflow
- Paper bridge with rollback pointer (config-gated OFF)
- Budget guard (fail-closed monthly ceiling)
- Shadow inference dispatch loop (scheduled, wired to API lifespan)
- 6 dashboard pages (overview, jobs, models, tournament, promotion, shadow health)

**The ML Dataset Evidence Spine (just completed) connects three silos:**
1. What the agent said → prediction log
2. What actually happened → settlement side-store (`SettlementStore`)
3. What the agent saw → feature snapshot store (`FeatureSnapshotStore`)

These three form an **evidence receipt** that answers: "For prediction X, what
features did the model see, what did it predict, and what was the realized outcome?"

**The evidence spine is wired into production:**
- `FeatureSnapshotStore` writes from the gbm_predictor publish loop
- `settlements.worker.tick` runs as a periodic poller in the API lifespan
- `/models/{name}/outcomes` reads from the new settlement store
- Approved-roots gate enforces path safety on `/models/train` and `/backtest/run`
- Shared walk-forward CV utility (`fincept_core.datasets.cv`) used by 3 call sites

### 1.2 What the readiness review says

The Limited Live Readiness Review (`docs/LIMITED_LIVE_READINESS_REVIEW.md`)
concludes: **NOT READY for limited paper-to-live pilot.**

All **code gaps are closed**. The remaining work is **operational only**:

| Blocker | Type | Status |
|---------|------|--------|
| B1 — No promoted model family | Operational | Partially resolved (endpoints exist, no real promotion processed) |
| B2 — Shadow inference stub-only | Code | **RESOLVED** (RealInferenceEngine loads ONNX/LightGBM) |
| B3 — Paper bridge never enabled | Operational | Partially resolved (27 tests pass, never run in production) |
| B4 — No production deployment | Operational | **OPEN** (AWS design only, nothing deployed) |
| B5 — No broker credentials | Operational | **OPEN** (Phase 12) |
| B6 — Real RunPod GPU never run | Operational | Code resolved (real trainer + inference engine), ops pending (rebuild containers) |
| B7 — Sentinel un-runnable | Operational | OPEN (no promoted model family to run against) |
| B8 — Settled history is empty | Operational | Partially resolved (sweep worker exists, only test data settled) |

### 1.3 The dirty worktree

There are **57 untracked files/directories** and **2 modified files**. The modified
files are the evidence documents from the last session (`.omo/evidence/`). The
untracked files fall into categories:

| Category | Items | Action needed |
|----------|-------|---------------|
| Stray scratch files in root | `2026-06-22-*.txt`, `DESIGN.md`, `MIGRATIONS_CONFIG_REVIEW.md`, `Sisyphus_*.md`, `security_best_practices_report.md`, `value_increase.md` | Move to `docs/` or delete |
| Untracked docs | `docs/AWS_DEPLOY_RUNBOOK.md`, `docs/PHASE_0_1_HANDOFF.md`, `docs/RELEASE_HYGIENE.md`, `docs/SYSTEM_IMPROVEMENT_REPORT.md`, `docs/audits/`, `docs/codebase-audit-*.md`, `docs/ui-audit-*.md`, `docs/text-readability-audit-*.md` | Commit or archive |
| Untracked code | `libs/fincept-core/src/fincept_core/http.py` + `tests/test_http.py`, `scripts/stage_baseline_training.py` | Review and commit |
| Untracked experiments | `experiments/news-impact-model/` (full sub-project) | Decide: commit, .gitignore, or extract |
| Verification receipts | `reports/verification/` (12 files) | Commit (these are referenced by the readiness review) |
| Generated data | `data/settlements/`, `research/`, `mcps/` | Add to `.gitignore` |
| Builder logs | `docs/AAA_GLM_SUPERTEAM_LOGS/BUILDER1_GLM.md`, `AGENT_TO_AGENT_MESSAGING/` | Commit |

---

## 2. The Three Tracks of Remaining Work

The system has reached a natural decision point. There are three distinct tracks
of work, each with different risk profiles and prerequisites:

### Track A — Operational Proof (HIGH RISK, HIGH VALUE)
**Goal:** Move from "code-complete" to "operationally proven" — close the 8
blockers in the readiness review.

### Track B — System Hardening (LOW RISK, MEDIUM VALUE)
**Goal:** Clean up the worktree, close test coverage gaps, consolidate divergent
systems, and pay down technical debt.

### Track C — Feature Expansion (MEDIUM RISK, VARIABLE VALUE)
**Goal:** Build the remaining planned features (TASK-1005 Alpha Genome Lab, news
impact model integration, portfolio optimizer) and extend the evidence spine.

---

## 3. Track A — Operational Proof (Recommended Priority)

This is the highest-value work because it transforms the system from a
code-complete stack into an operationally proven trading research platform. Every
item here closes a blocker from the readiness review.

### A1. Rebuild RunPod containers with real ML deps → re-run training + inference
**Closes:** B6 (partial), B2 (operational), B8 (partial)
**Prerequisites:** RunPod API key, endpoint IDs
**What to do:**
1. Rebuild `runpod/quant-foundry-training/` Docker image with `lightgbm>=4.0` + `pyarrow>=14.0`
2. Rebuild `runpod/quant-foundry-inference/` Docker image with `onnxruntime>=1.17` + `lightgbm>=4.3`
3. Dispatch a real training job → produce a real dossier
4. Dispatch real inference jobs → produce real shadow predictions
5. Let the settlement sweep worker settle them
**Risk:** GPU spend (mitigated by BudgetGuard fail-closed)
**Estimated effort:** 1-2 days of wall time (container builds + job runs)

### A2. Run shadow inference for 30+ days against real market data
**Closes:** B8 (full), B7 (partial — gives sentinel something to evaluate)
**Prerequisites:** A1 complete, market data ingestor running
**What to do:**
1. Configure `QUANT_FOUNDRY_ENABLED=true`, `QUANT_FOUNDRY_MODE=runpod_shadow`
2. Set `QUANT_FOUNDRY_SHADOW_DISPATCH_INTERVAL_SECONDS=300` (5 min)
3. Let the scheduled dispatch loop produce predictions continuously
4. Let the settlement sweep worker settle them as horizons expire
5. Monitor via `/quant-foundry/shadow` dashboard page
**Risk:** Low (shadow-only, no trading authority)
**Estimated effort:** 30 days of wall time (passive)

### A3. Process the first real promotion through the gate
**Closes:** B1 (full), B7 (full — sentinel runs on promoted family)
**Prerequisites:** A2 complete (30+ days of settled shadow history)
**What to do:**
1. Review tournament leaderboard for models with sufficient settled count
2. Run leakage/overfit sentinel on the candidate model family
3. Submit promotion request via `POST /quant-foundry/promotion/submit`
4. Review evidence packet (dossier + tournament score + sentinel receipt)
5. Approve via `POST /quant-foundry/promotion/approve` with human review note
**Risk:** Medium (promotion is irreversible but rollback pointer exists)
**Estimated effort:** 1 day (review + decision)

### A4. Enable paper bridge against the promoted model
**Closes:** B3 (full)
**Prerequisites:** A3 complete
**What to do:**
1. Set `QUANT_FOUNDRY_ALLOW_PAPER_BRIDGE=true`
2. The paper bridge will publish paper-only predictions to the bus
3. OMS simulates fills (paper mode)
4. Monitor P&L vs shadow predictions for 30+ days
5. Rollback pointer is created automatically — can disable with config flip
**Risk:** Medium (first time quant_foundry influences paper trading)
**Estimated effort:** 30 days of wall time (passive)

### A5. Deploy production control plane
**Closes:** B4 (full)
**Prerequisites:** AWS account, Terraform state
**What to do:**
1. Apply the Terraform config from TASK-0903 (ECS Fargate + ALB + WAF)
2. Configure Secrets Manager for JWT secret, RunPod API key, callback secret
3. Set up CloudWatch alarms for budget, error rate, settlement lag
4. Deploy API + dashboard to ECS
5. Configure Railway for staging (TASK-0902)
**Risk:** High (first production deployment)
**Estimated effort:** 3-5 days

### A6. Configure broker sandbox
**Closes:** B5 (full)
**Prerequisites:** A5 complete
**What to do:**
1. Create Alpaca paper trading account (sandbox)
2. Configure `FINCEPT_ALPACA_API_KEY` + `FINCEPT_ALPACA_API_SECRET` in Secrets Manager
3. Set `FINCEPT_OMS_ROUTER=alpaca` for the paper bridge strategy
4. Verify fills flow back through portfolio service
**Risk:** High (first live broker connection, even in paper mode)
**Estimated effort:** 1-2 days

### A7. Re-review limited live readiness
**Closes:** All remaining blockers
**Prerequisites:** A1–A6 complete
**What to do:**
1. Re-run the readiness review checklist
2. All 14 gates should now be MET
3. Make the go/no-go decision for limited live pilot
**Risk:** VERY HIGH (this is the gate to real money)
**Estimated effort:** 1 day (synthesis)

---

## 4. Track B — System Hardening (Can run in parallel with Track A)

These items are low-risk, high-leverage improvements that can be done while
Track A's long-running operational proofs execute.

### B1. Clean up the dirty worktree (IMMEDIATE)
**What to do:**
1. Commit the 2 modified `.omo/evidence/` files
2. Move stray root `.txt` and `.md` files into `docs/` or delete
3. Commit untracked docs (`AWS_DEPLOY_RUNBOOK.md`, `PHASE_0_1_HANDOFF.md`, etc.)
4. Review and commit `libs/fincept-core/src/fincept_core/http.py` + tests (centralized HTTP retry helper — looks production-ready)
5. Review and commit `scripts/stage_baseline_training.py`
6. Commit `reports/verification/` receipts (referenced by readiness review)
7. Add `data/settlements/`, `research/`, `mcps/` to `.gitignore`
8. Decide on `experiments/news-impact-model/` — commit as a sub-project or extract
**Estimated effort:** 2-4 hours

### B2. Consolidate the two settlement systems
**What:** The new `SettlementStore` (agent_id, v1.default, 5/3/0 bps) and the old
`quant_foundry.SettlementLedger` (model_id, cm-v1, 10/5/3 bps) coexist. In
production, `/outcomes` reads from the new store while the gateway sweep writes to
the old one.
**What to do:**
1. Reconcile cost models (pick one: v1.default or cm-v1)
2. Reconcile keying (agent_id vs model_id — or support both)
3. Migrate the quant_foundry settlement sweep to write to the new store
4. Migrate the quant_foundry dashboard to read from the new store
5. Deprecate the old `SettlementLedger`
**Estimated effort:** 1-2 days

### B3. Consume the dossier/calibration parity helpers
**What:** `fincept_core.datasets.dossier.build_dossier()` and
`build_calibration_sidecar()` are defined and tested but not consumed by any
service. They are meant to displace the `quant_foundry.dossier` internal
implementations.
**What to do:**
1. Wire `quant_foundry.dossier` to delegate to `fincept_core.datasets.dossier`
2. Or migrate callers directly to the new helpers
3. Deprecate the old internal implementation
**Estimated effort:** 1 day

### B4. Close test coverage gaps
**What:** Four services have no dedicated test files:
- `services/portfolio/` (0 tests — may be in fincept-core)
- `services/risk/` (0 tests — may be in fincept-core)
- `services/jobs/` (0 tests — scheduled jobs)
- `services/ingestor/` (0 tests — market data ingestor)
- `services/orchestrator/` (1 test file — light coverage)

**What to do:**
1. For portfolio + risk: check if tests exist in fincept-core (likely yes for the
   math, but the service-level integration is untested)
2. For jobs: add tests for APScheduler configuration and EOD loaders
3. For ingestor: add tests for WebSocket connection handling and normalization
4. For orchestrator: add tests for consensus building and notional allocation
**Estimated effort:** 2-3 days

### B5. Fix the pre-existing flaky test
**What:** `test_real_trainer_inference_e2e::test_full_pipeline_train_inference_ledger`
occasionally fails with `ResourceWarning: unclosed event loop` on Windows when
running all 6 packages together. Passes in isolation.
**What to do:**
1. Add explicit `asyncio.loop.close()` in the test fixture cleanup
2. Or use `pytest-asyncio`'s `scope="function"` with proper event loop management
**Estimated effort:** 1-2 hours

### B6. Wire the news-impact model into the evidence spine
**What:** The `experiments/news-impact-model/` sub-project has a full pipeline
(events, features, labels, evaluation, training) but is not integrated with the
main system's evidence spine.
**What to do:**
1. Decide: is this a permanent experiment or a candidate for promotion?
2. If promoting: wire its predictions into the prediction log + settlement store
3. If keeping as experiment: add to `.gitignore` or move to a separate repo
**Estimated effort:** 1 day (decision) + 2-3 days (integration if promoting)

---

## 5. Track C — Feature Expansion (After Track A or in parallel with Track B)

### C1. TASK-1005: Alpha Genome Lab
**Status:** The only remaining unplanned Phase 10 task.
**What:** Automate feature/model recipe generation while forcing every recipe
through leakage, walk-forward, shadow, and economic gates. Enforce trial budgets.
Kill underperforming sweeps early. Register only evidence-backed candidates.
**Prerequisites:** Phase 5, 6, 7 complete (DONE) + Track A operational proof
**Estimated effort:** Large (1-2 weeks)

### C2. Portfolio optimizer
**What:** The current branch is `codex/portfolio-optimizer-core`, suggesting
portfolio optimization work was planned. The dashboard has a
`/portfolio-builder` page. Check what's planned vs built.
**Estimated effort:** Unknown — needs scoping

### C3. News-impact model promotion
**What:** If Track B6 decides to promote the news-impact model, it needs to go
through the full evidence spine: prediction log → settlement → tournament →
promotion gate.
**Estimated effort:** 3-5 days

### C4. Dashboard polish
**What:** The dashboard has 20+ pages but some may need polish:
- `/signal-cockpit-demo` is labeled "experimental"
- Some pages may not handle all error states gracefully
- The `/system` page could surface the new evidence spine health
**Estimated effort:** 1-2 days

---

## 6. Recommended Execution Order

### Immediate (this session or next)
1. **B1 — Clean up the dirty worktree.** 57 untracked files is too many. Commit
   the good stuff, gitignore the generated stuff, delete the scratch stuff. This
   is a prerequisite for clean CI and for starting Track A.

2. **B5 — Fix the flaky test.** 1-2 hours, removes a known source of noise.

3. **Commit the evidence documents.** The 2 modified `.omo/evidence/` files
   should be committed to preserve the audit trail.

### Short-term (next 1-2 weeks)
4. **A1 — Rebuild RunPod containers + re-run training/inference.** This is the
   first operational step. It produces real dossiers and real shadow predictions,
   which are prerequisites for everything else in Track A.

5. **B2 — Consolidate the two settlement systems.** Do this before A2 so the
   30-day shadow run writes to the right store from the start.

6. **B3 — Consume the dossier/calibration parity helpers.** Do this before A1
   so the real dossier uses the shared implementation.

### Medium-term (next 1-3 months)
7. **A2 — Run shadow inference for 30+ days.** This is passive wall time. While
   it runs, do Track B items.

8. **B4 — Close test coverage gaps.** 2-3 days of work, can be done during the
   shadow inference run.

9. **B6 — Decide on news-impact model.** Commit or extract.

### Long-term (3-6 months)
10. **A3-A7 — The full operational proof chain.** Promote a model, enable paper
    bridge, deploy production, configure broker, re-review readiness. This is the
    path to limited live trading.

11. **C1 — Alpha Genome Lab.** Only after Track A proves the core loop works
    against real data.

---

## 7. Decision Points for the Operator

These are decisions that only the operator (you) can make. The system cannot
make them automatically, and they should not be rushed.

### Decision 1: When to start the operational proof (Track A)
The code is ready. The question is whether you want to spend GPU dollars and
commit to a 30+ day shadow inference run. This is a budget and time commitment,
not a technical decision.

### Decision 2: What to do with the news-impact experiment
Is `experiments/news-impact-model/` a permanent research experiment, or a
candidate for integration into the main system? It has a full pipeline but is
not wired to the evidence spine.

### Decision 3: Whether to consolidate settlement systems now or later
The two settlement systems coexist but diverge. Consolidating now (before the
30-day run) is cleaner. Consolidating later (after the run) means migrating
real data.

### Decision 4: Production deployment target
AWS (TASK-0903 design exists) or Railway (TASK-0902 staging design exists)?
The Terraform for AWS is written but not applied. Railway is simpler but less
production-grade.

### Decision 5: When to pursue limited live trading
This is the final gate. It requires all of Track A to be complete. The system
is designed to make this safe (rollback pointer, risk caps, human approval), but
it is still real money. The readiness review must say "READY" before this is
considered.

---

## 8. What the System Does NOT Need

To avoid scope creep, these are explicitly out of scope for the next phase:

- **No new ML models.** The existing LightGBM baseline + MoE router + conformal
  gate + drift sentinel are sufficient for the first operational proof. New
  models (transformers, foundation models, etc.) are Phase 12+.
- **No new data sources.** The existing Alpaca + yfinance + Binance + OpenBB +
  Exa + NewsAPI integrations are sufficient. New sources add complexity without
  improving the evidence loop.
- **No new trading venues.** Alpaca paper trading is the only broker path. Live
  trading is Phase 12.
- **No UI redesign.** The dashboard is feature-complete for operator workflows.
  Polish is fine, redesign is not.
- **No architecture changes.** The microservices + Redis Streams + TimescaleDB
  architecture is sound. Don't refactor what works.
- **No new framework adoptions.** No DuckDB, no Polars migration, no new ORM, no
  new web framework. The stack is chosen and stable.

---

## 9. Key Files for Reference

| What | File |
|------|------|
| Master plan | `docs/NEXT_STEPS_PLAN.md` (2291 lines) |
| Big plan | `AAAAAAAAA_BIG_PLAN.md` (2065 lines) |
| Readiness review | `docs/LIMITED_LIVE_READINESS_REVIEW.md` (496 lines) |
| Evidence spine context | `.omo/evidence/session-context.md` (704 lines) |
| Evidence spine audit | `.omo/evidence/in-depth-review.md` (269 lines) |
| Evidence spine plan | `.omo/plans/ml-dataset-evidence-spine.md` |
| QF remaining tasks plan | `.omo/plans/quant-foundry-remaining-tasks.md` (434 lines) |
| Builder logs | `docs/AAA_GLM_SUPERTEAM_LOGS/BUILDER{1..6}.md` |
| Handoff remainder | `docs/AAA_GLM_SUPERTEAM_LOGS/HANDOFF_REMAINDER_GLM52.md` |
| System architecture survey | (this session's subagent output) |

---

## 10. Summary

The Fincept Terminal is at a **code-complete inflection point**. Every planned
task from Phases 0–11 of the master plan is implemented and tested. The ML
Dataset Evidence Spine (the most recent work) closes the last architectural gap
by connecting predictions, outcomes, and features into a single evidence receipt.

The system is **NOT READY for live trading** — but the remaining work is
**operational, not technical**. The path to limited live is:

1. Rebuild RunPod containers → run real training + inference
2. Run shadow inference for 30+ days → build settled history
3. Promote a model through the gate → first real promotion
4. Enable paper bridge → first real quant_foundry influence on trading
5. Deploy production control plane → first real deployment
6. Configure broker sandbox → first real broker connection
7. Re-review readiness → go/no-go decision

The fastest path to value is to **start Track A immediately** (rebuild containers)
while **cleaning up the worktree (Track B1)** in parallel. Everything else can
wait until the 30-day shadow run produces data.
