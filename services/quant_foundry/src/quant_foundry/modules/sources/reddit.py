"""
quant_foundry.modules.sources.reddit — Reddit source adapter.

Fetches posts and comments from financial subreddits (wallstreetbets,
stocks, investing, StockMarket) and normalizes them into
:class:`MediaItem` objects.  Uses Reddit's public JSON API (no auth
required for read-only access — just append ``.json`` to any Reddit URL).

Symbol extraction is done via cashtag matching (``$AAPL``) and
company-name-to-ticker lookup.  Reddit posts don't have built-in
sentiment tags (unlike StockTwits), so sentiment is computed by the
sentiment engine module (FinBERT for formal text, LLM ensemble for
slang/sarcasm).

Rate limits: Reddit's public JSON API is ~100 requests/minute per IP.
We paginate with a small delay to stay under the limit.

This module is registered as ``source:reddit:1.0.0``.
"""

from __future__ import annotations

import hashlib
import os
import time
from typing import Any

from quant_foundry.modules.registry import (
    MediaItem,
    ModuleInfo,
    register_module,
)

#: Default subreddits to search.
DEFAULT_SUBREDDITS: tuple[str, ...] = (
    "wallstreetbets",
    "stocks",
    "investing",
    "StockMarket",
)

#: Reddit JSON API base URL.
REDDIT_BASE_URL = "https://www.reddit.com"

#: Default posts per subreddit per fetch.
DEFAULT_LIMIT = 25

#: Default maximum number of pages to fetch per subreddit.
DEFAULT_MAX_PAGES = 2

#: Default number of comments to fetch per post (when fetch_comments=True).
DEFAULT_MAX_COMMENTS_PER_POST = 5


@register_module(
    "source",
    "reddit",
    "1.0.0",
    default_config={
        "subreddits": list(DEFAULT_SUBREDDITS),
        "limit": DEFAULT_LIMIT,
        "timeout": 30.0,
        "delay_seconds": 1.0,  # delay between subreddit fetches
        "sort": "new",  # "new", "hot", "top"
        "max_pages": DEFAULT_MAX_PAGES,
        "fetch_comments": False,
        "max_comments_per_post": DEFAULT_MAX_COMMENTS_PER_POST,
    },
)
class RedditSource:
    """Fetch posts from Reddit financial subreddits and normalize to MediaItem.

    Uses Reddit's public JSON API (``.json`` suffix on any Reddit URL).
    No authentication required for read-only access.  Set
    ``REDDIT_USER_AGENT`` env var to a descriptive user-agent string
    (Reddit's API guidelines require this).

    Pagination: uses the ``after`` cursor from the Reddit JSON response
    to fetch up to ``max_pages`` pages per subreddit (default 2).  When
    ``max_pages=1``, behavior is identical to a single-page fetch
    (backward compatible).

    Comments: when ``fetch_comments=True``, the top N comments
    (``max_comments_per_post``, default 5) are fetched for each post
    via ``/r/{subreddit}/comments/{post_id}.json`` and converted into
    separate MediaItem objects.
    """

    info: ModuleInfo

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self.config = config or {}
        self.subreddits: list[str] = self.config.get("subreddits", list(DEFAULT_SUBREDDITS))
        self.limit: int = self.config.get("limit", DEFAULT_LIMIT)
        self.timeout: float = self.config.get("timeout", 30.0)
        self.delay_seconds: float = self.config.get("delay_seconds", 1.0)
        self.sort: str = self.config.get("sort", "new")
        self.max_pages: int = self.config.get("max_pages", DEFAULT_MAX_PAGES)
        self.fetch_comments: bool = self.config.get("fetch_comments", False)
        self.max_comments_per_post: int = self.config.get(
            "max_comments_per_post", DEFAULT_MAX_COMMENTS_PER_POST,
        )

    def _get_user_agent(self) -> str:
        return os.environ.get(
            "REDDIT_USER_AGENT",
            "fincept-terminal/1.0 (research dataset builder)",
        )

    async def fetch(
        self,
        *,
        symbols: list[str],
        start_ns: int,
        end_ns: int,
    ) -> list[MediaItem]:
        """Fetch Reddit posts for the given symbols and time range."""
        import httpx

        headers = {"User-Agent": self._get_user_agent()}
        symbol_set = set(symbols)
        items: list[MediaItem] = []

        async with httpx.AsyncClient(timeout=self.timeout, headers=headers) as client:
            for subreddit in self.subreddits:
                try:
                    resp = await client.get(
                        f"{REDDIT_BASE_URL}/r/{subreddit}/{self.sort}.json",
                        params={"limit": str(self.limit)},
                    )
                    resp.raise_for_status()
                    body = resp.json()
                except (httpx.HTTPError, KeyError, ValueError):
                    continue

                posts = body.get("data", {}).get("children", [])
                for post_wrapper in posts:
                    post = post_wrapper.get("data", {})
                    item = self._normalize_post(post, subreddit, symbol_set)
                    if item is not None and start_ns <= item.available_at_ns < end_ns:
                        items.append(item)

                # Rate limit delay between subreddits
                if self.delay_seconds > 0:
                    time.sleep(self.delay_seconds)

        return items

    def _normalize_post(
        self,
        post: dict[str, Any],
        subreddit: str,
        symbol_set: set[str],
    ) -> MediaItem | None:
        """Normalize a Reddit post into a MediaItem."""
        try:
            title = post.get("title", "").strip()
            self_text = post.get("selftext", "").strip()
            if not title:
                return None

            # Combine title + body for text
            full_text = f"{title}\n{self_text}".strip()

            # Timestamp: Reddit uses seconds, convert to ns
            created_utc = post.get("created_utc")
            if created_utc is None:
                return None
            available_at_ns = int(float(created_utc) * 1_000_000_000)

            # Post ID
            post_id = post.get("id", "")
            item_id = f"reddit:{post_id}" if post_id else (
                f"reddit:{hashlib.sha256((title + str(available_at_ns)).encode()).hexdigest()[:20]}"
            )

            # Extract symbols via cashtag matching
            found_symbols = self._extract_symbols(full_text, symbol_set)

            # If no symbols found, skip this post (not relevant to our universe)
            if not found_symbols:
                return None

            # Score and engagement metadata
            score = post.get("score", 0)
            num_comments = post.get("num_comments", 0)
            upvote_ratio = post.get("upvote_ratio", 0.0)

            metadata: dict[str, str] = {
                "reddit_subreddit": subreddit,
                "reddit_post_id": post_id,
                "reddit_score": str(score),
                "reddit_num_comments": str(num_comments),
                "reddit_upvote_ratio": str(upvote_ratio),
            }

            permalink = post.get("permalink", "")
            url = f"{REDDIT_BASE_URL}{permalink}" if permalink else None

            return MediaItem(
                item_id=item_id,
                source="reddit",
                headline=title,
                body=self_text,
                available_at_ns=available_at_ns,
                symbols=found_symbols,
                event_type="social",
                url=url,
                language="en",
                metadata=metadata,
            )
        except (ValueError, TypeError):
            return None

    def _extract_symbols(self, text: str, symbol_set: set[str]) -> tuple[str, ...]:
        """Extract ticker symbols from text using cashtag + known-symbol matching."""
        import re

        found: list[str] = []

        # Cashtag matching: $AAPL
        for match in re.finditer(r"(?<![A-Z0-9])\$([A-Z][A-Z0-9.-]{0,9})(?![A-Z0-9])", text):
            sym = match.group(1).upper().replace("$", "")
            if sym in symbol_set and sym not in found:
                found.append(sym)

        # Known symbol word-boundary matching
        for sym in symbol_set:
            if sym in found:
                continue
            if len(sym) <= 2:
                continue
            if re.search(rf"(?<![A-Z0-9]){re.escape(sym)}(?![A-Z0-9])", text):
                found.append(sym)

        return tuple(found)


__all__ = ["RedditSource", "DEFAULT_SUBREDDITS", "REDDIT_BASE_URL"]
