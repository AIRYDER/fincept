# C6 PR Gate Report

## Branch

`feature/c6-settlement-unification`

Rebased onto `origin/main` (`5cfb6cfa`) to exclude two unrelated C8 docs commits
that were on the original branch.  Four C6-only commits remain.

## Commit SHA

`b7882d3536f3d44deb2cd40c5e714d7233f38aba` (short: `b7882d35`)

## PR URL

https://github.com/AIRYDER/fincept/pull/5

## Diff scope

29 files changed, +7179/-75.

| Category | Files |
|----------|-------|
| `services/settlements/` | `compat.py`, `__init__.py`, `test_compat.py` |
| `services/api/` settlement/outcomes | `settlements_poller.py`, `routes/models.py`, `test_models_outcomes.py`, `test_settlements_poller.py`, `test_golden_e2e_smoke.py` |
| `libs/fincept-core/` evidence receipt | `datasets/__init__.py` |
| `scripts/c6_*` | `c6_post_unification_replay.py`, `c6_settlement_replay.py` |
| `reports/c6-settlement-replay/` | design doc, divergence report, replay results, implementation report, post-unification results |

No RunPod/C8 files. No unrelated implementation files. Diff is C6-only.

## Focused verification

| Suite | Tests | Result |
|-------|-------|--------|
| `services/settlements/tests` | 56 | passed |
| `services/api/tests/test_models_outcomes.py` | 13 | passed |
| `services/quant_foundry/tests/test_settlement_provider.py` | 9 | passed |
| `services/quant_foundry/tests/test_shadow_tournament.py` | 98 | passed |
| `services/quant_foundry/tests/test_auto_tournament.py` + `test_champion_challenger.py` | 27 | passed |
| `libs/fincept-core/tests -k "settlement or evidence"` | 35 | passed |
| Rollback flag (`SETTLEMENTS_USE_PATH_B=0`) outcomes | 13 | passed |
| Targeted claims (shorts, Brier, abnormal, calibration, cm-v1, borrow, idempotency, mapping) | 14 | passed |

Claims verified:
- short PnL is direction-aware (winning short positive, losing short negative)
- Brier uses `p_up`/confidence, not `(direction+1)/2`
- `abnormal_return` populated when benchmark available
- `calibration_bucket` populated for all settled records
- `cm-v1` cost model version on every record
- legacy outcomes API preserved (13 tests pass with `SETTLEMENTS_USE_PATH_B=0`)
- tournament provider, shadow tournament, champion/challenger all pass
- rollback flag `SETTLEMENTS_USE_PATH_B=0` tested and working

## Full verification

| Check | Result |
|-------|--------|
| `ruff format --check .` | 831 files OK |
| `ruff check libs services` | all passed |
| `mypy libs services` (CI scope) | 369 files, no issues |
| `mypy services/quant_foundry/src` | 162 files, no issues |
| `mypy services/settlements/src` | 4 files, no issues |
| `mypy libs/fincept-core/src` | 27 files, no issues |
| `pytest services/quant_foundry` | 4367 passed, 230 skipped |
| `pytest services/api/tests` | 494 passed |
| `pytest services/settlements/tests` | 56 passed |
| `pytest libs/fincept-core/tests -k "settlement or evidence"` | 35 passed |

## Replay verification

Script: `scripts/c6_post_unification_replay.py`
Results: `reports/c6-settlement-replay/aabeaaa6/post-unification/`

| Metric | Value |
|--------|-------|
| Divergences | 0 |
| Comparisons matched | 80/80 |
| winning_short gross | +0.05 |
| winning_short net | +0.0457 (positive after costs) |
| losing_short gross | -0.05 |
| losing_short net | -0.0543 (negative after costs) |
| Brier (conf=0.7) | 0.09 (uses `p_up`, not direction) |
| Brier (conf=0.9) | 0.01 |
| Brier (short conf=0.65) | 0.4225 |
| `abnormal_return` | populated on all settled records |
| `calibration_bucket` | populated on all settled records |
| `cost_model_version` | `cm-v1` on every record |

## Secret scan

| Pattern | Matches in C6 diff |
|---------|-------------------|
| `rps_[A-Za-z0-9]{20,}` | 0 |
| `RUNPOD_API_KEY=` | 0 |
| `S3_SECRET`/`S3_ACCESS` | 0 |
| `Authorization:` | 0 |
| `Bearer ` | 0 |

Gitleaks CI job: passed (6s).

No real credentials in the diff.

## PR CI status

Run ID: `29094998995`
Status: **success** (all 9 jobs green)

| Job | Status | Duration |
|-----|--------|----------|
| Secret scan (gitleaks) | ✓ | 6s |
| Python tests + coverage | ✓ | 2m44s |
| Verification receipt | ✓ | 1m10s |
| Python lint + typecheck (ruff + mypy) | ✓ | 36s |
| Startup safety matrix | ✓ | 15s |
| JS lint + typecheck + test | ✓ | 1m16s |
| Alembic downgrade/upgrade verify (C7 gate) | ✓ | 46s |
| Lockfile sync check | ✓ | 23s |

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

4. **CI cache warnings**: GitHub Actions cache service had transient 400
   errors during both CI runs. These are infrastructure noise, not code
   issues — all jobs completed successfully despite the cache warnings.

## Safe to merge C6: yes

All gate criteria met:
- branch diff is C6-only ✓
- focused tests pass ✓
- full verification passes ✓
- post-unification replay shows 0 divergences ✓
- secret scan is clean ✓
- PR CI is green ✓
- rollback flag exists and is tested ✓
- legacy API compatibility is preserved ✓
