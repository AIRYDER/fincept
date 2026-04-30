"""
oms.alpaca.news_sync — pull Alpaca news and cache it in Redis with
per-article per-symbol price snapshots so the dashboard can render
sparklines + book-impact dollars without any further API calls.

Storage layout (all keys TTL'd to 24h to cap memory):

  news:article:{id}   HASH   full article JSON (one field "data")
  news:index          ZSET   score=created_at_ns, member=article_id
                             (capped to last 500 entries)

The article payload written to Redis extends Alpaca's shape with a
``snapshots`` dict built at ingestion time:

    {
      "id": "12345",
      "headline": "...",
      "summary": "...",
      "source": "Benzinga",
      "url": "https://...",
      "author": "...",
      "created_at": "2026-04-29T23:15:00Z",
      "ts_event_ns": 1714...000000,
      "symbols": ["AAPL", "MSFT"],
      "snapshots": {
        "AAPL": {
          "price_at_publish": "175.23",
          "bars": [[ts_ns, close_px_str], ...]  # <=60
        }
      }
    }

``price_at_publish`` is the close of the first 1-min bar at or after
the article's ``created_at``; if no bar exists yet (article is newer
than the bar feed's latest sample) we fall back to the article's
publication minute's mark from Redis (md:last) if available, else
omit the snapshot for that symbol.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Any

import httpx
from redis.asyncio import Redis

from fincept_core.clock import now_ns
from fincept_core.logging import get_logger
from oms.alpaca.data import AlpacaDataClient, AlpacaDataError
from oms.alpaca.marks import read_mark

NEWS_INDEX_KEY = "news:index"
NEWS_INDEX_CAP = 500
NEWS_TTL_SEC = 86_400  # 24h
MAX_BARS_PER_SNAPSHOT = 60  # 60 mins of 1-min bars

# Headlines that match any of these patterns are skipped entirely - they
# are filler content from Benzinga's automated feeds (listicles, halt
# notifications) that have zero alpha for an algo trader.  Skipping at
# ingestion saves a bars-API call per skipped article and keeps the
# Redis index focused on real news.
_LISTICLE_PATTERNS = [
    re.compile(r"^\$[\d,]+\s+(invested|put)\s+in", re.IGNORECASE),
    re.compile(r"would\s+be\s+worth.*today", re.IGNORECASE),
    re.compile(r"here'?s\s+how\s+much.*would\s+have\s+made", re.IGNORECASE),
    re.compile(r"^here'?s\s+how\s+much\s+\$", re.IGNORECASE),
    re.compile(r"if\s+you\s+invested.*years?\s+ago", re.IGNORECASE),
]
_HALT_PATTERNS = [
    re.compile(r"^trading\s+halt:", re.IGNORECASE),
    re.compile(r"^halt\s+(news\s+)?pending", re.IGNORECASE),
    re.compile(r"^circuit\s+breaker", re.IGNORECASE),
]


def _is_filler(headline: str) -> bool:
    """Return True if the headline looks like filler we should skip."""
    head = headline.strip()
    if not head:
        return True
    return any(p.search(head) for p in _LISTICLE_PATTERNS) or any(
        p.search(head) for p in _HALT_PATTERNS
    )


log = get_logger(__name__)


def _article_key(article_id: str) -> str:
    return f"news:article:{article_id}"


def _iso_to_ns(iso: str) -> int:
    """Parse an Alpaca ISO-8601 timestamp into nanoseconds since epoch."""
    # Alpaca uses either "...Z" or "+00:00".  datetime.fromisoformat handles
    # offsets but not the trailing Z on Python <3.11; swap to be safe.
    if iso.endswith("Z"):
        iso = iso[:-1] + "+00:00"
    dt = datetime.fromisoformat(iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp() * 1_000_000_000)


def _ns_to_iso(ns: int) -> str:
    dt = datetime.fromtimestamp(ns / 1_000_000_000, tz=UTC)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


async def _snapshot_symbol(
    *,
    redis: Redis[Any],
    data_client: AlpacaDataClient,
    symbol: str,
    created_at_ns: int,
) -> dict[str, Any] | None:
    """Build the per-symbol snapshot (price_at_publish + bars).

    Pulls 1-min bars from publish time forward (capped).  If the bar
    feed has no data yet we try to at least record an anchor mark from
    Redis so the article still has a defined "price at publish".
    """
    start_iso = _ns_to_iso(created_at_ns)
    end_iso = _ns_to_iso(now_ns())
    try:
        payload = await data_client.list_bars(
            [symbol],
            timeframe="1Min",
            start=start_iso,
            end=end_iso,
            limit=MAX_BARS_PER_SNAPSHOT,
        )
    except AlpacaDataError as exc:
        log.debug(
            "news.bars.fetch_failed", symbol=symbol, error=str(exc)
        )
        payload = {"bars": {}}

    raw_bars = payload.get("bars", {}).get(symbol) or []
    # Each bar: {t, o, h, l, c, v}.  Store (ts_ns, close) only.
    bars: list[tuple[int, str]] = []
    for bar in raw_bars[-MAX_BARS_PER_SNAPSHOT:]:
        try:
            bars.append((_iso_to_ns(bar["t"]), str(bar["c"])))
        except (KeyError, ValueError):
            continue

    if bars:
        price_at_publish = bars[0][1]
        bars_available = True
    else:
        # Fallback: current mark from Redis.  Used only so the symbol
        # still surfaces in the feed; pct_change / dollar_impact will
        # be null because we have no real reaction signal.
        mark = await read_mark(redis, symbol)
        if mark is None:
            return None
        price_at_publish = str(mark)
        bars_available = False

    return {
        "price_at_publish": price_at_publish,
        "bars": [[ts, px] for ts, px in bars],
        "bars_available": bars_available,
    }


def _alpaca_to_internal(raw: dict[str, Any]) -> dict[str, Any]:
    """Strip Alpaca's shape down to the fields we actually render."""
    created_at = str(raw.get("created_at") or raw.get("updated_at") or "")
    if not created_at:
        raise ValueError("article missing created_at")
    return {
        "id": str(raw.get("id") or raw.get("ID") or ""),
        "headline": str(raw.get("headline", "")),
        "summary": str(raw.get("summary", ""))[:1000],
        "source": str(raw.get("source", "")),
        "url": str(raw.get("url", "")),
        "author": str(raw.get("author", "")),
        "created_at": created_at,
        "ts_event_ns": _iso_to_ns(created_at),
        "symbols": [str(s) for s in (raw.get("symbols") or []) if s],
    }


async def sync_recent_news(
    *,
    redis: Redis[Any],
    api_key: str,
    api_secret: str,
    lookback_minutes: int = 120,
    limit: int = 50,
    extra_symbols: list[str] | None = None,
) -> dict[str, Any]:
    """Pull recent articles + snapshot per-symbol bars.  Idempotent.

    Articles already present in Redis are skipped (we only snapshot
    new IDs to keep the bars call volume bounded).  Returns a summary
    with counts so callers can log the result.
    """
    start_iso = _ns_to_iso(
        now_ns() - lookback_minutes * 60 * 1_000_000_000
    )
    async with httpx.AsyncClient(timeout=30.0) as http:
        client = AlpacaDataClient(
            http=http, api_key=api_key, api_secret=api_secret
        )
        try:
            payload = await client.list_news(
                symbols=extra_symbols,
                limit=limit,
                start=start_iso,
            )
        except AlpacaDataError as exc:
            log.warning("news.fetch_failed", error=str(exc))
            return {"fetched": 0, "written": 0, "skipped": 0, "error": str(exc)}

        articles: list[dict[str, Any]] = list(payload.get("news") or [])
        written = 0
        skipped = 0
        filtered = 0

        for raw in articles:
            try:
                article = _alpaca_to_internal(raw)
            except ValueError:
                skipped += 1
                continue
            if not article["id"]:
                skipped += 1
                continue
            if _is_filler(article["headline"]):
                filtered += 1
                continue

            key = _article_key(article["id"])
            # Skip articles we've already snapshotted.
            exists = await redis.exists(key)
            if exists:
                skipped += 1
                continue

            snapshots: dict[str, Any] = {}
            for symbol in article["symbols"][:10]:  # cap per-article fanout
                snap = await _snapshot_symbol(
                    redis=redis,
                    data_client=client,
                    symbol=symbol,
                    created_at_ns=article["ts_event_ns"],
                )
                if snap is not None:
                    snapshots[symbol] = snap
            article["snapshots"] = snapshots

            await redis.set(key, json.dumps(article), ex=NEWS_TTL_SEC)
            await redis.zadd(
                NEWS_INDEX_KEY,
                {article["id"]: article["ts_event_ns"]},
            )
            written += 1

    # Cap the index to the most recent N and extend TTL.
    await redis.zremrangebyrank(NEWS_INDEX_KEY, 0, -NEWS_INDEX_CAP - 1)
    await redis.expire(NEWS_INDEX_KEY, NEWS_TTL_SEC)

    return {
        "fetched": len(articles),
        "written": written,
        "skipped": skipped,
        "filtered": filtered,
    }


async def refresh_snapshot_bars(
    *,
    redis: Redis[Any],
    api_key: str,
    api_secret: str,
    max_articles: int = 20,
) -> dict[str, Any]:
    """Extend the bar series on recent articles so sparklines keep
    ticking as time passes.

    Only updates the ``max_articles`` newest articles (others are
    effectively frozen - nobody cares about a 12h-old sparkline).
    """
    # Newest first.
    ids = await redis.zrevrange(NEWS_INDEX_KEY, 0, max_articles - 1)
    if not ids:
        return {"updated": 0}

    updated = 0
    async with httpx.AsyncClient(timeout=30.0) as http:
        client = AlpacaDataClient(
            http=http, api_key=api_key, api_secret=api_secret
        )
        for raw_id in ids:
            article_id = (
                raw_id.decode() if isinstance(raw_id, bytes) else raw_id
            )
            key = _article_key(article_id)
            payload = await redis.get(key)
            if payload is None:
                continue
            article = json.loads(
                payload.decode() if isinstance(payload, bytes) else payload
            )
            symbols: list[str] = list(article.get("snapshots", {}).keys())
            if not symbols:
                continue
            for symbol in symbols:
                snap = await _snapshot_symbol(
                    redis=redis,
                    data_client=client,
                    symbol=symbol,
                    created_at_ns=int(article["ts_event_ns"]),
                )
                if snap is None:
                    continue
                # Preserve the original price_at_publish; only refresh bars
                # and the bars_available flag (so an article that started
                # with no IEX data lights up once the feed catches up).
                article["snapshots"][symbol]["bars"] = snap["bars"]
                if snap.get("bars_available"):
                    article["snapshots"][symbol]["bars_available"] = True
                    # Re-anchor price_at_publish to the first real bar so
                    # the impact math is no longer pinned to "now".
                    if snap["bars"]:
                        article["snapshots"][symbol]["price_at_publish"] = (
                            snap["bars"][0][1]
                        )
            await redis.set(key, json.dumps(article), ex=NEWS_TTL_SEC)
            updated += 1
    return {"updated": updated}


def ns_from_iso(iso: str) -> int:
    """Re-export for tests / API handlers that need this helper."""
    return _iso_to_ns(iso)
