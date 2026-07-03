# ruff: noqa: T201 - stdout prints are the container's log channel
"""Minimal handler on the pytorch CUDA base image.

Purpose: isolate whether the pod-exit issue is caused by the CUDA base
image itself or by the heavy module-scope imports in the training handler.

This handler does NO heavy imports and NO work — it just returns a dict.
If this image also gets "Exited by Runpod", the issue is the base image /
CUDA runtime / RunPod GPU runtime interaction.  If it works, the issue is
the heavy imports (xgboost, catboost, lightgbm, quant_foundry).
"""
from __future__ import annotations

import json
import os
import platform
import sys
import time
from typing import Any

_STARTED_AT = int(time.time())


def _rss_kb() -> int | None:
    try:
        with open("/proc/self/status", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        return None
    return None


def handler(event: dict[str, Any]) -> dict[str, Any]:
    input_data = event.get("input") if isinstance(event, dict) else None
    job_id = input_data.get("job_id") if isinstance(input_data, dict) else None
    return {
        "ok": True,
        "handler": "cuda-minimal-test",
        "job_id": job_id,
        "rss_kb": _rss_kb(),
        "runtime": {
            "git_sha": os.environ.get("QUANT_FOUNDRY_GIT_SHA", "unknown"),
            "python": platform.python_version(),
            "platform": platform.platform(),
            "started_at": _STARTED_AT,
        },
    }


def _stdin_main() -> int:
    raw = sys.stdin.read()
    event = json.loads(raw) if raw.strip() else {"input": {"task": "local_test"}}
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
    print(
        f"[cuda-test] starting runpod.serverless.start "
        f"runpod_sdk={getattr(runpod, '__version__', 'unknown')} "
        f"rss_kb={_rss_kb()}",
        flush=True,
    )
    runpod.serverless.start({"handler": handler})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
