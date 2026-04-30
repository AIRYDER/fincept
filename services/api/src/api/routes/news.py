"""
api.routes.news — book-aware news feed.

``NewsScheduler`` keeps Redis primed with recent articles + 1-min bar
snapshots per symbol.  This handler does the read-path join:

  1. Fetch the N newest article IDs from ``news:index``.
  2. Load each article JSON from ``news:article:{id}``.
  3. Load the caller's positions (``PositionStore.get_all``) plus
     the live marks hash (``md:last:{symbol}``).
  4. For every (article, symbol) pair, compute:
        - ``pct_change``   = (mark - price_at_publish) / price_at_publish
        - ``dollar_impact`` = pct_change * abs(qty) * price_at_publish
     and stash a compact sparkline series suitable for an SVG.
  5. Split the response into ``impact`` (stories that touch the book,
     sorted by absolute dollar impact desc) and ``universe`` (the rest,
     time-sorted).

No external calls happen on the read path; everything is Redis-local
so the endpoint is fast enough for a 5-second dashboard refetch.
"""

from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation
from typing import Any

from fastapi import APIRouter, Depends, Query
from redis.asyncio import Redis

from api.auth import require_user
from api.deps import get_position_store, get_redis
from oms.alpaca.marks import read_marks
from oms.alpaca.news_sync import NEWS_INDEX_KEY, _article_key
from portfolio.store import PositionStore

router = APIRouter()

# Hard cap so one call can't slurp the entire index.
MAX_ARTICLES = 200


def _dec(value: Any) -> Decimal:
    if value in (None, ""):
        return Decimal(0)
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return Decimal(0)


async def _load_articles(
    redis: Redis,  # type: ignore[type-arg]
    limit: int,
) -> list[dict[str, Any]]:
    ids = await redis.zrevrange(NEWS_INDEX_KEY, 0, limit - 1)
    if not ids:
        return []
    pipe = redis.pipeline()
    for raw_id in ids:
        article_id = raw_id.decode() if isinstance(raw_id, bytes) else raw_id
        pipe.get(_article_key(article_id))
    rows = await pipe.execute()
    out: list[dict[str, Any]] = []
    for raw in rows:
        if raw is None:
            continue
        try:
            out.append(
                json.loads(raw.decode() if isinstance(raw, bytes) else raw)
            )
        except (ValueError, TypeError):
            continue
    return out


def _enrich_article(
    article: dict[str, Any],
    *,
    book_qty: dict[str, Decimal],
    marks: dict[str, Decimal],
) -> dict[str, Any]:
    """Attach per-symbol impact numbers and a flat sparkline."""
    snapshots = article.get("snapshots") or {}
    enriched_symbols: list[dict[str, Any]] = []
    article_impact = Decimal(0)
    touched_book = False
    has_impact_math = False

    for symbol in article.get("symbols") or []:
        snap = snapshots.get(symbol)
        in_book = symbol in book_qty
        # Tagging the book is separate from having bars - a story about
        # MSFT belongs in IMPACT lane whether or not we have sparkline
        # data yet.  We compute the impact math when bars exist, and
        # fall back to `null` (rendered as "-" by the UI) otherwise.
        if in_book:
            touched_book = True

        if not snap:
            enriched_symbols.append(
                {
                    "symbol": symbol,
                    "in_book": in_book,
                    "price_at_publish": None,
                    "mark": None,
                    "pct_change": None,
                    "dollar_impact": None,
                    "sparkline": [],
                }
            )
            continue

        price_at_publish = _dec(snap.get("price_at_publish"))
        mark = marks.get(symbol)
        bars_available = bool(snap.get("bars_available", False))
        pct = None
        dollar_impact = None
        # Only compute impact when the snapshot has a real anchor from the
        # bar feed.  The fallback path stores price_at_publish=mark which
        # would produce a structurally-zero pct - misleading, so we skip.
        if bars_available and price_at_publish > 0 and mark is not None:
            pct = (mark - price_at_publish) / price_at_publish
            qty = book_qty.get(symbol, Decimal(0))
            if qty != 0:
                # Use signed qty so shorts profit from drops.
                dollar_impact = pct * price_at_publish * qty
                article_impact += dollar_impact
                has_impact_math = True

        bars: list[list[Any]] = snap.get("bars") or []
        sparkline: list[float] = []
        for entry in bars[-60:]:
            if not isinstance(entry, list) or len(entry) < 2:
                continue
            try:
                sparkline.append(float(entry[1]))
            except (TypeError, ValueError):
                continue
        # Append current mark so the line connects to "now".
        if mark is not None and (
            not sparkline or sparkline[-1] != float(mark)
        ):
            sparkline.append(float(mark))

        enriched_symbols.append(
            {
                "symbol": symbol,
                "in_book": symbol in book_qty,
                "price_at_publish": str(price_at_publish)
                if price_at_publish > 0
                else None,
                "mark": str(mark) if mark is not None else None,
                "pct_change": float(pct) if pct is not None else None,
                "dollar_impact": str(dollar_impact)
                if dollar_impact is not None
                else None,
                "sparkline": sparkline,
            }
        )

    return {
        "id": article["id"],
        "headline": article.get("headline", ""),
        "summary": article.get("summary", ""),
        "source": article.get("source", ""),
        "url": article.get("url", ""),
        "author": article.get("author", ""),
        "created_at": article.get("created_at", ""),
        "ts_event_ns": int(article.get("ts_event_ns", 0)),
        "symbols": enriched_symbols,
        "touched_book": touched_book,
        "has_impact_math": has_impact_math,
        "total_dollar_impact": str(article_impact) if has_impact_math else None,
    }


@router.get("")
async def list_news(
    limit: int = Query(100, ge=1, le=MAX_ARTICLES),
    only_book: bool = Query(False),
    _: dict[str, Any] = Depends(require_user),
    store: PositionStore = Depends(get_position_store),
    redis: Redis = Depends(get_redis),  # type: ignore[type-arg]
) -> dict[str, Any]:
    """Return book-aware news split into IMPACT and UNIVERSE lanes.

    ``impact`` is sorted by absolute dollar impact descending so the
    most financially relevant stories land at the top.  ``universe``
    is the residual, time-sorted.
    """
    # Build per-symbol qty map across every strategy.
    book_qty: dict[str, Decimal] = {}
    for strategy_id in await store.known_strategies():
        positions = await store.get_all(strategy_id)
        for pos in positions.values():
            if pos.quantity == 0:
                continue
            book_qty[pos.symbol] = (
                book_qty.get(pos.symbol, Decimal(0)) + pos.quantity
            )

    articles = await _load_articles(redis, limit)
    if not articles:
        return {
            "impact": [],
            "universe": [],
            "book_symbols": sorted(book_qty),
            "book_total_impact": "0",
        }

    # Pull marks for the union of (book symbols + article symbols).
    symbol_set = set(book_qty)
    for article in articles:
        symbol_set.update(article.get("symbols") or [])
    marks = await read_marks(redis, sorted(symbol_set))

    impact: list[dict[str, Any]] = []
    universe: list[dict[str, Any]] = []
    total_impact = Decimal(0)

    for article in articles:
        enriched = _enrich_article(
            article, book_qty=book_qty, marks=marks
        )
        if enriched["touched_book"]:
            impact.append(enriched)
            total_impact += _dec(enriched["total_dollar_impact"])
        elif not only_book:
            universe.append(enriched)

    # Impact lane sort: stories with real impact math float to the top
    # by |$|, followed by book-tagged stories without bars yet, sorted
    # by recency.  Tuple sort: (has_math desc, |$| desc, ts desc).
    impact.sort(
        key=lambda a: (
            1 if a.get("has_impact_math") else 0,
            abs(_dec(a.get("total_dollar_impact"))),
            int(a.get("ts_event_ns", 0)),
        ),
        reverse=True,
    )

    return {
        "impact": impact,
        "universe": universe,
        "book_symbols": sorted(book_qty),
        "book_total_impact": str(total_impact),
    }
