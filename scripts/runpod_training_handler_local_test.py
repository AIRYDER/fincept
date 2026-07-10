"""Load a RunPod training handler file by path and invoke handler(event) locally.

The runpod/quant-foundry-training directory has a hyphen and cannot be
imported as a normal Python package, so the handler module is loaded via
importlib from an explicit file path.

Usage (from repo root):
    uv run python scripts/runpod_training_handler_local_test.py \
        --handler runpod/quant-foundry-training/handler_diagnostic.py \
        --payload '{"input": {"task": "callback_secret_canary", "job_id": "local-001", "nonce": "n1"}}'

Exit codes:
    0 = handler returned a dict (printed to stdout)
    1 = handler raised / module failed to load
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import traceback
from pathlib import Path


def load_handler_module(handler_path: Path):
    spec = importlib.util.spec_from_file_location("qf_local_handler", str(handler_path))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot build import spec for {handler_path}")
    module = importlib.util.module_from_spec(spec)
    # Register before exec: dataclasses/pydantic resolve annotations via
    # sys.modules[cls.__module__] and crash if the module is not registered.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--handler", required=True, help="Path to the handler .py file")
    parser.add_argument("--payload-json", default=None, help="Full event JSON")
    parser.add_argument("--payload-file", default=None, help="Path to event JSON file")
    parser.add_argument(
        "--secret",
        default=None,
        help="QUANT_FOUNDRY_CALLBACK_SECRET override (default: local-test-secret)",
    )
    args = parser.parse_args(argv)

    if not os.environ.get("QUANT_FOUNDRY_CALLBACK_SECRET"):
        os.environ["QUANT_FOUNDRY_CALLBACK_SECRET"] = args.secret or "local-test-secret"

    if args.payload_json:
        event = json.loads(args.payload_json)
    elif args.payload_file:
        event = json.loads(Path(args.payload_file).read_text(encoding="utf-8"))
    else:
        event = {
            "input": {
                "task": "callback_secret_canary",
                "job_id": "qf:diag-canary:local:001",
                "nonce": "diag-nonce-local-001",
            }
        }

    handler_path = Path(args.handler).resolve()
    print(f"[local-test] loading handler module from {handler_path}", flush=True)
    try:
        module = load_handler_module(handler_path)
    except Exception:
        print("[local-test] MODULE LOAD FAILED:", flush=True)
        traceback.print_exc()
        return 1

    handler_fn = getattr(module, "handler", None)
    if handler_fn is None:
        print("[local-test] module has no handler() function", flush=True)
        return 1

    print(f"[local-test] invoking handler() with event: {json.dumps(event)}", flush=True)
    try:
        result = handler_fn(event)
    except Exception:
        print("[local-test] HANDLER RAISED:", flush=True)
        traceback.print_exc()
        return 1

    print("[local-test] handler returned:", flush=True)
    print(json.dumps(result, indent=2, sort_keys=True, default=str), flush=True)

    try:
        json.dumps(result)
        print("[local-test] result is JSON-serializable: True", flush=True)
    except (TypeError, ValueError) as exc:
        print(f"[local-test] result is JSON-serializable: False ({exc})", flush=True)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
