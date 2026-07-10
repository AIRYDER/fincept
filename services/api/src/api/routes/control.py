"""
api.routes.control — operator control endpoints (kill-switch only for v1).

  POST   /kill-switch   Trip the kill-switch.  Publishes a critical
                        AlertEvent on STREAM_ALERTS that downstream
                        services (risk gate, OMS, strategy host)
                        should react to by halting new orders /
                        cancelling open ones.
  DELETE /kill-switch   Clear the kill-switch.  Publishes an
                        info-level all-clear AlertEvent.

The actual halting behavior lives in the consumer services (TASK-041
risk gate is the canonical consumer when it lands).  Until then, the
alert IS the canonical record — tests and operators can verify the
event landed on the bus by tailing ``events.alerts``.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fincept_bus.producer import Producer
from fincept_bus.streams import STREAM_ALERTS
from fincept_core.clock import now_ns
from fincept_core.events import Event
from fincept_core.heartbeat import read_all
from fincept_core.ids import new_id
from fincept_core.schemas import AlertEvent
from redis.asyncio import Redis

from api.auth import require_user
from api.deps import get_redis

router = APIRouter()

_FEATURE_SERVICES: dict[str, list[str]] = {
    "market_data": ["ingestor", "features"],
    "news_learning": ["information_enricher", "news_outcome_labeler"],
    "jobs": ["jobs"],
    "gbm_predictor": ["gbm_predictor"],
    "news_alpha_predictor": ["news_alpha_predictor"],
    "sentiment": ["sentiment_agent", "sentiment_features"],
    "regime": ["regime_agent"],
    "openbb": [],
}
_LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost", "testclient"}
_SERVICE_FRESH_SEC = 15
_FEATURE_LAST_CONTROL_TTL_SEC = 24 * 60 * 60
from risk.state import KILL_SWITCH_STATE_KEY as _KILL_SWITCH_STATE_KEY  # noqa: E402


def _script_path(name: str) -> Path:
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "scripts" / name
        if candidate.is_file():
            return candidate
    raise HTTPException(status_code=500, detail=f"{name} not found")


def _feature_script_path() -> Path:
    return _script_path("start_feature.ps1")


def _stop_feature_script_path() -> Path:
    return _script_path("stop_feature.ps1")


def _repo_root() -> Path:
    return _feature_script_path().parent.parent


def _news_alpha_model_dir() -> Path:
    repo = _repo_root()
    configured = os.environ.get("NEWS_ALPHA_MODEL_DIR")
    if configured:
        return Path(configured)
    active_pointer = repo / "models" / "active" / "news_alpha_predictor.v1.json"
    model_dir = repo / "models" / "news_alpha_predictor"
    if active_pointer.is_file():
        try:
            pointer = json.loads(active_pointer.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pointer = {}
        model_name = pointer.get("model_name") if isinstance(pointer, dict) else None
        if isinstance(model_name, str) and model_name.strip():
            model_dir = repo / "models" / model_name
    return model_dir


def _feature_preflight_blocker(feature_id: str) -> dict[str, Any] | None:
    repo = _repo_root()
    if feature_id == "gbm_predictor":
        model_dir = repo / "models" / "gbm_predictor"
        if not (model_dir / "model.txt").is_file():
            return {
                "reason": "missing_model",
                "message": f"gbm_predictor model.txt not found at {model_dir}",
                "next_step": "Train or copy a GBM model into models/gbm_predictor before starting this optional lane.",
                "path": str(model_dir),
            }
    if feature_id == "news_alpha_predictor":
        model_dir = _news_alpha_model_dir()
        if not (model_dir / "model.txt").is_file():
            return {
                "reason": "missing_model",
                "message": f"news_alpha_predictor model.txt not found at {model_dir}",
                "next_step": "Run the news-alpha training/export flow or set NEWS_ALPHA_MODEL_DIR to a promoted model directory before starting this optional lane.",
                "path": str(model_dir),
            }
    return None


def _assert_local_request(request: Request) -> None:
    host = request.client.host if request.client else None
    if host not in _LOCAL_HOSTS:
        raise HTTPException(status_code=403, detail="feature launcher is local-only")


def _fresh_services(beats: dict[str, float], service_names: list[str]) -> list[str]:
    now = time.time()
    return [
        name
        for name in service_names
        if name in beats and now - beats[name] <= _SERVICE_FRESH_SEC
    ]


async def _run_feature_script(script: Path, feature_id: str) -> dict[str, Any]:
    process = await asyncio.create_subprocess_exec(
        "pwsh",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(script),
        "-FeatureId",
        feature_id,
        cwd=str(script.parent.parent),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=20)
    except TimeoutError as exc:
        process.kill()
        raise HTTPException(
            status_code=504, detail="feature control timed out"
        ) from exc
    output = stdout.decode(errors="replace").strip()
    error = stderr.decode(errors="replace").strip()
    if process.returncode != 0:
        raise HTTPException(
            status_code=400,
            detail={
                "message": error or output or "feature control failed",
                "feature_id": feature_id,
            },
        )
    return {"output": output, "error": error}


async def _record_feature_control(
    redis: Redis,  # type: ignore[type-arg]
    feature_id: str,
    *,
    action: str,
    status: str,
    output: str = "",
) -> dict[str, Any]:
    payload = {
        "feature_id": feature_id,
        "action": action,
        "status": status,
        "output": output,
        "ts_unix": time.time(),
    }
    await redis.set(
        f"feature:control:last:{feature_id}",
        json.dumps(payload),
        ex=_FEATURE_LAST_CONTROL_TTL_SEC,
    )
    return payload


async def _read_feature_control(
    redis: Redis,  # type: ignore[type-arg]
    feature_id: str,
) -> dict[str, Any] | None:
    raw = await redis.get(f"feature:control:last:{feature_id}")
    if raw is None:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode(errors="replace")
    try:
        loaded = json.loads(str(raw))
    except json.JSONDecodeError:
        return {"feature_id": feature_id, "output": str(raw)}
    return loaded if isinstance(loaded, dict) else None


async def _emit_alert(
    redis: Redis[Any],
    *,
    code: str,
    severity: str,
    message: str,
    tags: dict[str, str],
) -> str:
    """Publish a canonical AlertEvent through the producer surface."""
    producer = Producer(redis)
    alert = AlertEvent(
        alert_id=new_id(),
        ts_event=now_ns(),
        severity=severity,
        source="api.control",
        code=code,
        message=message,
        tags=tags,
    )
    return await producer.publish(STREAM_ALERTS, Event(type="alert", payload=alert))


async def _record_kill_switch_state(
    redis: Redis[Any],
    *,
    engaged: bool,
    actor: str,
    reason: str | None,
    alert_id: str,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "engaged": engaged,
        "actor": actor,
        "reason": reason,
        "alert_id": alert_id,
        "ts_unix": time.time(),
    }
    await redis.set(_KILL_SWITCH_STATE_KEY, json.dumps(payload))
    return payload


async def _read_kill_switch_state(redis: Redis[Any]) -> dict[str, Any]:
    raw = await redis.get(_KILL_SWITCH_STATE_KEY)
    if raw is None:
        return {
            "engaged": False,
            "actor": None,
            "reason": None,
            "alert_id": None,
            "ts_unix": None,
        }
    if isinstance(raw, bytes):
        raw = raw.decode(errors="replace")
    try:
        loaded = json.loads(str(raw))
    except json.JSONDecodeError:
        return {
            "engaged": False,
            "actor": None,
            "reason": None,
            "alert_id": None,
            "ts_unix": None,
        }
    if not isinstance(loaded, dict):
        return {
            "engaged": False,
            "actor": None,
            "reason": None,
            "alert_id": None,
            "ts_unix": None,
        }
    return {
        "engaged": bool(loaded.get("engaged")),
        "actor": loaded.get("actor") if loaded.get("actor") is not None else None,
        "reason": loaded.get("reason") if loaded.get("reason") is not None else None,
        "alert_id": loaded.get("alert_id")
        if loaded.get("alert_id") is not None
        else None,
        "ts_unix": loaded.get("ts_unix") if loaded.get("ts_unix") is not None else None,
    }


@router.get("/kill-switch")
async def kill_switch_state(
    _: dict[str, Any] = Depends(require_user),
    redis: Redis = Depends(get_redis),  # type: ignore[type-arg]
) -> dict[str, Any]:
    return await _read_kill_switch_state(redis)


@router.post("/kill-switch")
async def trip_kill_switch(
    payload: dict[str, Any] = Body(default={}),
    user: dict[str, Any] = Depends(require_user),
    redis: Redis = Depends(get_redis),  # type: ignore[type-arg]
) -> dict[str, Any]:
    reason = str(payload.get("reason", "manual"))
    actor = str(user.get("sub", "unknown"))
    alert_id = await _emit_alert(
        redis,
        code="kill_switch_engaged",
        severity="critical",
        message=f"kill switch tripped by {actor}: {reason}",
        tags={"actor": actor, "reason": reason},
    )
    await _record_kill_switch_state(
        redis,
        engaged=True,
        actor=actor,
        reason=reason,
        alert_id=alert_id,
    )
    return {"ok": True, "alert_id": alert_id}


@router.delete("/kill-switch")
async def clear_kill_switch(
    user: dict[str, Any] = Depends(require_user),
    redis: Redis = Depends(get_redis),  # type: ignore[type-arg]
) -> dict[str, Any]:
    actor = str(user.get("sub", "unknown"))
    alert_id = await _emit_alert(
        redis,
        code="kill_switch_cleared",
        severity="info",
        message=f"kill switch cleared by {actor}",
        tags={"actor": actor},
    )
    await _record_kill_switch_state(
        redis,
        engaged=False,
        actor=actor,
        reason=None,
        alert_id=alert_id,
    )
    return {"ok": True, "alert_id": alert_id}


@router.post("/features/{feature_id}/start")
async def start_feature(
    feature_id: str,
    request: Request,
    _: dict[str, Any] = Depends(require_user),
    redis: Redis = Depends(get_redis),  # type: ignore[type-arg]
) -> dict[str, Any]:
    if feature_id not in _FEATURE_SERVICES:
        raise HTTPException(status_code=404, detail="unknown feature")
    _assert_local_request(request)
    services = _FEATURE_SERVICES[feature_id]
    beats = await read_all(redis)
    fresh = _fresh_services(beats, services)
    blocker = _feature_preflight_blocker(feature_id)
    if blocker is not None:
        output = f"{blocker['message']}\n{blocker['next_step']}"
        await _record_feature_control(
            redis,
            feature_id,
            action="start",
            status="blocked",
            output=output,
        )
        raise HTTPException(
            status_code=409,
            detail={
                "feature_id": feature_id,
                "status": "blocked",
                **blocker,
            },
        )
    if services and len(fresh) == len(services):
        await _record_feature_control(
            redis,
            feature_id,
            action="start",
            status="already_running",
            output="",
        )
        return {
            "ok": True,
            "feature_id": feature_id,
            "action": "start",
            "started": False,
            "status": "already_running",
            "services": services,
            "fresh_services": fresh,
        }

    result = await _run_feature_script(_feature_script_path(), feature_id)
    await _record_feature_control(
        redis,
        feature_id,
        action="start",
        status="launch_requested",
        output=result["output"],
    )
    return {
        "ok": True,
        "feature_id": feature_id,
        "action": "start",
        "started": True,
        "status": "launch_requested",
        "services": services,
        "fresh_services": fresh,
        "output": result["output"],
    }


@router.post("/features/{feature_id}/stop")
async def stop_feature(
    feature_id: str,
    request: Request,
    _: dict[str, Any] = Depends(require_user),
    redis: Redis = Depends(get_redis),  # type: ignore[type-arg]
) -> dict[str, Any]:
    if feature_id not in _FEATURE_SERVICES:
        raise HTTPException(status_code=404, detail="unknown feature")
    _assert_local_request(request)
    services = _FEATURE_SERVICES[feature_id]
    result = await _run_feature_script(_stop_feature_script_path(), feature_id)
    await _record_feature_control(
        redis,
        feature_id,
        action="stop",
        status="stop_requested",
        output=result["output"],
    )
    beats = await read_all(redis)
    fresh = _fresh_services(beats, services)
    return {
        "ok": True,
        "feature_id": feature_id,
        "action": "stop",
        "status": "stop_requested",
        "services": services,
        "fresh_services": fresh,
        "output": result["output"],
    }


@router.post("/features/{feature_id}/restart")
async def restart_feature(
    feature_id: str,
    request: Request,
    _: dict[str, Any] = Depends(require_user),
    redis: Redis = Depends(get_redis),  # type: ignore[type-arg]
) -> dict[str, Any]:
    if feature_id not in _FEATURE_SERVICES:
        raise HTTPException(status_code=404, detail="unknown feature")
    _assert_local_request(request)
    services = _FEATURE_SERVICES[feature_id]
    blocker = _feature_preflight_blocker(feature_id)
    if blocker is not None:
        output = f"{blocker['message']}\n{blocker['next_step']}"
        await _record_feature_control(
            redis,
            feature_id,
            action="restart",
            status="blocked",
            output=output,
        )
        raise HTTPException(
            status_code=409,
            detail={
                "feature_id": feature_id,
                "status": "blocked",
                **blocker,
            },
        )
    stop_result = await _run_feature_script(_stop_feature_script_path(), feature_id)
    start_result = await _run_feature_script(_feature_script_path(), feature_id)
    output = "\n".join(
        part for part in [stop_result["output"], start_result["output"]] if part
    )
    await _record_feature_control(
        redis,
        feature_id,
        action="restart",
        status="restart_requested",
        output=output,
    )
    beats = await read_all(redis)
    fresh = _fresh_services(beats, services)
    return {
        "ok": True,
        "feature_id": feature_id,
        "action": "restart",
        "status": "restart_requested",
        "services": services,
        "fresh_services": fresh,
        "output": output,
    }


@router.get("/features/{feature_id}/logs")
async def feature_logs(
    feature_id: str,
    request: Request,
    _: dict[str, Any] = Depends(require_user),
    redis: Redis = Depends(get_redis),  # type: ignore[type-arg]
) -> dict[str, Any]:
    if feature_id not in _FEATURE_SERVICES:
        raise HTTPException(status_code=404, detail="unknown feature")
    _assert_local_request(request)
    services = _FEATURE_SERVICES[feature_id]
    beats = await read_all(redis)
    fresh = _fresh_services(beats, services)
    return {
        "ok": True,
        "feature_id": feature_id,
        "services": services,
        "fresh_services": fresh,
        "last_control": await _read_feature_control(redis, feature_id),
    }
