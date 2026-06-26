"""
api.routes.data — read-only market-data endpoints.

  GET /universe                List active universe symbols (filtered by
                               asset_class if requested).
  GET /symbols/search          Typeahead matcher: ranks the merged
                               (universe + well-known) pool against a
                               free-text query.  Used by the strategy
                               + manual-order forms in the dashboard.
  GET /sources                 Datasource registry for capability/health
                               control surfaces.
  GET /bars/{symbol}           Historical OHLCV bars.  ``freq`` defaults
                               to "1m"; ``start``/``end`` are required
                               and use UTC nanoseconds.

Both delegate to fincept-db readers; the API layer adds auth + light
parameter validation + JSON serialisation only.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, cast

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query

from api.auth import require_user
from api.deps import get_position_store
from api.symbol_search import search_symbols
from fincept_core.config import get_settings
from fincept_db.bars import read_bar_coverage, read_bars
from fincept_db.universe import read_universe, upsert_universe_symbols
from oms.alpaca.data import DATA_BASE_URL, AlpacaDataClient, AlpacaDataError
from portfolio.store import PositionStore

router = APIRouter()
logger = logging.getLogger(__name__)

DEFAULT_COVERAGE_LOOKBACK_NS = 24 * 60 * 60 * 1_000_000_000
DEFAULT_COVERAGE_STALE_AFTER_NS = 60 * 60 * 1_000_000_000
DATASOURCE_REGISTRY: tuple[dict[str, Any], ...] = (
    {
        "id": "exa",
        "name": "Exa",
        "area": "Research/source-grounded search health",
        "category": "research",
        "safety": "read_only",
        "status": "registered",
        "call_surfaces": ["POST /research/exa", "research.exa_market"],
        "data": ["web_search_results", "source_grounding", "research_briefs"],
        "return_format": "structured_brief_with_grounding",
        "latency": "fast_to_deep_by_search_type",
        "health": {
            "mode": "configuration",
            "checks": ["EXA_API_KEY present", "last request ok"],
        },
        "config": ["EXA_API_KEY"],
    },
    {
        "id": "openbb",
        "name": "OpenBB",
        "area": "Provider/capability browser",
        "category": "market_data",
        "safety": "read_only",
        "status": "registered",
        "call_surfaces": [
            "POST /research/openbb",
            "POST /research/openbb/quote",
            "GET /research/openbb/health",
        ],
        "data": ["quotes", "fundamentals", "macro", "news", "provider_capabilities"],
        "return_format": "normalized_rows",
        "latency": "local_api_plus_provider",
        "health": {
            "mode": "active_probe",
            "checks": ["OpenBB API /openapi.json", "provider key availability"],
        },
        "config": ["OPENBB_API_URL", "OpenBB provider keys"],
    },
    {
        "id": "alpaca",
        "name": "Alpaca",
        "area": "Trading/data connectivity",
        "category": "broker_market_data",
        "safety": "paper_first",
        "status": "registered",
        "call_surfaces": [
            "orders/positions adapters",
            "market-data scheduler",
            "GET /data/alpaca/demo",
        ],
        "data": ["equity_quotes", "equity_news", "positions", "orders", "fills"],
        "return_format": "broker_records",
        "latency": "network_realtime",
        "health": {
            "mode": "connector",
            "checks": ["credentials present", "paper account reachable"],
        },
        "config": ["ALPACA_API_KEY", "ALPACA_SECRET_KEY"],
    },
    {
        "id": "binance",
        "name": "Binance",
        "area": "Crypto/market-data connectivity",
        "category": "crypto_market_data",
        "safety": "read_only",
        "status": "registered",
        "call_surfaces": ["ingestor", "market-data adapters"],
        "data": ["crypto_trades", "crypto_bars", "order_book_snapshots"],
        "return_format": "market_events",
        "latency": "network_realtime",
        "health": {
            "mode": "connector",
            "checks": ["public endpoint reachable", "symbol subscriptions healthy"],
        },
        "config": [],
    },
    {
        "id": "timescale_bars",
        "name": "Timescale bars",
        "area": "Local historical data heartbeat",
        "category": "local_timeseries",
        "safety": "read_only",
        "status": "registered",
        "call_surfaces": ["GET /data/bars/{symbol}", "GET /data/coverage"],
        "data": ["ohlcv_bars", "coverage", "freshness"],
        "return_format": "bars_and_coverage_rows",
        "latency": "local_db",
        "health": {
            "mode": "active_query",
            "checks": ["DB reachable", "latest bar age", "bar counts by symbol"],
        },
        "config": ["FINCEPT_DB_URL"],
    },
    {
        "id": "redis",
        "name": "Redis",
        "area": "Rate limits, marks, cached state",
        "category": "state_cache",
        "safety": "internal_state",
        "status": "registered",
        "call_surfaces": ["rate limiter", "md:last:*", "OpenBB health history"],
        "data": ["rate_limit_buckets", "latest_marks", "health_streams"],
        "return_format": "keys_streams_json",
        "latency": "local_cache",
        "health": {
            "mode": "active_ping",
            "checks": ["Redis ping", "expected key namespaces"],
        },
        "config": ["FINCEPT_REDIS_URL"],
    },
    {
        "id": "local_predictions",
        "name": "Local predictions",
        "area": "Model output visibility",
        "category": "model_outputs",
        "safety": "read_only",
        "status": "registered",
        "call_surfaces": ["GET /models", "GET /models/{name}", "prediction stores"],
        "data": ["prediction_records", "model_metadata", "feature_importance"],
        "return_format": "model_records",
        "latency": "local_artifact",
        "health": {
            "mode": "artifact_scan",
            "checks": ["model artifacts present", "latest prediction timestamp"],
        },
        "config": ["data/predictions"],
    },
    {
        "id": "news_impact_model",
        "name": "News impact model",
        "area": "Event/risk context",
        "category": "event_risk",
        "safety": "experimental_read_only",
        "status": "registered",
        "call_surfaces": ["GET /news-impact/status", "POST /news-impact/predict"],
        "data": ["news_events", "impact_predictions", "similar_events"],
        "return_format": "impact_prediction",
        "latency": "local_model",
        "health": {
            "mode": "experiment_status",
            "checks": ["dataset loaded", "last optimization summary"],
        },
        "config": ["experiments/news-impact-model"],
    },
)


def _debug_errors_enabled() -> bool:
    return os.getenv("FINCEPT_DEBUG_ERRORS", "").lower() in {
        "1",
        "true",
        "yes",
        "local",
    }


def _public_error(
    error_type: str,
    message: str,
    exc: Exception | None = None,
) -> dict[str, str]:
    body = {"error_type": error_type, "message": message}
    if exc is not None and _debug_errors_enabled():
        body["debug"] = str(exc)
    return body


def _with_venue_alias(row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    out.setdefault("venue", out.get("venue_default"))
    return out


def _infer_universe_defaults(symbol: str) -> dict[str, str]:
    normalized = symbol.upper()
    if "-" in normalized or "/" in normalized:
        return {"asset_class": "crypto_spot", "venue_default": "binance"}
    return {"asset_class": "equity", "venue_default": "alpaca"}


def _parse_equity_symbols(value: str) -> list[str]:
    symbols: list[str] = []
    for part in value.split(","):
        symbol = part.strip().upper()
        if symbol and symbol not in symbols:
            symbols.append(symbol)
    return symbols[:10]


def _utc_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _data_source_summary(sources: list[dict[str, Any]]) -> dict[str, Any]:
    by_category: dict[str, int] = {}
    for source in sources:
        category = str(source["category"])
        by_category[category] = by_category.get(category, 0) + 1
    return {"total": len(sources), "by_category": by_category}


@router.get("/sources")
async def data_sources(
    _: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    """Return the datasource capability registry used by control surfaces."""
    sources = [dict(source) for source in DATASOURCE_REGISTRY]
    return {"sources": sources, "summary": _data_source_summary(sources)}


@router.get("/universe")
async def list_universe(
    asset_class: str | None = Query(None),
    active_only: bool = Query(True),
    _: dict[str, Any] = Depends(require_user),
) -> list[dict[str, Any]]:
    """Return universe rows; optionally filter by asset_class."""
    rows = await read_universe(asset_class=asset_class, active_only=active_only)
    return [_with_venue_alias(row) for row in rows]


@router.post("/universe/seed-from-positions")
async def seed_universe_from_positions(
    _: dict[str, Any] = Depends(require_user),
    store: PositionStore = Depends(get_position_store),
) -> dict[str, Any]:
    rows_by_symbol: dict[str, dict[str, object]] = {}
    for strategy_id in await store.known_strategies():
        positions = await store.get_all(strategy_id)
        for pos in positions.values():
            if pos.quantity == Decimal(0):
                continue
            symbol = pos.symbol.strip().upper()
            if not symbol:
                continue
            defaults = _infer_universe_defaults(symbol)
            rows_by_symbol[symbol] = {
                "symbol": symbol,
                "asset_class": defaults["asset_class"],
                "venue_default": defaults["venue_default"],
                "active": True,
            }
    try:
        rows = await upsert_universe_symbols(list(rows_by_symbol.values()))
    except Exception as exc:
        logger.exception("universe_seed_from_positions_failed")
        raise HTTPException(
            status_code=503,
            detail=_public_error(
                "DataStoreUnavailable",
                "Universe seed unavailable because the data store could not be reached.",
                exc,
            ),
        ) from exc
    seeded_symbols = sorted(rows_by_symbol)
    seeded_set = set(seeded_symbols)
    return {
        "seeded": len(seeded_symbols),
        "symbols": seeded_symbols,
        "universe": [
            _with_venue_alias(row)
            for row in rows
            if str(row.get("symbol") or "").upper() in seeded_set
        ],
    }


@router.get("/alpaca/demo")
async def alpaca_data_demo(
    symbols: str = Query("AAPL,NVDA", min_length=1, max_length=120),
    news_limit: int = Query(5, ge=1, le=10),
    bar_limit: int = Query(12, ge=1, le=50),
    _: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    requested_symbols = _parse_equity_symbols(symbols)
    if not requested_symbols:
        raise HTTPException(status_code=400, detail="at least one symbol is required")

    settings = get_settings()
    if not settings.ALPACA_API_KEY or not settings.ALPACA_API_SECRET:
        raise HTTPException(
            status_code=503,
            detail=_public_error(
                "AlpacaCredentialsMissing",
                "Alpaca demo requires FINCEPT_ALPACA_API_KEY and FINCEPT_ALPACA_API_SECRET.",
            ),
        )

    now = datetime.now(timezone.utc)
    start = _utc_iso(now - timedelta(days=1))
    end = _utc_iso(now)
    try:
        async with httpx.AsyncClient(timeout=10.0) as http:
            client = AlpacaDataClient(
                http=http,
                api_key=settings.ALPACA_API_KEY,
                api_secret=settings.ALPACA_API_SECRET,
            )
            news = await client.list_news(
                symbols=requested_symbols,
                limit=news_limit,
                include_content=False,
            )
            bars = await client.list_bars(
                requested_symbols,
                timeframe="1Min",
                start=start,
                end=end,
                limit=bar_limit,
                feed="iex",
            )
    except AlpacaDataError as exc:
        status_code = exc.status_code if 400 <= exc.status_code < 500 else 502
        raise HTTPException(
            status_code=status_code,
            detail=_public_error(
                "AlpacaDataUnavailable",
                "Alpaca market-data demo request failed.",
                exc,
            ),
        ) from exc
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(
            status_code=503,
            detail=_public_error(
                "AlpacaDataUnavailable",
                "Alpaca market-data demo could not reach the data API.",
                exc,
            ),
        ) from exc

    raw_news_rows = news.get("news")
    raw_bar_rows = bars.get("bars")
    news_rows = raw_news_rows if isinstance(raw_news_rows, list) else []
    bar_rows = (
        cast(dict[str, list[Any]], raw_bar_rows)
        if isinstance(raw_bar_rows, dict)
        else {}
    )
    return {
        "ok": True,
        "provider": "alpaca",
        "base_url": DATA_BASE_URL,
        "symbols": requested_symbols,
        "feed": "iex",
        "timeframe": "1Min",
        "window": {"start": start, "end": end},
        "summary": {
            "news_count": len(news_rows),
            "symbols_with_bars": len([value for value in bar_rows.values() if value]),
            "bar_count": sum(
                len(value) for value in bar_rows.values() if isinstance(value, list)
            ),
        },
        "news": news_rows,
        "bars": bar_rows,
        "next_page_token": news.get("next_page_token"),
    }


@router.get("/symbols/search")
async def symbol_search(
    q: str = Query(
        ...,
        min_length=1,
        max_length=24,
        description="Query string (case-insensitive); 1-24 chars",
    ),
    limit: int = Query(10, ge=1, le=50),
    _: dict[str, Any] = Depends(require_user),
) -> list[dict[str, Any]]:
    """Return up to ``limit`` symbol matches for the typeahead query.

    Match ordering: exact > name-exact > prefix > word-prefix >
    substring > 1-edit fuzzy.  See :mod:`api.symbol_search` for the
    scoring function and the curated well-known list (mega-cap US
    equities + major crypto pairs) that backstops an empty universe.

    The endpoint includes inactive universe rows on purpose: an
    operator who paused a symbol can still find it to re-enable.
    """
    try:
        universe = await read_universe(active_only=False)
    except Exception as exc:
        logger.exception("symbol_search_universe_read_failed")
        raise HTTPException(
            status_code=503,
            detail=_public_error(
                "DataStoreUnavailable",
                "Symbol search unavailable because the data store could not be reached.",
                exc,
            ),
        ) from exc
    matches = search_symbols(q, universe_rows=universe, limit=limit)
    return [
        {
            "symbol": m.symbol,
            "name": m.name,
            "asset_class": m.asset_class,
            "score": m.score,
            "source": m.source,
        }
        for m in matches
    ]


@router.get("/coverage")
async def data_coverage(
    asset_class: str | None = Query(None),
    freq: str = Query("1m", description="Bar frequency to inspect"),
    venue: str | None = Query(None, description="Optional venue override"),
    as_of_ns: int | None = Query(None, description="Reference time in UTC nanoseconds"),
    lookback_ns: int = Query(
        DEFAULT_COVERAGE_LOOKBACK_NS,
        gt=0,
        description="Window size to scan backwards from as_of_ns",
    ),
    stale_after_ns: int = Query(
        DEFAULT_COVERAGE_STALE_AFTER_NS,
        ge=0,
        description="Maximum age before a symbol is marked stale",
    ),
    _: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    """Return per-symbol data coverage and freshness for the active universe."""
    end_ns = as_of_ns if as_of_ns is not None else time.time_ns()
    start_ns = end_ns - lookback_ns
    try:
        universe = await read_universe(asset_class=asset_class, active_only=True)
    except Exception as exc:
        logger.exception("data_coverage_universe_read_failed")
        raise HTTPException(
            status_code=503,
            detail=_public_error(
                "DataStoreUnavailable",
                "Data coverage unavailable because the data store could not be reached.",
                exc,
            ),
        ) from exc

    symbols = [str(symbol_row["symbol"]) for symbol_row in universe]
    try:
        coverage = await read_bar_coverage(
            symbols,
            freq,
            start_ns,
            end_ns + 1,
            venue=venue,
        )
    except Exception as exc:
        logger.exception("data_coverage_bar_read_failed")
        error = _public_error("BarReadFailed", "Bar coverage read failed.", exc)
        error_rows = [
            {
                "symbol": str(symbol_row["symbol"]),
                "asset_class": symbol_row.get("asset_class"),
                "venue": venue,
                "venue_default": symbol_row.get("venue_default"),
                "freq": freq,
                "status": "error",
                "bar_count": 0,
                "last_ts_event": None,
                "age_ns": None,
                "error_type": error["error_type"],
                "error": error["message"],
                **({"debug": error["debug"]} if "debug" in error else {}),
            }
            for symbol_row in universe
        ]
        total = len(error_rows)
        return {
            "freq": freq,
            "venue": venue,
            "as_of_ns": end_ns,
            "lookback_ns": lookback_ns,
            "stale_after_ns": stale_after_ns,
            "summary": {
                "total": total,
                "ok": 0,
                "stale": 0,
                "empty": 0,
                "error": total,
                "coverage_pct": 0.0,
            },
            "rows": error_rows,
        }

    coverage_rows: list[dict[str, Any]] = []
    for symbol_row in universe:
        symbol = str(symbol_row["symbol"])
        symbol_coverage = coverage.get(symbol)
        last_ts_event = (
            symbol_coverage.last_ts_event if symbol_coverage is not None else None
        )
        age_ns = (end_ns - last_ts_event) if last_ts_event is not None else None
        status = (
            "empty"
            if last_ts_event is None
            else "stale"
            if age_ns is not None and age_ns > stale_after_ns
            else "ok"
        )
        coverage_rows.append(
            {
                "symbol": symbol,
                "asset_class": symbol_row.get("asset_class"),
                "venue": venue,
                "venue_default": symbol_row.get("venue_default"),
                "freq": freq,
                "status": status,
                "bar_count": symbol_coverage.bar_count
                if symbol_coverage is not None
                else 0,
                "last_ts_event": last_ts_event,
                "age_ns": age_ns,
            }
        )

    counts = {
        "ok": sum(1 for row in coverage_rows if row["status"] == "ok"),
        "stale": sum(1 for row in coverage_rows if row["status"] == "stale"),
        "empty": sum(1 for row in coverage_rows if row["status"] == "empty"),
        "error": sum(1 for row in coverage_rows if row["status"] == "error"),
    }
    covered = counts["ok"] + counts["stale"]
    total = len(coverage_rows)
    coverage_pct = round((covered / total) * 100, 2) if total else 0.0
    return {
        "freq": freq,
        "venue": venue,
        "as_of_ns": end_ns,
        "lookback_ns": lookback_ns,
        "stale_after_ns": stale_after_ns,
        "summary": {
            "total": total,
            "ok": counts["ok"],
            "stale": counts["stale"],
            "empty": counts["empty"],
            "error": counts["error"],
            "coverage_pct": coverage_pct,
        },
        "rows": coverage_rows,
    }


@router.get("/bars/{symbol}")
async def get_bars(
    symbol: str,
    start: int = Query(
        ..., description="Start of range in UTC nanoseconds (inclusive)"
    ),
    end: int = Query(..., description="End of range in UTC nanoseconds (exclusive)"),
    freq: str = Query("1m", description="Bar frequency: 1m | 1h | 1d"),
    venue: str | None = Query(None, description="Optional venue filter"),
    _: dict[str, Any] = Depends(require_user),
) -> list[dict[str, Any]]:
    """Return bars in ``[start, end)`` for ``symbol`` at ``freq``."""
    if start >= end:
        raise HTTPException(status_code=400, detail="start must be < end")
    bars = await read_bars(symbol, freq, start, end, venue=venue)
    # Pydantic dump for JSON-friendly Decimal handling.
    return [bar.model_dump(mode="json") for bar in bars]
