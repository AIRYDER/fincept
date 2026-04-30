"""
api.routes.services - per-service heartbeat status.

Reads ``service:heartbeat:*`` keys from Redis and reports each service
as UP / STALE / DOWN based on the last beat timestamp.  No auth check
beyond ``require_user`` because operators should always be able to see
which background services are alive.
"""

from __future__ import annotations

import os
import pathlib
import time
from typing import Any

from fastapi import APIRouter, Depends
from redis.asyncio import Redis

from api.auth import require_user
from api.deps import get_redis
from fincept_core.config import get_settings
from fincept_core.heartbeat import DEFAULT_TTL_SEC, read_all

router = APIRouter()

# Always-expected services.  Each must heartbeat or it shows as DOWN.
_CORE_EXPECTED_SERVICES: list[str] = [
    "api",
    "ingestor",
    "features",
    "orchestrator",
    "oms",
    "portfolio",
    "jobs",
]

# gbm_predictor is opt-in - only counted as expected when a trained
# model exists on disk.  Otherwise we still surface its heartbeat if
# present (rogue lane), but a missing key isn't an error.
_GBM_MODEL_DIR = pathlib.Path(
    os.environ.get("GBM_MODEL_DIR", "models/gbm_predictor")
)


def _expected_services() -> list[str]:
    expected = list(_CORE_EXPECTED_SERVICES)
    if (_GBM_MODEL_DIR / "model.txt").exists():
        expected.append("gbm_predictor")
    settings = get_settings()
    if settings.NEWSAPI_API_KEY and (
        settings.ANTHROPIC_API_KEY or settings.OPENAI_API_KEY
    ):
        expected.append("sentiment_agent")
    if settings.FRED_API_KEY:
        expected.append("regime_agent")
    return expected

# A heartbeat older than this many seconds is reported as STALE; older
# than DEFAULT_TTL_SEC is automatically gone (key expired) so we only
# need a STALE band between them.
STALE_AFTER_SEC = 15


@router.get("")
async def list_services(
    _: dict[str, Any] = Depends(require_user),
    redis: Redis = Depends(get_redis),  # type: ignore[type-arg]
) -> dict[str, Any]:
    """Return per-service status with last-beat age.

    Status values:
      - ``up``     last beat within STALE_AFTER_SEC
      - ``stale``  last beat older than STALE_AFTER_SEC but still in TTL
      - ``down``   no heartbeat key present (service crashed or never started)
    """
    beats = await read_all(redis)
    now = time.time()
    expected = _expected_services()

    services: list[dict[str, Any]] = []
    seen: set[str] = set()

    for name in expected:
        last_beat = beats.get(name)
        seen.add(name)
        if last_beat is None:
            services.append(
                {
                    "name": name,
                    "status": "down",
                    "last_beat_unix": None,
                    "age_sec": None,
                    "expected": True,
                }
            )
            continue
        age = now - last_beat
        services.append(
            {
                "name": name,
                "status": "up" if age <= STALE_AFTER_SEC else "stale",
                "last_beat_unix": last_beat,
                "age_sec": round(age, 1),
                "expected": True,
            }
        )

    # Surface any heartbeats from services not on the expected list -
    # useful when you spin up an experimental agent and forget to
    # register it.  Won't ever appear in a production deploy but
    # helps in dev.
    for name, last_beat in beats.items():
        if name in seen:
            continue
        age = now - last_beat
        services.append(
            {
                "name": name,
                "status": "up" if age <= STALE_AFTER_SEC else "stale",
                "last_beat_unix": last_beat,
                "age_sec": round(age, 1),
                "expected": False,
            }
        )

    up = sum(1 for s in services if s["status"] == "up")
    return {
        "services": services,
        "summary": {
            "up": up,
            "expected": len(expected),
            "stale_after_sec": STALE_AFTER_SEC,
            "ttl_sec": DEFAULT_TTL_SEC,
        },
    }
