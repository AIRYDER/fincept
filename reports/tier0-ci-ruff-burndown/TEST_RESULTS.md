# Test Results

## pytest

**Status: NOT RUN (pre-existing environment limitation)**

The test suite could not be executed because the codebase requires Python 3.12+ but the
environment has Python 3.10.6:

```
pip install -e libs/fincept-core ...
ERROR: Package 'fincept-core' requires a different Python: 3.10.6 not in '>=3.12'
```

Even with PYTHONPATH set to bypass installation, tests fail on pre-existing Python 3.10
incompatibilities:

```
libs/fincept-core/src/fincept_core/schemas.py:4: in <module>
    from enum import StrEnum
E   ImportError: cannot import name 'StrEnum' from 'enum'
```

`StrEnum` was added in Python 3.11. This is a **pre-existing** issue in the codebase,
NOT caused by ruff changes. The file `schemas.py` was not modified by ruff (verified
via `git diff`).

## python -m compileall

**Status: PASSED**

All 289 changed Python files were compiled successfully:

```bash
git diff --name-only -- '*.py' > changed_py.txt
python -m compileall -q (Get-Content changed_py.txt)
# Exit code 0 — no errors
```

This verifies that no syntax errors were introduced by the ruff auto-fixes or formatter.

## ruff check (syntax verification)

**Status: PASSED**

`ruff check .` reports 880 lint errors but **zero syntax errors**. No `SyntaxError`,
`E902`, or similar fatal errors appear in the output.

## Conclusion

- No syntax errors introduced by ruff --fix or ruff format.
- Runtime behavior changes: 1 UP017 fix (datetime.UTC) was reverted because it would
  break on Python 3.10. See REMAINING_LINT_DEBT.md.
- Full test suite verification requires a Python 3.12+ environment, which is not
  available in this worker's environment.
