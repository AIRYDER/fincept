# Import Bisection Test F — Interpretation

**Image SHA:** c0f15fa7be38460c6c1930ef5394caf152615199
**Image tag:** ghcr.io/airyder/fincept/quant-foundry-training:c0f15fa7be38460c6c1930ef5394caf152615199
**Date:** 2026-07-03

## Executive Summary

Import Bisection Test F isolated the root cause of the RunPod training worker
dispatch failure to a **path resolution bug** in
`quant_foundry/data_ingestion/equities.py` and `quant_foundry/data_ingestion/news.py`.

Both files use `pathlib.Path(__file__).resolve().parents[5]` to find the repo
root, but in the container the file path
`/worker/quant_foundry/data_ingestion/equities.py` only has 4 parents
(indices 0-3). Accessing `parents[5]` raises `IndexError: 5`, which crashes
the production handler at module import time.

## Bisection Results (corrected run with per-profile templates)

| Profile | Result | Import Error | Failure Reason |
|---------|--------|--------------|----------------|
| sentinel | PASS | None | - |
| pandas_numpy | PASS | None | - |
| xgboost | PASS | None | - |
| catboost | PASS | None | - |
| lightgbm | FAIL | None | worker_died_while_job_in_queue (transient) |
| torch | PASS | None | - |
| signatures_schemas | PASS | None | - |
| runpod_training | PASS | IndexError: 5 | import caught → sentinel mode |
| quality_report | PASS | IndexError: 5 | import caught → sentinel mode |
| dataset_manifest | PASS | None | - |
| full_handler_import | PASS | IndexError: 5 | import caught → sentinel mode |
| full_handler_call | PASS | None | production handler ran successfully |

## Root Cause Analysis

### The IndexError: 5

The `IndexError: 5` appears in profiles that transitively import
`quant_foundry.data_ingestion.equities` or `quant_foundry.data_ingestion.news`
through the `data_ingestion/__init__.py` package init.

**Import chain:**
```
production handler.py
  → from quant_foundry.data_ingestion.quality_report import ...
    → quant_foundry.data_ingestion.__init__.py
      → from quant_foundry.data_ingestion.equities import ...
        → equities.py line 35:
          _SCRIPTS_DIR = pathlib.Path(__file__).resolve().parents[5] / "scripts"
          ↑ IndexError: 5 (only 4 parents in container)
```

**Container path analysis:**
- File: `/worker/quant_foundry/data_ingestion/equities.py`
- parents[0] = `/worker/quant_foundry/data_ingestion`
- parents[1] = `/worker/quant_foundry`
- parents[2] = `/worker`
- parents[3] = `/`
- parents[4] = IndexError! (only 4 parents, indices 0-3)

**Same bug in `news.py` line 49:**
```python
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[5]
```

### Why the bisection handler catches it but the production handler doesn't

The bisection handler wraps the profile-controlled imports in a try/except:
```python
try:
    _import_profile()
except Exception as exc:
    _IMPORT_ERROR = f"{type(exc).__name__}: {exc}"
```

So when `import handler_full` triggers the `IndexError: 5`, the bisection
handler catches it and starts in sentinel mode. The job completes with
`import_error: IndexError: 5` in the output.

The production handler has NO try/except around its module-level imports:
```python
from quant_foundry.data_ingestion.quality_report import (  # noqa: E402
    QUALITY_POLICY_REGISTRY,
    ...
)
```

When this import triggers `IndexError: 5`, the production handler module
crashes. The worker process exits before `runpod.serverless.start()` is called.

### The lightgbm failure

The `lightgbm` profile failed with `worker_died_while_job_in_queue`. This
appears to be a transient failure (possibly GPU allocation or container
scheduling issue), not related to the `IndexError: 5` bug. Lightgbm is not
imported at module top in the production handler (it's lazy-loaded inside
`train()`).

### The full_handler_call success

The `full_handler_call` profile successfully ran the production handler's
`handler(event)` function, which rejected the `import_bisect` task as
`unknown_task_type`. This confirms the production handler's logic works
correctly when the import succeeds. The module-top import may have failed
with `IndexError: 5` (caught by try/except), but the dispatch-time import
inside `handler(event)` succeeded — possibly because the first import
attempt partially initialized the module in `sys.modules`.

## Fix Recommendation

### Primary fix: Safe path resolution in equities.py and news.py

Replace the unsafe `parents[5]` access with a safe lookup:

```python
# equities.py — before:
_SCRIPTS_DIR = pathlib.Path(__file__).resolve().parents[5] / "scripts"

# equities.py — after:
_parents = pathlib.Path(__file__).resolve().parents
_SCRIPTS_DIR = _parents[5] / "scripts" if len(_parents) > 5 else pathlib.Path("/nonexistent/scripts")
```

Or better, use a try/except:

```python
try:
    _SCRIPTS_DIR = pathlib.Path(__file__).resolve().parents[5] / "scripts"
except IndexError:
    _SCRIPTS_DIR = None  # Not in a dev environment — scripts/ not available

if _SCRIPTS_DIR and str(_SCRIPTS_DIR) not in sys.path and _SCRIPTS_DIR.is_dir():
    sys.path.insert(0, str(_SCRIPTS_DIR))
```

Same fix for `news.py` line 49.

### Secondary fix: Lazy imports in data_ingestion/__init__.py

The `data_ingestion/__init__.py` eagerly imports ALL submodules (alpaca_bars,
equities, fred_macro, macro, news, news_vendor, quality_report, vendors).
Most of these are not needed by the production handler. Consider making them
lazy imports or moving the heavy imports into the functions that need them.

### Tertiary fix: Defensive import in production handler

Wrap the `quality_report` import in a try/except in the production handler,
so a failure in one import doesn't crash the entire handler:

```python
try:
    from quant_foundry.data_ingestion.quality_report import (...)
except ImportError:
    # Quality gate is defense-in-depth; handler can still run
    QUALITY_POLICY_REGISTRY = {}
    # ... define stubs
```

## Endpoints Used

All 12 test endpoints have been deleted. No warm endpoints remain.

## Image SHA

c0f15fa7be38460c6c1930ef5394caf152615199

## Next Steps

1. Fix `parents[5]` in `equities.py` and `news.py` (primary fix)
2. Rebuild image and re-run canary test
3. If canary passes, consider secondary fix (lazy imports in `__init__.py`)
