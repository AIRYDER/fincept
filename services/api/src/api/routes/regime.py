"""
api.routes.regime - latest macro regime + classifier inputs.

Reads the snapshot key written by the regime_agent
(``service:regime:latest``) and surfaces the regime label, confidence,
the raw FRED inputs (VIX, yield spread, fed funds), and the
classifier rationale.  When the agent isn't running (or the key
expired), returns ``status="unavailable"`` so the dashboard can show
a placeholder instead of crashing.

Optional ``?history=N`` reads the last N events from
``STREAM_SIG_REGIME`` so operators can see the recent regime change
timeline.  History is bounded (default 0, max 100) to keep the
endpoint cheap.
"""

from __future__ import annotations

import json
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fincept_bus.streams import STREAM_SIG_REGIME
from redis.asyncio import Redis

from api.auth import require_user
from api.deps import get_redis

router = APIRouter()

# Must match agents/regime_agent/main.py REGIME_SNAPSHOT_KEY.
SNAPSHOT_KEY = "service:regime:latest"
HISTORY_MAX = 100


def _decode_value(raw: Any) -> str:
    return raw.decode() if isinstance(raw, (bytes, bytearray)) else str(raw)


async def _read_snapshot(redis: Redis[Any]) -> dict[str, Any] | None:
    """Read + parse the snapshot key, or None if missing / unparseable."""
    raw = await redis.get(SNAPSHOT_KEY)
    if raw is None:
        return None
    try:
        parsed = json.loads(_decode_value(raw))
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None
    # Confirmed dict; coerce keys to str for mypy + safety.
    return {str(k): v for k, v in parsed.items()}


async def _read_history(redis: Redis[Any], *, count: int) -> list[dict[str, Any]]:
    """Return the last ``count`` regime stream events, newest first.

    Uses XREVRANGE so even on a stream with thousands of entries we
    only pay for the trailing window.  Each entry is dict-shaped to
    match the snapshot for easy table rendering on the dashboard.
    """
    if count <= 0:
        return []
    entries = await redis.xrevrange(STREAM_SIG_REGIME, count=count)
    parsed: list[dict[str, Any]] = []
    for entry_id, fields in entries:
        # Stream values are stored as redis-string field maps; the
        # producer puts the JSON-encoded payload under "payload".
        decoded = {
            (k.decode() if isinstance(k, (bytes, bytearray)) else k): _decode_value(v)
            for k, v in fields.items()
        }
        try:
            payload = json.loads(decoded.get("payload", "{}"))
        except json.JSONDecodeError:
            continue
        parsed.append(
            {
                "stream_id": entry_id.decode()
                if isinstance(entry_id, (bytes, bytearray))
                else str(entry_id),
                "agent_id": payload.get("agent_id"),
                "ts_event": payload.get("ts_event"),
                "regime": payload.get("regime"),
                "confidence": payload.get("confidence"),
            }
        )
    return parsed


@router.get("")
async def get_regime(
    history: int = Query(0, ge=0, le=HISTORY_MAX),
    _: dict[str, Any] = Depends(require_user),
    redis: Redis = Depends(get_redis),  # type: ignore[type-arg]
) -> dict[str, Any]:
    """Return the latest classifier view + optional change history.

    Response shape::

        {
          "status": "ok" | "unavailable",
          "snapshot": { ...full regime view..., "age_seconds": int } | None,
          "history": [ { stream_id, regime, confidence, ts_event, agent_id }, ... ],
          "direction_map": { "risk_on": 0.20, ... }
        }

    ``status="unavailable"`` means no snapshot key was found (agent
    down or key TTL expired); the dashboard should render an "agent
    inactive" placeholder rather than treat zero as a real reading.
    """
    if history > HISTORY_MAX:
        raise HTTPException(
            status_code=400, detail=f"history exceeds max of {HISTORY_MAX}"
        )
    snapshot = await _read_snapshot(redis)
    history_rows = await _read_history(redis, count=history)
    if snapshot is None:
        return {
            "status": "unavailable",
            "snapshot": None,
            "history": history_rows,
            "direction_map": _DIRECTION_MAP,
        }

    # Augment with age so the dashboard can show "freshness 12s ago".
    ts_event_ns = snapshot.get("ts_event")
    age_seconds: float | None = None
    if isinstance(ts_event_ns, (int, float)):
        age_seconds = max(0.0, time.time() - (float(ts_event_ns) / 1_000_000_000))
    snapshot = {**snapshot, "age_seconds": age_seconds}
    return {
        "status": "ok",
        "snapshot": snapshot,
        "history": history_rows,
        "direction_map": _DIRECTION_MAP,
    }


# Imported lazily so test runs don't pay for the agents-package import
# tree just to load this module.  Falls back to an empty dict if the
# regime agent isn't installed.
def _load_direction_map() -> dict[str, float]:
    try:
        from agents.regime_agent.rules import REGIME_DIRECTION

        return dict(REGIME_DIRECTION)
    except ImportError:
        return {}


_DIRECTION_MAP: dict[str, float] = _load_direction_map()
