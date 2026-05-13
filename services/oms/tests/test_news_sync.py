from __future__ import annotations

from typing import Any

import fakeredis.aioredis

from fincept_bus.streams import STREAM_INFO_RAW
from fincept_core.events import deserialize
from fincept_core.schemas import InformationEvent
from oms.alpaca.data import AlpacaDataClient
from oms.alpaca.news_sync import NEWS_INDEX_KEY, _article_key, sync_recent_news


async def test_sync_recent_news_publishes_information_event(monkeypatch) -> None:
    redis = fakeredis.aioredis.FakeRedis()

    async def fake_list_news(
        self: AlpacaDataClient,
        *,
        symbols: list[str] | None = None,
        limit: int = 50,
        start: str | None = None,
        end: str | None = None,
        page_token: str | None = None,
        include_content: bool = False,
    ) -> dict[str, Any]:
        return {
            "news": [
                {
                    "id": "n1",
                    "headline": "Nvidia expands AI chip supply",
                    "summary": "Nvidia said demand remains strong.",
                    "source": "Benzinga",
                    "url": "https://example.com/nvda/",
                    "author": "wire",
                    "created_at": "2026-05-05T12:00:00Z",
                    "symbols": ["nvda"],
                }
            ]
        }

    async def fake_list_bars(
        self: AlpacaDataClient,
        symbols: list[str],
        *,
        timeframe: str = "1Min",
        start: str | None = None,
        end: str | None = None,
        limit: int = 1000,
        feed: str = "iex",
    ) -> dict[str, Any]:
        return {"bars": {"nvda": [{"t": "2026-05-05T12:00:00Z", "c": "100.25"}]}}

    monkeypatch.setattr(AlpacaDataClient, "list_news", fake_list_news)
    monkeypatch.setattr(AlpacaDataClient, "list_bars", fake_list_bars)

    try:
        result = await sync_recent_news(
            redis=redis,
            api_key="key",
            api_secret="secret",
            limit=1,
        )

        assert result["written"] == 1
        assert result["info_published"] == 1
        assert result["info_publish_failed"] == 0
        assert await redis.exists(_article_key("n1")) == 1
        assert await redis.zcard(NEWS_INDEX_KEY) == 1

        entries = await redis.xrange(STREAM_INFO_RAW)
        assert len(entries) == 1
        event = deserialize(entries[0][1])
        assert event.type == "information"
        assert isinstance(event.payload, InformationEvent)
        assert event.payload.event_id == "alpaca_news:n1"
        assert event.payload.source == "Benzinga"
        assert event.payload.source_type == "alpaca_news"
        assert event.payload.headline == "Nvidia expands AI chip supply"
        assert event.payload.body == "Nvidia said demand remains strong."
        assert event.payload.url == "https://example.com/nvda/"
        assert event.payload.symbols == ["NVDA"]
        assert event.payload.entities == ["NVDA"]
        assert event.payload.information_type == "news"
        assert event.payload.raw_payload_ref == _article_key("n1")
        assert event.payload.dedupe_key == "url:https://example.com/nvda"
    finally:
        await redis.aclose()


async def test_sync_recent_news_does_not_republish_existing_article(monkeypatch) -> None:
    redis = fakeredis.aioredis.FakeRedis()

    async def fake_list_news(
        self: AlpacaDataClient,
        *,
        symbols: list[str] | None = None,
        limit: int = 50,
        start: str | None = None,
        end: str | None = None,
        page_token: str | None = None,
        include_content: bool = False,
    ) -> dict[str, Any]:
        return {
            "news": [
                {
                    "id": "n1",
                    "headline": "Nvidia expands AI chip supply",
                    "summary": "Nvidia said demand remains strong.",
                    "source": "Benzinga",
                    "url": "https://example.com/nvda/",
                    "author": "wire",
                    "created_at": "2026-05-05T12:00:00Z",
                    "symbols": ["NVDA"],
                }
            ]
        }

    async def fake_list_bars(
        self: AlpacaDataClient,
        symbols: list[str],
        *,
        timeframe: str = "1Min",
        start: str | None = None,
        end: str | None = None,
        limit: int = 1000,
        feed: str = "iex",
    ) -> dict[str, Any]:
        return {"bars": {"NVDA": [{"t": "2026-05-05T12:00:00Z", "c": "100.25"}]}}

    monkeypatch.setattr(AlpacaDataClient, "list_news", fake_list_news)
    monkeypatch.setattr(AlpacaDataClient, "list_bars", fake_list_bars)

    try:
        first = await sync_recent_news(redis=redis, api_key="key", api_secret="secret", limit=1)
        second = await sync_recent_news(redis=redis, api_key="key", api_secret="secret", limit=1)

        assert first["info_published"] == 1
        assert second["written"] == 0
        assert second["skipped"] == 1
        assert second["info_published"] == 0
        assert len(await redis.xrange(STREAM_INFO_RAW)) == 1
    finally:
        await redis.aclose()
