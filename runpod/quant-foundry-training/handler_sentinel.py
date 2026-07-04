"""Test E sentinel handler — trivial, no heavy imports.

This handler imports ONLY runpod + stdlib (same as the proven-working smoke
handler). It is copied to /worker/handler.py in the training image to test
whether the RunPod SDK job loop works inside the training image shape
independent of the production handler's heavy imports (quant_foundry, torch,
xgboost, catboost, lightgbm).

If this sentinel completes a live job, the failure is isolated to the
production handler's import/startup path. If the sentinel also fails, the
failure is in the SDK/image runtime itself.

See docs/runpod-fix-plan/02-single-variable-tests.md Test E.
"""

from __future__ import annotations

import json
import os
import platform
import sys
import time
from typing import Any

_STARTED_AT = int(time.time())


def _runpod_sdk_version() -> str:
    runpod_module = sys.modules.get("runpod")
    if runpod_module is None:
        return "not_imported"
    return str(getattr(runpod_module, "__version__", "unknown"))


def handler(event: dict[str, Any]) -> dict[str, Any]:
    input_data = event.get("input") if isinstance(event, dict) else None
    job_id = input_data.get("job_id") if isinstance(input_data, dict) else None

    return {
        "ok": True,
        "handler": "quant-foundry-sentinel",
        "job_id": job_id,
        "received": event,
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
    event = json.loads(raw) if raw.strip() else {"input": {"task": "sentinel"}}
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
        "[sentinel] starting runpod.serverless.start "
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
