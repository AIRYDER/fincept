from __future__ import annotations

from typing import Any

import fakeredis.aioredis
import httpx
import pytest
from fincept_bus.producer import Producer
from fincept_bus.streams import STREAM_SIG_SENT
from fincept_core.events import deserialize
from fincept_core.schemas import InformationEvent, SentimentSignal

from agents.sentiment_agent.llm import SentimentScore
from agents.sentiment_agent.main import _info_seen_key, _process_information_event


class FakeRouter:
    def __init__(self, result: tuple[SentimentScore, str] | None) -> None:
        self.result = result
        self.calls: list[dict[str, Any]] = []
        self.has_capacity = True

    async def score(
        self,
        client: httpx.AsyncClient,
        *,
        symbol: str,
        title: str,
        description: str,
        source: str,
        max_tokens: int = 200,
    ) -> tuple[SentimentScore, str] | None:
        self.calls.append(
            {
                "symbol": symbol,
                "title": title,
                "description": description,
                "source": source,
                "max_tokens": max_tokens,
            }
        )
        return self.result


def _info(**overrides: object) -> InformationEvent:
    payload = {
        "event_id": "alpaca_news:n1",
        "source": "Benzinga",
        "source_type": "alpaca_news",
        "headline": "Nvidia expands AI chip supply",
        "body": "Datacenter demand remains strong.",
        "url": "https://example.com/nvda",
        "published_at": "2026-05-05T12:00:00Z",
        "ts_event": 1_000,
        "symbols": ["NVDA", "MSFT"],
        "entities": ["Nvidia", "NVDA"],
        "information_type": "news",
        "event_category": "product",
        "raw_payload_ref": "news:article:n1",
        "source_quality": 0.72,
        "dedupe_key": "url:https://example.com/nvda",
        "dedupe_group_id": "info:group1",
        "novelty_score": 1.0,
        "recency_score": 0.9,
    }
    payload.update(overrides)
    return InformationEvent.model_validate(payload)


async def test_process_information_event_emits_sentiment_for_each_symbol() -> None:
    redis = fakeredis.aioredis.FakeRedis()
    producer = Producer(redis)
    router = FakeRouter(
        (
            SentimentScore(
                score=0.6,
                confidence=0.8,
                event_type="product",
                rationale="positive supply signal",
            ),
            "openai",
        )
    )

    try:
        async with httpx.AsyncClient() as client:
            emitted = await _process_information_event(
                info=_info(),
                llm_router=router,  # type: ignore[arg-type]
                http=client,
                redis=redis,
                producer=producer,
                max_symbols=5,
            )

        assert emitted == 2
        assert [call["symbol"] for call in router.calls] == ["NVDA", "MSFT"]
        entries = await redis.xrange(STREAM_SIG_SENT)
        assert len(entries) == 2
        first = deserialize(entries[0][1])
        assert first.type == "sentiment"
        assert isinstance(first.payload, SentimentSignal)
        assert first.payload.symbol == "NVDA"
        assert first.payload.score == pytest.approx(0.6)
        assert first.payload.confidence == pytest.approx(0.8)
        assert first.payload.event_type == "product"
        assert first.payload.source_url == "https://example.com/nvda"
        assert first.payload.source_excerpt == "Nvidia expands AI chip supply"
        assert first.payload.entities == ["Nvidia", "NVDA"]
        assert await redis.exists(_info_seen_key(_info(), "NVDA")) == 1
        assert await redis.exists(_info_seen_key(_info(), "MSFT")) == 1
    finally:
        await redis.aclose()


async def test_process_information_event_skips_seen_symbol() -> None:
    redis = fakeredis.aioredis.FakeRedis()
    producer = Producer(redis)
    router = FakeRouter(
        (
            SentimentScore(
                score=-0.3, confidence=0.7, event_type="product", rationale="test"
            ),
            "openai",
        )
    )
    info = _info()
    await redis.set(_info_seen_key(info, "NVDA"), "1")

    try:
        async with httpx.AsyncClient() as client:
            emitted = await _process_information_event(
                info=info,
                llm_router=router,  # type: ignore[arg-type]
                http=client,
                redis=redis,
                producer=producer,
                max_symbols=5,
            )

        assert emitted == 1
        assert [call["symbol"] for call in router.calls] == ["MSFT"]
        entries = await redis.xrange(STREAM_SIG_SENT)
        assert len(entries) == 1
        event = deserialize(entries[0][1])
        assert isinstance(event.payload, SentimentSignal)
        assert event.payload.symbol == "MSFT"
    finally:
        await redis.aclose()


async def test_process_information_event_marks_parse_failures_without_emitting() -> (
    None
):
    redis = fakeredis.aioredis.FakeRedis()
    producer = Producer(redis)
    router = FakeRouter(None)
    info = _info(symbols=["NVDA"])

    try:
        async with httpx.AsyncClient() as client:
            emitted = await _process_information_event(
                info=info,
                llm_router=router,  # type: ignore[arg-type]
                http=client,
                redis=redis,
                producer=producer,
                max_symbols=5,
            )

        assert emitted == 0
        assert len(await redis.xrange(STREAM_SIG_SENT)) == 0
        assert await redis.exists(_info_seen_key(info, "NVDA")) == 1
    finally:
        await redis.aclose()
