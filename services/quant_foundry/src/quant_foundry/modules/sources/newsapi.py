"""
quant_foundry.modules.sources.newsapi — NewsAPI source adapter.

Fetches news articles from NewsAPI.org and normalizes them into
:class:`MediaItem` objects.  Wraps the existing
``quant_foundry.data_ingestion.news_vendor.fetch_newsapi_articles``
function so we reuse the same API calling logic.

This module is registered as ``source:newsapi:1.0.0``.
"""

from __future__ import annotations

import hashlib
from typing import Any

from quant_foundry.modules.registry import (
    MediaItem,
    ModuleInfo,
    register_module,
)

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
    },
)
class NewsAPISource:
    """Fetch news articles from NewsAPI and normalize to MediaItem.

    Uses ``NEWSAPI_KEY`` env var for authentication.  Fetches articles
    via the ``/v2/everything`` endpoint, normalizes them into
    :class:`MediaItem` objects with event-type classification and
    symbol extraction.
    """

    info: ModuleInfo

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self.config = config or {}
        self.query: str = self.config.get("query", "stock market")
        self.page_size: int = self.config.get("page_size", 100)
        self.language: str = self.config.get("language", "en")

    async def fetch(
        self,
        *,
        symbols: list[str],
        start_ns: int,
        end_ns: int,
    ) -> list[MediaItem]:
        """Fetch news articles for the given symbols and time range.

        Converts ``start_ns``/``end_ns`` to ISO date strings for the
        NewsAPI ``from``/``to`` parameters.
        """
        import datetime as dt

        from quant_foundry.data_ingestion.news_vendor import fetch_newsapi_articles

        start_date = dt.datetime.fromtimestamp(
            start_ns / 1_000_000_000, tz=dt.timezone.utc,
        ).strftime("%Y-%m-%d")
        end_date = dt.datetime.fromtimestamp(
            end_ns / 1_000_000_000, tz=dt.timezone.utc,
        ).strftime("%Y-%m-%d")

        # Build a query that includes the symbols for better targeting.
        # NewsAPI's `q` parameter supports OR and quotes.
        if symbols and self.query == "stock market":
            # Use a few top symbols in the query to keep it manageable.
            sym_query = " OR ".join(symbols[:5])
            query = sym_query
        else:
            query = self.query

        events_path = await fetch_newsapi_articles(
            query=query,
            start=start_date,
            end=end_date,
            page_size=self.page_size,
        )

        # Parse the normalized articles into MediaItem objects.
        import json

        payload = json.loads(events_path.read_text(encoding="utf-8"))
        articles = payload.get("articles", [])

        items: list[MediaItem] = []
        for article in articles:
            headline = article.get("headline", "")
            body = article.get("body", "")
            source = article.get("source", "newsapi")
            url = article.get("url")
            published_at = article.get("published_at", "")

            # Parse timestamp
            try:
                from news_impact_model.events import parse_timestamp_ns

                available_at_ns = parse_timestamp_ns(published_at)
            except (ImportError, ValueError):
                continue

            # Classify event type
            event_type = normalize_event_type(classify_event_type(headline, body))

            # Extract symbols from headline/body using cashtag matching
            item_symbols = self._extract_symbols(headline, body, symbols)

            # Stable item ID
            item_id = f"newsapi:{hashlib.sha256((source + headline + str(available_at_ns)).encode()).hexdigest()[:20]}"

            items.append(MediaItem(
                item_id=item_id,
                source="newsapi",
                headline=headline,
                body=body,
                available_at_ns=available_at_ns,
                symbols=item_symbols,
                event_type=event_type,
                url=url,
                language=self.language,
            ))

        return items

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


__all__ = ["NewsAPISource"]
