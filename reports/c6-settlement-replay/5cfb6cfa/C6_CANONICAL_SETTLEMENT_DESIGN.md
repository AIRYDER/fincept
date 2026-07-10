# C6 Canonical Settlement Design

## Main SHA

`5cfb6cfaf75bae5bb67fad298fc1716217682a9d`

## Date / Operator

2026-07-10 / Devin (GLM-5.2 High)

## Inputs Reviewed

| File | Status |
|------|--------|
| `settlement_path_inventory.md` | Reviewed — 2 settlement paths + 1 position PnL path |
| `fixtures.json` | Reviewed — 8 deterministic fixtures |
| `replay_results.json` | Reviewed — 16 settlement operations |
| `replay_results_normalized.csv` | Reviewed — 16 normalized rows |
| `divergence_report.md` | Reviewed — 58 divergences classified |
| `divergence_report.json` | Reviewed — 58 structured divergences |
| `summary.md` | Reviewed — Task 10 summary |
| `C6_SETTLEMENT_DIVERGENCE_REPORT.md` | Reviewed — Task 11 canonical recommendation |

Additional codebase files reviewed for this design:

| File | Purpose |
|------|---------|
| `services/api/src/api/settlements_poller.py` | Current Path A poller wiring |
| `services/api/src/api/main.py` | Poller lifecycle (lines 203-212) |
| `services/api/src/api/routes/models.py` | `/models/{name}/outcomes` route (lines 948-1042) |
| `libs/fincept-core/src/fincept_core/datasets/__init__.py` | `build_evidence_receipt` (lines 125-177) |
| `services/quant_foundry/src/quant_foundry/settlement.py` | Path B `SettlementLedger` |
| `services/quant_foundry/src/quant_foundry/settlement_sweep.py` | Path B `SettlementSweep` |
| `services/quant_foundry/src/quant_foundry/outcomes.py` | Path B `SettlementRecord` / `CostModel` |
| `services/quant_foundry/src/quant_foundry/metrics.py` | Path B return/cost/brier formulas |
| `services/quant_foundry/src/quant_foundry/schemas.py` | `ShadowPrediction` schema (model_id, p_up) |
| `services/quant_foundry/src/quant_foundry/shadow_ledger.py` | ShadowLedger (model_id keyed) |
| `services/quant_foundry/src/quant_foundry/settlement_provider.py` | `SettledComparisonInputProvider` |
| `services/quant_foundry/src/quant_foundry/gateway.py` | `run_settlement_sweep` (lines 2222-2245) |
| `libs/fincept-core/src/fincept_core/prediction_log.py` | `PredictionRow` (agent_id keyed, no p_up) |
| `libs/fincept-core/src/fincept_core/datasets/settlement.py` | Path A `SettlementStore` / `SettlementRecord` |
| `services/settlements/src/settlements/worker.py` | Path A `tick` / `tick_sync` |

## Decision Summary

| Decision | Choice | Confidence |
|----------|--------|------------|
| Canonical ledger | Path B: `quant_foundry.SettlementLedger` | High |
| Path A strategy | B: Thin compatibility wrapper, then retire | High |
| Cost model | `cm-v1` (Path B's model) as canonical | High |
| Key mapping | `model_id` canonical; `agent_id` → `model_id` adapter | Medium |
| Store location | `data/quant-foundry/settlements/` (Path B) | High |
| API poller | Rewire to Path B via adapter; preserve response shape | High |

## Canonical Settlement Path

### Decision: Path B — `quant_foundry.SettlementLedger`

### Why Path B wins

Path B is the canonical settlement path based on 9 criteria from the
Task 11 divergence report:

1. **Most complete input handling** — accepts `p_up`, `direction`,
   `confidence`, benchmark prices, and `CostModel` with borrow cost.
   Path A's `PredictionRow` lacks `p_up` and has no benchmark input.

2. **Most complete output schema** — produces `realized_return_gross`,
   `realized_return_net`, `abnormal_return`, `brier`,
   `calibration_bucket`, `cost_model_version`, `decision_window_start/end`.
   Path A is missing `abnormal_return` and `calibration_bucket`.

3. **Used closest to promotion/tournament** — feeds
   `SettledComparisonInputProvider` → `AutoPromotionOrchestrator` →
   `compare_champion_challenger` → `PromotionDecision`. Path A feeds
   only a display API route.

4. **Best test coverage** — 134 tests vs 22 tests (6x more).

5. **Best cost model** — `cm-v1` (fee 10, spread 5, slippage 3, borrow
   25 bps/day) is more realistic than `v1.default` (fee 5, spread 3,
   slippage 0, no borrow).

6. **Least ambiguous semantics** — direction-aware return formula is
   correct for directional trading. Path A's direction-ignoring formula
   produces wrong-sign PnL for shorts.

7. **Most deterministic** — both paths are deterministic; Path B uses
   frozen dataclass, Path A uses frozen Pydantic. Equal on this axis.

8. **Easiest migration** — Path B is already wired to tournament and
   promotion. Migrating the API poller to Path B is simpler than
   retrofitting Path A with all of Path B's features.

9. **Lowest risk to live/paper parity** — Path B is already the live
   path. Making paper use Path B ensures parity.

### Canonical entrypoint

```
quant_foundry.settlement.SettlementLedger.settle(
    prediction: dict | PredictionInput,
    prices: Sequence[PriceTick],
    benchmark_prices: Sequence[PriceTick] | None,
    cost_model: CostModel,
    now_ns: int,
    holding_days: int = 1,
) -> SettlementRecord
```

### Canonical orchestration

```
quant_foundry.settlement_sweep.SettlementSweep.sweep(now_ns)
```

The sweep reads from `ShadowLedger`, fetches prices via
`BarDataAdapter`, and settles via `SettlementLedger.settle()`. This
is the existing live path and will remain the canonical orchestration
entrypoint.

## Path A Retirement / Compatibility Strategy

### Decision: B — Thin compatibility wrapper, then retire

### Rationale

Retiring Path A immediately would break the `/models/{name}/outcomes`
API route, which reads from Path A's `SettlementStore` keyed by
`agent_id`. Retrofitting Path A with Path B semantics would duplicate
all settlement math and create a maintenance burden.

A thin compatibility wrapper avoids both problems:
- The API poller continues to accept `PredictionRow` inputs (agent_id
  keyed) but delegates settlement to Path B's `SettlementLedger`
- The `/models/{name}/outcomes` route reads from Path B's ledger via
  an adapter that maps `agent_id` → `model_id`
- No duplicate settlement math — Path B is the sole computation engine
- The wrapper is clearly marked as deprecated and has a removal target

### Wrapper design

```
settlements.compat.PathACompatAdapter
```

This adapter:
1. Accepts `PredictionRow` (agent_id, model_name, direction, confidence)
2. Maps `agent_id` → `model_id` via a configurable mapping function
3. Derives `p_up` from `confidence` (since `PredictionRow` lacks `p_up`)
4. Converts `PredictionRow` + price lookups into `PredictionInput` dict
5. Delegates to `SettlementLedger.settle()` with `cm-v1` cost model
6. Returns a `fincept_core.datasets.SettlementRecord`-shaped dict for
   API compatibility (the wrapper translates Path B's
   `quant_foundry.outcomes.SettlementRecord` into the shape
   `build_evidence_receipt` expects)

### Deprecation timeline

| Phase | Status of Path A | Status of wrapper |
|-------|-----------------|-------------------|
| Phase 1-3 | Active | Wrapper introduced |
| Phase 4-5 | Deprecated | Wrapper active, Path A math unused |
| Phase 6 | Removed | Wrapper removed, API reads Path B directly |

## Canonical Cost Model

### Decision: `cm-v1` (Path B's model)

### Cost model definition

```python
CostModel(
    version="cm-v1",
    fee_bps=10.0,           # round-trip exchange/broker fee
    spread_bps=5.0,         # modeled bid-ask spread (round-trip)
    slippage_bps=3.0,       # modeled market-impact / slippage (round-trip)
    borrow_bps_per_day=25.0, # financing/borrow cost per day (shorts only)
)
```

### Field documentation

| Field | Value | Unit | Applies to |
|-------|-------|------|------------|
| `fee_bps` | 10.0 | basis points | All trades (round-trip) |
| `spread_bps` | 5.0 | basis points | All trades (round-trip) |
| `slippage_bps` | 3.0 | basis points | All trades (round-trip) |
| `borrow_bps_per_day` | 25.0 | basis points/day | Short positions only |
| `cost_model_version` | `cm-v1` | string | Stored on every settled record |

### Where version is stored

The `cost_model_version` field is stored on every `SettlementRecord`
(both Path A's `fincept_core.datasets.SettlementRecord` and Path B's
`quant_foundry.outcomes.SettlementRecord`). The idempotency key is
`(prediction_id, cost_model_version)` — a new cost model version
appends a new record rather than mutating history.

### Future cost model migration

To introduce `cm-v2`:
1. Define a new `CostModel(version="cm-v2", ...)` instance
2. Pass it to `SettlementLedger.settle()` for new settlements
3. Old `cm-v1` records remain in the ledger (append-only)
4. Downstream consumers filter by `cost_model_version` if needed
5. The replay harness can verify `cm-v2` against `cm-v1` for regression

### Why not `v2.unified`

Defining a new `v2.unified` cost model would require:
- Choosing new fee/spread/slippage/borrow values (no evidence basis)
- Re-settling all existing predictions under the new model
- Updating all downstream consumers to filter by the new version

Using `cm-v1` avoids all of this — it is already in production, already
consumed by the tournament and promotion gate, and already has test
coverage. If a future audit shows `cm-v1` is too conservative or too
loose, a `cm-v2` can be introduced following the migration process above.

## Identity / Key Mapping

### Problem

| Path | Key field | Source |
|------|-----------|--------|
| Path A | `agent_id` | `PredictionRow.agent_id` (e.g. `gbm_predictor.v1`) |
| Path B | `model_id` | `ShadowPrediction.model_id` (e.g. `gbm_predictor-v1`) |

The two key spaces are not identical. `agent_id` is the agent process
identifier (e.g. `gbm_predictor.v1`); `model_id` is the model
identifier used by the tournament and registry (e.g.
`gbm_predictor-v1` or a registry-assigned ID).

### Decision: `model_id` is canonical

`model_id` is the canonical key because:
1. The tournament consumes `model_id`
2. The promotion gate consumes `model_id`
3. The model registry is keyed by `model_id`
4. `SettledComparisonInputProvider` filters by `model_id`

### Mapping design

The adapter provides a configurable `agent_id → model_id` mapping
function. The default mapping is:

```python
def default_agent_to_model_id(agent_id: str) -> str:
    """Map agent_id to model_id.

    Default: replace '.' with '-' (gbm_predictor.v1 → gbm_predictor-v1).
    This can be overridden with a custom mapping function for
    non-standard agent IDs.
    """
    return agent_id.replace(".", "-")
```

This mapping is used by:
1. The compatibility wrapper (`PathACompatAdapter`)
2. The rewritten API poller
3. The `/models/{name}/outcomes` route (reverse mapping: `model_id` →
   `agent_id` for API response compatibility)

### Reverse mapping for API responses

The `/models/{name}/outcomes` route currently returns `agent_id` in its
response. To preserve API compatibility, the route will:
1. Accept `agent_id` as a query parameter (as today)
2. Map it to `model_id` via the adapter
3. Read settlements from Path B's ledger by `model_id`
4. Return the response with `agent_id` preserved (reverse map
   `model_id` → `agent_id`)

### Migration risk

| Risk | Level | Mitigation |
|------|-------|------------|
| agent_id → model_id mapping is wrong for some agents | Medium | Configurable mapping function; default is conservative |
| Existing Path A settlements are keyed by agent_id | Low | One-time migration script reads old `data/settlements/*.jsonl` and re-writes to `data/quant-foundry/settlements/` with model_id |
| API clients depend on agent_id in response | Low | Reverse mapping preserves agent_id in response |

## Store Location

### Decision: `data/quant-foundry/settlements/` (Path B)

### Current state

| Path | Location | Layout |
|------|----------|--------|
| Path A | `data/settlements/` | `<agent_id>.jsonl` |
| Path B | `data/quant-foundry/settlements/` | `<model_id>.settlements.jsonl` |

### Canonical location

`data/quant-foundry/settlements/<model_id>.settlements.jsonl`

This is controlled by `$QUANT_FOUNDRY_SETTLEMENTS_DIR` (default
`data/quant-foundry/settlements`).

### Legacy read migration

A one-time migration script will:
1. Read all `data/settlements/<agent_id>.jsonl` files
2. For each record, map `agent_id` → `model_id`
3. Convert Path A's `SettlementRecord` shape to Path B's
   `SettlementRecord` shape (adding missing fields as None)
4. Append to `data/quant-foundry/settlements/<model_id>.settlements.jsonl`
   with `cost_model_version="v1.default.migrated"` to distinguish
   migrated records from natively settled ones

### After migration

- `data/settlements/` is kept as a backup (not deleted)
- The API poller writes only to `data/quant-foundry/settlements/`
- The `/models/{name}/outcomes` route reads only from
  `data/quant-foundry/settlements/`

## API Poller Rewrite Plan

### Current state

```
api.main lifespan
  → _poll_settlements_worker(interval)
    → settlements.worker.tick(predictions_dir, settlements_dir, market_data_source)
      → _build_settled_record(pred, close_t1, close_t2)  # Path A math
      → SettlementStore.append(record)                    # Path A store
```

### Target state

```
api.main lifespan
  → _poll_settlements_worker(interval)
    → PathACompatAdapter.settle_due_predictions(predictions_dir, now_ns)
      → For each due PredictionRow:
        1. Map agent_id → model_id
        2. Derive p_up from confidence
        3. Fetch prices via BarDataAdapter (existing market_data_bridge)
        4. Fetch benchmark prices via BarDataAdapter
        5. Delegate to SettlementLedger.settle()  # Path B math
        6. Return SettlementRecord (Path B shape)
```

### What changes

| Component | Before | After |
|-----------|--------|-------|
| Settlement math | `_build_settled_record` (Path A) | `SettlementLedger.settle` (Path B) |
| Cost model | `v1.default` (5/3/0 bps) | `cm-v1` (10/5/3/25 bps) |
| Return formula | `(t2/t1)-1` (no direction) | Direction-aware |
| Brier prob_up | `(direction+1)/2` | `p_up` from prediction |
| Abnormal return | Not computed | Computed (needs benchmark) |
| Calibration bucket | Not computed | Computed |
| Store | `data/settlements/<agent_id>.jsonl` | `data/quant-foundry/settlements/<model_id>.settlements.jsonl` |
| Benchmark prices | Not fetched | Fetched via `BarDataAdapter.get_benchmark_prices` |

### What stays the same

| Component | Status |
|-----------|--------|
| `SETTLEMENTS_WORKER_POLL_S` env var | Preserved (poll interval) |
| `PREDICTIONS_DIR` env var | Preserved (prediction log location) |
| `/models/{name}/outcomes` response shape | Preserved (via adapter) |
| `build_evidence_receipt` function | Updated to read from Path B store |
| `PredictionLog` | Unchanged (still the prediction source) |

### API response compatibility

The `/models/{name}/outcomes` route will be updated to:

1. Read from `SettlementLedger` (Path B) instead of `SettlementStore` (Path A)
2. Filter by `model_id` (mapped from `agent_id` query param)
3. Convert Path B's `SettlementRecord` to the API response shape via
   an updated `build_evidence_receipt` that includes the new fields
   (`abnormal_return`, `calibration_bucket`)

The response will be **backward compatible** — existing fields keep
their names and types. New fields (`abnormal_return`,
`calibration_bucket`) are added as optional (null when not available).

## Canonical Output Schema

### Primary record: `quant_foundry.outcomes.SettlementRecord`

```python
@dataclass(frozen=True)
class SettlementRecord:
    prediction_id: str
    model_id: str                    # canonical key
    symbol: str
    ts_event: int                    # decision time (ns)
    horizon_ns: int
    status: SettlementStatus         # "pending_time" | "pending_data" | "settled"
    settled_at_ns: int | None
    realized_return_gross: float | None   # direction-aware
    realized_return_net: float | None     # gross - costs
    abnormal_return: float | None         # realized - benchmark
    brier: float | None                   # (p_up - actual_up)^2
    calibration_bucket: str | None        # "0.0-0.2" | "0.2-0.4" | ...
    cost_model_version: str               # "cm-v1"
    decision_window_start: int            # ts_event
    decision_window_end: int              # ts_event + horizon_ns
```

### API response shape (updated `build_evidence_receipt`)

```json
{
  "prediction_id": "...",
  "agent_id": "...",              // preserved for API compat
  "model_name": "...",
  "ts_event": 123,
  "horizon_ns": 900000000000,
  "symbol": "BTC-USD",
  "direction": 0.5,
  "confidence": 0.5,
  "settlement_status": "settled",
  "realized_return_gross": 0.01,
  "realized_return_net": 0.0002,
  "settled_at_ns": 456,
  "brier_component": 0.25,
  "abnormal_return": 0.005,       // NEW (null if no benchmark)
  "calibration_bucket": "0.4-0.6",// NEW (null if not settled)
  "cost_model_version": "cm-v1"   // NEW
}
```

### Cost model fields on settled records

The cost model parameters are not stored on individual `SettlementRecord`
instances in Path B (they are stored on the `CostModel` object passed to
`settle()`). To make the cost model auditable per-record, the design
adds the following fields to the JSONL persistence layer (not to the
dataclass itself — they are derived from the `cost_model_version`):

| Field | Source | Stored in JSONL? |
|-------|--------|-------------------|
| `cost_model_version` | `SettlementRecord.cost_model_version` | Yes |
| `cost_fee_bps` | `CostModel.fee_bps` | Derived from version |
| `cost_spread_bps` | `CostModel.spread_bps` | Derived from version |
| `cost_slippage_bps` | `CostModel.slippage_bps` | Derived from version |
| `borrow_bps_per_day` | `CostModel.borrow_bps_per_day` | Derived from version |

A future enhancement can add these fields directly to the record if
per-record cost auditing is needed. For now, the `cost_model_version`
is the audit key — the cost parameters are looked up from the version.

### Legacy agent_id preservation

For API compatibility, the adapter adds `legacy_agent_id` to the
metadata when a settlement is created via the compatibility wrapper:

```json
{
  "metadata": {
    "legacy_agent_id": "gbm_predictor.v1",
    "migration_source": "path_a_compat"
  }
}
```

## Implementation Phases

### Phase 1: Compatibility adapter

**Goal:** Create `PathACompatAdapter` that accepts Path A inputs and
delegates to Path B settlement.

**Changes:**
- New: `services/settlements/src/settlements/compat.py`
  - `PathACompatAdapter` class
  - `default_agent_to_model_id()` function
  - `derive_p_up_from_confidence()` helper
- No changes to existing code

**Tests:**
- Unit tests for the adapter (all 8 fixture scenarios)
- Mapping tests (agent_id → model_id)
- p_up derivation tests

**Verification:** Adapter unit tests pass; no existing tests broken.

### Phase 2: Rewire API poller

**Goal:** Replace Path A math in the poller with the adapter.

**Changes:**
- Modify: `services/api/src/api/settlements_poller.py`
  - Replace `settlements.worker.tick` call with `PathACompatAdapter`
  - Add benchmark price fetching via `BarDataAdapter`
  - Write to `SettlementLedger` (Path B store) instead of `SettlementStore`
- Modify: `services/api/src/api/main.py`
  - Update poller task name / logging

**Feature flag:** `SETTLEMENTS_USE_PATH_B=1` env var
- `0` (default during Phase 2): Use Path A (current behavior)
- `1`: Use Path B via adapter (new behavior)

**Tests:**
- Integration test: poller with `SETTLEMENTS_USE_PATH_B=1` produces
  Path B records
- Integration test: poller with `SETTLEMENTS_USE_PATH_B=0` still
  produces Path A records (backward compat)

**Verification:** Both modes pass; replay harness shows Path B semantics
when flag is on.

### Phase 3: Preserve API response shape

**Goal:** Update `/models/{name}/outcomes` to read from Path B store
while preserving the API response shape.

**Changes:**
- Modify: `services/api/src/api/routes/models.py`
  - `_get_settlement_store()` → return `SettlementLedger` when
    `SETTLEMENTS_USE_PATH_B=1`, else `SettlementStore`
  - `get_outcomes()` → read from Path B ledger by model_id (mapped
    from agent_id) when flag is on
- Modify: `libs/fincept-core/src/fincept_core/datasets/__init__.py`
  - `build_evidence_receipt()` → accept Path B's `SettlementRecord`
    shape (or a union type); add `abnormal_return`,
    `calibration_bucket`, `cost_model_version` to response

**Tests:**
- API test: `/models/{name}/outcomes` with Path B store returns
  same fields as before plus new optional fields
- API test: response shape is backward compatible

**Verification:** API tests pass; replay harness confirms same
semantics.

### Phase 4: Replay harness verification

**Goal:** Prove that the API display route now matches Path B semantics.

**Changes:**
- Run: `scripts/c6_settlement_replay.py` with `SETTLEMENTS_USE_PATH_B=1`
- New: `reports/c6-settlement-replay/<sha>/post_migration_replay.json`
- Compare: post-migration replay results against Task 10's
  pre-migration results

**Expected result:** All fixtures that previously diverged on
`realized_return_gross` (shorts), `brier_component`,
`abnormal_return`, `calibration_bucket` now MATCH between the API
display route and Path B.

**Verification:** 0 SEMANTIC_DIFFERENCE, 0 MISSING_FIELD divergences
in post-migration replay.

### Phase 5: Deprecate Path A math

**Goal:** Mark Path A settlement math as deprecated.

**Changes:**
- Modify: `services/settlements/src/settlements/worker.py`
  - Add `DeprecationWarning` to `tick()` and `tick_sync()`
  - Add module-level deprecation notice in docstring
- Modify: `services/api/src/api/settlements_poller.py`
  - Default `SETTLEMENTS_USE_PATH_B` to `1` (Path B is now default)
  - `SETTLEMENTS_USE_PATH_B=0` still works but emits deprecation warning

**Tests:**
- Deprecation warning test: `tick()` emits `DeprecationWarning`
- Existing Path A tests still pass (math unchanged, just deprecated)

**Verification:** All tests pass; deprecation warnings are emitted
for Path A usage.

### Phase 6: Remove Path A math

**Goal:** Remove Path A settlement math entirely.

**Changes:**
- Remove: `services/settlements/src/settlements/worker.py`
  (`_build_settled_record`, `_build_pending_data_record`, `tick`,
  `tick_sync`)
- Remove: `services/settlements/src/settlements/market_data_bridge.py`
  (no longer needed — adapter uses `BarDataAdapter` directly)
- Remove: `SETTLEMENTS_USE_PATH_B` feature flag (Path B is the only path)
- Remove: `SETTLEMENTS_WORKER_POLL_S` env var (replaced by gateway
  sweep interval if needed, or kept for the adapter-driven poller)
- Keep: `services/settlements/src/settlements/compat.py` (still
  needed for API compatibility)

**Tests:**
- Remove: `services/settlements/tests/test_worker.py` (Path A tests)
- Keep: `services/settlements/tests/test_market_data_bridge.py` if
  the bridge is still used by the adapter; otherwise remove
- All Path B tests still pass

**Verification:** All tests pass; no references to Path A math remain.

## Test Plan

### Required tests for implementation

| # | Test | Path | Covers |
|---|------|------|--------|
| 1 | `test_compat_winning_long` | compat | Long, price +5%, direction-aware return |
| 2 | `test_compat_losing_long` | compat | Long, price -5%, direction-aware return |
| 3 | `test_compat_winning_short` | compat | Short, price -5%, direction-aware return (sign check) |
| 4 | `test_compat_losing_short` | compat | Short, price +5%, direction-aware return (sign check) |
| 5 | `test_compat_flat` | compat | No price movement, net = -costs |
| 6 | `test_compat_missing_prices` | compat | No price data → pending_data |
| 7 | `test_compat_partial_prices` | compat | Entry only → pending_data |
| 8 | `test_compat_high_confidence` | compat | Confidence 0.9, Brier uses p_up not direction |
| 9 | `test_cost_model_versioning` | compat | Same prediction + different cost model → two records |
| 10 | `test_agent_to_model_id_mapping` | compat | agent_id → model_id default mapping |
| 11 | `test_agent_to_model_id_custom` | compat | Custom mapping function |
| 12 | `test_legacy_api_compatibility` | API | `/models/{name}/outcomes` response shape preserved |
| 13 | `test_api_outcomes_includes_abnormal_return` | API | New field in response |
| 14 | `test_api_outcomes_includes_calibration_bucket` | API | New field in response |
| 15 | `test_tournament_settlement_provider` | quant_foundry | `SettledComparisonInputProvider` reads from Path B (existing test, verify still passes) |
| 16 | `test_promotion_comparison_provider` | quant_foundry | `compare_champion_challenger` with Path B records (existing test, verify still passes) |
| 17 | `test_replay_harness_post_migration` | replay | Replay harness shows 0 SEMANTIC_DIFFERENCE after migration |
| 18 | `test_brier_uses_p_up_not_direction` | compat | Brier score uses prediction's p_up, not (direction+1)/2 |
| 19 | `test_borrow_cost_for_shorts` | compat | Short positions pay borrow_bps_per_day * holding_days |
| 20 | `test_feature_flag_path_b_default` | poller | `SETTLEMENTS_USE_PATH_B=1` produces Path B records |

### Existing tests to verify (not modify)

| Test file | Tests | Concern |
|-----------|-------|---------|
| `services/settlements/tests/test_worker.py` | 14 | Path A math (deprecate in Phase 5, remove in Phase 6) |
| `services/settlements/tests/test_market_data_bridge.py` | 8 | Bridge (keep if adapter uses it) |
| `services/quant_foundry/tests/test_settlement_provider.py` | 9 | Path B provider (must still pass) |
| `services/quant_foundry/tests/test_shadow_tournament.py` | 96 | Tournament (must still pass) |
| `services/quant_foundry/tests/test_auto_tournament.py` | 9 | Auto tournament (must still pass) |
| `services/quant_foundry/tests/test_champion_challenger.py` | 20 | Champion/challenger (must still pass) |

### Test coverage requirements

- **Shorts must be tested** — the direction-aware return formula is
  the most critical fix. Tests 3 and 4 verify that winning shorts get
  positive gross and losing shorts get negative gross.
- **Brier scoring must be tested** — Test 18 verifies that Brier uses
  `p_up` from the prediction, not `(direction+1)/2`. This is the
  second most critical fix.
- **Cost model versioning must be tested** — Test 9 verifies that
  re-settling with a different cost model version appends a new record
  rather than overwriting.

## Replay Verification Plan

### Pre-migration baseline

Task 10's replay results are the pre-migration baseline:
- `reports/c6-settlement-replay/5cfb6cfa/replay_results.json`
- 58 divergences (32 EXPECTED_MODE_DIFFERENCE, 12 MISSING_FIELD,
  9 SEMANTIC_DIFFERENCE, 5 ROUNDING_ONLY)

### Post-migration verification

After Phase 4, run the replay harness again:

```bash
SETTLEMENTS_USE_PATH_B=1 uv run python scripts/c6_settlement_replay.py
```

**Expected results:**

| Classification | Pre-migration | Post-migration expected |
|----------------|---------------|------------------------|
| MATCH | 22 | 80 |
| ROUNDING_ONLY | 5 | 0 |
| EXPECTED_MODE_DIFFERENCE | 32 | 0 |
| MISSING_FIELD | 12 | 0 |
| SEMANTIC_DIFFERENCE | 9 | 0 |
| BUG_LIKELY | 0 | 0 |
| REVIEW_REQUIRED | 0 | 0 |
| **Total divergences** | **58** | **0** |

All 80 comparisons should MATCH because both the API display route
and the tournament/promotion path will use the same settlement math
(Path B) with the same cost model (`cm-v1`).

### Regression check

The post-migration replay results will be saved to:
```
reports/c6-settlement-replay/<post_migration_sha>/replay_results.json
```

If any divergences remain, they must be classified and resolved before
Phase 5 (deprecation).

## Backward Compatibility Risks

| Risk | Level | Mitigation |
|------|-------|------------|
| API response shape changes | Low | New fields are optional (null when unavailable); existing fields preserved |
| Cost model change (v1.default → cm-v1) | Medium | Feature flag allows gradual rollout; old records preserved in ledger |
| agent_id → model_id mapping errors | Medium | Configurable mapping function; default is conservative; one-time migration script |
| Existing Path A settlements not readable | Low | One-time migration script converts old records; old store kept as backup |
| Tournament/promotion behavior changes | Low | Path B is already the tournament/promotion path — no change for them |
| Poller interval changes | None | `SETTLEMENTS_WORKER_POLL_S` env var preserved |
| Benchmark price fetch adds latency | Low | Benchmark fetch is best-effort; `abnormal_return` is None if benchmark unavailable |
| Borrow cost makes shorts more expensive | Medium | This is intentional — cm-v1 is more realistic; documented in cost model |

## Rollback Plan

### Feature flag

`SETTLEMENTS_USE_PATH_B` env var:
- `0`: Use Path A (pre-migration behavior)
- `1`: Use Path B via adapter (post-migration behavior)

Setting `SETTLEMENTS_USE_PATH_B=0` instantly reverts to Path A math
without code changes. This is the primary rollback mechanism during
Phases 2-5.

### Legacy poller fallback

If the adapter fails or produces unexpected results:

1. Set `SETTLEMENTS_USE_PATH_B=0`
2. The poller falls back to `settlements.worker.tick` (Path A)
3. The API reads from `SettlementStore` (Path A store)
4. Old Path A settlements remain in `data/settlements/` (not deleted)

### Replay comparison before/after

Before each phase transition, run the replay harness:

1. Run with `SETTLEMENTS_USE_PATH_B=0` (baseline)
2. Run with `SETTLEMENTS_USE_PATH_B=1` (new)
3. Compare results
4. If new results show regressions (new divergences not present in
   baseline), do not proceed to the next phase

### Data rollback

If the one-time migration script corrupts data:

1. `data/quant-foundry/settlements/` contains the corrupted files
2. `data/settlements/` contains the original Path A files (untouched)
3. Delete `data/quant-foundry/settlements/<model_id>.settlements.jsonl`
   files that were created by the migration
4. Re-run the migration script after fixing the bug

### Code rollback

If the code changes break existing tests:

1. `git revert` the phase commit
2. The feature flag defaults to `0` (Path A)
3. No data migration is needed (Phase 1 adds code only, no data changes)

## Open Questions

| # | Question | Status | Default if unresolved |
|---|----------|--------|----------------------|
| 1 | Should `PredictionRow` gain a `p_up` field? | Open | No — the adapter derives `p_up` from `confidence` as a fallback. Adding `p_up` to `PredictionRow` is a separate enhancement. |
| 2 | Should the one-time migration script re-settle old predictions with `cm-v1` or keep `v1.default`? | Open | Keep `v1.default` for migrated records (use `cost_model_version="v1.default.migrated"`). New settlements use `cm-v1`. |
| 3 | Should `SETTLEMENTS_WORKER_POLL_S` be renamed? | Open | No — preserve the name for backward compatibility. |
| 4 | Should the adapter fetch benchmark prices for every prediction? | Open | Yes, but best-effort — `abnormal_return` is None if benchmark unavailable. |
| 5 | Should `holding_days` be configurable per prediction? | Open | No — default to 1 day (matching current Path B behavior). |
| 6 | Should the migration script be idempotent? | Open | Yes — the script checks for existing migrated records and skips them. |

## Safe to Proceed to Task 13: yes

All stop conditions are clear:
- Task 11 report present ✓
- Canonical path decision matches Task 11 recommendation ✓
- Path A compatibility strategy is clear (wrapper → retire) ✓
- Cost model decision is clear (`cm-v1`) ✓
- agent_id/model_id mapping is resolved (configurable adapter) ✓
- Store location is resolved (`data/quant-foundry/settlements/`) ✓
- API poller rewrite plan is documented ✓
- Test plan covers shorts (tests 3, 4) and Brier scoring (test 18) ✓
- Rollback plan is documented (feature flag + data backup) ✓
- No secrets in report ✓
- ruff/mypy pass ✓

**Proceed to Task 13 — C6 settlement unification implementation.**
