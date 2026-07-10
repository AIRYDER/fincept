# C6 Task 10 — Settlement Path Inventory

## Main SHA

`5cfb6cfaf75bae5bb67fad298fc1716217682a9d`

## Overview

Two settlement paths coexist in the codebase. They are documented as
coexisting in `services/api/src/api/settlements_poller.py` (lines 7-27).
A third path (portfolio position PnL) exists but is not a prediction
settlement path — it is position-level fill math shared by the
backtester and live portfolio.

---

## Path A — New / fincept_core

| Field | Value |
|-------|-------|
| **Module** | `services/settlements/src/settlements/worker.py` |
| **Entrypoint** | `tick()` (async) / `tick_sync()` (sync) |
| **Record class** | `fincept_core.datasets.SettlementRecord` (Pydantic, frozen) |
| **Store class** | `fincept_core.datasets.SettlementStore` |
| **Store layout** | `data/settlements/<agent_id>.jsonl` |
| **Key field** | `agent_id` |
| **Cost model version** | `v1.default` |
| **Cost model** | fee 5 bps, spread 3 bps, slippage 0 bps |
| **Gross return formula** | `(close_t2 / close_t1) - 1.0` (no direction) |
| **Net return formula** | `gross - (fee_bps + spread_bps) / 10000.0` |
| **Brier formula** | `(prob_up - actual_up) ** 2` where `prob_up = (direction + 1) / 2` |
| **Abnormal return** | Not computed |
| **Calibration bucket** | Not computed |
| **Borrow cost** | Not modeled |
| **Statuses** | `pending_time`, `pending_data`, `settled`, `failed` |
| **Idempotency key** | `(prediction_id, cost_model_version)` |
| **Look-ahead guard** | Yes (decision_window_end_ns <= now_ns) |
| **Callers** | `settlements_poller._poll_settlements_worker()` (API lifespan) |
| **Tests** | `services/settlements/tests/test_worker.py` (22 tests) |
| **Used by tournament** | No (tournament uses Path B) |
| **Used by promotion** | No (promotion uses Path B via SettledComparisonInputProvider) |
| **Used by live/paper** | Paper (API poller) |
| **Known assumptions** | Direction is encoded in prob_up, not in return formula. No benchmark. No borrow. |

### Path A — Internal helpers

- `_build_settled_record(pred, now_ns, close_t1, close_t2)` — builds settled record
- `_build_pending_data_record(pred)` — builds pending_data record
- `_load_due_predictions(predictions_dir, now_ns)` — scans prediction log
- `_existing_status(store, agent_id, prediction_id, cost_model_version)` — idempotency check

### Path A — Market data bridge

- `settlements.market_data_bridge.make_async_market_data_source(bar_adapter)`
- Wraps `quant_foundry.market_data_adapter.BarDataAdapter` into async contract
- Returns close at `ts2` (later timestamp)

---

## Path B — Old / quant_foundry

| Field | Value |
|-------|-------|
| **Module** | `services/quant_foundry/src/quant_foundry/settlement.py` |
| **Entrypoint** | `SettlementLedger.settle()` |
| **Record class** | `quant_foundry.outcomes.SettlementRecord` (dataclass, frozen) |
| **Store class** | `quant_foundry.settlement.SettlementLedger` |
| **Store layout** | `data/quant-foundry/settlements/<model_id>.settlements.jsonl` |
| **Key field** | `model_id` |
| **Cost model version** | `cm-v1` |
| **Cost model** | fee 10 bps, spread 5 bps, slippage 3 bps, borrow 25 bps/day |
| **Gross return formula** | Direction-aware: long `(exit-entry)/entry`, short `(entry-exit)/entry` |
| **Net return formula** | `gross - (fee + spread + slippage + borrow) / 10000.0` |
| **Brier formula** | `(p_up - actual_up) ** 2` where `p_up` from prediction field |
| **Abnormal return** | Computed (realized - benchmark) |
| **Calibration bucket** | Computed (5 buckets: 0.0-0.2, 0.2-0.4, 0.4-0.6, 0.6-0.8, 0.8-1.0) |
| **Borrow cost** | Modeled (short only, borrow_bps_per_day * holding_days) |
| **Statuses** | `pending_time`, `pending_data`, `settled` |
| **Idempotency key** | `(prediction_id, cost_model_version)` |
| **Look-ahead guard** | Yes (now_ns < window_end → pending_time) |
| **Callers** | `settlement_sweep.SettlementSweep.sweep()`, `shadow_settlement.ShadowSettlementOrchestrator.settle_batch()` |
| **Tests** | `services/quant_foundry/tests/test_settlement_provider.py`, `test_shadow_tournament.py`, `test_auto_tournament.py`, `test_champion_challenger.py` |
| **Used by tournament** | Yes (via `tournament.py` and `shadow_tournament.py`) |
| **Used by promotion** | Yes (via `settlement_provider.SettledComparisonInputProvider`) |
| **Used by live/paper** | Live (via `gateway.run_settlement_sweep`) |
| **Known assumptions** | Direction-aware return. Benchmark available. Borrow cost for shorts. |

### Path B — Orchestration wrappers (delegate to Path B)

1. **`settlement_sweep.SettlementSweep`** (`settlement_sweep.py`)
   - Periodic sweep of `ShadowLedger` → `SettlementLedger.settle()`
   - Called by `gateway.run_settlement_sweep()`
   - Not a separate settlement path — delegates to Path B

2. **`shadow_settlement.ShadowSettlementOrchestrator`** (`shadow_settlement.py`)
   - Stores + settles shadow prediction batches
   - Delegates to `SettlementLedger.settle()`
   - Not a separate settlement path — delegates to Path B

3. **`settlement_provider.SettledComparisonInputProvider`** (`settlement_provider.py`)
   - Reads settled records from Path B's ledger
   - Builds `ComparisonInput` for champion/challenger promotion
   - Consumer of Path B, not a settlement path

---

## Path C — Portfolio Position PnL (not a settlement path)

| Field | Value |
|-------|-------|
| **Module** | `libs/fincept-core/src/fincept_core/portfolio.py` |
| **Entrypoint** | `apply_fill_to_position(prev, fill, strategy_id)` |
| **Purpose** | Position-level PnL from fills (not prediction settlement) |
| **Used by** | `services/backtester/engine.py`, `services/portfolio/` |
| **Arithmetic** | `Decimal` (not float) |
| **Relevance** | Not a prediction settlement path — included for completeness |

---

## Cost Model Comparison

| Parameter | Path A (v1.default) | Path B (cm-v1) | Delta |
|-----------|---------------------|----------------|-------|
| fee_bps | 5.0 | 10.0 | +5.0 |
| spread_bps | 3.0 | 5.0 | +2.0 |
| slippage_bps | 0.0 | 3.0 | +3.0 |
| borrow_bps_per_day | (not modeled) | 25.0 | +25.0 |
| **Total round-trip (long)** | **8 bps** | **18 bps** | **+10 bps** |
| **Total round-trip (short, 1 day)** | **8 bps** | **43 bps** | **+35 bps** |

---

## Canonical path today

**Path B (quant_foundry.SettlementLedger)** is the canonical path today:
- Used by the tournament (`shadow_tournament.py`, `tournament.py`)
- Used by promotion (`settlement_provider.SettledComparisonInputProvider`)
- Used by live settlement sweep (`gateway.run_settlement_sweep`)
- Has richer metrics (abnormal return, calibration, borrow cost)

Path A (settlements.worker) is the newer operational path:
- Used by the API poller for paper-mode outcomes
- Feeds the `/models/{name}/outcomes` API route
- Simpler cost model, no benchmark, no borrow

---

## Key divergences requiring unification decisions

1. **Return formula**: Path A ignores direction; Path B is direction-aware
2. **Cost model**: v1.default (8 bps) vs cm-v1 (18-43 bps)
3. **Brier prob_up**: Path A derives from direction; Path B uses prediction's p_up
4. **Abnormal return**: Only Path B computes it
5. **Calibration bucket**: Only Path B computes it
6. **Borrow cost**: Only Path B models it
7. **Key field**: agent_id (Path A) vs model_id (Path B)
8. **Store layout**: `data/settlements/` vs `data/quant-foundry/settlements/`
