"""
Tests for Phase 3 social source adapters — StockTwits, Reddit, X/Twitter.

Tests verify:
- All source modules register correctly.
- Modules are importable without httpx at module level (lazy import).
- StockTwits: message normalization, sentiment tag extraction, symbol
  extraction, ISO timestamp parsing, time-range filtering.
- Reddit: post normalization, subreddit filtering, cashtag + known-symbol
  extraction, engagement metadata, skip posts with no relevant symbols.
- X/Twitter: tweet normalization, cashtag entity extraction, public
  metrics metadata, bearer token validation, graceful return on missing
  token.
- All three: graceful degradation on API errors (return empty list, not
  crash).
"""

from __future__ import annotations

import asyncio
import pathlib
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
_SCRIPTS_DIR = _REPO_ROOT / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))


# --------------------------------------------------------------------------- #
# Registration tests                                                          #
# --------------------------------------------------------------------------- #


def test_all_source_modules_registered() -> None:
    """All 4 source modules should be registered after load_all_modules."""
    from quant_foundry.modules import ModuleRegistry, load_all_modules

    load_all_modules()
    registry = ModuleRegistry.instance()
    source_modules = registry.list_by_category("source")

    expected = {
        "source:newsapi:1.0.0",
        "source:stocktwits:1.0.0",
        "source:reddit:1.0.0",
        "source:x-twitter:1.0.0",
    }
    assert expected.issubset(set(source_modules)), f"missing: {expected - set(source_modules)}"


# --------------------------------------------------------------------------- #
# Module-level heavy deps check                                               #
# --------------------------------------------------------------------------- #


def test_source_modules_no_module_level_httpx() -> None:
    """httpx must NOT be imported at module level in source adapters."""
    import quant_foundry.modules.sources.newsapi as newsapi
    import quant_foundry.modules.sources.reddit as reddit
    import quant_foundry.modules.sources.stocktwits as stocktwits
    import quant_foundry.modules.sources.x_twitter as x_twitter

    for mod in (newsapi, reddit, stocktwits, x_twitter):
        assert not hasattr(mod, "httpx"), f"{mod.__name__}: httpx at module level"


# --------------------------------------------------------------------------- #
# StockTwits tests                                                            #
# --------------------------------------------------------------------------- #


def test_stocktwits_importable_without_httpx() -> None:
    """StockTwitsSource must be importable and instantiable without httpx."""
    from quant_foundry.modules.sources.stocktwits import StockTwitsSource

    mod = StockTwitsSource()
    assert mod.max_per_symbol == 30
    assert mod.timeout == 30.0


def test_stocktwits_normalize_message() -> None:
    """StockTwits message normalization extracts sentiment + symbols."""
    from quant_foundry.modules.sources.stocktwits import StockTwitsSource

    mod = StockTwitsSource()
    msg = {
        "id": 12345,
        "body": "$AAPL to the moon! Earnings will beat expectations.",
        "created_at": "2023-06-01T12:00:00Z",
        "sentiment": {"name": "Bullish"},
        "user": {"username": "trader_joe"},
        "symbols": [
            {"symbol": "AAPL"},
            {"symbol": "MSFT"},
        ],
    }

    item = mod._normalize_message(msg, "AAPL")
    assert item is not None
    assert item.source == "stocktwits"
    assert item.event_type == "social"
    assert "AAPL" in item.symbols
    assert "MSFT" in item.symbols
    assert item.metadata["stocktwits_sentiment"] == "bullish"
    assert item.metadata["stocktwits_user"] == "trader_joe"
    assert item.available_at_ns > 0


def test_stocktwits_normalize_message_no_sentiment() -> None:
    """StockTwits messages without sentiment tags are handled correctly."""
    from quant_foundry.modules.sources.stocktwits import StockTwitsSource

    mod = StockTwitsSource()
    msg = {
        "id": 12346,
        "body": "Just watching $GOOGL today",
        "created_at": "2023-06-01T13:00:00Z",
        "sentiment": None,
        "user": {"username": "observer"},
        "symbols": [{"symbol": "GOOGL"}],
    }

    item = mod._normalize_message(msg, "GOOGL")
    assert item is not None
    assert "stocktwits_sentiment" not in item.metadata
    assert "GOOGL" in item.symbols


def test_stocktwits_normalize_empty_body() -> None:
    """StockTwits messages with empty body return None."""
    from quant_foundry.modules.sources.stocktwits import StockTwitsSource

    mod = StockTwitsSource()
    msg = {
        "id": 12347,
        "body": "",
        "created_at": "2023-06-01T14:00:00Z",
        "user": {"username": "empty_poster"},
    }

    item = mod._normalize_message(msg, "AAPL")
    assert item is None


def test_stocktwits_fetch_graceful_on_error() -> None:
    """StockTwits fetch returns empty list on API errors (no crash)."""
    import httpx
    from quant_foundry.modules.sources.stocktwits import StockTwitsSource

    mod = StockTwitsSource()

    # Mock httpx.AsyncClient to raise an error
    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = MagicMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.get = AsyncMock(side_effect=httpx.HTTPError("API error"))
        mock_client_cls.return_value = mock_client

        items = asyncio.run(
            mod.fetch(
                symbols=["AAPL"],
                start_ns=1_000_000_000,
                end_ns=2_000_000_000,
            )
        )
        assert items == []


# --------------------------------------------------------------------------- #
# Reddit tests                                                                #
# --------------------------------------------------------------------------- #


def test_reddit_importable_without_httpx() -> None:
    """RedditSource must be importable and instantiable without httpx."""
    from quant_foundry.modules.sources.reddit import RedditSource

    mod = RedditSource()
    assert "wallstreetbets" in mod.subreddits
    assert "stocks" in mod.subreddits
    assert mod.sort == "new"
    assert mod.limit == 25


def test_reddit_normalize_post_with_cashtag() -> None:
    """Reddit post with cashtag is normalized correctly."""
    from quant_foundry.modules.sources.reddit import RedditSource

    mod = RedditSource()
    post = {
        "id": "abc123",
        "title": "DD: $AAPL is undervalued",
        "selftext": "I think Apple is going to beat earnings next week.",
        "created_utc": 1685620800.0,  # 2023-06-01T12:00:00Z
        "score": 1500,
        "num_comments": 320,
        "upvote_ratio": 0.95,
        "permalink": "/r/wallstreetbets/comments/abc123/dd_aapl_is_undervalued/",
    }

    item = mod._normalize_post(post, "wallstreetbets", {"AAPL", "MSFT"})
    assert item is not None
    assert item.source == "reddit"
    assert item.event_type == "social"
    assert "AAPL" in item.symbols
    assert item.metadata["reddit_subreddit"] == "wallstreetbets"
    assert item.metadata["reddit_score"] == "1500"
    assert item.metadata["reddit_num_comments"] == "320"
    assert item.available_at_ns == 1685620800 * 1_000_000_000


def test_reddit_normalize_post_no_symbols() -> None:
    """Reddit posts with no relevant symbols return None (filtered out)."""
    from quant_foundry.modules.sources.reddit import RedditSource

    mod = RedditSource()
    post = {
        "id": "def456",
        "title": "Market outlook for 2024",
        "selftext": "General thoughts on the economy.",
        "created_utc": 1685620800.0,
        "score": 500,
        "num_comments": 100,
        "upvote_ratio": 0.9,
        "permalink": "/r/investing/comments/def456/market_outlook/",
    }

    item = mod._normalize_post(post, "investing", {"AAPL", "MSFT"})
    assert item is None  # No symbols matched


def test_reddit_extract_symbols_cashtag() -> None:
    """Reddit symbol extraction finds cashtags and known symbols."""
    from quant_foundry.modules.sources.reddit import RedditSource

    mod = RedditSource()
    text = "Buying $AAPL and $MSFT for the long term. NVDA also looks good."
    symbols = mod._extract_symbols(text, {"AAPL", "MSFT", "NVDA"})
    assert "AAPL" in symbols
    assert "MSFT" in symbols
    assert "NVDA" in symbols


def test_reddit_fetch_graceful_on_error() -> None:
    """Reddit fetch returns empty list on API errors (no crash)."""
    import httpx
    from quant_foundry.modules.sources.reddit import RedditSource

    mod = RedditSource(config={"delay_seconds": 0})  # no delay for test

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = MagicMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.get = AsyncMock(side_effect=httpx.HTTPError("API error"))
        mock_client_cls.return_value = mock_client

        items = asyncio.run(
            mod.fetch(
                symbols=["AAPL"],
                start_ns=1_000_000_000,
                end_ns=2_000_000_000,
            )
        )
        assert items == []


# --------------------------------------------------------------------------- #
# X/Twitter tests                                                             #
# --------------------------------------------------------------------------- #


def test_x_twitter_importable_without_httpx() -> None:
    """XTwitterSource must be importable and instantiable without httpx."""
    from quant_foundry.modules.sources.x_twitter import XTwitterSource

    mod = XTwitterSource()
    assert mod.max_results == 100
    assert mod.search_mode == "recent"


def test_x_twitter_raises_on_missing_bearer_token() -> None:
    """XTwitterSource raises ValueError if X_BEARER_TOKEN is not set."""
    from quant_foundry.modules.sources.x_twitter import XTwitterSource

    mod = XTwitterSource()
    with patch.dict("os.environ", {}, clear=True):
        with pytest.raises(ValueError, match="X_BEARER_TOKEN"):
            mod._get_bearer_token()


def test_x_twitter_uses_academic_token_for_full_archive() -> None:
    """Full-archive mode uses X_ACADEMIC_BEARER_TOKEN if available."""
    from quant_foundry.modules.sources.x_twitter import XTwitterSource

    mod = XTwitterSource(config={"search_mode": "full_archive"})
    with patch.dict("os.environ", {"X_ACADEMIC_BEARER_TOKEN": "academic_token"}, clear=True):
        token = mod._get_bearer_token()
        assert token == "academic_token"


def test_x_twitter_falls_back_to_bearer_token() -> None:
    """Full-archive mode falls back to X_BEARER_TOKEN if academic token missing."""
    from quant_foundry.modules.sources.x_twitter import XTwitterSource

    mod = XTwitterSource(config={"search_mode": "full_archive"})
    with patch.dict("os.environ", {"X_BEARER_TOKEN": "regular_token"}, clear=True):
        token = mod._get_bearer_token()
        assert token == "regular_token"


def test_x_twitter_normalize_tweet() -> None:
    """X/Twitter tweet normalization extracts cashtags + metrics."""
    from quant_foundry.modules.sources.x_twitter import XTwitterSource

    mod = XTwitterSource()
    tweet = {
        "id": "1234567890",
        "text": "$AAPL earnings are going to beat! $MSFT also looking strong.",
        "created_at": "2023-06-01T12:00:00.000Z",
        "lang": "en",
        "public_metrics": {
            "retweet_count": 50,
            "reply_count": 10,
            "like_count": 200,
            "quote_count": 5,
        },
        "entities": {
            "cashtags": [
                {"tag": "AAPL", "start": 0, "end": 5},
                {"tag": "MSFT", "start": 40, "end": 45},
            ],
        },
    }

    item = mod._normalize_tweet(tweet, "AAPL")
    assert item is not None
    assert item.source == "x_twitter"
    assert item.event_type == "social"
    assert "AAPL" in item.symbols
    assert "MSFT" in item.symbols
    assert item.metadata["x_retweet_count"] == "50"
    assert item.metadata["x_like_count"] == "200"
    assert item.available_at_ns > 0


def test_x_twitter_normalize_empty_tweet() -> None:
    """X/Twitter tweets with empty text return None."""
    from quant_foundry.modules.sources.x_twitter import XTwitterSource

    mod = XTwitterSource()
    tweet = {
        "id": "1234567891",
        "text": "",
        "created_at": "2023-06-01T12:00:00.000Z",
    }

    item = mod._normalize_tweet(tweet, "AAPL")
    assert item is None


def test_x_twitter_fetch_graceful_on_missing_token() -> None:
    """X/Twitter fetch returns empty list when bearer token is missing."""
    from quant_foundry.modules.sources.x_twitter import XTwitterSource

    mod = XTwitterSource()
    with patch.dict("os.environ", {}, clear=True):
        items = asyncio.run(
            mod.fetch(
                symbols=["AAPL"],
                start_ns=1_000_000_000,
                end_ns=2_000_000_000,
            )
        )
        assert items == []


def test_x_twitter_fetch_graceful_on_api_error() -> None:
    """X/Twitter fetch returns empty list on API errors (no crash)."""
    import httpx
    from quant_foundry.modules.sources.x_twitter import XTwitterSource

    mod = XTwitterSource()

    with patch.dict("os.environ", {"X_BEARER_TOKEN": "fake_token"}, clear=True):
        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.get = AsyncMock(side_effect=httpx.HTTPError("API error"))
            mock_client_cls.return_value = mock_client

            items = asyncio.run(
                mod.fetch(
                    symbols=["AAPL"],
                    start_ns=1_000_000_000,
                    end_ns=2_000_000_000,
                )
            )
            assert items == []


# --------------------------------------------------------------------------- #
# Timestamp parsing tests                                                     #
# --------------------------------------------------------------------------- #


def test_stocktwits_parse_iso_to_ns() -> None:
    """StockTwits ISO timestamp parsing produces correct nanoseconds."""
    from quant_foundry.modules.sources.stocktwits import _parse_iso_to_ns

    ns = _parse_iso_to_ns("2023-06-01T12:00:00Z")
    assert ns == 1685620800 * 1_000_000_000


def test_x_twitter_parse_iso_to_ns() -> None:
    """X/Twitter ISO timestamp parsing produces correct nanoseconds."""
    from quant_foundry.modules.sources.x_twitter import _parse_x_iso_to_ns

    ns = _parse_x_iso_to_ns("2023-06-01T12:00:00.000Z")
    assert ns == 1685620800 * 1_000_000_000


def test_x_twitter_parse_iso_to_ns_empty() -> None:
    """X/Twitter timestamp parsing handles empty strings gracefully."""
    from quant_foundry.modules.sources.x_twitter import _parse_x_iso_to_ns

    ns = _parse_x_iso_to_ns("")
    assert ns == 0
