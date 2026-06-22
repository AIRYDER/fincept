from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any, cast

from sqlalchemy import CursorResult, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from .engine import session_scope
from .models import ProviderData

SCHEMA_VERSION = "provider-data.v1"
JSONDict = dict[str, Any]


@dataclass(frozen=True)
class ProviderDataRecord:
    record_id: str
    schema_version: str
    provider: str
    source: str
    dataset: str
    endpoint: str
    symbol: str | None
    ts_event: int
    ts_observed: int | None
    request_hash: str
    request: JSONDict
    normalized: JSONDict
    raw: JSONDict
    row_count: int
    ok: bool
    error_type: str | None


def build_exa_record(
    *,
    request: dict[str, Any],
    response: dict[str, Any],
    ts_event: int | None = None,
) -> ProviderDataRecord:
    clean_request = _json_safe(request)
    clean_response = _json_safe(response)
    brief = clean_response.get("brief") if isinstance(clean_response.get("brief"), dict) else {}
    sources = clean_response.get("sources") if isinstance(clean_response.get("sources"), list) else []
    grounding = clean_response.get("grounding") if isinstance(clean_response.get("grounding"), list) else []
    symbol = _clean_symbol(clean_request.get("symbol"))
    normalized = {
        "schema_version": SCHEMA_VERSION,
        "kind": "research_brief",
        "provider": "exa",
        "symbol": symbol,
        "query": clean_request.get("query"),
        "search_type": clean_request.get("search_type"),
        "headline": brief.get("headline"),
        "summary": brief.get("summary"),
        "source_count": len(sources),
        "grounding_count": len(grounding),
        "cost_dollars": clean_response.get("cost_dollars"),
        "request_id": clean_response.get("request_id"),
        "sources": sources,
        "grounding": grounding,
    }
    return _make_record(
        provider="exa",
        source="research.exa_market",
        dataset="research_brief",
        endpoint="POST /research/exa",
        symbol=symbol,
        ts_event=ts_event,
        request=clean_request,
        normalized=normalized,
        raw=clean_response,
        row_count=len(sources),
        ok=bool(clean_response.get("ok", True)),
        error_type=_clean_error_type(clean_response.get("error_type")),
    )


def build_openbb_quote_record(
    *,
    request: dict[str, Any],
    response: dict[str, Any],
    ts_event: int | None = None,
) -> ProviderDataRecord:
    clean_request = _json_safe(request)
    clean_response = _json_safe(response)
    rows = _rows_from_response(clean_response)
    symbol = _clean_symbol(clean_request.get("symbol") or _first_row_value(rows, "symbol"))
    provider = str(clean_response.get("provider") or clean_request.get("provider") or "openbb")
    normalized = {
        "schema_version": SCHEMA_VERSION,
        "kind": "market_rows",
        "provider": "openbb",
        "upstream_provider": provider,
        "dataset": "equity.price.quote",
        "symbol": symbol,
        "row_count": len(rows),
        "fields": _fields(rows),
        "rows": rows,
    }
    return _make_record(
        provider="openbb",
        source="research.openbb_quote",
        dataset="equity.price.quote",
        endpoint="POST /research/openbb/quote",
        symbol=symbol,
        ts_event=ts_event,
        request=clean_request,
        normalized=normalized,
        raw=clean_response,
        row_count=len(rows),
        ok=bool(clean_response.get("ok", True)),
        error_type=_clean_error_type(clean_response.get("error_type")),
    )


def build_openbb_call_record(
    *,
    request: dict[str, Any],
    response: dict[str, Any],
    ts_event: int | None = None,
) -> ProviderDataRecord:
    clean_request = _json_safe(request)
    clean_response = _json_safe(response)
    params = clean_request.get("params") if isinstance(clean_request.get("params"), dict) else {}
    path = str(clean_request.get("path") or clean_response.get("path") or "")
    rows = _rows_from_response(clean_response)
    symbol = _clean_symbol(params.get("symbol") or _first_row_value(rows, "symbol"))
    upstream_provider = clean_response.get("provider") or params.get("provider")
    dataset = _dataset_from_openbb_path(path)
    normalized = {
        "schema_version": SCHEMA_VERSION,
        "kind": "market_rows",
        "provider": "openbb",
        "upstream_provider": upstream_provider,
        "dataset": dataset,
        "path": path,
        "symbol": symbol,
        "row_count": len(rows),
        "fields": _fields(rows),
        "rows": rows,
    }
    return _make_record(
        provider="openbb",
        source="research.openbb_call",
        dataset=dataset,
        endpoint=path or "POST /research/openbb",
        symbol=symbol,
        ts_event=ts_event,
        request=clean_request,
        normalized=normalized,
        raw=clean_response,
        row_count=len(rows),
        ok=bool(clean_response.get("ok", True)),
        error_type=_clean_error_type(clean_response.get("error_type")),
    )


def build_alpaca_news_record(
    *,
    request: dict[str, Any],
    response: dict[str, Any],
    ts_event: int | None = None,
) -> ProviderDataRecord:
    """Evidence record for Alpaca news ingest (used by oms.alpaca.news_sync)."""
    clean_request = _json_safe(request)
    clean_response = _json_safe(response)
    articles = clean_response.get("news") if isinstance(clean_response.get("news"), list) else []
    symbol = _clean_symbol((clean_request.get("symbols") or [""])[0] if clean_request.get("symbols") else None)
    normalized = {
        "schema_version": SCHEMA_VERSION,
        "kind": "news_articles",
        "provider": "alpaca",
        "article_count": len(articles),
        "symbols": clean_request.get("symbols"),
        "start": clean_request.get("start"),
    }
    return _make_record(
        provider="alpaca",
        source="alpaca.news",
        dataset="news",
        endpoint="GET /v2/stocks/news",
        symbol=symbol,
        ts_event=ts_event,
        request=clean_request,
        normalized=normalized,
        raw=clean_response,
        row_count=len(articles),
        ok=bool(clean_response.get("ok", True)),
        error_type=_clean_error_type(clean_response.get("error_type")),
    )


def build_alpaca_mark_record(
    *,
    symbol: str,
    price: Any,
    ts_ns: int | None = None,
) -> ProviderDataRecord:
    """Light evidence for price mark freshness (from oms.alpaca.marks)."""
    now = ts_ns or time.time_ns()
    normalized = {
        "schema_version": SCHEMA_VERSION,
        "kind": "mark_price",
        "provider": "alpaca",
        "symbol": _clean_symbol(symbol),
        "price": str(price) if price is not None else None,
    }
    return _make_record(
        provider="alpaca",
        source="alpaca.marks",
        dataset="price.mark",
        endpoint="md:last (redis)",
        symbol=_clean_symbol(symbol),
        ts_event=now,
        request={"symbol": _clean_symbol(symbol)},
        normalized=normalized,
        raw={"px": str(price) if price is not None else None, "ts_ns": str(now)},
        row_count=1,
        ok=price is not None,
        error_type=None,
    )


async def write_provider_data(records: Iterable[ProviderDataRecord]) -> int:
    rows = [_record_to_row(record) for record in records]
    if not rows:
        return 0
    async with session_scope() as session:
        stmt = pg_insert(ProviderData).values(rows)
        stmt = stmt.on_conflict_do_update(
            index_elements=["record_id", "ts_event"],
            set_={
                "schema_version": stmt.excluded.schema_version,
                "provider": stmt.excluded.provider,
                "source": stmt.excluded.source,
                "dataset": stmt.excluded.dataset,
                "endpoint": stmt.excluded.endpoint,
                "symbol": stmt.excluded.symbol,
                "ts_observed": stmt.excluded.ts_observed,
                "request_hash": stmt.excluded.request_hash,
                "request": stmt.excluded.request,
                "normalized": stmt.excluded.normalized,
                "raw": stmt.excluded.raw,
                "row_count": stmt.excluded.row_count,
                "ok": stmt.excluded.ok,
                "error_type": stmt.excluded.error_type,
            },
        )
        result = cast("CursorResult[Any]", await session.execute(stmt))
        return int(result.rowcount or 0)


async def read_provider_data(
    *,
    provider: str | None = None,
    dataset: str | None = None,
    symbol: str | None = None,
    limit: int = 100,
) -> list[ProviderDataRecord]:
    async with session_scope() as session:
        query = select(ProviderData)
        if provider is not None:
            query = query.where(ProviderData.provider == provider)
        if dataset is not None:
            query = query.where(ProviderData.dataset == dataset)
        if symbol is not None:
            query = query.where(ProviderData.symbol == symbol.upper())
        query = query.order_by(ProviderData.ts_event.desc()).limit(limit)
        rows = (await session.execute(query)).scalars().all()
        return [_row_to_record(row) for row in rows]


def _make_record(
    *,
    provider: str,
    source: str,
    dataset: str,
    endpoint: str,
    symbol: str | None,
    ts_event: int | None,
    request: JSONDict,
    normalized: JSONDict,
    raw: JSONDict,
    row_count: int,
    ok: bool,
    error_type: str | None,
) -> ProviderDataRecord:
    event_ns = ts_event if ts_event is not None else time.time_ns()
    # Hash on pre-redaction content for stable request identity/dedup
    request_hash = _hash_json({"provider": provider, "source": source, "dataset": dataset, "request": request})
    payload_hash = _hash_json({"normalized": normalized, "raw": raw})
    record_id = _hash_json(
        {
            "provider": provider,
            "source": source,
            "dataset": dataset,
            "endpoint": endpoint,
            "symbol": symbol,
            "ts_event": event_ns,
            "request_hash": request_hash,
            "payload_hash": payload_hash,
        }
    )
    # Redact sensitive before storing in evidence record (never written/returned with secrets)
    redacted_request = _redact_sensitive(request)
    redacted_normalized = _redact_sensitive(normalized)
    redacted_raw = _redact_sensitive(raw)
    return ProviderDataRecord(
        record_id=record_id,
        schema_version=SCHEMA_VERSION,
        provider=provider,
        source=source,
        dataset=dataset,
        endpoint=endpoint,
        symbol=symbol,
        ts_event=event_ns,
        ts_observed=_extract_ts_observed(redacted_normalized),
        request_hash=request_hash,
        request=redacted_request,
        normalized=redacted_normalized,
        raw=redacted_raw,
        row_count=row_count,
        ok=ok,
        error_type=error_type,
    )


def _rows_from_response(response: JSONDict) -> list[JSONDict]:
    raw_rows = response.get("results", [])
    if isinstance(raw_rows, dict):
        raw_rows = [raw_rows]
    if not isinstance(raw_rows, list):
        return []
    return [row for row in (_json_safe(item) for item in raw_rows) if isinstance(row, dict)]


def _fields(rows: list[JSONDict]) -> list[str]:
    fields: set[str] = set()
    for row in rows:
        fields.update(row.keys())
    return sorted(fields)


def _first_row_value(rows: list[JSONDict], field: str) -> Any:
    for row in rows:
        value = row.get(field)
        if value is not None:
            return value
    return None


def _dataset_from_openbb_path(path: str) -> str:
    prefix = "/api/v1/"
    if path.startswith(prefix):
        return path[len(prefix) :].strip("/").replace("/", ".") or "unknown"
    return "unknown"


def _clean_symbol(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip().upper()
    return cleaned or None


def _clean_error_type(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    return value[:64]


def _extract_ts_observed(normalized: JSONDict) -> int | None:
    rows = normalized.get("rows")
    if not isinstance(rows, list):
        return None
    for row in rows:
        if not isinstance(row, dict):
            continue
        for key in ("ts_event", "timestamp", "date"):
            value = row.get(key)
            parsed = _parse_ts(value)
            if parsed is not None:
                return parsed
    return None


def _parse_ts(value: Any) -> int | None:
    if isinstance(value, int):
        if value >= 100_000_000_000_000_000:
            return value
        if value >= 100_000_000_000_000:
            return value * 1_000
        if value >= 100_000_000_000:
            return value * 1_000_000
        return value * 1_000_000_000
    if isinstance(value, float):
        return int(value * 1_000_000_000)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return int(parsed.timestamp() * 1_000_000_000)
    return None


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_json_safe(item) for item in value]
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if value is None or isinstance(value, str | int | float | bool):
        return value
    return str(value)


def _redact_sensitive(value: Any) -> Any:
    """Conservative redaction for provider evidence receipts.

    Strips token-like strings, account ids, raw private URLs/creds, and
    sensitive fragments before write or return. Never leaks secrets.
    Applied to request/raw/normalized copies.
    """
    if isinstance(value, dict):
        return {str(k): _redact_sensitive(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact_sensitive(item) for item in value]
    if not isinstance(value, str):
        return value
    s = value
    # Standalone secret-like values (prefixes or long opaque tokens) - case insen
    if re.match(r'^(sk[-_]|pk[-_]|xoxb-|eyJ[A-Za-z0-9_-]{10,}|Bearer\s|ACCT-|acct-)', s, re.IGNORECASE):
        return "[REDACTED]"
    if len(s) >= 16 and re.search(r'[A-Za-z0-9_\-]{12,}', s) and any(p in s.lower() for p in ("secret", "key", "token", "pass", "auth")):
        return "[REDACTED]"
    # Common secret key/token patterns embedded (use IGNORECASE flag)
    s = re.sub(
        r'(api[_-]?key|token|secret|password|bearer|authorization|private_key|access_key)["\'\s:=]+[\w\-.]{6,}',
        r'\1=[REDACTED]',
        s,
        flags=re.IGNORECASE,
    )
    # URL embedded credentials user:pass@
    s = re.sub(
        r'(https?://|postgres://|mysql://|redis://)[^:@/ \s]+:[^@ \s]+@',
        r'\1[REDACTED]@',
        s,
        flags=re.IGNORECASE,
    )
    # Account identifiers
    s = re.sub(
        r'(account[_-]?id|acct|account)["\'\s:=]+[\w\-]{6,}',
        r'\1=[REDACTED]',
        s,
        flags=re.IGNORECASE,
    )
    # Conn string private host
    s = re.sub(
        r'([a-z]+://)[^@ \s]+@',
        r'\1[REDACTED]@',
        s,
        flags=re.IGNORECASE,
    )
    if s != value:
        return s
    return s


def _hash_json(payload: JSONDict) -> str:
    encoded = json.dumps(_json_safe(payload), sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _record_to_row(record: ProviderDataRecord) -> JSONDict:
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
        "request": record.request,
        "normalized": record.normalized,
        "raw": record.raw,
        "row_count": record.row_count,
        "ok": record.ok,
        "error_type": record.error_type,
    }


def _row_to_record(row: ProviderData) -> ProviderDataRecord:
    return ProviderDataRecord(
        record_id=row.record_id,
        schema_version=row.schema_version,
        provider=row.provider,
        source=row.source,
        dataset=row.dataset,
        endpoint=row.endpoint,
        symbol=row.symbol,
        ts_event=row.ts_event,
        ts_observed=row.ts_observed,
        request_hash=row.request_hash,
        request=row.request,
        normalized=row.normalized,
        raw=row.raw,
        row_count=row.row_count,
        ok=row.ok,
        error_type=row.error_type,
    )
