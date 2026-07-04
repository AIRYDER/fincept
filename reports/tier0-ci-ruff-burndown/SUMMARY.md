# Ruff CI Burn-Down — Summary

## Task
- **Task ID:** task-mr6i5cl5-7860eff8
- **Agent:** Builder 4
- **Branch:** `tier0/ruff-burndown` (created from `tier0/metric-sanity`)
- **Ruff version:** 0.15.12
- **Date:** 2025-01-XX

## Before / After Error Counts

| Metric              | Before  | After   | Delta   |
|---------------------|---------|---------|---------|
| Total errors        | 1845    | 880     | -965    |
| Auto-fixable ([*])  | 955     | 1       | -954    |
| Unsafe-fixable ([-])| 245     | 248     | +3      |

**Reduction: 965 errors (52.3% burn-down)**

## What Was Done

1. Captured baseline: `ruff check .` → 1845 errors (955 auto-fixable).
2. Ran safe auto-fix: `ruff check . --fix` → 1014 errors fixed.
3. Ran formatter: `ruff format .` → 272 files reformatted.
4. Ran `ruff check . --fix` a second time → 1 additional fix (RUF100 unused-noqa).
5. Ran `ruff format .` again → no changes.
6. Captured after state: `ruff check .` → 880 errors.
7. Verified no syntax errors via `python -m compileall` on all 289 changed .py files.
8. Reverted 1 UP017 fix (datetime.UTC) that would break on Python 3.10 (see REMAINING_LINT_DEBT.md).

## Rules Auto-Fixed (safe fixes only)

| Rule     | Description                  | Before Count | Fixed |
|----------|------------------------------|--------------|-------|
| I001     | unsorted-imports             | 208          | 208   |
| RUF102   | invalid-rule-code            | 157          | 157   |
| UP017    | datetime-timezone-utc        | 104          | 103*  |
| F541     | f-string-missing-placeholders| 74           | 74    |
| UP037    | quoted-annotation            | 68           | 68    |
| RUF100   | unused-noqa                  | 48           | 48    |
| RUF022   | unsorted-dunder-all          | 41           | 41    |
| UP015    | redundant-open-modes         | 10           | 10    |
| UP035    | deprecated-import            | 10           | 10    |
| UP012    | unnecessary-encode-utf8      | 6            | 6     |
| SIM114   | if-with-same-arms            | 2            | 2     |
| B009     | get-attr-with-constant       | 1            | 1     |
| RUF010   | explicit-f-string-type-conversion | 1      | 1     |
| SIM300   | yoda-conditions              | 1            | 1     |
| UP024    | os-error-alias               | 1            | 1     |
| (other)  | various small fixes          | —            | ~191  |

*UP017: 103 fixed, 1 reverted (datetime.UTC incompatible with Python 3.10 runtime).

## Files Changed
- 289 Python files modified (auto-fix + format)
- 2 non-Python files in pre-existing working tree (not touched by ruff)

## Config Changes
- **None.** ruff.toml and pyproject.toml were NOT modified.
- Per-file ignores for tests, scripts, notebooks, and runpod probe tools were respected.
- B008 ignore in pyproject.toml (FastAPI Depends) was preserved.

## Test Results
- `pytest` could not run: codebase requires Python 3.12+ but environment has Python 3.10.6.
  - `fincept-core` requires `>=3.12` (setup.cfg)
  - `StrEnum` import fails on Python 3.10 (pre-existing, not caused by ruff)
- `python -m compileall` on all 289 changed .py files: **PASSED** (no syntax errors)
- See TEST_RESULTS.md for details.

## Key Decisions
1. **No --unsafe-fixes used.** Only safe `--fix` was applied. 248 unsafe fixes remain available but were not applied per task constraints.
2. **UP017 reverted (1 instance).** `datetime.UTC` requires Python 3.11+ but runtime is 3.10. See REMAINING_LINT_DEBT.md.
3. **No noqa comments added.** No broad ignores added.
4. **Other workers' changes preserved.** Ruff was run on the current state including changes from other workers (handler.py, run_live_canary.py, etc.).
