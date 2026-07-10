"""
api.routes.modules — on-demand module control for local and staging (TASK-0203).

This route is a higher-level, operator-facing abstraction over the existing
``/features/*`` control surface in ``api.routes.control``. It adds:

  - A predeclared, allowlisted **module registry** with operator metadata:
    display name, description, cost class, idle timeout, allowed
    environments, and the service names used for heartbeat freshness.
  - **Idle timeout enforcement** (``POST /modules/sweep-idle``) that stops
    optional modules whose idle timeout has elapsed, recording an
    ``auto_stop`` receipt.
  - **Stop-all** (``POST /modules/stop-all``) to stop every running optional
    module in one call.
  - A **receipts catalog** (``GET /modules/receipts``) of every start / stop /
    restart / auto_stop action, with redacted output.

Security invariants (non-negotiable, per TASK-0203 spec):

  - **No arbitrary shell command execution from user input.** Module IDs are
    allowlisted against ``MODULE_REGISTRY``; the start/stop dispatch reuses
    ``control._run_feature_script`` which invokes the predeclared
    ``start_feature.ps1`` / ``stop_feature.ps1`` scripts keyed only by the
    allowlisted module ID. The user never supplies a command string.
  - **API requires auth** for every operator endpoint (``require_user``).
  - **Local-only** launch (``control._assert_local_request``).
  - **Secrets are never echoed.** Receipt output is run through
    ``_redact_output`` before persistence and before any response.

File-disjoint from TASK-0304 (Builder 2: quant_foundry outbox/inbox) and
TASK-0401 (Builder 1: settlement ledger). This module does NOT edit
``control.py``; it imports its allowlisted helpers.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fincept_core.heartbeat import read_all
from redis.asyncio import Redis

from api.auth import require_user
from api.deps import get_redis
from api.routes.control import (
    _feature_preflight_blocker,
    _feature_script_path,
    _fresh_services,
    _run_feature_script,
    _stop_feature_script_path,
)

router = APIRouter()

# Re-export the local-host allowlist so tests can monkeypatch it through the
# modules namespace without touching control.py.
_LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost", "testclient"}

# Redis keys.
_RECEIPTS_KEY = "module:receipts"
_STATE_KEY_PREFIX = "module:state:"
_RECEIPT_TTL_SEC = 7 * 24 * 60 * 60  # 7 days


# --------------------------------------------------------------------------- #
# Module registry (predeclared, allowlisted)                                   #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ModuleSpec:
    """A predeclared optional module the operator can start on demand.

    ``feature_id`` maps 1:1 to the existing allowlisted feature control
    surface in ``control._FEATURE_SERVICES``; no new subprocess invocations
    are introduced here.
    """

    module_id: str
    feature_id: str
    display_name: str
    description: str
    cost_class: str  # "low" | "medium" | "high"
    idle_timeout_sec: int
    allowed_environments: tuple[str, ...]
    services: tuple[str, ...]


# The canonical optional-module registry. Each entry maps to an existing
# allowlisted feature_id so start/stop reuses the hardened script runner.
# idle_timeout_sec is intentionally conservative for a one-operator dev box.
MODULE_REGISTRY: dict[str, ModuleSpec] = {
    "openbb": ModuleSpec(
        module_id="openbb",
        feature_id="openbb",
        display_name="OpenBB Research Terminal",
        description="OpenBB API provider for quotes, fundamentals, and research data. Optional — start only when doing research.",
        cost_class="medium",
        idle_timeout_sec=30 * 60,  # 30 min
        allowed_environments=("local", "staging"),
        services=(),
    ),
    "market_data": ModuleSpec(
        module_id="market_data",
        feature_id="market_data",
        display_name="Market Data Ingestion",
        description="Ingestor + features online transforms. Start to capture live market data.",
        cost_class="medium",
        idle_timeout_sec=60 * 60,  # 60 min
        allowed_environments=("local", "staging"),
        services=("ingestor", "features"),
    ),
    "news_learning": ModuleSpec(
        module_id="news_learning",
        feature_id="news_learning",
        display_name="News Learning Loop",
        description="Information enricher + news outcome labeler. Start for the news learning pipeline.",
        cost_class="medium",
        idle_timeout_sec=45 * 60,
        allowed_environments=("local", "staging"),
        services=("information_enricher", "news_outcome_labeler"),
    ),
    "jobs": ModuleSpec(
        module_id="jobs",
        feature_id="jobs",
        display_name="Background Jobs Worker",
        description="Nightly / scheduled jobs worker. Start to run background job processing.",
        cost_class="low",
        idle_timeout_sec=20 * 60,
        allowed_environments=("local", "staging"),
        services=("jobs",),
    ),
    "gbm_predictor": ModuleSpec(
        module_id="gbm_predictor",
        feature_id="gbm_predictor",
        display_name="GBM Predictor Agent",
        description="LightGBM online inference agent. Requires a trained model in models/gbm_predictor.",
        cost_class="low",
        idle_timeout_sec=30 * 60,
        allowed_environments=("local", "staging"),
        services=("gbm_predictor",),
    ),
    "news_alpha_predictor": ModuleSpec(
        module_id="news_alpha_predictor",
        feature_id="news_alpha_predictor",
        display_name="News Alpha Predictor",
        description="News-alpha predictor agent. Requires a promoted model pointer.",
        cost_class="low",
        idle_timeout_sec=30 * 60,
        allowed_environments=("local", "staging"),
        services=("news_alpha_predictor",),
    ),
    "sentiment": ModuleSpec(
        module_id="sentiment",
        feature_id="sentiment",
        display_name="Sentiment Agents",
        description="Sentiment agent + sentiment features. Requires ANTHROPIC_API_KEY or OPENAI_API_KEY.",
        cost_class="high",
        idle_timeout_sec=15 * 60,
        allowed_environments=("local", "staging"),
        services=("sentiment_agent", "sentiment_features"),
    ),
    "regime": ModuleSpec(
        module_id="regime",
        feature_id="regime",
        display_name="Regime Agent",
        description="FRED-based regime detection agent. Requires FRED_API_KEY.",
        cost_class="low",
        idle_timeout_sec=60 * 60,
        allowed_environments=("local", "staging"),
        services=("regime_agent",),
    ),
}


# --------------------------------------------------------------------------- #
# Output redaction — never echo secrets into receipts/responses                #
# --------------------------------------------------------------------------- #

_REDACT_PATTERS = (
    "sk-",
    "Bearer ",
    "token=",
    "password=",
    "api_key=",
    "apikey=",
    "secret=",
    "private_key=",
    "BEGIN PRIVATE KEY",
    "BEGIN RSA PRIVATE KEY",
)


def _redact_output(text: str) -> str:
    """Strip secret-looking substrings from script output before persistence."""
    if not text:
        return ""
    redacted = text
    for pat in _REDACT_PATTERS:
        if pat in redacted:
            # Replace the pattern and the following token-ish run with a marker.
            idx = redacted.find(pat)
            # Replace from the pattern to the next whitespace or end.
            end = idx + len(pat)
            while end < len(redacted) and not redacted[end].isspace():
                end += 1
            redacted = redacted[:idx] + "[REDACTED]" + redacted[end:]
    return redacted


# --------------------------------------------------------------------------- #
# State helpers (Redis-backed)                                                 #
# --------------------------------------------------------------------------- #


async def _read_state(redis: Redis, module_id: str) -> dict[str, Any] | None:  # type: ignore[type-arg]
    raw = await redis.get(f"{_STATE_KEY_PREFIX}{module_id}")
    if raw is None:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode(errors="replace")
    try:
        loaded = json.loads(str(raw))
    except json.JSONDecodeError:
        return None
    return loaded if isinstance(loaded, dict) else None


async def _write_state(
    redis: Redis,  # type: ignore[type-arg]
    module_id: str,
    *,
    status: str,
    actor: str,
    started_at_unix: float | None = None,
    last_activity_unix: float | None = None,
) -> dict[str, Any]:
    prev = await _read_state(redis, module_id) or {}
    payload = {
        "status": status,
        "started_at_unix": started_at_unix
        if started_at_unix is not None
        else prev.get("started_at_unix"),
        "last_activity_unix": last_activity_unix
        if last_activity_unix is not None
        else prev.get("last_activity_unix"),
        "actor": actor,
    }
    await redis.set(f"{_STATE_KEY_PREFIX}{module_id}", json.dumps(payload))
    return payload


async def _record_receipt(
    redis: Redis,  # type: ignore[type-arg]
    *,
    module_id: str,
    action: str,
    status: str,
    actor: str,
    output: str = "",
) -> dict[str, Any]:
    rec = {
        "module_id": module_id,
        "action": action,
        "status": status,
        "actor": actor,
        "output": _redact_output(output),
        "ts_unix": time.time(),
    }
    await redis.lpush(_RECEIPTS_KEY, json.dumps(rec))
    # Trim to a reasonable catalog size.
    await redis.ltrim(_RECEIPTS_KEY, 0, 499)
    return rec


async def _read_receipts(redis: Redis, limit: int = 50) -> list[dict[str, Any]]:  # type: ignore[type-arg]
    raw_items = await redis.lrange(_RECEIPTS_KEY, 0, limit - 1)
    out: list[dict[str, Any]] = []
    for raw in raw_items:
        if isinstance(raw, bytes):
            raw = raw.decode(errors="replace")
        try:
            item = json.loads(str(raw))
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            out.append(item)
    return out


def _module_status_from_state(
    spec: ModuleSpec, state: dict[str, Any] | None, fresh_services: list[str]
) -> str:
    """Derive the operator-facing status for a module."""
    if state is None:
        return "stopped"
    status = str(state.get("status", "stopped"))
    if status == "running":
        # If declared services exist and none are fresh, mark degraded.
        if spec.services and not fresh_services:
            return "degraded"
        return "running"
    return status


def _idle_seconds(state: dict[str, Any] | None, now: float) -> int:
    if state is None:
        return 0
    last = state.get("last_activity_unix")
    if last is None:
        return 0
    try:
        return max(0, int(now - float(last)))
    except (TypeError, ValueError):
        return 0


# --------------------------------------------------------------------------- #
# Endpoints                                                                    #
# --------------------------------------------------------------------------- #


def _assert_known_module(module_id: str) -> ModuleSpec:
    spec = MODULE_REGISTRY.get(module_id)
    if spec is None:
        raise HTTPException(status_code=404, detail="unknown module")
    return spec


def _assert_local(request: Request) -> None:
    """Local-only guard using the modules-namespace allowlist (test-overridable)."""
    host = request.client.host if request.client else None
    if host not in _LOCAL_HOSTS:
        raise HTTPException(status_code=403, detail="module launcher is local-only")


async def _fresh_for(redis: Redis, spec: ModuleSpec) -> list[str]:  # type: ignore[type-arg]
    if not spec.services:
        return []
    beats = await read_all(redis)
    return _fresh_services(beats, list(spec.services))


def _serialize_module(
    spec: ModuleSpec,
    state: dict[str, Any] | None,
    fresh_services: list[str],
    now: float,
) -> dict[str, Any]:
    status = _module_status_from_state(spec, state, fresh_services)
    idle = _idle_seconds(state, now)
    countdown = max(0, spec.idle_timeout_sec - idle)
    return {
        "module_id": spec.module_id,
        "display_name": spec.display_name,
        "description": spec.description,
        "cost_class": spec.cost_class,
        "idle_timeout_sec": spec.idle_timeout_sec,
        "allowed_environments": list(spec.allowed_environments),
        "services": list(spec.services),
        "status": status,
        "started_at_unix": state.get("started_at_unix") if state else None,
        "last_activity_unix": state.get("last_activity_unix") if state else None,
        "idle_seconds": idle if status == "running" else 0,
        "idle_countdown_sec": countdown
        if status == "running"
        else spec.idle_timeout_sec,
        "fresh_services": fresh_services,
    }


@router.get("")
async def list_modules(
    _: dict[str, Any] = Depends(require_user),
    redis: Redis = Depends(get_redis),  # type: ignore[type-arg]
) -> dict[str, Any]:
    """List every allowlisted module with live status + idle countdown."""
    now = time.time()
    modules: list[dict[str, Any]] = []
    for spec in MODULE_REGISTRY.values():
        state = await _read_state(redis, spec.module_id)
        fresh = await _fresh_for(redis, spec)
        modules.append(_serialize_module(spec, state, fresh, now))
    return {"ok": True, "modules": modules}


@router.get("/receipts")
async def list_receipts(
    _: dict[str, Any] = Depends(require_user),
    redis: Redis = Depends(get_redis),  # type: ignore[type-arg]
) -> dict[str, Any]:
    """Return the recent module control receipts (redacted)."""
    receipts = await _read_receipts(redis, limit=50)
    return {"ok": True, "receipts": receipts}


@router.get("/{module_id}")
async def get_module(
    module_id: str,
    _: dict[str, Any] = Depends(require_user),
    redis: Redis = Depends(get_redis),  # type: ignore[type-arg]
) -> dict[str, Any]:
    spec = _assert_known_module(module_id)
    state = await _read_state(redis, module_id)
    fresh = await _fresh_for(redis, spec)
    return {
        "ok": True,
        "module": _serialize_module(spec, state, fresh, time.time()),
    }


@router.post("/{module_id}/start")
async def start_module(
    module_id: str,
    request: Request,
    user: dict[str, Any] = Depends(require_user),
    redis: Redis = Depends(get_redis),  # type: ignore[type-arg]
) -> dict[str, Any]:
    spec = _assert_known_module(module_id)
    _assert_local(request)
    actor = str(user.get("sub", "unknown"))

    # Preflight blocker (e.g. missing model) — reused from control.py.
    blocker = _feature_preflight_blocker(spec.feature_id)
    if blocker is not None:
        output = f"{blocker['message']}\n{blocker['next_step']}"
        await _record_receipt(
            redis,
            module_id=module_id,
            action="start",
            status="blocked",
            actor=actor,
            output=output,
        )
        raise HTTPException(
            status_code=409,
            detail={"module_id": module_id, "status": "blocked", **blocker},
        )

    fresh = await _fresh_for(redis, spec)
    if spec.services and len(fresh) == len(spec.services):
        # Already running — do NOT spawn another process.
        await _write_state(
            redis,
            module_id,
            status="running",
            actor=actor,
            last_activity_unix=time.time(),
        )
        await _record_receipt(
            redis,
            module_id=module_id,
            action="start",
            status="already_running",
            actor=actor,
        )
        return {
            "ok": True,
            "module_id": module_id,
            "action": "start",
            "started": False,
            "status": "already_running",
            "services": list(spec.services),
            "fresh_services": fresh,
        }

    result = await _run_feature_script(_feature_script_path(), spec.feature_id)
    now = time.time()
    await _write_state(
        redis,
        module_id,
        status="running",
        actor=actor,
        started_at_unix=now,
        last_activity_unix=now,
    )
    await _record_receipt(
        redis,
        module_id=module_id,
        action="start",
        status="launch_requested",
        actor=actor,
        output=result["output"],
    )
    return {
        "ok": True,
        "module_id": module_id,
        "action": "start",
        "started": True,
        "status": "launch_requested",
        "services": list(spec.services),
        "fresh_services": fresh,
        "output": _redact_output(result["output"]),
    }


@router.post("/{module_id}/stop")
async def stop_module(
    module_id: str,
    request: Request,
    user: dict[str, Any] = Depends(require_user),
    redis: Redis = Depends(get_redis),  # type: ignore[type-arg]
) -> dict[str, Any]:
    spec = _assert_known_module(module_id)
    _assert_local(request)
    actor = str(user.get("sub", "unknown"))

    result = await _run_feature_script(_stop_feature_script_path(), spec.feature_id)
    await _write_state(
        redis,
        module_id,
        status="stopped",
        actor=actor,
        started_at_unix=None,
        last_activity_unix=None,
    )
    await _record_receipt(
        redis,
        module_id=module_id,
        action="stop",
        status="stop_requested",
        actor=actor,
        output=result["output"],
    )
    fresh = await _fresh_for(redis, spec)
    return {
        "ok": True,
        "module_id": module_id,
        "action": "stop",
        "status": "stop_requested",
        "services": list(spec.services),
        "fresh_services": fresh,
        "output": _redact_output(result["output"]),
    }


@router.post("/{module_id}/restart")
async def restart_module(
    module_id: str,
    request: Request,
    user: dict[str, Any] = Depends(require_user),
    redis: Redis = Depends(get_redis),  # type: ignore[type-arg]
) -> dict[str, Any]:
    spec = _assert_known_module(module_id)
    _assert_local(request)
    actor = str(user.get("sub", "unknown"))

    blocker = _feature_preflight_blocker(spec.feature_id)
    if blocker is not None:
        output = f"{blocker['message']}\n{blocker['next_step']}"
        await _record_receipt(
            redis,
            module_id=module_id,
            action="restart",
            status="blocked",
            actor=actor,
            output=output,
        )
        raise HTTPException(
            status_code=409,
            detail={"module_id": module_id, "status": "blocked", **blocker},
        )

    stop_result = await _run_feature_script(
        _stop_feature_script_path(), spec.feature_id
    )
    start_result = await _run_feature_script(_feature_script_path(), spec.feature_id)
    output = "\n".join(
        part for part in [stop_result["output"], start_result["output"]] if part
    )
    now = time.time()
    await _write_state(
        redis,
        module_id,
        status="running",
        actor=actor,
        started_at_unix=now,
        last_activity_unix=now,
    )
    await _record_receipt(
        redis,
        module_id=module_id,
        action="restart",
        status="restart_requested",
        actor=actor,
        output=output,
    )
    fresh = await _fresh_for(redis, spec)
    return {
        "ok": True,
        "module_id": module_id,
        "action": "restart",
        "status": "restart_requested",
        "services": list(spec.services),
        "fresh_services": fresh,
        "output": _redact_output(output),
    }


@router.post("/stop-all")
async def stop_all_modules(
    request: Request,
    user: dict[str, Any] = Depends(require_user),
    redis: Redis = Depends(get_redis),  # type: ignore[type-arg]
) -> dict[str, Any]:
    """Stop every optional module currently marked running."""
    _assert_local(request)
    actor = str(user.get("sub", "unknown"))
    stopped: list[str] = []
    now = time.time()
    for spec in MODULE_REGISTRY.values():
        state = await _read_state(redis, spec.module_id)
        if state is None or state.get("status") != "running":
            continue
        result = await _run_feature_script(_stop_feature_script_path(), spec.feature_id)
        await _write_state(
            redis,
            spec.module_id,
            status="stopped",
            actor=actor,
            started_at_unix=None,
            last_activity_unix=None,
        )
        await _record_receipt(
            redis,
            module_id=spec.module_id,
            action="stop",
            status="stop_requested",
            actor=actor,
            output=result["output"],
        )
        stopped.append(spec.module_id)
    return {"ok": True, "stopped": stopped, "ts_unix": now}


@router.post("/sweep-idle")
async def sweep_idle_modules(
    request: Request,
    user: dict[str, Any] = Depends(require_user),
    redis: Redis = Depends(get_redis),  # type: ignore[type-arg]
) -> dict[str, Any]:
    """Stop any running module whose idle timeout has elapsed.

    This is the canonical idle-timeout enforcement. The dashboard should poll
    it on a timer (e.g. every 60s). It only stops modules that are both marked
    running AND past their idle_timeout_sec with no fresh service heartbeats.
    """
    _assert_local(request)
    actor = str(user.get("sub", "system"))
    now = time.time()
    stopped: list[str] = []
    for spec in MODULE_REGISTRY.values():
        state = await _read_state(redis, spec.module_id)
        if state is None or state.get("status") != "running":
            continue
        idle = _idle_seconds(state, now)
        # If declared services are still fresh, the module is actively in use
        # even if last_activity_unix is stale — do not stop it.
        fresh = await _fresh_for(redis, spec)
        if fresh:
            # Refresh activity timestamp; module is alive.
            await _write_state(
                redis,
                spec.module_id,
                status="running",
                actor=actor,
                last_activity_unix=now,
            )
            continue
        if idle >= spec.idle_timeout_sec:
            result = await _run_feature_script(
                _stop_feature_script_path(), spec.feature_id
            )
            await _write_state(
                redis,
                spec.module_id,
                status="stopped",
                actor=actor,
                started_at_unix=None,
                last_activity_unix=None,
            )
            await _record_receipt(
                redis,
                module_id=spec.module_id,
                action="auto_stop",
                status="idle_timeout_elapsed",
                actor=actor,
                output=result["output"],
            )
            stopped.append(spec.module_id)
    return {"ok": True, "stopped": stopped, "ts_unix": now}
