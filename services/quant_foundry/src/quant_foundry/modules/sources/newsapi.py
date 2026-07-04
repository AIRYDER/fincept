"""
quant_foundry.modules.sources.newsapi — NewsAPI source adapter.

Fetches news articles from NewsAPI.org and normalizes them into
:class:`MediaItem` objects.  Uses the NewsAPI ``/v2/everything``
endpoint directly with ``httpx`` and supports multi-page fetching via
the ``page`` parameter (driven by the ``totalResults`` field).

This module is registered as ``source:newsapi:1.0.0``.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
from typing import Any

from quant_foundry.modules.registry import (
    MediaItem,
    ModuleInfo,
    register_module,
)

#: NewsAPI base URL for the ``/v2/everything`` endpoint.
NEWSAPI_BASE_URL = "https://newsapi.org/v2/everything"

#: Default maximum number of pages to fetch.
DEFAULT_MAX_PAGES = 5

#: Delay between pages for rate limiting (seconds).
DEFAULT_PAGE_DELAY = 1.0

# Reuse the event-type classifier from the news-impact-model experiment.
import pathlib
import sys

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[6]
_NEWS_SRC = _REPO_ROOT / "experiments" / "news-impact-model" / "src"
if str(_NEWS_SRC) not in sys.path:
    sys.path.insert(0, str(_NEWS_SRC))

try:
    from news_impact_model.events import classify_event_type, normalize_event_type
except ImportError:  # pragma: no cover

    def classify_event_type(headline: str, body: str = "") -> str:
        return "general"

    def normalize_event_type(value: str) -> str:
        return "general"


@register_module(
    "source",
    "newsapi",
    "1.0.0",
    default_config={
        "query": "stock market",
        "page_size": 100,
        "language": "en",
        "max_pages": DEFAULT_MAX_PAGES,
        "page_delay": DEFAULT_PAGE_DELAY,
        "timeout": 30.0,
    },
)
class NewsAPISource:
    """Fetch news articles from NewsAPI and normalize to MediaItem.

    Uses ``NEWSAPI_KEY`` env var for authentication.  Fetches articles
    via the ``/v2/everything`` endpoint, normalizes them into
    :class:`MediaItem` objects with event-type classification and
    symbol extraction.

    Pagination: uses the ``page`` parameter and the ``totalResults``
    field from the API response to fetch up to ``max_pages`` pages
    (default 5).  A ``page_delay`` second delay is inserted between
    pages for rate limiting.  When ``max_pages=1``, behavior is
    identical to a single-page fetch (backward compatible).
    """

    info: ModuleInfo

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self.config = config or {}
        self.query: str = self.config.get("query", "stock market")
        self.page_size: int = self.config.get("page_size", 100)
        self.language: str = self.config.get("language", "en")
        self.max_pages: int = self.config.get("max_pages", DEFAULT_MAX_PAGES)
        self.page_delay: float = self.config.get("page_delay", DEFAULT_PAGE_DELAY)
        self.timeout: float = self.config.get("timeout", 30.0)

    def _get_api_key(self) -> str:
        key = os.environ.get("NEWSAPI_KEY", "")
        if not key:
            raise ValueError("NEWSAPI_KEY is not set")
        return key

    async def fetch(
        self,
        *,
        symbols: list[str],
        start_ns: int,
        end_ns: int,
    ) -> list[MediaItem]:
        """Fetch news articles for the given symbols and time range.

        Converts ``start_ns``/``end_ns`` to ISO date strings for the
        NewsAPI ``from``/``to`` parameters.  Fetches up to ``max_pages``
        pages using the ``page`` parameter, stopping early when all
        ``totalResults`` have been retrieved.
        """
        import datetime as dt

        import httpx

        try:
            api_key = self._get_api_key()
        except ValueError:
            return []

        start_date = dt.datetime.fromtimestamp(
            start_ns / 1_000_000_000,
            tz=dt.UTC,
        ).strftime("%Y-%m-%d")
        end_date = dt.datetime.fromtimestamp(
            end_ns / 1_000_000_000,
            tz=dt.UTC,
        ).strftime("%Y-%m-%d")

        # Build a query that includes the symbols for better targeting.
        # NewsAPI's `q` parameter supports OR and quotes.
        if symbols and self.query == "stock market":
            # Use a few top symbols in the query to keep it manageable.
            sym_query = " OR ".join(symbols[:5])
            query = sym_query
        else:
            query = self.query

        items: list[MediaItem] = []
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            for page in range(1, self.max_pages + 1):
                try:
                    params = {
                        "q": query,
                        "from": start_date,
                        "to": end_date,
                        "apiKey": api_key,
                        "pageSize": str(self.page_size),
                        "language": self.language,
                        "sortBy": "publishedAt",
                        "page": str(page),
                    }
                    resp = await client.get(NEWSAPI_BASE_URL, params=params)
                    resp.raise_for_status()
                    body = resp.json()
                except (httpx.HTTPError, KeyError, ValueError):
                    break

                articles = body.get("articles", [])
                total_results = body.get("totalResults", 0)

                for article in articles:
                    item = self._normalize_article(
                        article,
                        symbols,
                        start_ns,
                        end_ns,
                    )
                    if item is not None:
                        items.append(item)

                # Stop if we've fetched all available results or the
                # current page returned fewer than page_size (last page).
                fetched = page * self.page_size
                if not articles or fetched >= total_results:
                    break

                # Rate-limit delay between pages (skip after the last page).
                if self.page_delay > 0 and page < self.max_pages:
                    await asyncio.sleep(self.page_delay)

        return items

    def _normalize_article(
        self,
        article: dict[str, Any],
        known_symbols: list[str],
        start_ns: int,
        end_ns: int,
    ) -> MediaItem | None:
        """Normalize a raw NewsAPI article into a MediaItem."""
        # NewsAPI article fields: title, description, source.name,
        # publishedAt, url.  Map to the normalized vendor-news shape.
        source_obj = article.get("source") or {}
        source = ""
        if isinstance(source_obj, dict):
            source = str(source_obj.get("name") or "").strip() or "newsapi"
        else:
            source = "newsapi"

        headline = str(article.get("title") or "").strip()
        body = str(article.get("description") or "").strip()
        url = str(article.get("url") or "").strip() or None
        published_at = str(article.get("publishedAt") or "").strip()

        if not headline:
            return None

        # Parse timestamp
        try:
            from news_impact_model.events import parse_timestamp_ns

            available_at_ns = parse_timestamp_ns(published_at)
        except (ImportError, ValueError):
            return None

        # Time-range filter
        if not (start_ns <= available_at_ns < end_ns):
            return None

        # Classify event type
        event_type = normalize_event_type(classify_event_type(headline, body))

        # Extract symbols from headline/body using cashtag matching
        item_symbols = self._extract_symbols(headline, body, known_symbols)

        # Stable item ID
        item_id = f"newsapi:{hashlib.sha256((source + headline + str(available_at_ns)).encode()).hexdigest()[:20]}"

        return MediaItem(
            item_id=item_id,
            source="newsapi",
            headline=headline,
            body=body,
            available_at_ns=available_at_ns,
            symbols=item_symbols,
            event_type=event_type,
            url=url,
            language=self.language,
        )

    def _extract_symbols(
        self,
        headline: str,
        body: str,
        known_symbols: list[str],
    ) -> tuple[str, ...]:
        """Extract symbols from text using cashtag matching + known symbol lookup."""
        import re

        text = f"{headline}\n{body}"
        found: list[str] = []

        # Cashtag matching: $AAPL
        for match in re.finditer(r"(?<![A-Z0-9])\$([A-Z][A-Z0-9.-]{0,9})(?![A-Z0-9])", text):
            sym = match.group(1).upper().replace("$", "")
            if sym in known_symbols and sym not in found:
                found.append(sym)

        # Known symbol word-boundary matching
        for sym in known_symbols:
            if sym in found:
                continue
            if len(sym) <= 2:
                continue
            if re.search(rf"(?<![A-Z0-9]){re.escape(sym)}(?![A-Z0-9])", text):
                found.append(sym)

        return tuple(found)


__all__ = ["DEFAULT_MAX_PAGES", "NEWSAPI_BASE_URL", "NewsAPISource"]
