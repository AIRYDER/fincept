# C6 Task 10 — Settlement Replay Harness Summary

## Main SHA

`5cfb6cfaf75bae5bb67fad298fc1716217682a9d`

## Task 9 verification

Task 9 (`C8_VERIFICATION_GATE.md`) returned `APPROVED_FOR_C6`.
Verified at `reports/runpod-test-runs/5cfb6cfa/c8-live-proof-ladder/C8_VERIFICATION_GATE.md`.

## Settlement paths found

Two prediction settlement paths coexist:

1. **Path A (new/fincept_core)** — `settlements.worker.tick` → `fincept_core.datasets.SettlementStore`
   - Cost model: `v1.default` (fee 5 bps, spread 3 bps, slippage 0 bps)
   - Key: `agent_id`, store: `data/settlements/<agent_id>.jsonl`
   - Used by: API poller (paper mode), `/models/{name}/outcomes` route

2. **Path B (old/quant_foundry)** — `quant_foundry.settlement.SettlementLedger.settle`
   - Cost model: `cm-v1` (fee 10 bps, spread 5 bps, slippage 3 bps, borrow 25 bps/day)
   - Key: `model_id`, store: `data/quant-foundry/settlements/<model_id>.settlements.jsonl`
   - Used by: tournament, promotion gate, live settlement sweep

A third module (`fincept_core.portfolio.apply_fill_to_position`) handles
position-level PnL from fills but is not a prediction settlement path.

## Replay harness location

`scripts/c6_settlement_replay.py`

## Fixtures created

8 deterministic fixtures at `reports/c6-settlement-replay/5cfb6cfa/fixtures.json`:

| # | Name | Description |
|---|------|-------------|
| 1 | winning_long | Long, price +5% |
| 2 | losing_long | Long, price -5% |
| 3 | winning_short | Short, price -5% |
| 4 | flat | No price movement |
| 5 | missing_prices | No price data (pending_data) |
| 6 | partial_prices_entry_only | Entry only, exit missing (pending_data) |
| 7 | high_confidence_win | Long, confidence 0.9, price +10% |
| 8 | losing_short | Short, price +5% |

## Paths replayed

2 paths × 8 fixtures = 16 settlement operations.

## Replay results path

`reports/c6-settlement-replay/5cfb6cfa/`

Files:
- `settlement_path_inventory.md` — full path inventory
- `fixtures.json` — deterministic fixture set
- `replay_results.json` — raw replay outputs (both paths per fixture)
- `replay_results_normalized.csv` — normalized CSV for diffing
- `divergence_report.md` — detailed divergence analysis
- `divergence_report.json` — machine-readable divergences
- `summary.md` — this file

## Divergence summary

| Classification | Count |
|----------------|-------|
| MATCH | 22 |
| ROUNDING_ONLY | 5 |
| EXPECTED_MODE_DIFFERENCE | 32 |
| MISSING_FIELD | 12 |
| SEMANTIC_DIFFERENCE | 9 |
| BUG_LIKELY | 0 |
| REVIEW_REQUIRED | 0 |
| **Total divergences** | **58 / 80 comparisons** |

## Matches

22 of 80 field comparisons matched exactly. These were primarily:
- `status` field (both paths produce same status for same inputs)
- `realized_return_gross` on long predictions (formula agrees when direction=+1)
- `settled_at_ns` (both use the same `now_ns` input)
- All fields on pending_data fixtures (both paths produce None)

## Rounding-only differences

5 occurrences — `realized_return_net` on long/flat fixtures and one
`brier_component` on high_confidence_win. The net return delta is
exactly 10 bps (the cost model difference: 18 bps - 8 bps = 10 bps =
0.001). Not a bug.

## Expected mode differences

32 occurrences — `cost_model_version`, `cost_fee_bps`,
`cost_spread_bps`, `cost_slippage_bps` differ on every fixture because
the two paths use different cost models by design. Documented in
`settlements_poller.py` lines 13-20.

## Review-required differences

0 occurrences. All divergences were classifiable.

## Likely bugs

0 occurrences classified as BUG_LIKELY. However, two SEMANTIC_DIFFERENCE
categories represent design flaws in Path A that should be treated as
bugs if Path A is to be considered canonical:

1. **Path A ignores direction in gross return** — short predictions
   get the opposite sign. A winning short shows as negative gross.

2. **Path A derives prob_up from direction** — `prob_up = (direction+1)/2`
   makes Brier score degenerate (always 0.0 or 1.0). This makes Path A
   useless for calibration analysis.

Both are documented as MVP simplifications, not accidental bugs.

## Tests added

0 new tests. The replay harness (`scripts/c6_settlement_replay.py`) is
a characterization script, not a test suite. It does not modify
production behavior.

## Verification results

| Check | Result |
|-------|--------|
| `ruff format --check .` | 833 files already formatted |
| `ruff check libs services` | All checks passed |
| `mypy services/quant_foundry/src` | Success: no issues found in 162 source files |
| `pytest services/settlements -q` | 22 passed |
| `pytest services/quant_foundry -q` | 4367 passed, 230 skipped |
| Behavior changes | None (audit/replay only) |
| Secret literals in reports | None |

## Recommended canonical path candidate

**Path B (`quant_foundry.SettlementLedger`)** is the recommended
canonical path candidate:

1. **Direction-aware return** — correctly handles long and short predictions
2. **Richer metrics** — computes abnormal_return, calibration_bucket, brier with proper p_up
3. **Borrow cost** — models financing cost for short positions
4. **Already used by tournament and promotion** — the downstream consumers already depend on Path B
5. **More conservative cost model** — cm-v1 (18-43 bps) is more realistic than v1.default (8 bps)

Path A should be retired or brought up to Path B's feature parity
during C6 unification. Specifically:
- Add `p_up` to `PredictionRow` so Path A can compute proper Brier scores
- Add direction-aware return formula to Path A
- Add benchmark prices to Path A for abnormal_return
- Add calibration_bucket to Path A
- Unify cost model (choose cm-v1 or a new unified model)
- Unify key field (agent_id vs model_id — need a mapping)

## Safe to proceed to Task 11: yes

All stop conditions are clear:
- Task 9 approved C6 ✓
- Settlement paths identified ✓
- Fixtures ran through both paths ✓
- Outputs normalized ✓
- No production behavior changed ✓
- No secrets in reports ✓
- ruff/mypy/pytest all pass ✓

**Proceed to Task 11 — C6 settlement divergence report.**
