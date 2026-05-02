"""
api.routes.data — read-only market-data endpoints.

  GET /universe                List active universe symbols (filtered by
                               asset_class if requested).
  GET /symbols/search          Typeahead matcher: ranks the merged
                               (universe + well-known) pool against a
                               free-text query.  Used by the strategy
                               + manual-order forms in the dashboard.
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
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from api.auth import require_user
from api.symbol_search import search_symbols
from fincept_db.bars import read_bar_coverage, read_bars
from fincept_db.universe import read_universe

router = APIRouter()
logger = logging.getLogger(__name__)

DEFAULT_COVERAGE_LOOKBACK_NS = 24 * 60 * 60 * 1_000_000_000
DEFAULT_COVERAGE_STALE_AFTER_NS = 60 * 60 * 1_000_000_000


def _debug_errors_enabled() -> bool:
    return os.getenv("FINCEPT_DEBUG_ERRORS", "").lower() in {"1", "true", "yes", "local"}


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


@router.get("/universe")
async def list_universe(
    asset_class: str | None = Query(None),
    active_only: bool = Query(True),
    _: dict[str, Any] = Depends(require_user),
) -> list[dict[str, Any]]:
    """Return universe rows; optionally filter by asset_class."""
    rows = await read_universe(asset_class=asset_class, active_only=active_only)
    return [_with_venue_alias(row) for row in rows]


@router.get("/symbols/search")
async def symbol_search(
    q: str = Query(
        ..., min_length=1, max_length=24,
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
    universe = await read_universe(active_only=False)
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
        rows = [
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
        total = len(rows)
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
            "rows": rows,
        }

    rows: list[dict[str, Any]] = []
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
        rows.append(
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
        "ok": sum(1 for row in rows if row["status"] == "ok"),
        "stale": sum(1 for row in rows if row["status"] == "stale"),
        "empty": sum(1 for row in rows if row["status"] == "empty"),
        "error": sum(1 for row in rows if row["status"] == "error"),
    }
    covered = counts["ok"] + counts["stale"]
    total = len(rows)
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
        "rows": rows,
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
