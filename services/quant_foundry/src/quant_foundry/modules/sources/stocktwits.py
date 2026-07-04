"""
quant_foundry.modules.sources.stocktwits — StockTwits source adapter.

Fetches messages from StockTwits' public API and normalizes them into
:class:`MediaItem` objects.  StockTwits is the highest-signal social
source for finance because:

1. Every message is tagged with a ticker (cashtag is mandatory).
2. Users can tag their message with a sentiment: **Bullish** or **Bearish**.
   This is free, human-labeled sentiment ground truth — no LLM needed.
3. The API is free and requires only a client ID (optional for public
reads, required for higher rate limits).

The built-in sentiment is stored in ``metadata["stocktwits_sentiment"]``
so the sentiment engine can use it as ground truth or cross-validation.

API docs: https://api.stocktwits.com/developers/docs/api.html

This module is registered as ``source:stocktwits:1.0.0``.
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

#: StockTwits API base URL.
STOCKTWITS_BASE_URL = "https://api.stocktwits.com/api/2"

#: Default subreddits to search.
DEFAULT_MAX_PER_SYMBOL = 30

#: Default maximum number of pages to fetch per symbol.
DEFAULT_MAX_PAGES = 3


@register_module(
    "source",
    "stocktwits",
    "1.0.0",
    default_config={
        "max_per_symbol": DEFAULT_MAX_PER_SYMBOL,
        "timeout": 30.0,
        "max_pages": DEFAULT_MAX_PAGES,
    },
)
class StockTwitsSource:
    """Fetch messages from StockTwits and normalize to MediaItem.

    Uses the StockTwits public symbol stream endpoint
    (``/streams/symbol/{ticker}.json``) to fetch recent messages for
    each ticker.  The API returns messages with optional sentiment
    tags (Bullish/Bearish).

    Rate limits: 200 requests/hour per IP (no auth), 400/hour with
    client_id.  Set ``STOCKTWITS_CLIENT_ID`` env var for higher limits.

    Pagination: uses the ``more.since`` cursor from the API response
    to fetch up to ``max_pages`` pages per symbol (default 3).  When
    ``max_pages=1``, behavior is identical to a single-page fetch
    (backward compatible).
    """

    info: ModuleInfo

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self.config = config or {}
        self.max_per_symbol: int = self.config.get("max_per_symbol", DEFAULT_MAX_PER_SYMBOL)
        self.timeout: float = self.config.get("timeout", 30.0)
        self.max_pages: int = self.config.get("max_pages", DEFAULT_MAX_PAGES)

    def _get_client_id(self) -> str | None:
        return os.environ.get("STOCKTWITS_CLIENT_ID") or None

    async def fetch(
        self,
        *,
        symbols: list[str],
        start_ns: int,
        end_ns: int,
    ) -> list[MediaItem]:
        """Fetch StockTwits messages for the given symbols and time range.

        Paginates per symbol using the ``more.since`` cursor from the
        API response, fetching up to ``max_pages`` pages.
        """
        import httpx

        client_id = self._get_client_id()
        base_params: dict[str, str] = {"limit": str(min(self.max_per_symbol, 30))}
        if client_id:
            base_params["client_id"] = client_id

        items: list[MediaItem] = []
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            for sym in symbols:
                params = dict(base_params)
                for _page in range(1, self.max_pages + 1):
                    try:
                        resp = await client.get(
                            f"{STOCKTWITS_BASE_URL}/streams/symbol/{sym}.json",
                            params=params,
                        )
                        resp.raise_for_status()
                        body = resp.json()
                    except (httpx.HTTPError, KeyError, ValueError):
                        break

                    messages = body.get("messages", [])
                    for msg in messages[: self.max_per_symbol]:
                        item = self._normalize_message(msg, sym)
                        if item is not None and start_ns <= item.available_at_ns < end_ns:
                            items.append(item)

                    # Extract the `since` cursor from the `more` field
                    # for pagination.  Stop if no cursor or no messages.
                    more = body.get("more") or {}
                    since = more.get("since") if isinstance(more, dict) else None
                    if not since or not messages:
                        break
                    params["since"] = str(since)

        return items

    def _normalize_message(self, msg: dict[str, Any], symbol: str) -> MediaItem | None:
        """Normalize a StockTwits message into a MediaItem."""
        try:
            body_text = msg.get("body", "").strip()
            if not body_text:
                return None

            created_at = msg.get("created_at", "")
            available_at_ns = _parse_iso_to_ns(created_at)

            # Extract sentiment tag if present
            sentiment = None
            sentiment_data = msg.get("sentiment")
            if sentiment_data and isinstance(sentiment_data, dict):
                sentiment = sentiment_data.get("name", "").lower()  # "bullish" or "bearish"

            # User info
            user = msg.get("user", {})
            username = user.get("username", "unknown") if isinstance(user, dict) else "unknown"

            # Message ID
            msg_id = str(msg.get("id", ""))
            item_id = (
                f"stocktwits:{msg_id}"
                if msg_id
                else (
                    f"stocktwits:{hashlib.sha256((body_text + str(available_at_ns)).encode()).hexdigest()[:20]}"
                )
            )

            # Extract all symbols mentioned
            symbols_mentioned: tuple[str, ...] = (symbol,)
            symbols_data = msg.get("symbols", [])
            if isinstance(symbols_data, list):
                extra = [
                    s.get("symbol", "").upper()
                    for s in symbols_data
                    if isinstance(s, dict) and s.get("symbol")
                ]
                all_syms = [symbol] + [s for s in extra if s and s != symbol]
                symbols_mentioned = tuple(dict.fromkeys(all_syms))  # dedupe, preserve order

            metadata: dict[str, str] = {
                "stocktwits_user": username,
                "stocktwits_msg_id": msg_id,
            }
            if sentiment:
                metadata["stocktwits_sentiment"] = sentiment

            return MediaItem(
                item_id=item_id,
                source="stocktwits",
                headline=body_text[
                    :200
                ],  # StockTwits messages are short; headline = first 200 chars
                body=body_text,
                available_at_ns=available_at_ns,
                symbols=symbols_mentioned,
                event_type="social",
                url=f"https://stocktwits.com/{username}/message/{msg_id}" if msg_id else None,
                language="en",
                metadata=metadata,
            )
        except (ValueError, TypeError):
            return None


def _parse_iso_to_ns(iso_str: str) -> int:
    """Parse an ISO-8601 timestamp to nanoseconds since epoch."""
    import datetime as dt

    normalized = iso_str.strip().replace("Z", "+00:00")
    parsed = dt.datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    else:
        parsed = parsed.astimezone(dt.UTC)
    return int(parsed.timestamp() * 1_000_000_000)


__all__ = ["DEFAULT_MAX_PAGES", "STOCKTWITS_BASE_URL", "StockTwitsSource"]
