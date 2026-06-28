"""
Tests for source robustness — cross-source deduplication, pagination
config, and Reddit comments config.

Tests verify:
- Dedup: exact item_id, content hash, URL match, order preservation,
  no false positives on unique items.
- Pagination config: max_pages is accepted by NewsAPI, StockTwits,
  Reddit; max_results is accepted by X/Twitter (which uses a different
  pagination model).
- Reddit comments: fetch_comments and max_comments_per_post config
  options exist and are accepted.
"""

from __future__ import annotations

import pathlib
import sys

import pytest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
_SCRIPTS_DIR = _REPO_ROOT / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))


# --------------------------------------------------------------------------- #
# Dedup tests                                                                  #
# --------------------------------------------------------------------------- #


def _make_item(
    item_id: str,
    *,
    headline: str = "Test headline",
    body: str = "Test body",
    source: str = "newsapi",
    url: str | None = None,
    available_at_ns: int = 1_000_000_000,
) -> "object":
    """Create a minimal MediaItem for dedup testing."""
    from quant_foundry.modules.registry import MediaItem

    return MediaItem(
        item_id=item_id,
        source=source,
        headline=headline,
        body=body,
        available_at_ns=available_at_ns,
        symbols=("AAPL",),
        event_type="earnings",
        url=url,
    )


def test_dedup_exact_item_id() -> None:
    """Two items with the same item_id — one is removed."""
    from quant_foundry.modules.sources.dedup import deduplicate_items

    items = [
        _make_item("dup-1", headline="First", body="Body A"),
        _make_item("dup-1", headline="Second", body="Body B"),
    ]

    result = deduplicate_items(items)
    assert len(result) == 1
    # First occurrence is kept
    assert result[0].headline == "First"


def test_dedup_content_hash() -> None:
    """Two items with same headline+body but different item_ids — one removed."""
    from quant_foundry.modules.sources.dedup import deduplicate_items

    items = [
        _make_item("src-a-1", headline="Company beats earnings", body="Strong quarter...",
                   source="newsapi"),
        _make_item("src-b-1", headline="Company beats earnings", body="Strong quarter...",
                   source="reddit"),
    ]

    result = deduplicate_items(items)
    assert len(result) == 1
    # First occurrence kept
    assert result[0].item_id == "src-a-1"


def test_dedup_url_match() -> None:
    """Two items with the same URL — one is removed."""
    from quant_foundry.modules.sources.dedup import deduplicate_items

    items = [
        _make_item("url-1", headline="Headline A", body="Body A",
                   url="https://example.com/story/123"),
        _make_item("url-2", headline="Headline B", body="Body B",
                   url="https://example.com/story/123"),
    ]

    result = deduplicate_items(items)
    assert len(result) == 1
    assert result[0].item_id == "url-1"


def test_dedup_order_preservation() -> None:
    """First occurrence is kept and original order is preserved."""
    from quant_foundry.modules.sources.dedup import deduplicate_items

    items = [
        _make_item("a", headline="A", body="A"),
        _make_item("b", headline="B", body="B"),
        _make_item("a", headline="A dup", body="A dup"),  # dup of "a"
        _make_item("c", headline="C", body="C"),
        _make_item("b", headline="B dup", body="B dup"),  # dup of "b"
    ]

    result = deduplicate_items(items)
    ids = [item.item_id for item in result]
    assert ids == ["a", "b", "c"], f"order not preserved: {ids}"


def test_dedup_no_duplicates() -> None:
    """All unique items — all are kept."""
    from quant_foundry.modules.sources.dedup import deduplicate_items

    items = [
        _make_item("u1", headline="Unique 1", body="Body 1",
                   url="https://example.com/1"),
        _make_item("u2", headline="Unique 2", body="Body 2",
                   url="https://example.com/2"),
        _make_item("u3", headline="Unique 3", body="Body 3",
                   url="https://example.com/3"),
    ]

    result = deduplicate_items(items)
    assert len(result) == 3
    # Each surviving item should have a content_hash in metadata
    for item in result:
        assert "content_hash" in item.metadata


# --------------------------------------------------------------------------- #
# Pagination config tests                                                      #
# --------------------------------------------------------------------------- #


def test_newsapi_pagination_config() -> None:
    """NewsAPI accepts max_pages config; default is 5."""
    from quant_foundry.modules.sources.newsapi import NewsAPISource, DEFAULT_MAX_PAGES

    # Default
    mod_default = NewsAPISource()
    assert mod_default.max_pages == DEFAULT_MAX_PAGES == 5

    # Custom
    mod_custom = NewsAPISource(config={"max_pages": 10})
    assert mod_custom.max_pages == 10


def test_stocktwits_pagination_config() -> None:
    """StockTwits accepts max_pages config; default is 3."""
    from quant_foundry.modules.sources.stocktwits import (
        StockTwitsSource,
        DEFAULT_MAX_PAGES,
    )

    # Default
    mod_default = StockTwitsSource()
    assert mod_default.max_pages == DEFAULT_MAX_PAGES == 3

    # Custom
    mod_custom = StockTwitsSource(config={"max_pages": 7})
    assert mod_custom.max_pages == 7


def test_reddit_pagination_config() -> None:
    """Reddit accepts max_pages config; default is 2."""
    from quant_foundry.modules.sources.reddit import RedditSource, DEFAULT_MAX_PAGES

    # Default
    mod_default = RedditSource()
    assert mod_default.max_pages == DEFAULT_MAX_PAGES == 2

    # Custom
    mod_custom = RedditSource(config={"max_pages": 5})
    assert mod_custom.max_pages == 5


def test_x_twitter_pagination_config() -> None:
    """X/Twitter accepts max_results config; default is 100.

    X/Twitter uses the API v2 ``max_results`` parameter (not page-based
    pagination), so we verify that config is accepted and the module
    can be instantiated.
    """
    from quant_foundry.modules.sources.x_twitter import (
        XTwitterSource,
        DEFAULT_MAX_RESULTS,
    )

    # Default
    mod_default = XTwitterSource()
    assert mod_default.max_results == DEFAULT_MAX_RESULTS == 100

    # Custom
    mod_custom = XTwitterSource(config={"max_results": 50})
    assert mod_custom.max_results == 50


# --------------------------------------------------------------------------- #
# Reddit comments config tests                                                 #
# --------------------------------------------------------------------------- #


def test_reddit_comments_config() -> None:
    """Reddit has fetch_comments and max_comments_per_post config options."""
    from quant_foundry.modules.sources.reddit import (
        RedditSource,
        DEFAULT_MAX_COMMENTS_PER_POST,
    )

    # Defaults
    mod_default = RedditSource()
    assert mod_default.fetch_comments is False
    assert mod_default.max_comments_per_post == DEFAULT_MAX_COMMENTS_PER_POST == 5

    # Custom
    mod_custom = RedditSource(config={
        "fetch_comments": True,
        "max_comments_per_post": 20,
    })
    assert mod_custom.fetch_comments is True
    assert mod_custom.max_comments_per_post == 20
