# Remaining Lint Debt

## After Auto-Fix: 880 errors remaining

These errors are NOT auto-fixable with safe `--fix` only. They require either
`--unsafe-fixes` (not used per task constraints) or manual code changes.

## Top Remaining Rules (by count)

| Count | Rule    | Description                          | Fix Type     | Notes |
|-------|---------|--------------------------------------|--------------|-------|
| 429   | B017    | assert-raises-exception              | Manual       | Tests use `pytest.raises(Exception)` — should specify concrete exception types. Requires test logic changes. |
| 58    | T201    | print                                | Manual       | Print statements in non-test/script files. Need logging or removal. |
| 45    | RUF059  | unused-unpacked-variable             | Manual       | Unpacked variables not used. Need `_` prefix or removal. |
| 38    | B905    | zip-without-explicit-strict          | Manual       | Need `zip(..., strict=True/False)`. Requires understanding of intent. |
| 37    | F841    | unused-variable                      | Unsafe [-]   | Local variable assigned but never used. Can be auto-fixed with --unsafe-fixes but may hide side effects. |
| 31    | E402    | module-import-not-at-top             | Manual       | Imports not at top of file. Often intentional (conditional imports). |
| 26    | S110    | try-except-pass                      | Manual       | Silent exception swallowing. Need logging or re-raise. |
| 21    | SIM108  | if-else-block-instead-of-if-exp      | Manual       | Ternary conversion — may reduce readability. |
| 19    | RUF001  | ambiguous-unicode-character-string   | Manual       | Unicode chars that look like ASCII. Need explicit replacement. |
| 17    | SIM102  | collapsible-if                       | Manual       | Nested if that could be combined. |
| 16    | SIM117  | multiple-with-statements             | Unsafe [-]   | Combine with statements. Can break if context managers have side effects. |
| 12    | B007    | unused-loop-control-variable         | Manual       | Loop variable not used. Rename to `_`. |
| 12    | SIM103  | needless-bool                        | Manual       | Return condition directly instead of if/else. |
| 11    | S112    | try-except-continue                  | Manual       | Silent exception continue. Need logging. |
| 10    | E741    | ambiguous-variable-name              | Manual       | Variable names like `l`, `I`, `O`. |
| 10    | RUF002  | ambiguous-unicode-character-docstring| Manual       | Unicode in docstrings. |
| 8     | RUF005  | collection-literal-concatenation     | Manual       | Use `+` for list concat instead of literal. |
| 8     | S311    | suspicious-non-cryptographic-random  | Manual       | `random` module used in security context. |
| 7     | RUF043  | pytest-raises-ambiguous-pattern      | Manual       | pytest.raises with ambiguous match. |
| 7     | S108    | hardcoded-temp-file                  | Manual       | /tmp path hardcoded. |
| 7     | S607    | start-process-with-partial-path      | Manual       | Partial executable path. |
| 6     | RUF046  | unnecessary-cast-to-int              | Manual       | |
| 6     | S603    | subprocess-without-shell-equals-true | Manual       | |
| 5     | RUF003  | ambiguous-unicode-character-comment  | Manual       | |
| 5     | S310    | suspicious-url-open-usage            | Manual       | |
| 5     | SIM105  | suppressible-exception               | Manual       | Use contextlib.suppress. |
| 3     | E731    | lambda-assignment                    | Manual       | Assign lambda to variable — use def. |
| 3     | F821    | undefined-name                       | Manual       | **Potentially real bugs** — undefined names referenced. |
| 3     | RUF015  | unnecessary-iterable-allocation      | Manual       | |
| 3     | S301    | suspicious-pickle-usage              | Manual       | pickle.load security concern. |
| 3     | UP042   | replace-str-enum                     | Manual       | Replace StrEnum with (str, Enum). |
| 2     | B904    | raise-without-from-inside-except     | Manual       | Use `raise ... from err`. |
| 1     | ASYNC251| blocking-sleep-in-async-function     | Manual       | |
| 1     | F401    | unused-import                        | Unsafe [-]   | 1 remaining (218 were unsafe-fix, not applied). |
| 1     | RUF012  | mutable-class-default                | Manual       | |
| 1     | S104    | hardcoded-bind-all-interfaces        | Manual       | |
| 1     | S306    | suspicious-mktemp-usage              | Manual       | |
| 1     | SIM118  | in-dict-keys                         | Manual       | Use `key in dict` not `key in dict.keys()`. |
| 1     | UP017   | datetime-timezone-utc                | Safe [*]     | **Reverted** — see below. |

## Reverted Fix: UP017 (1 instance)

**File:** `services/quant_foundry/src/quant_foundry/receipt_bundle.py` (line 594)

**What happened:** Ruff auto-fix replaced `datetime.timezone.utc` with `datetime.UTC`.

**Why reverted:** `datetime.UTC` was added in Python 3.11. The current runtime environment
is Python 3.10.6. Using `datetime.UTC` would cause `AttributeError: module 'datetime' has
no attribute 'UTC'` at runtime.

**Note:** The ruff.toml `target-version = "py312"` is correct for the deployment target
(Python 3.12+), but the development/CI environment runs Python 3.10. This mismatch means
UP017 fixes are technically valid for the target but break the current dev environment.
The fix should be re-applied once the CI environment is upgraded to Python 3.12+.

## Unsafe Fixes Available (248, NOT applied)

These could be applied with `--unsafe-fixes` but were NOT used per task constraints:
- F401 (unused-import): 218 → mostly safe but can break re-exports
- F841 (unused-variable): 37 → can hide side effects
- SIM117 (multiple-with-statements): 16 → can change scoping
- Others: ~-23

## F821 (undefined-name) — Potential Real Bugs

3 instances of F821 (undefined-name) were found. These may indicate real bugs where
a name is referenced but not defined. These should be investigated manually:

```bash
ruff check . --select F821
```

## Recommended Next Steps

1. **B017 (429 errors):** Bulk-update test files to use concrete exception types instead
   of bare `Exception`. This is the single largest remaining category.
2. **F821 (3 errors):** Investigate as potential real bugs.
3. **T201 (58 errors):** Replace print statements with logging in non-test files.
4. **B905 (38 errors):** Add `strict=` parameter to `zip()` calls.
5. **Consider --unsafe-fixes** for F401 (unused-import) in a separate pass with careful
   review of re-exports.
6. **Re-apply UP017** once CI environment is upgraded to Python 3.12+.
