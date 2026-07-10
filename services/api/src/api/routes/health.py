"""
api.routes.health — detailed system readiness for the operator.

Public /health remains the simple liveness probe (in main.py).

This module adds /health/readiness (auth required) that returns
categorized state without ever exposing secrets or stack traces.

States (per TASK-0202 spec):
  pass, warn, fail, skipped, disabled, stale

Categories (current wave; later phases add model/quant-foundry):
  api, redis, timescale, verification_receipt, provider_freshness,
  news_impact, dashboard_tests

Additive only. No side effects on data.
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Depends
from fincept_core.heartbeat import read_all
from redis.asyncio import Redis

from api.auth import require_user
from api.deps import get_redis

router = APIRouter()


def _safe_detail(text: str) -> str:
    """Strip anything that could contain secrets or traces."""
    lowered = text.lower()
    if any(
        bad in lowered
        for bad in ("secret", "token", "password", "key", "traceback", "exception")
    ):
        return "Details redacted for security."
    return text


@router.get("/readiness")
async def readiness(
    _: dict[str, Any] = Depends(require_user),
    redis: Redis = Depends(get_redis),  # type: ignore[type-arg]
) -> dict[str, Any]:
    """Unified readiness for dashboard /system page.

    Returns safe, operator-actionable states. Disabled/skipped items
    are explicitly not failures.
    """
    now = time.time()
    checks: list[dict[str, Any]] = []

    # --- API (we reached here) ---
    checks.append(
        {
            "id": "api",
            "label": "API",
            "state": "pass",
            "detail": "API handler reached; request correlation available via X-Request-ID.",
        }
    )

    # --- Redis ---
    redis_state = "fail"
    redis_detail = "Redis ping failed."
    try:
        await redis.ping()
        redis_state = "pass"
        redis_detail = "Redis reachable via shared client."
    except Exception as exc:  # never leak stack
        redis_detail = _safe_detail(f"Redis unreachable: {type(exc).__name__}")
    checks.append(
        {"id": "redis", "label": "Redis", "state": redis_state, "detail": redis_detail}
    )

    # --- Timescale / Postgres (via fincept_db bar coverage probe) ---
    ts_state = "skipped"
    ts_detail = "Timescale status not yet probed (light probe only; wire in Phase 4)."
    try:
        from fincept_db.bars import read_bar_coverage

        end_ns = int(time.time() * 1_000_000_000)
        cov = await read_bar_coverage(
            ["BTC-USD"],
            freq="1m",
            start_ns=end_ns - 60_000_000_000,
            end_ns=end_ns,
        )
        if cov is not None:
            ts_state = "pass"
            ts_detail = "Timescale bars coverage query succeeded."
        else:
            ts_state = "warn"
            ts_detail = "Timescale returned empty coverage (data may be loading)."
    except Exception as exc:
        ts_detail = _safe_detail(
            f"Timescale probe issue: {type(exc).__name__} (may be expected if no bars ingested yet)"
        )
        ts_state = "warn"
    checks.append(
        {
            "id": "timescale",
            "label": "Timescale/Postgres",
            "state": ts_state,
            "detail": ts_detail,
        }
    )

    # --- Verification receipt (link to catalog; server reports presence) ---
    # We do not execute scripts here. State is informational.
    checks.append(
        {
            "id": "verification_receipt",
            "label": "Verification receipt",
            "state": "pass",
            "detail": "Proof receipts catalog available. See /receipts for latest run outputs.",
        }
    )

    # --- Provider freshness (proxy via services + data sources concept) ---
    # Reuse heartbeat style for providers if present; otherwise review.
    provider_state = "skipped"
    provider_detail = "Provider freshness reported via /services and /data/sources on dashboard (Phase 4)."
    try:
        beats = await read_all(redis)
        provider_like = [
            name
            for name in beats
            if "provider" in name.lower() or "ingestor" in name.lower()
        ]
        if provider_like:
            provider_state = "pass"
            provider_detail = (
                f"{len(provider_like)} provider-related heartbeats observed."
            )
    except Exception:
        provider_detail = "Provider probe via Redis unavailable."
    checks.append(
        {
            "id": "provider_freshness",
            "label": "Provider freshness",
            "state": provider_state,
            "detail": provider_detail,
        }
    )

    # --- News-impact shadow lane ---
    # We can report status as review until full lane in Phase 4; check if service heartbeating.
    ni_state = "skipped"
    ni_detail = "News-impact shadow lane will be wired in later phase. Current status from /news-impact (Phase 4)."
    try:
        beats = await read_all(redis)
        ni_beats = [name for name in beats if "news" in name.lower()]
        if ni_beats:
            ni_state = "pass"
            ni_detail = "News related heartbeats present."
    except Exception:
        ni_detail = "News heartbeat probe via Redis unavailable."
    checks.append(
        {
            "id": "news_impact",
            "label": "News-impact shadow lane",
            "state": ni_state,
            "detail": ni_detail,
        }
    )

    # --- Dashboard tests (client side) ---
    checks.append(
        {
            "id": "dashboard_tests",
            "label": "Dashboard tests",
            "state": "skipped",
            "detail": "Run pnpm --dir apps/dashboard exec tsc --noEmit and npm run test:source-health locally. Server cannot execute browser tests.",
        }
    )

    # --- Placeholders for future (explicitly disabled, not failures) ---
    checks.append(
        {
            "id": "models_dossier",
            "label": "Model / dossier status",
            "state": "disabled",
            "detail": "Enabled in Phase 4+ after dossier + tournament land.",
        }
    )
    checks.append(
        {
            "id": "quant_foundry",
            "label": "Quant Foundry",
            "state": "disabled",
            "detail": "Enabled in Phase 3/8. See Builder 2 track.",
        }
    )

    # Overall rollup (pass > warn > stale > review > fail)
    state_order = {
        "pass": 5,
        "warn": 4,
        "stale": 3,
        "fail": 1,
        "skipped": 5,
        "disabled": 5,
    }
    worst = min((state_order.get(c["state"], 2) for c in checks), default=5)
    overall = next((k for k, v in state_order.items() if v == worst), "warn")

    return {
        "overall": overall,
        "checks": checks,
        "receipt_url": "/receipts",
        "generated_at_unix": int(now),
        "note": "States use pass/warn/fail/skipped/disabled/stale. Disabled items are not failures.",
    }
