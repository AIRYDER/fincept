"""
agents.sentiment_agent.news - NewsAPI client.

We hit ``https://newsapi.org/v2/everything`` directly via httpx.  The
free tier allows 100 requests/day per key, so we batch queries: one
request per universe symbol per cycle.

Symbol -> query mapping is hard-coded for the v1 crypto universe.
Equity tickers (TSLA, AAPL) work fine if you set ``q=Tesla`` etc.,
but mapping ticker -> company name is an entire problem we punt on
until equity is in the universe.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass
from typing import Any

import httpx


@dataclass(frozen=True)
class Article:
    """One article from NewsAPI, normalized to the fields we use."""

    url: str
    title: str
    description: str
    source: str
    published_at_unix: int


# Hard-coded symbol -> NewsAPI query.  Keep narrow; broad queries hit
# rate limits and add noise (e.g. "Tesla" matches Nikola Tesla too).
SYMBOL_QUERIES: dict[str, str] = {
    "BTC-USD": "Bitcoin OR BTC",
    "ETH-USD": "Ethereum OR Ether",
    "SOL-USD": "Solana",
}


def query_for_symbol(symbol: str) -> str | None:
    """Return the NewsAPI ``q=`` string for ``symbol``, or None if unmapped."""
    return SYMBOL_QUERIES.get(symbol)


async def fetch_articles(
    client: httpx.AsyncClient,
    *,
    query: str,
    api_key: str,
    lookback_minutes: int = 30,
    page_size: int = 10,
) -> list[Article]:
    """Pull the latest matching articles from NewsAPI ``/everything``.

    ``lookback_minutes`` clips the result to the recent past so a
    poll cycle that runs every 5 min only re-evaluates articles in
    the last half hour - prevents historical articles from being
    re-scored every cycle.
    """
    cutoff = _dt.datetime.now(tz=_dt.UTC) - _dt.timedelta(minutes=lookback_minutes)
    from_iso = cutoff.replace(microsecond=0).isoformat().replace("+00:00", "Z")

    resp = await client.get(
        "https://newsapi.org/v2/everything",
        params={
            "q": query,
            "from": from_iso,
            "sortBy": "publishedAt",
            "language": "en",
            "pageSize": page_size,
        },
        headers={"X-Api-Key": api_key},
        timeout=10.0,
    )
    resp.raise_for_status()
    body = resp.json()
    if body.get("status") != "ok":
        raise RuntimeError(f"newsapi error: {body.get('message') or body.get('code')}")

    out: list[Article] = []
    for raw in body.get("articles", []) or []:
        article = _coerce_article(raw)
        if article is not None:
            out.append(article)
    return out


def _coerce_article(raw: dict[str, Any]) -> Article | None:
    """Pull our subset out of a NewsAPI article record."""
    url = raw.get("url")
    if not url:
        return None
    title = (raw.get("title") or "").strip()
    if not title:
        return None
    description = (raw.get("description") or "").strip()
    source = ((raw.get("source") or {}).get("name") or "unknown").strip()
    published_at = raw.get("publishedAt") or ""
    try:
        # NewsAPI uses ISO8601 with a "Z" suffix.
        ts = _dt.datetime.fromisoformat(published_at.replace("Z", "+00:00"))
        published_at_unix = int(ts.timestamp())
    except (ValueError, TypeError):
        published_at_unix = int(_dt.datetime.now(tz=_dt.UTC).timestamp())
    return Article(
        url=url,
        title=title,
        description=description,
        source=source,
        published_at_unix=published_at_unix,
    )
