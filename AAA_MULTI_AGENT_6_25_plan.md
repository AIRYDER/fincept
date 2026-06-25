# Multi-Agent Plan: Settlement, Tournament/Promotion, Paper Bridge

> **Created:** 2026-06-25
> **Branch:** `codex/portfolio-optimizer-core`
> **Prerequisite:** RunPod loop is live-proven (commits `3f29bbb`, `f3bc3d0`). Shadow predictions and dossiers are flowing into durable stores.

## Executive Summary

The codebase already contains fully implemented settlement, tournament, promotion, and paper bridge modules — but they are **not wired together or to the gateway's operational loop**. The gap is integration, not greenfield development.

| Area | Code Status | Operational Status |
|---|---|---|
| Settlement ledger | ✅ Full (`settlement.py`, `outcomes.py`, `metrics.py`) | ❌ No sweep, no market data feed, not wired to gateway |
| Shadow settlement | ✅ Full (`shadow_settlement.py`) | ❌ Not connected to gateway callback path |
| Tournament scoring | ✅ Full (`tournament.py`, 38 tests) | ❌ No sweep, no real settlement input |
| Expanded leaderboard | ✅ Full (`leaderboard_expanded.py`, 30+ tests) | ❌ Not fed real data |
| Promotion gate | ✅ Full (`promotion.py`, 20+ tests) | ❌ No POST API endpoints, dashboard buttons are preview-only |
| Paper bridge | ✅ Full (`paper_bridge.py`, 19 test classes) | ❌ Never enabled, no promoted model to bridge |
| Retirement/decay | ✅ Full (`retirement.py`) | ❌ Not wired to tournament sweep |

### What's Already Done (Do Not Reimplement)

- `SettlementLedger.settle()` — look-ahead guard, idempotent, cost-versioned, JSONL-durable
- `ShadowSettlementOrchestrator` — HMAC verification, batch hashing, rejection tracking
- `Tournament.score()` — 8 weighted components, 6 hard gates, DSR, bootstrap p-value
- `ExpandedLeaderboard` — horizon/regime/cluster slices, decay indicators, explanations
- `PromotionGate.evaluate()` — 5-gate fail-closed evaluation, waivers, immutable receipts
- `PaperBridge.publish()` — 7-step validation, circuit breaker, rollback pointer
- `ShadowLedger` — JSONL storage, order-field rejection, idempotency
- All API GET endpoints for health, jobs, dossiers, tournament, promotion
- All dashboard pages (overview, jobs, models, tournament, promotion)

### What's Missing (This Plan)

1. **Settlement sweep** — a periodic worker that settles pending shadow predictions using real market data
2. **Market data adapter** — feeds bar prices into the settlement ledger
3. **Tournament sweep** — a periodic worker that scores models from settlement records and updates the leaderboard
4. **Promotion write endpoints** — POST endpoints for submit/approve/reject
5. **Dashboard wiring** — connect approve/reject buttons to real POST endpoints
6. **Gateway integration** — wire `shadow_health()`, `tournament_leaderboard()`, and `pending_promotions()` to real data
7. **Paper bridge operational proof** — end-to-end test with a promoted model

---

## Dependency Graph

```
Track A (Settlement)          Track B (Tournament/Promotion)     Track C (Paper Bridge)
─────────────────────         ──────────────────────────────     ─────────────────────
A1: Market data adapter       B1: Tournament sweep               (waits for A + B)
A2: Settlement sweep          B2: Gateway tournament wiring      
A3: Gateway settlement wiring B3: Promotion POST endpoints       
A4: Settlement integration    B4: Dashboard promotion wiring     
        │                            │
        └──────────┬─────────────────┘
                   ▼
            C1: Paper bridge proof
            C2: End-to-end demonstration
```

**Parallelism:** Tracks A and B run in parallel. Track C starts after A and B complete. Within tracks A and B, steps are sequential.

---

## Track A: Settlement — Settle Shadow Predictions Against Realized Outcomes

**Owner:** Agent A (settlement)
**Goal:** A periodic sweep that takes pending shadow predictions, fetches market data, settles them, and exposes real settlement metrics through the gateway.

### A1: Market Data Adapter

**Files owned:**
- `services/quant_foundry/src/quant_foundry/market_data_adapter.py` (NEW)
- `services/quant_foundry/tests/test_market_data_adapter.py` (NEW)

**Files inspected (read-only):**
- `libs/fincept-db/src/fincept_db/bars.py` — existing bar data storage
- `services/oms/src/oms/alpaca/data.py` — existing Alpaca data client
- `services/quant_foundry/src/quant_foundry/metrics.py` — `realized_return()` expects `prices` as `list[PricePoint]`

**Implementation:**
1. Define `PricePoint` (or reuse existing) — `ts_ns: int`, `close: float`, `open: float | None`
2. Implement `BarDataAdapter`:
   - `get_prices(symbol, start_ns, end_ns) -> list[PricePoint]`
   - Source 1: `fincept_db.bars` (local SQLite/Postgres bar storage)
   - Source 2 (fallback): Alpaca data API (if `ALPACA_API_KEY` set)
   - Missing data returns empty list (settlement will produce `PENDING_DATA`)
3. Add `get_benchmark_prices(symbol, start_ns, end_ns)` for abnormal return (default: SPY)
4. Tests: fixture bars, missing data, date range filtering, benchmark fallback

**Acceptance:**
- `get_prices("AAPL", ...)` returns sorted price points
- Missing symbol returns `[]` (no crash)
- No look-ahead: only returns prices with `ts_ns >= start_ns`

### A2: Settlement Sweep Worker

**Files owned:**
- `services/quant_foundry/src/quant_foundry/settlement_sweep.py` (NEW)
- `services/quant_foundry/tests/test_settlement_sweep.py` (NEW)

**Files inspected (read-only):**
- `services/quant_foundry/src/quant_foundry/shadow_ledger.py` — `ShadowLedger.list()`, `read_by_model()`
- `services/quant_foundry/src/quant_foundry/settlement.py` — `SettlementLedger.settle()`
- `services/quant_foundry/src/quant_foundry/outcomes.py` — `CostModel`, `SettlementStatus`
- `services/quant_foundry/src/quant_foundry/shadow_settlement.py` — `ShadowSettlementOrchestrator`

**Implementation:**
1. Define `SettlementSweep`:
   ```python
   class SettlementSweep:
       def __init__(self, shadow_ledger, settlement_ledger, market_data_adapter, cost_model, benchmark_symbol="SPY"):
           ...
       def sweep(self, now_ns: int | None = None) -> SweepReceipt:
           # 1. List all shadow predictions
           # 2. Filter to predictions where ts_event + horizon_ns <= now_ns (horizon expired)
           # 3. Skip already-settled (idempotent — SettlementLedger handles this)
           # 4. Fetch prices for each (symbol, ts_event, ts_event + horizon_ns)
           # 5. Settle each prediction
           # 6. Return receipt: settled_count, pending_time_count, pending_data_count, failed_count
   ```
2. Default `CostModel`: fee_bps=10, spread_bps=5, slippage_bps=3, borrow_bps_per_day=25
3. Sweep is idempotent — safe to rerun
4. Tests: fixture predictions + fixture prices → deterministic settlement, missing data → PENDING_DATA, not-yet-expired → PENDING_TIME, rerun produces same records

**Acceptance:**
- Pending predictions settle after horizon expires
- Missing market data produces `PENDING_DATA` (no crash)
- Reruns do not duplicate outcomes
- Receipt feeds tournament scoring

### A3: Gateway Settlement Wiring

**Files owned:**
- `services/quant_foundry/src/quant_foundry/gateway.py` (EDIT — settlement methods only)
- `services/quant_foundry/tests/test_gateway_settlement.py` (NEW)

**File ownership constraint:** Only edit the `shadow_health()`, `settlement_sweep()`, and related settlement methods in `gateway.py`. Do NOT touch tournament or promotion methods (Agent B owns those).

**Implementation:**
1. Add `SettlementSweep` instance to `QuantFoundryGateway.__init__()` (lazy-init from env)
2. Add `gateway.run_settlement_sweep() -> dict` method — delegates to `SettlementSweep.sweep()`
3. Update `gateway.shadow_health()` to read real `settled_count` and `settlement_lag_seconds` from the settlement ledger
4. Add `gateway.settlement_status() -> dict` — returns settled/pending/failed counts
5. Wire settlement sweep into the API startup poller (alongside RunPod poller in `main.py`)
   - **File:** `services/api/src/api/main.py` (EDIT — add settlement poll task)
   - Interval: `QUANT_FOUNDRY_SETTLEMENT_INTERVAL_SECONDS` (default 60)
6. Add `GET /quant-foundry/settlement/status` API endpoint
   - **File:** `services/api/src/api/routes/quant_foundry.py` (EDIT — add settlement endpoint only)

**Acceptance:**
- `shadow_health()` returns real `settled_count > 0` after sweep runs
- `settlement_lag_seconds` is computed from actual settlement timestamps
- Settlement sweep runs periodically without blocking the RunPod poller

### A4: Settlement Integration Test

**Files owned:**
- `services/quant_foundry/tests/test_settlement_integration.py` (NEW)

**Implementation:**
1. End-to-end test: store shadow prediction → wait for horizon → run sweep → verify settlement record
2. Test with the live-proven shadow predictions from the RunPod loop
3. Verify `shadow_health()` returns real metrics after sweep
4. Verify settlement records can feed `Tournament.score()`

**Acceptance:**
- Shadow predictions from the RunPod loop settle into outcomes
- Settlement lag is visible in `shadow_health()`
- No prediction reaches `sig.predict`

---

## Track B: Tournament/Promotion — Score Models and Enable Human-Gated Promotion

**Owner:** Agent B (tournament/promotion)
**Goal:** A periodic tournament sweep that scores models from settlement evidence, updates the leaderboard, and exposes POST endpoints for human-gated promotion.

### B1: Tournament Sweep Worker

**Files owned:**
- `services/quant_foundry/src/quant_foundry/tournament_sweep.py` (NEW)
- `services/quant_foundry/tests/test_tournament_sweep.py` (NEW)

**Files inspected (read-only):**
- `services/quant_foundry/src/quant_foundry/tournament.py` — `Tournament.score()`
- `services/quant_foundry/src/quant_foundry/leaderboard_expanded.py` — `ExpandedLeaderboard`
- `services/quant_foundry/src/quant_foundry/settlement.py` — `SettlementLedger.read_all()`
- `services/quant_foundry/src/quant_foundry/retirement.py` — decay/retirement flags
- `services/quant_foundry/src/quant_foundry/dossier.py` — `DossierRegistry.list()`

**Implementation:**
1. Define `TournamentSweep`:
   ```python
   class TournamentSweep:
       def __init__(self, settlement_ledger, dossier_registry, tournament, leaderboard, retirement_checker=None):
           ...
       def sweep(self, now_ns: int | None = None) -> TournamentSweepReceipt:
           # 1. Read all settlement records
           # 2. Group by model_id
           # 3. For each model with enough settled predictions:
           #    a. Build ScoringInput from settlement records
           #    b. Run Tournament.score()
           #    c. Build ExpandedLeaderboardEntry with slices
           #    d. Check retirement/decay flags
           #    e. Add to leaderboard
           # 4. Return receipt: scored_models, blocked_models, stale_models
   ```
2. Build `ScoringInput` from `SettlementRecord` list:
   - `oos_returns_net` = `[r.realized_return_net for r in records]`
   - `settled_count` = `len(records)`
   - `calibration_signals` = Brier scores + confidence buckets
   - `trial_count` = number of models scored in this sweep
3. Tests: fixture settlement records → deterministic tournament scores, insufficient evidence → INSUFFICIENT_EVIDENCE, stale records → STALE

**Acceptance:**
- Models with settled predictions get scored
- Models with insufficient evidence are blocked
- Leaderboard updates after each sweep
- Output can feed promotion evidence packets

### B2: Gateway Tournament Wiring

**Files owned:**
- `services/quant_foundry/src/quant_foundry/gateway.py` (EDIT — tournament methods only)
- `services/quant_foundry/tests/test_gateway_tournament.py` (NEW)

**File ownership constraint:** Only edit `tournament_leaderboard()`, `run_tournament_sweep()`, and related tournament methods. Do NOT touch settlement methods (Agent A owns those).

**Implementation:**
1. Add `TournamentSweep` instance to `QuantFoundryGateway.__init__()` (lazy-init)
2. Add `gateway.run_tournament_sweep() -> dict` — delegates to `TournamentSweep.sweep()`
3. Update `gateway.tournament_leaderboard()` to return real leaderboard data from `ExpandedLeaderboard.ranked()`
4. Wire tournament sweep into the API startup poller
   - **File:** `services/api/src/api/main.py` (EDIT — add tournament poll task)
   - Interval: `QUANT_FOUNDRY_TOURNAMENT_INTERVAL_SECONDS` (default 300)
5. Add `GET /quant-foundry/tournament/status` endpoint
   - **File:** `services/api/src/api/routes/quant_foundry.py` (EDIT — add tournament status endpoint only)

**Acceptance:**
- `tournament_leaderboard()` returns real ranked models after sweep
- Stale/decayed models are flagged
- Leaderboard explains why a model ranks where it does

### B3: Promotion POST Endpoints

**Files owned:**
- `services/api/src/api/routes/quant_foundry.py` (EDIT — POST endpoints only)
- `services/quant_foundry/src/quant_foundry/gateway.py` (EDIT — promotion submit/approve/reject methods only)
- `services/api/tests/test_promotion_endpoints.py` (NEW)

**File ownership constraint:** Only add new POST endpoints and gateway promotion methods. Do NOT modify existing GET endpoints or tournament/settlement methods.

**Implementation:**
1. Add `POST /quant-foundry/promotion/submit`:
   - Body: `{model_id, target_level, review_note}`
   - Gateway builds `PromotionEvidence` from dossier + tournament result + sentinel receipt
   - Submits to `PromotionReviewQueue`
   - Returns pending entry ID
2. Add `POST /quant-foundry/promotion/approve`:
   - Body: `{model_id, review_note}`
   - Processes next pending entry for this model
   - Returns `PromotionReceipt` with decision=APPROVED
3. Add `POST /quant-foundry/promotion/reject`:
   - Body: `{model_id, review_note, rejection_reason}`
   - Processes next pending entry for this model
   - Returns `PromotionReceipt` with decision=REJECTED
4. Add `gateway.submit_promotion(model_id, target_level, review_note) -> dict`
5. Add `gateway.process_promotion(model_id, approve: bool, review_note, rejection_reason=None) -> dict`
6. Tests: submit → approve flow, submit → reject flow, missing model → 404, insufficient evidence → 422

**Acceptance:**
- Operator can submit a promotion request via API
- Operator can approve/reject via API
- Promotion receipts are immutable and stored
- No model promotes without all gates passing

### B4: Dashboard Promotion Wiring

**Files owned:**
- `apps/dashboard/src/app/quant-foundry/promotion/page.tsx` (EDIT)
- `apps/dashboard/src/lib/api.ts` (EDIT — add promotion POST methods)
- `apps/dashboard/src/lib/types.ts` (EDIT — add promotion POST types)

**Implementation:**
1. Add `api.quantFoundrySubmitPromotion(token, {model_id, target_level, review_note})` to `api.ts`
2. Add `api.quantFoundryApprovePromotion(token, {model_id, review_note})` to `api.ts`
3. Add `api.quantFoundryRejectPromotion(token, {model_id, review_note, rejection_reason})` to `api.ts`
4. Wire the promotion page's approve button to call `approvePromotion`
5. Wire the reject button to call `rejectPromotion`
6. Add a "Submit for Promotion" form on the models page
7. Add confirmation dialogs with evidence summary before approve/reject
8. Add toast/notification on success/failure
9. TypeScript types for POST request/response bodies

**Acceptance:**
- Approve button calls real API and updates the queue
- Reject button calls real API and updates the queue
- Confirmation dialog shows evidence summary before action
- Loading states during API call
- Error states on API failure

---

## Track C: Paper Bridge Operational Proof

**Owner:** Agent C (paper bridge)
**Goal:** Prove the paper bridge end-to-end with a real promoted model, then document the live-readiness gap.

**Start condition:** Tracks A and B are complete. At least one model has:
- A dossier in `DossierRegistry`
- Settled predictions in `SettlementLedger`
- A tournament score in `ExpandedLeaderboard`
- Passed through the promotion gate to `shadow_approved` or `paper_approved`

### C1: Paper Bridge End-to-End Proof

**Files owned:**
- `scripts/paper_bridge_proof.py` (NEW)
- `services/quant_foundry/tests/test_paper_bridge_integration.py` (NEW)

**Files inspected (read-only):**
- `services/quant_foundry/src/quant_foundry/paper_bridge.py`
- `services/quant_foundry/src/quant_foundry/promotion.py`
- `services/quant_foundry/src/quant_foundry/gateway.py`

**Implementation:**
1. Create `paper_bridge_proof.py`:
   - Step 1: Verify a model exists with `paper_approved` status (or promote one through the gate)
   - Step 2: Set `QUANT_FOUNDRY_ALLOW_PAPER_BRIDGE=true`
   - Step 3: Set `runtime_mode=paper`
   - Step 4: Call `PaperBridge.publish()` with the model's shadow prediction
   - Step 5: Verify `BridgeReceipt` status is `PUBLISHED`
   - Step 6: Verify rollback pointer was created
   - Step 7: Verify `PaperPrediction` has no order/OMS fields
   - Step 8: Trip the circuit breaker and verify it blocks further publishes
   - Step 9: Reset circuit breaker and verify publishes resume
2. Integration test: full flow from shadow prediction → settlement → tournament → promotion → paper bridge publish

**Acceptance:**
- Bridge publishes a paper prediction for an approved model
- Rollback pointer exists
- Circuit breaker trips on failures and blocks further publishes
- No order/OMS/risk fields in the paper prediction
- OMS and risk services are not touched

### C2: Live Readiness Assessment Update

**Files owned:**
- `docs/LIMITED_LIVE_READINESS_REVIEW.md` (EDIT — update with paper bridge proof results)

**Implementation:**
1. Update the 14-gate checklist with paper bridge proof evidence
2. Document remaining blockers for live bridge:
   - Long shadow evidence period (minimum 30 days)
   - Long paper evidence period (minimum 30 days)
   - Production deployment environment
   - Broker sandbox credentials
   - Position size limits and kill switch
3. State explicit verdict: NOT READY for live (with evidence)

**Acceptance:**
- Every gate has a status (MET / PARTIAL / NOT MET) with evidence
- Remaining blockers are specific and actionable
- No claim of live readiness without evidence

---

## File Ownership Matrix

| File | Agent A | Agent B | Agent C |
|---|---|---|---|
| `market_data_adapter.py` | **WRITE** | read | read |
| `settlement_sweep.py` | **WRITE** | read | read |
| `tournament_sweep.py` | read | **WRITE** | read |
| `gateway.py` (settlement methods) | **WRITE** | — | — |
| `gateway.py` (tournament methods) | — | **WRITE** | — |
| `gateway.py` (promotion methods) | — | **WRITE** | — |
| `gateway.py` (paper bridge methods) | — | — | **WRITE** |
| `main.py` (settlement poll task) | **WRITE** | — | — |
| `main.py` (tournament poll task) | — | **WRITE** | — |
| `routes/quant_foundry.py` (settlement endpoint) | **WRITE** | — | — |
| `routes/quant_foundry.py` (tournament endpoint) | — | **WRITE** | — |
| `routes/quant_foundry.py` (promotion POST endpoints) | — | **WRITE** | — |
| `promotion/page.tsx` | — | **WRITE** | — |
| `api.ts` (promotion POST methods) | — | **WRITE** | — |
| `types.ts` (promotion POST types) | — | **WRITE** | — |
| `paper_bridge_proof.py` | — | — | **WRITE** |
| `test_settlement_sweep.py` | **WRITE** | — | — |
| `test_tournament_sweep.py` | — | **WRITE** | — |
| `test_gateway_settlement.py` | **WRITE** | — | — |
| `test_gateway_tournament.py` | — | **WRITE** | — |
| `test_promotion_endpoints.py` | — | **WRITE** | — |
| `test_paper_bridge_integration.py` | — | — | **WRITE** |
| `LIMITED_LIVE_READINESS_REVIEW.md` | — | — | **WRITE** |

**Conflict resolution for `gateway.py`:** Each agent adds methods in a clearly delimited section with a comment header. No agent edits another agent's section. The `__init__` method is extended by each agent adding their lazy-init fields — coordinate via separate PR commits or sequential merging.

**Conflict resolution for `main.py`:** Each agent adds their poll task in the lifespan function. Agent A adds settlement poll, Agent B adds tournament poll. Both are independent `asyncio.create_task` calls.

**Conflict resolution for `routes/quant_foundry.py`:** Each agent adds their endpoints at the end of the file. No agent edits another agent's endpoints.

---

## Execution Order

```
Time ──────────────────────────────────────────────────────────────────────────►

Agent A:  [A1: market data] → [A2: settlement sweep] → [A3: gateway wiring] → [A4: integration test]
                                                                              │
Agent B:  [B1: tournament sweep] → [B2: gateway wiring] → [B3: POST endpoints] → [B4: dashboard wiring]
                                                                              │
                                                                              ▼
Agent C:                                                              [C1: paper bridge proof] → [C2: readiness update]
```

**A1 and B1 start in parallel immediately** — they have no dependencies on each other.

**B1 can start in parallel with A1** because tournament sweep code can be written against the settlement ledger interface before the settlement sweep is wired. B1's tests use fixture settlement records.

**A3 and B2 can run in parallel** — they edit different methods in `gateway.py`.

**B3 and B4 are sequential** — B4 (dashboard wiring) depends on B3 (POST endpoints).

**C1 starts after A4 and B4 complete** — it needs a promoted model with real settlement evidence.

---

## Verification Commands

```powershell
# Agent A tests
$env:UV_CACHE_DIR = (Get-Location).Path + '\.uv-cache'
uv run --package quant-foundry pytest services/quant_foundry/tests/test_market_data_adapter.py -q
uv run --package quant-foundry pytest services/quant_foundry/tests/test_settlement_sweep.py -q
uv run --package quant-foundry pytest services/quant_foundry/tests/test_gateway_settlement.py -q
uv run --package quant-foundry pytest services/quant_foundry/tests/test_settlement_integration.py -q

# Agent B tests
uv run --package quant-foundry pytest services/quant_foundry/tests/test_tournament_sweep.py -q
uv run --package quant-foundry pytest services/quant_foundry/tests/test_gateway_tournament.py -q
uv run --package api pytest services/api/tests/test_promotion_endpoints.py -q

# Agent C tests
uv run --package quant-foundry pytest services/quant_foundry/tests/test_paper_bridge_integration.py -q

# Dashboard typecheck
pnpm --dir apps/dashboard exec tsc --noEmit --pretty false

# Full suite
uv run --package quant-foundry pytest services/quant_foundry/tests -q
uv run --package api pytest services/api/tests -q -k quant_foundry
```

---

## Safety Invariants (Non-Negotiable)

These are enforced by existing code and must not be broken by any agent:

1. **No RunPod worker gets broker credentials.**
2. **No RunPod worker writes to `ord.orders`, `ord.decisions`, `ord.fills`, `ord.positions`, or `sig.predict`.**
3. **All callbacks are HMAC-signed and verified. Fail-closed on bad signature.**
4. **`ModelDossier` always carries `authority=SHADOW_ONLY`.** Promotion to live is human-gated.
5. **Paper bridge is disabled by default.** `QUANT_FOUNDRY_ALLOW_PAPER_BRIDGE=true` required.
6. **Paper bridge refuses non-paper runtime.** `runtime_mode` must be `"paper"`.
7. **Paper bridge refuses models without evidence packet.** Dossier + tournament result + sentinel receipt required.
8. **Paper bridge refuses models not `paper-approved`.** Promotion gate must have approved.
9. **Circuit breaker trips after 5 failures.** Blocks further publishes until reset.
10. **Rollback pointer created before publishing.** Operator can always roll back.
11. **OMS and risk services remain authoritative.** Paper bridge only produces `PaperPrediction` — no order fields.
12. **Settlement uses only post-decision prices.** Look-ahead guard enforced in `metrics.py`.
13. **Settlement is idempotent.** Same `(prediction_id, cost_model_version)` returns existing record.
14. **Tournament is advisory.** No automatic promotion. Human approval required.
15. **Promotion gate fails closed.** Missing evidence → REJECTED, not APPROVED.
16. **MVP promotion limit: `shadow_approved` max.** `paper_approved` requires explicit level unlock.
17. **Secrets never in source, logs, receipts, or dashboard responses.**
18. **No new dependencies without package manager command** (`uv add` / `pnpm add`).

---

## Environment Variables (New)

| Variable | Default | Purpose |
|---|---|---|
| `QUANT_FOUNDRY_SETTLEMENT_INTERVAL_SECONDS` | `60` | Settlement sweep poll interval |
| `QUANT_FOUNDRY_TOURNAMENT_INTERVAL_SECONDS` | `300` | Tournament sweep poll interval |
| `QUANT_FOUNDRY_COST_MODEL_FEE_BPS` | `10` | Settlement cost model: fee |
| `QUANT_FOUNDRY_COST_MODEL_SPREAD_BPS` | `5` | Settlement cost model: spread |
| `QUANT_FOUNDRY_COST_MODEL_SLIPPAGE_BPS` | `3` | Settlement cost model: slippage |
| `QUANT_FOUNDRY_COST_MODEL_BORROW_BPS_PER_DAY` | `25` | Settlement cost model: borrow |
| `QUANT_FOUNDRY_BENCHMARK_SYMBOL` | `SPY` | Abnormal return benchmark |
| `QUANT_FOUNDRY_ALLOW_PAPER_BRIDGE` | `false` | Enable paper bridge (TASK-0704) |
| `QUANT_FOUNDRY_RUNTIME_MODE` | `paper` | Runtime mode for paper bridge |

---

## Commit Strategy

Each agent commits their own work in focused commits:

```
Agent A:
  feat(quant_foundry): market data adapter for settlement price feeds
  feat(quant_foundry): settlement sweep worker with periodic polling
  feat(quant_foundry): wire settlement into gateway health and API startup
  test(quant_foundry): settlement integration test with RunPod shadow predictions

Agent B:
  feat(quant_foundry): tournament sweep worker with periodic scoring
  feat(quant_foundry): wire tournament leaderboard into gateway
  feat(api): promotion submit/approve/reject POST endpoints
  feat(dashboard): wire promotion approve/reject buttons to real API

Agent C:
  test(quant_foundry): paper bridge end-to-end integration proof
  docs: update limited live readiness review with paper bridge evidence
```

---

## Success Criteria

The plan is complete when:

1. **Settlement loop works:** Shadow predictions from the RunPod loop are settled against market data, and `shadow_health()` shows real `settled_count > 0` and `settlement_lag_seconds`.
2. **Tournament leaderboard is real:** `tournament_leaderboard()` returns models scored from real settlement evidence, with horizon/regime/cluster slices and decay flags.
3. **Promotion is operable:** An operator can submit, approve, and reject promotion requests through the dashboard, with immutable receipts.
4. **Paper bridge is proven:** A model promoted to `paper_approved` has its shadow predictions bridged to `PaperPrediction` with a rollback pointer, and the circuit breaker works.
5. **Live readiness is documented:** The readiness review is updated with evidence from steps 1-4, and the verdict is clear about what remains for live trading.
6. **No safety invariants are broken:** All 18 invariants above are verified by tests.
