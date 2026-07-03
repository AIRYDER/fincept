"""Layered isolation handler for the RunPod training worker crash hunt.

One image, six code-path layers. The ONLY changing variable between tests
is the handler code path, selected per job:

    Layer selection order:
      1. env  QF_DIAG_LAYER            (use for Layer 0 — no input parsing)
      2. payload input["diag_layer"]   (use for Layers 1-5)
      3. default 5                     (current full canary path)

    Layer 0  handler_entry             return immediately, no input parse
    Layer 1  input_parse               read event["input"], return
    Layer 2  canary_only               _handle_canary, NO SecurityPreflight
    Layer 3  preflight_only            SecurityPreflight.run(), tiny static output
    Layer 4  preflight_plus_canary     preflight + canary, reduced response
    Layer 5  full                      the real handler.handler(event), unchanged

The real production handler module is imported at module scope (startup
imports are proven NOT to kill the worker: the diagnostic endpoint reached
ready=1 idle=1 with the full quant_foundry tree loaded).

Extra runtime diagnostics (to catch native crashes / forced exits):
  - faulthandler enabled + SIGTERM traceback dump (if the platform kills us,
    the container log shows where every thread was).
  - RSS memory logged before/after each layer (OOM-kill evidence).
  - Result JSON-serializability is self-checked before returning.
"""

# ruff: noqa: T201 - stdout prints are the container's log channel
from __future__ import annotations

import faulthandler
import importlib.util
import json
import os
import sys
import time
import traceback
from typing import Any

faulthandler.enable()
try:  # SIGTERM traceback dump — SIGTERM is what docker/RunPod sends on kill.
    import signal

    faulthandler.register(signal.SIGTERM, chain=True)
    print("[layered] faulthandler registered for SIGTERM", flush=True)
except (ImportError, AttributeError, ValueError, OSError) as _sig_exc:
    print(f"[layered] faulthandler SIGTERM registration skipped: {_sig_exc}", flush=True)

_HERE = os.path.dirname(os.path.abspath(__file__))

# --- lazy load the real production handler module --------------------------
# The full handler imports quant_foundry, xgboost, catboost, lightgbm, etc.
# at module scope — ~2 GB RSS.  Importing it eagerly at startup means the
# worker starts with that memory already committed, leaving very little head
# room for the RunPod SDK's job-processing allocation.  On RTX 4090 serverless
# pods (48 GB host RAM shared across tenants) this caused OOM-kills seconds
# after the first job arrived, even with a Layer-0 immediate-return handler.
#
# Fix: import lazily — only when a layer actually needs it (layers 2-5).
# Layer 0 and 1 never touch the production handler and can run in <50 MB.
_full: Any = None


def _get_full() -> Any:
    """Lazily import the production handler module on first use."""
    global _full
    if _full is not None:
        return _full
    print("[layered] importing production handler module (lazy)...", flush=True)
    t0 = time.monotonic()
    try:
        import handler_full as mod  # type: ignore[import-not-found]
        _full = mod
    except Exception as exc:
        print(f"[layered] import handler_full failed ({type(exc).__name__}: {exc}), trying file path...", flush=True)
        _spec = importlib.util.spec_from_file_location(
            "handler_full", os.path.join(_HERE, "handler_full.py")
        )
        if _spec is None or _spec.loader is None:
            raise RuntimeError("cannot locate production handler module") from None
        _full = importlib.util.module_from_spec(_spec)
        sys.modules["handler_full"] = _full
        _spec.loader.exec_module(_full)
    elapsed = int((time.monotonic() - t0) * 1000)
    print(f"[layered] production handler module imported OK in {elapsed}ms", flush=True)
    if not hasattr(_full, "handler"):
        attrs = [a for a in dir(_full) if not a.startswith("_")]
        raise RuntimeError(
            f"handler_full module loaded but has no 'handler' attribute. "
            f"Available attrs: {attrs[:20]}"
        )
    return _full


def _rss_kb() -> int | None:
    """Best-effort RSS in kB from /proc/self/status (Linux only)."""
    try:
        with open("/proc/self/status", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        return None
    return None


def _resolve_layer(event: Any) -> tuple[int, str]:
    """Resolve the diagnostic layer. Env wins (needed for pure Layer 0)."""
    raw_env = os.environ.get("QF_DIAG_LAYER", "").strip()
    if raw_env:
        try:
            return int(raw_env), "env"
        except ValueError:
            print(f"[layered] invalid QF_DIAG_LAYER={raw_env!r}, ignoring", flush=True)
    try:
        if isinstance(event, dict):
            input_data = event.get("input")
            if isinstance(input_data, dict) and "diag_layer" in input_data:
                return int(input_data["diag_layer"]), "payload"
    except (TypeError, ValueError) as exc:
        print(f"[layered] invalid payload diag_layer: {exc}", flush=True)
    return 5, "default"


# --- individual layers ------------------------------------------------------


def _layer_0(event: Any) -> dict[str, Any]:
    return {
        "ok": True,
        "layer": "handler_entry",
        "event_type": str(type(event)),
        "event_keys": list(event.keys()) if isinstance(event, dict) else None,
    }


def _layer_1(event: Any) -> dict[str, Any]:
    input_data = event.get("input", {}) if isinstance(event, dict) else {}
    return {
        "ok": True,
        "layer": "input_parse",
        "input_type": str(type(input_data)),
        "input_keys": list(input_data.keys()) if isinstance(input_data, dict) else None,
    }


def _layer_2(event: Any) -> dict[str, Any]:
    input_data = event.get("input", {}) if isinstance(event, dict) else {}
    if isinstance(input_data, dict) and input_data.get("task") == "callback_secret_canary":
        full = _get_full()
        result = full._handle_canary(input_data)
        result["layer"] = "canary_only_no_preflight"
        return result
    return {"ok": True, "layer": "canary_bypass_non_canary"}


def _layer_3(event: Any) -> dict[str, Any]:
    input_data = event.get("input", {}) if isinstance(event, dict) else {}
    full = _get_full()
    preflight_mode = full._resolve_preflight_mode(
        input_data if isinstance(input_data, dict) else {}
    )
    preflight = full.SecurityPreflight(mode=preflight_mode)
    result = preflight.run()
    return {
        "ok": True,
        "layer": "preflight_only",
        "preflight_type": str(type(result)),
        "preflight_passed": bool(result.passed),
    }


def _layer_4(event: Any) -> dict[str, Any]:
    input_data = event.get("input", {}) if isinstance(event, dict) else {}
    if not isinstance(input_data, dict):
        input_data = {}
    full = _get_full()
    preflight_mode = full._resolve_preflight_mode(input_data)
    preflight = full.SecurityPreflight(mode=preflight_mode)
    preflight.run()

    if input_data.get("task") == "callback_secret_canary":
        canary = full._handle_canary(input_data)
        return {
            "ok": True,
            "layer": "preflight_plus_canary_small",
            "canary_keys": sorted(canary.keys()) if isinstance(canary, dict) else None,
        }
    return {"ok": True, "layer": "preflight_plus_non_canary"}


def _layer_5(event: Any) -> dict[str, Any]:
    try:
        full = _get_full()
        result = full.handler(event)
        if isinstance(result, dict):
            result.setdefault("layer", "full_current_path")
        return result
    except Exception as exc:
        import traceback
        tb = traceback.format_exc()
        print(f"[layered] _layer_5 ERROR: {exc}\n{tb}", flush=True)
        return {
            "ok": False,
            "layer": "layer_5_exception",
            "error": str(exc),
            "error_type": type(exc).__name__,
            "traceback": tb,
        }


_LAYERS = {0: _layer_0, 1: _layer_1, 2: _layer_2, 3: _layer_3, 4: _layer_4, 5: _layer_5}

print(
    f"[layered] module loaded (lazy imports) rss_kb={_rss_kb()} "
    f"diag_layer_env={os.environ.get('QF_DIAG_LAYER', '<unset>')}",
    flush=True,
)


def handler(event: dict[str, Any]) -> dict[str, Any]:
    """Layered RunPod serverless handler entrypoint."""
    started = time.monotonic()
    layer, layer_source = _resolve_layer(event)
    print(f"[layered] handler() layer={layer} source={layer_source} rss_kb={_rss_kb()}", flush=True)

    layer_fn = _LAYERS.get(layer)
    if layer_fn is None:
        return {
            "ok": False,
            "layer": f"unknown_layer_{layer}",
            "error": f"diag layer must be 0-5, got {layer}",
        }

    try:
        result = layer_fn(event)
    except Exception as exc:
        tb = traceback.format_exc()
        print(f"[layered] layer {layer} EXCEPTION: {exc}\n{tb}", flush=True)
        return {
            "ok": False,
            "layer": f"layer_{layer}_exception",
            "error": str(exc),
            "traceback": tb,
        }

    elapsed_ms = int((time.monotonic() - started) * 1000)
    if isinstance(result, dict):
        result["diag_layer"] = layer
        result["diag_layer_source"] = layer_source
        result["diag_elapsed_ms"] = elapsed_ms
        result["diag_rss_kb_after"] = _rss_kb()

    # Self-check serializability so a non-JSON-safe field is reported as a
    # clean job result instead of crashing the SDK's result serialization.
    try:
        serialized = json.dumps(result)
        print(
            f"[layered] layer={layer} done in {elapsed_ms}ms "
            f"result_bytes={len(serialized)} rss_kb={_rss_kb()}",
            flush=True,
        )
    except (TypeError, ValueError) as exc:
        tb = traceback.format_exc()
        print(f"[layered] layer {layer} NON-SERIALIZABLE RESULT: {exc}", flush=True)
        return {
            "ok": False,
            "layer": f"layer_{layer}_not_json_serializable",
            "error": str(exc),
            "traceback": tb,
            "result_repr_truncated": repr(result)[:2000],
        }

    return result


if __name__ == "__main__":
    # Mirror the proven smoke-worker startup path exactly (plain
    # runpod.serverless.start, no os._exit, no extra wrappers).
    try:
        import runpod

        print(
            f"[layered] starting runpod.serverless.start "
            f"runpod_sdk={getattr(runpod, '__version__', 'unknown')} "
            f"git_sha={os.environ.get('QUANT_FOUNDRY_GIT_SHA', 'unknown')} "
            f"qf_diag_layer_env={os.environ.get('QF_DIAG_LAYER', '<unset>')}",
            flush=True,
        )
        runpod.serverless.start({"handler": handler})
    except ImportError:
        raw = sys.stdin.read()
        event = json.loads(raw) if raw.strip() else {}
        print(json.dumps(handler(event), indent=2, sort_keys=True, default=str))
    except Exception as exc:
        print(f"[layered] STARTUP ERROR: {exc}\n{traceback.format_exc()}", flush=True)
        raise
