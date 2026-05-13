"""
api.routes.news — book-aware news feed with composite priority scoring.

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
  5. Score each article with the composite priority described below
     and split the response into three lanes:
        - ``alert``    book stories that pass the priority threshold
                       (recent, adverse, large vs gross book equity)
        - ``impact``   other book stories, sorted by score
        - ``universe`` everything else, time-sorted

Composite score
~~~~~~~~~~~~~~~

Each article gets a ``score`` value computed as::

    base    = |Σ signed_dollar_impact|              # raw $ exposure
    recency = exp(-age_hours / RECENCY_HALF_LIFE_H) # decay, default 12h
    adverse = ADVERSE_BOOST if signed_total < 0     # +30% if hurting book
              else 1.0
    score   = base * recency * adverse

The decay matters because dashboard refetches every 10 s and a 24h-old
$1k-impact story shouldn't outrank a 5-minute-old $999 story.  The
adverse boost matters because when a story moves *against* our position
it's actionable -- we may want to flatten or hedge -- whereas a
favourable move is information without urgency.

A story is promoted to the ``alert`` lane when *any* of:
    - ``pct_of_book >= ALERT_PCT_OF_BOOK`` (default 0.5%), AND
    - ``has_impact_math`` (i.e. we have a real anchor price, not a
      fallback).

The percent-of-book rule scales with account size, so the same code
gives sensible alerts on both a $10k paper account and a $10M live
account without configuration.

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
from fincept_core.clock import now_ns
from oms.alpaca.marks import read_marks
from oms.alpaca.news_sync import NEWS_INDEX_KEY, _article_key
from portfolio.store import PositionStore

router = APIRouter()

# Hard cap so one call can't slurp the entire index.
MAX_ARTICLES = 200

# Composite-score parameters.  Tuned conservatively for a paper account
# refreshing every 10s; revisit when we have real operator feedback.
RECENCY_HALF_LIFE_H = 12.0
ADVERSE_BOOST = Decimal("1.3")
ALERT_PCT_OF_BOOK = Decimal("0.005")  # 0.5% of gross book equity
NS_PER_HOUR = 3_600_000_000_000


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


def _score(
    *,
    base_abs_impact: Decimal,
    age_hours: float,
    is_adverse: bool,
) -> Decimal:
    """Composite priority score.

    Pulled out as a free function so unit tests can pin down each
    component without spinning up Redis or building articles.

    Decay is a true half-life: ``score(age=H) = score(0) / 2`` when
    ``age == RECENCY_HALF_LIFE_H``.  We cap the half-count at ~50 so
    a year-old story doesn't underflow.
    """
    if base_abs_impact <= 0:
        return Decimal(0)
    half_lives = max(0.0, age_hours / RECENCY_HALF_LIFE_H)
    half_lives = min(half_lives, 50.0)
    decay = Decimal(str(0.5 ** half_lives))
    boost = ADVERSE_BOOST if is_adverse else Decimal(1)
    return base_abs_impact * decay * boost


def _enrich_article(
    article: dict[str, Any],
    *,
    book_qty: dict[str, Decimal],
    marks: dict[str, Decimal],
    book_equity_usd: Decimal,
    now_ns_value: int,
) -> dict[str, Any]:
    """Attach per-symbol impact numbers, sparkline, and composite score."""
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
        bars: list[list[Any]] = snap.get("bars") or []
        bars_available_raw = snap.get("bars_available")
        bars_available = (
            bool(bars) if bars_available_raw is None else bool(bars_available_raw)
        )
        pct = None
        dollar_impact = None
        # Compute impact from a bar-backed anchor, or from a fallback anchor
        # after the live mark has moved away from that stored anchor.
        can_compute_impact = (
            bars_available or (mark is not None and mark != price_at_publish)
        )
        if can_compute_impact and price_at_publish > 0 and mark is not None:
            pct = (mark - price_at_publish) / price_at_publish
            qty = book_qty.get(symbol, Decimal(0))
            if qty != 0:
                # Use signed qty so shorts profit from drops.
                dollar_impact = pct * price_at_publish * qty
                article_impact += dollar_impact
                has_impact_math = True

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

    # --- composite score & tier classification ---------------------- #
    is_adverse = has_impact_math and article_impact < 0
    base_abs = article_impact.copy_abs() if has_impact_math else Decimal(0)
    age_ns = max(0, now_ns_value - int(article.get("ts_event_ns", 0) or 0))
    age_hours = age_ns / NS_PER_HOUR
    score = _score(
        base_abs_impact=base_abs,
        age_hours=age_hours,
        is_adverse=is_adverse,
    )

    pct_of_book: Decimal | None = None
    if has_impact_math and book_equity_usd > 0:
        pct_of_book = base_abs / book_equity_usd

    # Tier is *advisory* — list_news may downgrade to "impact" if the
    # alert lane gets too crowded, but we set the natural rank here so
    # the caller can sort cleanly.
    tier: str
    if not touched_book:
        tier = "universe"
    elif (
        has_impact_math
        and pct_of_book is not None
        and pct_of_book >= ALERT_PCT_OF_BOOK
    ):
        tier = "alert"
    else:
        tier = "impact"

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
        "is_adverse": is_adverse,
        "age_hours": float(age_hours),
        "score": float(score),
        "pct_of_book": str(pct_of_book) if pct_of_book is not None else None,
        "tier": tier,
    }


def _book_equity_usd(
    book_qty: dict[str, Decimal],
    marks: dict[str, Decimal],
) -> Decimal:
    """Gross notional = Σ |qty| × mark.

    Gross (not net) is the right denominator for the alert-tier
    threshold because a hedged book can be net-zero while still being
    fully exposed to single-name news.  Mark-less symbols contribute 0
    -- conservative, and a separate alarm if marks go missing for a
    held symbol.
    """
    total = Decimal(0)
    for symbol, qty in book_qty.items():
        mark = marks.get(symbol)
        if mark is None or qty == 0:
            continue
        total += qty.copy_abs() * mark
    return total


@router.get("")
async def list_news(
    limit: int = Query(100, ge=1, le=MAX_ARTICLES),
    only_book: bool = Query(False),
    _: dict[str, Any] = Depends(require_user),
    store: PositionStore = Depends(get_position_store),
    redis: Redis = Depends(get_redis),  # type: ignore[type-arg]
) -> dict[str, Any]:
    """Return book-aware news split into ALERT, IMPACT, and UNIVERSE lanes.

    ``alert`` is the highest-priority lane: book stories whose absolute
    dollar impact is at least ``ALERT_PCT_OF_BOOK`` of gross book
    equity, sorted by composite score desc.  ``impact`` is the rest of
    the book-touching stories, also sorted by score.  ``universe`` is
    the residual, time-sorted.
    """
    # Build per-symbol qty map across every strategy.  Keep it signed so
    # impact math knows whether a price drop helps (short) or hurts (long).
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
            "alert": [],
            "impact": [],
            "universe": [],
            "book_symbols": sorted(book_qty),
            "book_total_impact": "0",
            "book_equity_usd": "0",
            "alert_pct_of_book": float(ALERT_PCT_OF_BOOK),
            "recency_half_life_h": RECENCY_HALF_LIFE_H,
        }

    # Pull marks for the union of (book symbols + article symbols).
    symbol_set = set(book_qty)
    for article in articles:
        symbol_set.update(article.get("symbols") or [])
    marks = await read_marks(redis, sorted(symbol_set))
    book_equity_usd = _book_equity_usd(book_qty, marks)

    now = now_ns()
    alert: list[dict[str, Any]] = []
    impact: list[dict[str, Any]] = []
    universe: list[dict[str, Any]] = []
    total_impact = Decimal(0)

    for article in articles:
        enriched = _enrich_article(
            article,
            book_qty=book_qty,
            marks=marks,
            book_equity_usd=book_equity_usd,
            now_ns_value=now,
        )
        tier = enriched["tier"]
        if tier == "alert":
            alert.append(enriched)
            total_impact += _dec(enriched["total_dollar_impact"])
        elif tier == "impact":
            impact.append(enriched)
            total_impact += _dec(enriched["total_dollar_impact"])
        elif not only_book:
            universe.append(enriched)

    # Score-desc on alert + impact lanes.  Stories without impact math
    # naturally fall to the bottom because their score is 0.
    score_key = lambda a: (a["score"], int(a.get("ts_event_ns", 0)))  # noqa: E731
    alert.sort(key=score_key, reverse=True)
    impact.sort(key=score_key, reverse=True)

    return {
        "alert": alert,
        "impact": impact,
        "universe": universe,
        "book_symbols": sorted(book_qty),
        "book_total_impact": str(total_impact),
        "book_equity_usd": str(book_equity_usd),
        "alert_pct_of_book": float(ALERT_PCT_OF_BOOK),
        "recency_half_life_h": RECENCY_HALF_LIFE_H,
    }
