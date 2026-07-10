# C6 Merge Report

## PR merged

PR #5: https://github.com/AIRYDER/fincept/pull/5
Title: `feat(c6): unify settlement on canonical ledger`
Branch: `feature/c6-settlement-unification` → `main`
Merged at: 2026-07-10T13:31:48Z
Merge method: merge commit

## Merge commit

`984c353fd586e45171affd70fc4ec701c327aa06`

## Local main HEAD

`984c353fd586e45171affd70fc4ec701c327aa06`

## Origin/main HEAD

`984c353fd586e45171affd70fc4ec701c327aa06`

Local main matches origin/main.

## Post-merge focused validation

| Suite | Tests | Result |
|-------|-------|--------|
| `services/settlements/tests` | 56 | passed |
| `services/api/tests/test_models_outcomes.py` | 13 | passed |
| `services/api/tests/test_settlements_poller.py` | 15 | passed |
| `services/quant_foundry/tests/test_settlement_provider.py` | 9 | passed |
| `services/quant_foundry/tests/test_shadow_tournament.py` | 98 | passed |
| `services/quant_foundry/tests/test_auto_tournament.py` + `test_champion_challenger.py` | 27 | passed |
| `libs/fincept-core/tests -k "settlement or evidence"` | 35 | passed |

Total focused: 124 passed, 452 deselected.

## Post-merge full validation

| Check | Result |
|-------|--------|
| `ruff format --check .` | 831 files OK |
| `ruff check libs services` | all passed |
| `mypy libs services` | 369 files, no issues |
| `pytest services/quant_foundry` | 4367 passed, 230 skipped |
| `pytest services/api/tests` | 494 passed |
| `pytest services/settlements/tests` | 56 passed |

Total full: 4917 passed, 230 skipped.

## Post-merge replay result

Script: `scripts/c6_post_unification_replay.py`
Results: `reports/c6-settlement-replay/984c353f/post-unification/`

| Metric | Value |
|--------|-------|
| Divergences | 0 |
| Comparisons matched | 80/80 |
| winning_short gross | +0.05 |
| winning_short net | +0.0457 (positive after costs) |
| losing_short gross | -0.05 |
| losing_short net | -0.0543 (negative after costs) |
| Brier (short conf=0.65) | 0.4225 (uses `p_up`, not direction) |
| `abnormal_return` | populated on all settled records |
| `calibration_bucket` | populated on all settled records |
| `cost_model_version` | `cm-v1` on every record |

## Main CI status

Run ID: `29096394936`
Status: **success** (all 8 jobs green)

| Job | Status | Duration |
|-----|--------|----------|
| Verification receipt | ✓ | 1m1s |
| Alembic downgrade/upgrade verify (C7 gate) | ✓ | 39s |
| Secret scan (gitleaks) | ✓ | 8s |
| Lockfile sync check | ✓ | 18s |
| Python tests + coverage | ✓ | 3m43s |
| Python lint + typecheck | ✓ | 31s |
| Startup safety matrix | ✓ | 16s |
| JS lint + typecheck + test | ✓ | 1m3s |

## Rollback flag status

`SETTLEMENTS_USE_PATH_B=0` tested and working:
- All 13 outcomes tests pass with rollback flag
- All 15 poller tests pass with rollback flag (legacy Path A path)
- Legacy `SettlementStore` still functional when flag is off

## Remaining risks

1. **Path A → Path B migration at runtime**: The `SETTLEMENTS_USE_PATH_B=1` flag
   is default. Existing deployments that rely on Path A's `SettlementStore` will
   silently switch to Path B's `SettlementLedger` on next deploy. The rollback
   flag (`SETTLEMENTS_USE_PATH_B=0`) is tested and working, but operators must
   be aware of the flag before deploying.

2. **Historical settlement data**: Existing Path A settlement files
   (`SettlementStore` JSONL) are not migrated to Path B's `SettlementLedger`
   format. The outcomes route reads from Path B when the flag is on, so
   historical settlements will not appear in outcomes until migrated or until
   the flag is set to `0` for legacy queries.

3. **`agent_id` → `model_id` mapping**: The `default_agent_to_model_id`
   function in `compat.py` uses a deterministic derivation
   (`agent_id.replace(".", "_")`). If production agents use IDs that don't
   map cleanly to model IDs, the mapping table in `compat.py` must be
   updated.

## Branch cleanup

- Local branch `feature/c6-settlement-unification`: deleted (`was 5469ce9d`)
- Remote branch `feature/c6-settlement-unification`: deleted

## Final verdict

C6_MERGED_AND_GREEN
