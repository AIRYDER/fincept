# Commands Run

All commands executed from `C:\Users\nolan\CascadeProjects\fincept-terminal` on branch `tier0/ruff-burndown`.

## 1. Baseline Capture
```bash
ruff --version
# ruff 0.15.12

ruff check . --output-format=json > reports/tier0-ci-ruff-burndown/ruff_baseline.json 2>&1
# 1845 errors

ruff check . > reports/tier0-ci-ruff-burndown/RUFF_BEFORE.txt 2>&1
# Found 1845 errors.
# [*] 955 fixable with the `--fix` option (245 hidden fixes can be enabled with the `--unsafe-fixes` option).

ruff check . --statistics > reports/tier0-ci-ruff-burndown/RUFF_BEFORE_STATS.txt 2>&1
```

## 2. Safe Auto-Fix
```bash
ruff check . --fix
# Found 1901 errors (1014 fixed, 887 remaining).
# No fixes available (247 hidden fixes can be enabled with the `--unsafe-fixes` option).
```

## 3. Formatter
```bash
ruff format .
# 272 files reformatted, 501 files left unchanged
```

## 4. Second Pass (catch formatter-exposed fixes)
```bash
ruff check . --fix
# Found 880 errors (1 fixed, 879 remaining).

ruff format .
# 773 files left unchanged
```

## 5. After State Capture
```bash
ruff check . > reports/tier0-ci-ruff-burndown/RUFF_AFTER.txt 2>&1
# Found 880 errors.

ruff check . --statistics > reports/tier0-ci-ruff-burndown/RUFF_AFTER_STATS.txt 2>&1
```

## 6. Syntax Verification
```bash
# Check for syntax errors
ruff check . 2>&1 | Select-String "SyntaxError|syntax"
# (no output — no syntax errors)

# Compile all changed Python files
git diff --name-only -- '*.py' > changed_py.txt
python -m compileall -q (Get-Content changed_py.txt)
# Exit code 0 — all 289 files compiled successfully
```

## 7. Test Attempt
```bash
python -m pytest --tb=short -x
# ERROR: ModuleNotFoundError: No module named 'fincept_core'
# (pre-existing — packages require Python 3.12+, environment has 3.10.6)

pip install -e libs/fincept-core ...
# ERROR: Package 'fincept-core' requires a different Python: 3.10.6 not in '>=3.12'

# With PYTHONPATH set:
$env:PYTHONPATH = "libs/fincept-core/src;..."
python -m pytest libs/fincept-core/tests services/quant_foundry/tests --tb=short -x
# ImportError: cannot import name 'StrEnum' from 'enum'
# (pre-existing — StrEnum requires Python 3.11+)
```

## 8. UP017 Revert
```bash
# Found 1 instance of datetime.UTC introduced by UP017 auto-fix
# Reverted in services/quant_foundry/src/quant_foundry/receipt_bundle.py
# datetime.UTC requires Python 3.11+, runtime is Python 3.10
```

## 9. Git Operations
```bash
git checkout -b tier0/ruff-burndown
# Switched to a new branch 'tier0/ruff-burndown'
```
