"""
api.openbb_health_store — Redis-backed persistence for OpenBB health probes.

Two keys are written on every probe:

``obb:health:last`` (STRING)
    Latest raw probe dict, JSON-encoded.  Cheap ``GET`` for the pill.

``obb:health:log`` (STREAM)
    Append-only log of the last ~720 probes with ``MAXLEN ~ 720`` so
    the series naturally caps at a few hours of history regardless of
    polling cadence.  Each stream entry carries ``ok``, ``latency_ms``,
    ``url``, ``error``, and ``error_type`` as plain string fields so a
    dashboard or ops dashboard can read them without having to parse
    JSON blobs.

The persistence call is best-effort: Redis failures must never prevent
the live probe response from reaching the dashboard, so callers wrap
the write in a try/except and log warnings only.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from redis.asyncio import Redis

LAST_KEY = "obb:health:last"
LOG_KEY = "obb:health:log"
#: Rolling log cap.  At a 30 s poll cadence this covers ~6 h of history,
#: which is plenty for uptime sparklines in the dashboard without
#: growing unbounded when a polling client is left open overnight.
LOG_MAXLEN = 720

logger = logging.getLogger(__name__)


def _coerce_scalar(value: Any) -> str:
    """Stream fields must be strings / bytes / ints / floats.

    None and booleans become stable string literals so readers don't
    have to distinguish ``b"None"`` from missing.
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int | float):
        return str(value)
    return str(value)


async def record_health(redis: Redis[Any], result: dict[str, Any]) -> None:
    """Persist a single health probe result.

    Writes both the ``last`` snapshot and one stream entry.  Any Redis
    error is swallowed and logged — health persistence is a nice-to-
    have, never a request blocker.
    """
    now_ms = int(time.time() * 1000)
    snapshot = {"ts_ms": now_ms, **result}

    try:
        await redis.set(LAST_KEY, json.dumps(snapshot))
        await redis.xadd(
            LOG_KEY,
            {
                "ts_ms": str(now_ms),
                "ok": _coerce_scalar(result.get("ok")),
                "latency_ms": _coerce_scalar(result.get("latency_ms")),
                "url": _coerce_scalar(result.get("url")),
                "error_type": _coerce_scalar(result.get("error_type")),
                "error": _coerce_scalar(result.get("error")),
                "warning": _coerce_scalar(result.get("warning")),
            },
            maxlen=LOG_MAXLEN,
            approximate=True,
        )
    except Exception:
        logger.warning("openbb_health_persist_failed", exc_info=True)


def _decode(field: Any) -> str:
    if isinstance(field, bytes):
        return field.decode("utf-8", errors="replace")
    return str(field)


def _parse_entry(entry_id: Any, fields: dict[Any, Any]) -> dict[str, Any]:
    decoded: dict[str, str] = {_decode(k): _decode(v) for k, v in fields.items()}
    ts_ms = int(decoded.get("ts_ms") or 0)
    ok = decoded.get("ok") == "1"
    latency_raw = decoded.get("latency_ms")
    latency_ms: int | None
    if latency_raw:
        try:
            latency_ms = int(float(latency_raw))
        except ValueError:
            latency_ms = None
    else:
        latency_ms = None
    entry: dict[str, Any] = {
        "id": _decode(entry_id),
        "ts_ms": ts_ms,
        "ok": ok,
        "latency_ms": latency_ms,
        "url": decoded.get("url") or None,
        "error_type": decoded.get("error_type") or None,
        "error": decoded.get("error") or None,
        "warning": decoded.get("warning") or None,
    }
    return entry


async def fetch_history(
    redis: Redis[Any],
    *,
    limit: int = 120,
) -> list[dict[str, Any]]:
    """Return the most recent ``limit`` probe entries, oldest → newest.

    Reading XREVRANGE + reversing keeps the hot path O(limit) even when
    the stream is full, and gives the dashboard a chart-ready series.
    """
    try:
        raw = await redis.xrevrange(LOG_KEY, count=limit)
    except Exception:
        logger.warning("openbb_health_fetch_failed", exc_info=True)
        return []
    entries = [_parse_entry(entry_id, fields) for entry_id, fields in raw]
    entries.reverse()
    return entries


def summarise(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """Compact uptime / latency summary over ``entries``.

    Pure function so tests can exercise it without Redis.  Empty inputs
    return a fully null summary rather than raising so the dashboard
    can render the "no data yet" state uniformly.
    """
    if not entries:
        return {
            "samples": 0,
            "uptime_pct": None,
            "latency_p50_ms": None,
            "latency_p95_ms": None,
            "last_error_type": None,
        }
    ok_count = sum(1 for e in entries if e["ok"])
    latencies = sorted(
        e["latency_ms"] for e in entries if e["ok"] and e["latency_ms"] is not None
    )
    p50: int | None = None
    p95: int | None = None
    if latencies:
        p50 = latencies[len(latencies) // 2]
        p95_index = max(0, round(len(latencies) * 0.95) - 1)
        p95 = latencies[min(p95_index, len(latencies) - 1)]
    last_error = next(
        (e["error_type"] for e in reversed(entries) if not e["ok"]),
        None,
    )
    return {
        "samples": len(entries),
        "uptime_pct": round(ok_count / len(entries) * 100.0, 2),
        "latency_p50_ms": p50,
        "latency_p95_ms": p95,
        "last_error_type": last_error,
    }
