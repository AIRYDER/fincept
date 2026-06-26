"""Research endpoints for read-only external intelligence tools."""

from __future__ import annotations

import logging
from collections import Counter
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from redis.asyncio import Redis

from api.auth import require_user
from api.deps import get_redis
from api.openbb_health_store import fetch_history, record_health, summarise
from api.rate_limit import RateLimitExceeded, enforce_rate_limit
from fincept_db.provider_data import (
    ProviderDataRecord,
    build_exa_record,
    build_openbb_call_record,
    build_openbb_quote_record,
    read_provider_data,
    write_provider_data,
)
from fincept_tools.research.exa import ExaMarketResearchInput, ExaMarketResearchTool
from fincept_tools.research.openbb import (
    OpenBBCallInput,
    OpenBBCallTool,
    OpenBBQuoteInput,
    OpenBBQuoteTool,
    check_openbb_health,
    check_openbb_readiness,
)

router = APIRouter()
logger = logging.getLogger(__name__)

SearchType = Literal["auto", "fast", "instant", "deep-lite", "deep", "deep-reasoning"]

# Same path validator the tool uses; mirrored here so we can return a
# proper 422 with a friendly field name instead of bubbling the regex
# failure out of OpenBBCallInput.
_OPENBB_PATH_PATTERN = r"^/api/v1/[A-Za-z0-9._/-]+$"

# --------------------------------------------------------------------------- #
# OpenBB dispatcher guardrails                                                #
# --------------------------------------------------------------------------- #
#
# The dispatcher lets the dashboard hit any ``/api/v1/...`` endpoint on the
# local OpenBB backend without us shipping a bespoke Pydantic model per
# endpoint.  That power comes with two guardrails:
#
#   1. An allowlist of top-level namespaces.  OpenBB's API is read-only,
#      but new /admin/* style endpoints could land upstream — the
#      allowlist keeps us from accidentally exposing them the day they
#      do.  Extend via ``OPENBB_DISPATCH_ALLOWED_PREFIXES`` if you need
#      to whitelist a new namespace without a code change.
#
#   2. A per-user rate limit backed by Redis.  The dispatcher is the
#      shortest path from an LLM agent to an external provider, which
#      is exactly where runaway loops cost real money.  A 60-request /
#      60-second fixed window is enough for humans browsing panels
#      while still tripping obviously-malicious patterns.
OPENBB_DISPATCH_ALLOWED_PREFIXES: tuple[str, ...] = (
    "/api/v1/equity/",
    "/api/v1/etf/",
    "/api/v1/index/",
    "/api/v1/derivatives/",
    "/api/v1/economy/",
    "/api/v1/fixedincome/",
    "/api/v1/currency/",
    "/api/v1/commodity/",
    "/api/v1/crypto/",
    "/api/v1/news/",
    "/api/v1/regulators/",
)
OPENBB_DISPATCH_RATE_LIMIT = 60
OPENBB_DISPATCH_RATE_WINDOW_SEC = 60


# --------------------------------------------------------------------------- #
# Error -> HTTP status mapping                                                #
# --------------------------------------------------------------------------- #
#
# ``BaseTool`` catches typed ``ToolError`` subclasses and serialises them
# as ``ok=False`` output dicts.  That's convenient for agents but the
# REST surface needs proper HTTP semantics so the dashboard can branch
# on status + ``error_type`` without string-parsing the body.
_OPENBB_ERROR_STATUS: dict[str, int] = {
    "OpenBBUnavailable": 503,
    "ToolBackendError": 502,
}


def _status_for_openbb_error(error_type: str | None) -> int:
    return _OPENBB_ERROR_STATUS.get(error_type or "", 500)


def _respond_with_tool_result(result: dict[str, Any]) -> dict[str, Any] | JSONResponse:
    """Translate an ``ok=False`` tool dict into a stable HTTP error.

    The returned body preserves the tool's ``{ok, error, error_type}``
    shape so the dashboard can branch on ``error_type`` and render the
    raw message.  Successful results are returned verbatim for FastAPI
    to serialise.
    """
    if result.get("ok"):
        return result
    status_code = _status_for_openbb_error(result.get("error_type"))
    return JSONResponse(status_code=status_code, content=result)


class ExaResearchRequest(BaseModel):
    query: str = Field(min_length=3, max_length=500)
    symbol: str | None = Field(default=None, max_length=16)
    search_type: SearchType = "deep"
    num_results: int = Field(default=10, ge=1, le=20)
    max_age_hours: int | None = Field(default=None, ge=-1)


class OpenBBQuoteRequest(BaseModel):
    symbol: str = Field(min_length=1, max_length=32)
    provider: str = Field(default="yfinance", min_length=1, max_length=64)


class OpenBBCallRequest(BaseModel):
    """Generic OpenBB dispatch payload.

    The dashboard sends ``path`` as the segment after the OpenBB API
    base URL and ``params`` as a flat string-to-string dict — same
    shape the underlying tool consumes, just with the auth wrapping
    handled at the route layer.
    """

    path: str = Field(min_length=1, max_length=256, pattern=_OPENBB_PATH_PATTERN)
    params: dict[str, str] = Field(default_factory=dict)


async def run_exa_research(**kwargs: object) -> dict[str, Any]:
    tool = ExaMarketResearchTool()
    payload = ExaMarketResearchInput.model_validate(kwargs)
    result = await tool(payload)
    return result.model_dump()


async def run_openbb_quote(**kwargs: object) -> dict[str, Any]:
    tool = OpenBBQuoteTool()
    payload = OpenBBQuoteInput.model_validate(kwargs)
    result = await tool(payload)
    return result.model_dump()


async def run_openbb_call(**kwargs: object) -> dict[str, Any]:
    tool = OpenBBCallTool()
    payload = OpenBBCallInput.model_validate(kwargs)
    result = await tool(payload)
    return result.model_dump()


async def run_openbb_health() -> dict[str, Any]:
    result = dict(await check_openbb_health())
    extra = {
        "openbb_ok": result.get("ok"),
        "openbb_url": result.get("url"),
        "openbb_latency_ms": result.get("latency_ms"),
        "openbb_error_type": result.get("error_type"),
    }
    if result.get("ok"):
        logger.info("openbb_health_ok", extra=extra)
    else:
        logger.warning("openbb_health_failed", extra=extra)
    return result


async def run_openbb_readiness(symbol: str, provider: str) -> dict[str, Any]:
    result = dict(await check_openbb_readiness(symbol=symbol, provider=provider))
    extra = {
        "openbb_ok": result.get("ok"),
        "openbb_api_reachable": result.get("api_reachable"),
        "openbb_provider_ready": result.get("provider_ready"),
        "openbb_url": result.get("url"),
        "openbb_symbol": result.get("symbol"),
        "openbb_provider": result.get("provider"),
    }
    if result.get("ok"):
        logger.info("openbb_readiness_ok", extra=extra)
    else:
        logger.warning("openbb_readiness_failed", extra=extra)
    return result


async def _capture_provider_record(record: ProviderDataRecord) -> None:
    try:
        await write_provider_data([record])
    except RuntimeError as exc:  # pragma: no cover - defensive best-effort capture
        if "FINCEPT_DB_URL is empty" in str(exc):
            logger.debug(
                "provider_data_capture_skipped",
                extra={
                    "provider": record.provider,
                    "source": record.source,
                    "dataset": record.dataset,
                    "endpoint": record.endpoint,
                    "symbol": record.symbol,
                    "reason": "db_url_missing",
                },
            )
            return
        logger.warning(
            "provider_data_capture_failed",
            extra={
                "provider": record.provider,
                "source": record.source,
                "dataset": record.dataset,
                "endpoint": record.endpoint,
                "symbol": record.symbol,
                "error_type": type(exc).__name__,
            },
            exc_info=True,
        )
    except Exception as exc:  # pragma: no cover - defensive best-effort capture
        logger.warning(
            "provider_data_capture_failed",
            extra={
                "provider": record.provider,
                "source": record.source,
                "dataset": record.dataset,
                "endpoint": record.endpoint,
                "symbol": record.symbol,
                "error_type": type(exc).__name__,
            },
            exc_info=True,
        )


def _is_path_allowed(path: str) -> bool:
    return any(path.startswith(prefix) for prefix in OPENBB_DISPATCH_ALLOWED_PREFIXES)


def _provider_record_to_response(record: ProviderDataRecord) -> dict[str, Any]:
    return {
        "record_id": record.record_id,
        "schema_version": record.schema_version,
        "provider": record.provider,
        "source": record.source,
        "dataset": record.dataset,
        "endpoint": record.endpoint,
        "symbol": record.symbol,
        "ts_event": record.ts_event,
        "ts_observed": record.ts_observed,
        "request_hash": record.request_hash,
        "row_count": record.row_count,
        "ok": record.ok,
        "error_type": record.error_type,
        "normalized": record.normalized,
    }


def _provider_capture_summary(records: list[ProviderDataRecord]) -> dict[str, Any]:
    providers = Counter(record.provider for record in records)
    datasets = Counter(record.dataset for record in records)
    latest_ts_event = max((record.ts_event for record in records), default=None)
    return {
        "total_records": len(records),
        "ok_records": sum(1 for record in records if record.ok),
        "error_records": sum(1 for record in records if not record.ok),
        "latest_ts_event": latest_ts_event,
        "providers": dict(sorted(providers.items())),
        "datasets": dict(sorted(datasets.items())),
    }


def _provider_capture_unavailable(error_type: str, error: str) -> dict[str, Any]:
    return {
        "ok": False,
        "capture_enabled": False,
        "error_type": error_type,
        "error": error,
        "summary": _provider_capture_summary([]),
        "records": [],
    }


@router.get("/provider-data")
async def provider_data_status(
    provider: str | None = Query(default=None, min_length=1, max_length=32),
    dataset: str | None = Query(default=None, min_length=1, max_length=128),
    symbol: str | None = Query(default=None, min_length=1, max_length=32),
    limit: int = Query(default=20, ge=1, le=100),
    _user: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    clean_provider = provider.strip().lower() if provider else None
    clean_dataset = dataset.strip() if dataset else None
    clean_symbol = symbol.strip().upper() if symbol else None
    try:
        records = await read_provider_data(
            provider=clean_provider,
            dataset=clean_dataset,
            symbol=clean_symbol,
            limit=limit,
        )
    except RuntimeError as exc:
        if "FINCEPT_DB_URL is empty" in str(exc):
            return _provider_capture_unavailable("ProviderDataDisabled", str(exc))
        logger.warning(
            "provider_data_read_failed",
            extra={"error_type": type(exc).__name__},
            exc_info=True,
        )
        return _provider_capture_unavailable(type(exc).__name__, str(exc))
    except Exception as exc:
        logger.warning(
            "provider_data_read_failed",
            extra={"error_type": type(exc).__name__},
            exc_info=True,
        )
        return _provider_capture_unavailable(type(exc).__name__, str(exc))
    return {
        "ok": True,
        "capture_enabled": True,
        "summary": _provider_capture_summary(records),
        "records": [_provider_record_to_response(record) for record in records],
    }


@router.post("/exa")
async def exa_research(
    body: ExaResearchRequest,
    _user: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    """Run read-only Exa research and return a structured operator brief."""
    request_payload = body.model_dump()
    result = await run_exa_research(**request_payload)
    await _capture_provider_record(
        build_exa_record(request=request_payload, response=result)
    )
    return result


@router.post("/openbb/quote", response_model=None)
async def openbb_quote(
    body: OpenBBQuoteRequest,
    _user: dict[str, Any] = Depends(require_user),
) -> dict[str, Any] | JSONResponse:
    """Run read-only OpenBB quote lookup for a symbol."""
    data = body.model_dump()
    data["symbol"] = body.symbol.strip().upper()
    data["provider"] = body.provider.strip()
    result = await run_openbb_quote(**data)
    await _capture_provider_record(
        build_openbb_quote_record(request=data, response=result)
    )
    return _respond_with_tool_result(result)


@router.post("/openbb", response_model=None)
async def openbb_call(
    body: OpenBBCallRequest,
    user: dict[str, Any] = Depends(require_user),
    redis: Redis = Depends(get_redis),  # type: ignore[type-arg]
) -> dict[str, Any] | JSONResponse:
    """Generic OpenBB API dispatcher.

    Forwards ``{path, params}`` to the configured local OpenBB API and
    returns the normalised tool output.  Two guardrails apply before
    the call is made:

    * **Allowlist**: ``path`` must start with one of
      :data:`OPENBB_DISPATCH_ALLOWED_PREFIXES`.  Anything else returns
      ``403`` with ``error_type='PathNotAllowed'``.
    * **Rate limit**: ``OPENBB_DISPATCH_RATE_LIMIT`` requests per
      ``OPENBB_DISPATCH_RATE_WINDOW_SEC`` seconds per authenticated
      user.  Breaching it returns ``429`` with ``Retry-After`` and
      ``error_type='RateLimited'``.

    Tool-layer failures are mapped to HTTP status codes so callers can
    branch on status without parsing strings:
    ``OpenBBUnavailable`` → 503, ``ToolBackendError`` → 502.
    """
    if not _is_path_allowed(body.path):
        return JSONResponse(
            status_code=403,
            content={
                "ok": False,
                "error_type": "PathNotAllowed",
                "error": (
                    f"OpenBB dispatcher path '{body.path}' is not in the allowlist."
                ),
                "path": body.path,
            },
        )

    user_id = str(user.get("sub") or "anonymous")
    try:
        state = await enforce_rate_limit(
            redis,
            f"rl:openbb:dispatch:{user_id}",
            limit=OPENBB_DISPATCH_RATE_LIMIT,
            window_sec=OPENBB_DISPATCH_RATE_WINDOW_SEC,
        )
    except RateLimitExceeded as exc:
        logger.warning(
            "openbb_dispatch_rate_limited",
            extra={
                "user": user_id,
                "limit": exc.limit,
                "window_sec": exc.window_sec,
                "retry_after": exc.retry_after,
                "path": body.path,
            },
        )
        return JSONResponse(
            status_code=429,
            content={
                "ok": False,
                "error_type": "RateLimited",
                "error": str(exc),
                "retry_after": exc.retry_after,
                "limit": exc.limit,
                "window_sec": exc.window_sec,
            },
            headers={"Retry-After": str(exc.retry_after)},
        )

    request_payload = body.model_dump()
    result = await run_openbb_call(**request_payload)
    await _capture_provider_record(
        build_openbb_call_record(request=request_payload, response=result)
    )
    response = _respond_with_tool_result(result)
    # Attach rate-limit diagnostics so the dashboard can surface "57/60
    # left this minute" without a second round-trip.
    headers = {
        "X-RateLimit-Limit": str(state.limit),
        "X-RateLimit-Remaining": str(state.remaining),
        "X-RateLimit-Reset": str(state.reset_sec),
    }
    if isinstance(response, JSONResponse):
        for key, value in headers.items():
            response.headers[key] = value
        return response
    return JSONResponse(status_code=200, content=response, headers=headers)


@router.get("/openbb/health")
async def openbb_health(
    _user: dict[str, Any] = Depends(require_user),
    redis: Redis = Depends(get_redis),  # type: ignore[type-arg]
) -> dict[str, Any]:
    """Reachability probe for the local OpenBB API.

    Drives the dashboard status pill — kept fast (one HTTP GET against
    ``/openapi.json``) so polling every 30s is cheap.  The response is
    the raw output of :func:`check_openbb_health` so the UI can read
    ``ok``, ``latency_ms``, ``url``, and any ``error`` field directly.

    Every probe is persisted to Redis (``obb:health:last`` +
    ``obb:health:log`` stream) so the ``/history`` endpoint can render
    uptime % and latency sparklines.
    """
    result = await run_openbb_health()
    await record_health(redis, result)
    return result


@router.get("/openbb/health/history")
async def openbb_health_history(
    limit: int = Query(default=120, ge=1, le=720),
    _user: dict[str, Any] = Depends(require_user),
    redis: Redis = Depends(get_redis),  # type: ignore[type-arg]
) -> dict[str, Any]:
    """Recent OpenBB health samples + rollup stats.

    Returns oldest → newest so the dashboard can feed the array
    directly into a chart without sorting.  ``summary`` carries uptime
    percentage and p50/p95 latency so callers that only want the
    headline number can skip iterating.
    """
    entries = await fetch_history(redis, limit=limit)
    return {"entries": entries, "summary": summarise(entries)}


@router.get("/openbb/readiness")
async def openbb_readiness(
    symbol: str = Query(default="NVDA", min_length=1, max_length=32),
    provider: str = Query(default="yfinance", min_length=1, max_length=64),
    _user: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    """Deeper OpenBB readiness probe for provider-backed data paths.

    This route intentionally does more than ``/health``.  It checks the
    local OpenBB API process plus representative quote and fundamentals
    provider calls, returning per-check diagnostics so operators can see
    whether a failure is API reachability or provider-specific data.
    """
    return await run_openbb_readiness(symbol.strip().upper(), provider.strip())
