"""
quant_foundry.modules.sources.x_twitter — X (Twitter) source adapter.

Fetches posts from X/Twitter via the X API v2 using cashtag search
(``$AAPL``).  X is the highest-volume real-time social signal for
finance — breaking news, analyst opinions, and retail sentiment all
flow through X first.

Uses the X API v2 ``GET /2/tweets/search/recent`` endpoint with a
cashtag query.  Requires a Bearer token (``X_BEARER_TOKEN`` env var)
from the X API.  The recent search endpoint covers the last 7 days;
for historical data, the academic/full-archive endpoint is needed
(``X_ACADEMIC_BEARER_TOKEN``).

This module is registered as ``source:x-twitter:1.0.0``.
"""

from __future__ import annotations

import hashlib
import os
from typing import Any

from quant_foundry.modules.registry import (
    MediaItem,
    ModuleInfo,
    register_module,
)

#: X API v2 base URL.
X_API_BASE_URL = "https://api.twitter.com/2"

#: Default max results per symbol (API max is 100 for recent search).
DEFAULT_MAX_RESULTS = 100


@register_module(
    "source",
    "x-twitter",
    "1.0.0",
    default_config={
        "max_results": DEFAULT_MAX_RESULTS,
        "timeout": 30.0,
        "search_mode": "recent",  # "recent" or "full_archive"
    },
)
class XTwitterSource:
    """Fetch posts from X/Twitter via API v2 cashtag search.

    Uses the ``GET /2/tweets/search/recent`` endpoint (or
    ``/all`` for full-archive with academic access).  Searches for
    cashtag queries like ``$AAPL`` to find posts mentioning specific
    tickers.

    Requires ``X_BEARER_TOKEN`` env var (or
    ``X_ACADEMIC_BEARER_TOKEN`` for full-archive mode).
    """

    info: ModuleInfo

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self.config = config or {}
        self.max_results: int = self.config.get("max_results", DEFAULT_MAX_RESULTS)
        self.timeout: float = self.config.get("timeout", 30.0)
        self.search_mode: str = self.config.get("search_mode", "recent")

    def _get_bearer_token(self) -> str:
        if self.search_mode == "full_archive":
            token = os.environ.get("X_ACADEMIC_BEARER_TOKEN", "")
            if token:
                return token
        token = os.environ.get("X_BEARER_TOKEN", "")
        if not token:
            raise ValueError(
                "X_BEARER_TOKEN is not set. Set it in the environment "
                "or RunPod container env. (Use X_ACADEMIC_BEARER_TOKEN "
                "for full-archive search mode.)"
            )
        return token

    async def fetch(
        self,
        *,
        symbols: list[str],
        start_ns: int,
        end_ns: int,
    ) -> list[MediaItem]:
        """Fetch X/Twitter posts for the given symbols and time range."""
        import httpx

        try:
            bearer_token = self._get_bearer_token()
        except ValueError:
            return []

        headers = {
            "Authorization": f"Bearer {bearer_token}",
        }

        # Build time range for the API (ISO format)
        import datetime as dt

        start_time = dt.datetime.fromtimestamp(
            start_ns / 1_000_000_000, tz=dt.timezone.utc,
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        end_time = dt.datetime.fromtimestamp(
            end_ns / 1_000_000_000, tz=dt.timezone.utc,
        ).strftime("%Y-%m-%dT%H:%M:%SZ")

        # Determine endpoint
        if self.search_mode == "full_archive":
            endpoint = f"{X_API_BASE_URL}/tweets/search/all"
        else:
            endpoint = f"{X_API_BASE_URL}/tweets/search/recent"

        # Tweet fields to request
        tweet_fields = "created_at,public_metrics,entities,lang"

        items: list[MediaItem] = []
        async with httpx.AsyncClient(timeout=self.timeout, headers=headers) as client:
            for sym in symbols:
                try:
                    # Build cashtag query
                    query = f"${sym} -is:retweet lang:en"

                    params = {
                        "query": query,
                        "max_results": str(min(self.max_results, 100)),
                        "start_time": start_time,
                        "end_time": end_time,
                        "tweet.fields": tweet_fields,
                    }

                    resp = await client.get(endpoint, params=params)
                    resp.raise_for_status()
                    body = resp.json()
                except (httpx.HTTPError, KeyError, ValueError):
                    continue

                tweets = body.get("data", [])
                for tweet in tweets:
                    item = self._normalize_tweet(tweet, sym)
                    if item is not None:
                        items.append(item)

        return items

    def _normalize_tweet(self, tweet: dict[str, Any], searched_symbol: str) -> MediaItem | None:
        """Normalize an X/Twitter tweet into a MediaItem."""
        try:
            text = tweet.get("text", "").strip()
            if not text:
                return None

            # Parse created_at (ISO format from X API)
            created_at = tweet.get("created_at", "")
            available_at_ns = _parse_x_iso_to_ns(created_at)

            # Tweet ID
            tweet_id = tweet.get("id", "")
            item_id = f"x_twitter:{tweet_id}" if tweet_id else (
                f"x_twitter:{hashlib.sha256((text + str(available_at_ns)).encode()).hexdigest()[:20]}"
            )

            # Extract all cashtag symbols from the tweet
            found_symbols: list[str] = [searched_symbol]
            entities = tweet.get("entities", {})
            cashtags = entities.get("cashtags", [])
            if isinstance(cashtags, list):
                for ct in cashtags:
                    if isinstance(ct, dict):
                        tag = ct.get("tag", "").upper()
                        if tag and tag not in found_symbols:
                            found_symbols.append(tag)

            # Public metrics
            metrics = tweet.get("public_metrics", {})
            metadata: dict[str, str] = {
                "x_tweet_id": tweet_id,
                "x_retweet_count": str(metrics.get("retweet_count", 0)),
                "x_reply_count": str(metrics.get("reply_count", 0)),
                "x_like_count": str(metrics.get("like_count", 0)),
                "x_quote_count": str(metrics.get("quote_count", 0)),
            }

            return MediaItem(
                item_id=item_id,
                source="x_twitter",
                headline=text[:200],  # tweets are short; headline = first 200 chars
                body=text,
                available_at_ns=available_at_ns,
                symbols=tuple(dict.fromkeys(found_symbols)),
                event_type="social",
                url=f"https://x.com/i/web/status/{tweet_id}" if tweet_id else None,
                language=tweet.get("lang", "en"),
                metadata=metadata,
            )
        except (ValueError, TypeError):
            return None


def _parse_x_iso_to_ns(iso_str: str) -> int:
    """Parse an X API ISO-8601 timestamp to nanoseconds since epoch."""
    import datetime as dt

    if not iso_str:
        return 0
    # X API returns format like "2023-06-01T12:00:00.000Z"
    normalized = iso_str.strip().replace("Z", "+00:00")
    parsed = dt.datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    else:
        parsed = parsed.astimezone(dt.timezone.utc)
    return int(parsed.timestamp() * 1_000_000_000)


__all__ = ["XTwitterSource", "X_API_BASE_URL"]
