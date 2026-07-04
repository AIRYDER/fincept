"""
quant_foundry.data_ingestion.news_vendor — fetch news articles from a
vendor news API (NewsAPI.org) and ingest them through the news pipeline.

This module adds a vendor API adapter for NewsAPI
(https://newsapi.org/v2/everything) that fetches news articles and feeds
them into the same leakage-safe :func:`ingest_news_events` pipeline used by
local-file ingestion.

We hit NewsAPI directly with ``httpx`` (no SDK).  The NewsAPI
``/v2/everything`` response is transformed into the normalized vendor-news
row format consumed by :func:`load_vendor_news_events` (from the
``news-impact-model`` experiment): each article becomes a row with
``headline``, ``body``, ``source``, ``published_at``, and ``url`` fields.

Env vars (from ``.env.example``):

- ``NEWSAPI_KEY`` — NewsAPI API key.

Heavy dependencies (httpx) are imported lazily inside functions so this
module is importable without them.
"""

from __future__ import annotations

import json
import os
import pathlib
import tempfile
from typing import Any

from quant_foundry.data_ingestion.equities import IngestionResult
from quant_foundry.data_ingestion.news import ingest_news_events

#: NewsAPI base URL for the ``/v2/everything`` endpoint.
NEWSAPI_BASE_URL = "https://newsapi.org/v2/everything"

#: Env var name for the NewsAPI API key.
_NEWSAPI_KEY_ENV = "NEWSAPI_KEY"


def _resolve_api_key(api_key: str | None) -> str:
    """Resolve the NewsAPI API key, falling back to the env var.

    Raises ``ValueError`` with a clear message if the key is missing.
    """
    key = api_key or os.environ.get(_NEWSAPI_KEY_ENV, "")
    if not key:
        raise ValueError(
            f"NewsAPI key not provided; pass api_key= or set {_NEWSAPI_KEY_ENV}",
        )
    return key


def _normalize_newsapi_article(article: dict[str, Any]) -> dict[str, Any]:
    """Transform a NewsAPI article into a vendor-news row.

    Maps NewsAPI's field names to the keys consumed by
    :func:`load_vendor_news_events`:

    - ``title``       -> ``headline``
    - ``description`` -> ``body``
    - ``source.name`` -> ``source``
    - ``publishedAt`` -> ``published_at`` (ISO-8601 string)
    - ``url``         -> ``url``
    """
    source_obj = article.get("source") or {}
    source_name = ""
    if isinstance(source_obj, dict):
        source_name = str(source_obj.get("name") or "").strip()
    return {
        "headline": str(article.get("title") or "").strip(),
        "body": str(article.get("description") or "").strip(),
        "source": source_name or "newsapi",
        "published_at": str(article.get("publishedAt") or "").strip(),
        "url": str(article.get("url") or "").strip(),
    }


async def fetch_newsapi_articles(
    *,
    query: str = "stock market",
    start: str,
    end: str,
    api_key: str | None = None,
    page_size: int = 100,
) -> pathlib.Path:
    """Fetch news articles from NewsAPI and write to a temp JSON file.

    Parameters
    ----------
    query
        Search query for the ``/v2/everything`` endpoint (default
        ``"stock market"``).
    start
        ISO date string for the ``from`` parameter (e.g. ``"2024-01-01"``).
    end
        ISO date string for the ``to`` parameter (e.g. ``"2024-06-30"``).
    api_key
        NewsAPI API key.  Falls back to ``NEWSAPI_KEY``.
    page_size
        Number of articles per page (default 100, the NewsAPI maximum).

    Returns
    -------
    pathlib.Path
        Path to the temp JSON file with an ``articles`` key holding the
        normalized vendor-news rows.

    Raises
    ------
    ValueError
        If the API key is missing or no articles are returned.
    """
    import httpx

    key = _resolve_api_key(api_key)

    params = {
        "q": query,
        "from": start,
        "to": end,
        "apiKey": key,
        "pageSize": str(page_size),
        "language": "en",
        "sortBy": "publishedAt",
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(NEWSAPI_BASE_URL, params=params)
        resp.raise_for_status()
        body = resp.json()

    raw_articles = body.get("articles") or []
    if not raw_articles:
        raise ValueError(
            f"no articles returned from NewsAPI for query {query!r} in [{start}, {end}]",
        )

    normalized = [_normalize_newsapi_article(a) for a in raw_articles]

    tmp_dir = pathlib.Path(tempfile.mkdtemp(prefix="newsapi_"))
    out_path = tmp_dir / "newsapi_articles.json"
    out_path.write_text(
        json.dumps({"articles": normalized}, ensure_ascii=False),
        encoding="utf-8",
    )
    return out_path


async def ingest_newsapi_events(
    *,
    query: str,
    start: str,
    end: str,
    output_dir: pathlib.Path,
    dataset_id: str,
    n_folds: int = 3,
    api_key: str | None = None,
) -> IngestionResult:
    """Fetch news from NewsAPI and ingest into a leakage-safe dataset.

    Fetches articles via :func:`fetch_newsapi_articles`, then runs the full
    :func:`ingest_news_events` pipeline (features + labels + manifest +
    receipt + quality report).

    Parameters
    ----------
    query
        Search query for the ``/v2/everything`` endpoint.
    start
        ISO date string for the ``from`` parameter.
    end
        ISO date string for the ``to`` parameter.
    output_dir
        Directory to write the dataset artifacts.  Created if needed.
    dataset_id
        Unique dataset identifier.
    n_folds
        Number of purged-k-fold validation windows (default 3).
    api_key
        NewsAPI API key; falls back to env var.

    Returns
    -------
    IngestionResult
        Paths to all emitted artifacts plus the manifest and quality report.
    """
    events_path = await fetch_newsapi_articles(
        query=query,
        start=start,
        end=end,
        api_key=api_key,
    )
    return ingest_news_events(
        events_path,
        output_dir=pathlib.Path(output_dir),
        dataset_id=dataset_id,
        source_type="newsapi",
        n_folds=n_folds,
    )


__all__ = [
    "NEWSAPI_BASE_URL",
    "fetch_newsapi_articles",
    "ingest_newsapi_events",
]
