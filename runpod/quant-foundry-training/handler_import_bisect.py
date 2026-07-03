"""Import Bisection Test F — isolate which import poisons the RunPod worker.

This diagnostic handler imports only runpod + stdlib unconditionally, then
reads QF_IMPORT_PROFILE from the environment and imports exactly one
controlled group at module top. The handler(event) function is trivial —
it returns diagnostic info without calling any production handler logic.

The goal is to test whether the worker can boot, become ready, receive a
job, and complete after a specific import profile has already loaded. If
the worker goes unhealthy at dispatch time for a given profile, that
import group is the culprit.

See docs/runpod-fix-plan/02-single-variable-tests.md (Test E extension).

Profiles (tested in this order per operator direction):
  sentinel              — no project/ML imports (control, same as Test E)
  pandas_numpy          — import pandas and numpy
  xgboost               — import xgboost only
  catboost              — import catboost only
  lightgbm              — import lightgbm only
  torch                 — import torch only
  signatures_schemas    — import quant_foundry.signatures + quant_foundry.schemas
  runpod_training       — import quant_foundry.runpod_training only
  quality_report        — import quant_foundry.data_ingestion.quality_report
  dataset_manifest      — import quant_foundry.dataset_manifest
  full_handler_import   — import handler_full but do NOT call handler_full.handler
  full_handler_call     — call production handler canary path (only after full_handler_import passes)

Additional profiles available but not in the primary test order:
  compat_only           — compatibility shims only (_strenum_compat)
  quant_foundry_package — import quant_foundry package only
  fincept_core_package  — import fincept_core package only
  training_manifest     — import quant_foundry.training_manifest
  sklearn               — import sklearn only
"""
from __future__ import annotations

import json
import os
import platform
import sys
import time
from typing import Any

# --- Profile selection (module top, before any project imports) -------------

_PROFILE = os.environ.get("QF_IMPORT_PROFILE", "sentinel").strip().lower()
_IMPORT_ERROR: str | None = None
_IMPORTED_MODULES: list[str] = []

# Stdlib + runpod are always imported (same as the proven-working sentinel).
# The profile-controlled imports happen below.


def _record(module_name: str) -> None:
    """Record that a module was imported for the diagnostic output."""
    _IMPORTED_MODULES.append(module_name)


def _import_profile() -> None:
    """Import the controlled group based on QF_IMPORT_PROFILE."""
    global _IMPORT_ERROR

    if _PROFILE == "sentinel":
        # No project/ML imports — same as the proven-working Test E sentinel.
        return

    if _PROFILE == "compat_only":
        try:
            import _strenum_compat  # type: ignore[import-not-found]  # noqa: F401
            _record("_strenum_compat")
        except ImportError:
            pass  # Not present on Python 3.12 — that's fine.
        return

    if _PROFILE == "pandas_numpy":
        import numpy
        import pandas
        _record("numpy")
        _record("pandas")
        return

    if _PROFILE == "xgboost":
        import xgboost
        _record("xgboost")
        return

    if _PROFILE == "catboost":
        import catboost
        _record("catboost")
        return

    if _PROFILE == "lightgbm":
        import lightgbm
        _record("lightgbm")
        return

    if _PROFILE == "torch":
        import torch
        _record("torch")
        return

    if _PROFILE == "sklearn":
        import sklearn
        _record("sklearn")
        return

    if _PROFILE == "signatures_schemas":
        from quant_foundry.schemas import RunPodTrainingRequest  # noqa: F401
        from quant_foundry.signatures import sign_callback  # noqa: F401
        _record("quant_foundry.schemas")
        _record("quant_foundry.signatures")
        return

    if _PROFILE == "runpod_training":
        from quant_foundry.runpod_training import (  # noqa: F401
            LocalTrainer,
            RunPodTrainingHandler,
            build_callback,
            build_failure_envelope,
        )
        _record("quant_foundry.runpod_training")
        return

    if _PROFILE == "quality_report":
        from quant_foundry.data_ingestion.quality_report import (  # noqa: F401
            DatasetQualityReport,
            QualityPolicy,
        )
        _record("quant_foundry.data_ingestion.quality_report")
        return

    if _PROFILE == "dataset_manifest":
        from quant_foundry.dataset_manifest import (  # noqa: F401
            ColumnRoles,
            FoldSpec,
            FeatureLakeManifest,
            TrainingMode,
        )
        _record("quant_foundry.dataset_manifest")
        return

    if _PROFILE == "training_manifest":
        from quant_foundry.training_manifest import (  # noqa: F401
            MODE_RULES,
            ModelTaskSpec,
            TrainingMode,
        )
        _record("quant_foundry.training_manifest")
        return

    if _PROFILE == "quant_foundry_package":
        import quant_foundry
        _record("quant_foundry")
        return

    if _PROFILE == "fincept_core_package":
        import fincept_core
        _record("fincept_core")
        return

    if _PROFILE == "full_handler_import":
        # Import the production handler module WITHOUT calling handler().
        # This tests whether the production handler's module-level imports
        # (pydantic + quant_foundry + fincept_core) poison the process.
        import handler_full  # type: ignore[import-not-found]  # noqa: F401
        _record("handler_full (module-level imports only)")
        return

    # full_handler_call is handled in handler(event), not here, because it
    # needs to call the production handler's canary path at dispatch time.
    # But we still import handler_full at module top so the module is loaded.
    if _PROFILE == "full_handler_call":
        import handler_full  # type: ignore[import-not-found]  # noqa: F401
        _record("handler_full (module imported, handler() will be called at dispatch)")
        return

    # Unknown profile — no imports, will be visible in the diagnostic output.
    _IMPORT_ERROR = f"unknown QF_IMPORT_PROFILE: {_PROFILE!r}"


# Execute the profile-controlled imports at module top.
try:
    _import_profile()
except Exception as exc:
    _IMPORT_ERROR = f"{type(exc).__name__}: {exc}"


# --- Trivial handler ---------------------------------------------------------

_STARTED_AT = int(time.time())


def _runpod_sdk_version() -> str:
    runpod_module = sys.modules.get("runpod")
    if runpod_module is None:
        return "not_imported"
    return str(getattr(runpod_module, "__version__", "unknown"))


def handler(event: dict[str, Any]) -> dict[str, Any]:
    """Trivial diagnostic handler — returns import profile info.

    For the full_handler_call profile, delegates to the production handler's
    canary path instead of returning diagnostic info.
    """
    input_data = event.get("input") if isinstance(event, dict) else None
    job_id = input_data.get("job_id") if isinstance(input_data, dict) else None

    if _PROFILE == "full_handler_call":
        # Delegate to the production handler's canary path.
        # This tests whether handler(event) crashes at dispatch time.
        import handler_full  # type: ignore[import-not-found]  # noqa: F811
        return handler_full.handler(event)

    return {
        "ok": _IMPORT_ERROR is None,
        "handler": "import-bisect",
        "import_profile": _PROFILE,
        "imported_modules": _IMPORTED_MODULES,
        "import_error": _IMPORT_ERROR,
        "job_id": job_id,
        "event_input_keys": list(input_data.keys()) if isinstance(input_data, dict) else [],
        "runtime": {
            "git_sha": os.environ.get("QUANT_FOUNDRY_GIT_SHA", "unknown"),
            "python": platform.python_version(),
            "platform": platform.platform(),
            "runpod_sdk": _runpod_sdk_version(),
            "started_at": _STARTED_AT,
        },
    }


def _stdin_main() -> int:
    raw = sys.stdin.read()
    event = json.loads(raw) if raw.strip() else {"input": {"task": "import_bisect"}}
    result = handler(event)
    sys.stdout.write(json.dumps(result, indent=2, sort_keys=True))
    sys.stdout.write("\n")
    return 0


def main() -> int:
    if os.environ.get("RUNPOD_SMOKE_STDIN") == "1":
        return _stdin_main()

    try:
        import runpod
    except ImportError:
        return _stdin_main()

    sys.stdout.write(
        f"[import-bisect] starting runpod.serverless.start "
        f"profile={_PROFILE} "
        f"runpod_sdk={getattr(runpod, '__version__', 'unknown')} "
        f"git_sha={os.environ.get('QUANT_FOUNDRY_GIT_SHA', 'unknown')}\n"
    )
    sys.stdout.flush()
    serverless = vars(runpod)["serverless"]
    start = vars(serverless)["start"]
    start({"handler": handler})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
