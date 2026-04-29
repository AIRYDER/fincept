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

from typing import Any

from fastapi import APIRouter, Body, Depends
from redis.asyncio import Redis

from api.auth import require_user
from api.deps import get_redis
from fincept_bus.producer import Producer
from fincept_bus.streams import STREAM_ALERTS
from fincept_core.clock import now_ns
from fincept_core.events import Event
from fincept_core.ids import new_id
from fincept_core.schemas import AlertEvent

router = APIRouter()


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
    return {"ok": True, "alert_id": alert_id}
