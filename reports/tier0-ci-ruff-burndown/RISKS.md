# Ruff Burn-Down — Risks

## R1: Large number of files reformatted (272 files)
- **Risk:** `ruff format .` reformatted 272 files. While formatting is mechanical, the large blast radius means any merge conflict with other branches will be noisy.
- **Mitigation:** This branch (`tier0/ruff-burndown`) is separate from all feature branches. Merge it AFTER all Tier 0 feature branches are merged, then rebase. The formatting changes are whitespace-only and will not conflict semantically.

## R2: 880 errors remain (47.7% of original)
- **Risk:** CI is still not clean. The remaining errors require manual fixes (B017 assert-raises-exception, T201 print, F841 unused-variable, etc.).
- **Mitigation:** REMAINING_LINT_DEBT.md documents the top remaining categories. These should be addressed in a follow-up task, not mixed with Tier 0 feature work.

## R3: 1 UP017 fix reverted (datetime.UTC)
- **Risk:** Ruff auto-fixed `datetime.timezone.utc` → `datetime.UTC` (UP017), which is Python 3.11+ only. The local environment is Python 3.10.6.
- **Mitigation:** The fix was reverted. Documented in REMAINING_LINT_DEBT.md. This fix should be applied once the project drops Python 3.10 support (the Dockerfile already uses 3.12).

## R4: No test verification on Python 3.12
- **Risk:** Local Python is 3.10.6; pytest could not run on most packages. compileall passed on all 289 changed files, but runtime correctness was not verified.
- **Mitigation:** The integration reviewer should run pytest in a 3.12 environment or verify that the formatting changes are whitespace-only (ruff format does not change AST).

## R5: 248 unsafe-fixable errors remain
- **Risk:** Unsafe fixes (e.g., removing unused imports in __init__.py that may be re-exported) could break runtime behavior if applied blindly.
- **Mitigation:** No unsafe fixes were applied. Each unsafe fix requires manual review and should be done individually in a follow-up.
