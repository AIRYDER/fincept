"""
api.ws — WebSocket multiplexer over Redis Streams.

Client subscribes to a set of topics by sending a JSON frame on
connection:

    {"topics": ["positions", "fills", "predictions", "alerts"]}

The server then pushes one JSON frame per stream message in monotonic
order across the subscribed topics.  Each frame has the shape:

    {"topic": "positions", "event": <full Event JSON>}

Implementation: a single ``redis.xread`` loop watches all subscribed
streams concurrently with a short block timeout.  No consumer groups —
WebSockets are transient broadcast, not durable.  If a client
disconnects and reconnects, they pick up from the live tail (``$``);
catching up on missed events is a different concern (use the audit log
or REST endpoints for backfill).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import jwt
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, status
from fincept_bus.streams import (
    STREAM_ALERTS,
    STREAM_FILLS,
    STREAM_POSITIONS,
    STREAM_SIG_PREDICT,
)
from fincept_core.config import get_settings
from fincept_core.events import deserialize
from redis.asyncio import Redis

_log = logging.getLogger(__name__)

router = APIRouter()

# Topic name (client-facing) -> Redis stream key.
_TOPIC_STREAMS: dict[str, str] = {
    "positions": STREAM_POSITIONS,
    "fills": STREAM_FILLS,
    "predictions": STREAM_SIG_PREDICT,
    "alerts": STREAM_ALERTS,
}

DEFAULT_TOPICS = ("positions", "fills", "alerts")
BLOCK_MS = 1_000  # how long XREAD blocks waiting for new messages


async def _authenticate(ws: WebSocket) -> dict[str, Any] | None:
    """Verify the bearer token from the ``Authorization`` header.

    Returns the decoded claims, or None if unauthenticated.  We do NOT
    accept the token via ``?token=...`` query string because web servers
    log query strings in access logs, which would leak the JWT.
    """
    auth = ws.headers.get("authorization")
    if not auth or not auth.lower().startswith("bearer "):
        return None
    token = auth.split(" ", 1)[1].strip()
    if not token:
        return None
    try:
        return jwt.decode(token, get_settings().JWT_SECRET, algorithms=["HS256"])
    except jwt.PyJWTError:
        return None


def _resolve_topics(requested: list[str] | None) -> dict[str, str]:
    """Map requested topic names to their Redis stream keys.

    Unknown topic names are silently dropped (the client can re-send
    its subscription if it wants to reset).  At minimum we honour the
    DEFAULT_TOPICS so the dashboard's first connection always shows
    something useful.
    """
    requested = requested or list(DEFAULT_TOPICS)
    out: dict[str, str] = {}
    for topic in requested:
        if topic in _TOPIC_STREAMS:
            out[topic] = _TOPIC_STREAMS[topic]
    return out


@router.websocket("/stream")
async def stream(ws: WebSocket) -> None:
    claims = await _authenticate(ws)
    if claims is None:
        await ws.close(
            code=status.WS_1008_POLICY_VIOLATION, reason="missing or invalid token"
        )
        return

    await ws.accept()
    settings = get_settings()
    redis: Redis[Any] = Redis.from_url(settings.REDIS_URL)

    # First frame may carry topic subscription; otherwise use defaults.
    try:
        first = await asyncio.wait_for(ws.receive_json(), timeout=1.0)
        topic_map = _resolve_topics(
            first.get("topics") if isinstance(first, dict) else None
        )
    except (TimeoutError, Exception):
        topic_map = _resolve_topics(None)

    if not topic_map:
        await ws.close(code=status.WS_1008_POLICY_VIOLATION, reason="no valid topics")
        await redis.aclose()  # type: ignore[attr-defined]
        return

    # Track per-stream cursors so we don't replay history on reconnect;
    # "$" tells XREAD "only deliver messages newer than now".
    cursors: dict[str, str] = dict.fromkeys(topic_map.values(), "$")
    stream_to_topic = {v: k for k, v in topic_map.items()}

    try:
        while True:
            response = await redis.xread(streams=cursors, block=BLOCK_MS, count=100)
            for stream_name_raw, messages in response:
                stream_name = (
                    stream_name_raw.decode()
                    if isinstance(stream_name_raw, bytes)
                    else stream_name_raw
                )
                topic = stream_to_topic.get(stream_name)
                if topic is None:
                    continue
                for message_id, fields in messages:
                    cursors[stream_name] = (
                        message_id.decode()
                        if isinstance(message_id, bytes)
                        else message_id
                    )
                    try:
                        event = deserialize(fields)
                    except Exception:
                        continue
                    await ws.send_json(
                        {
                            "topic": topic,
                            "event": {
                                "type": event.type,
                                "payload": event.payload.model_dump(mode="json"),
                            },
                        }
                    )
    except WebSocketDisconnect:
        pass
    finally:
        try:
            await redis.aclose()  # type: ignore[attr-defined]
        except Exception:
            _log.warning("ws.redis_close_failed", exc_info=True)
